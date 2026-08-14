"""Atlas webhook ingestion and the operator's manual fallback trigger."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from apps.api.settings import ReboundMode, get_settings
from packages.agents.watcher import DisruptionSignal, Watcher
from packages.atlas.cassette import CassettePlayer
from packages.atlas.client import AtlasClient
from packages.atlas.transport import LiveTransport, ReplayTransport
from packages.domain.db import session_factory
from packages.domain.enums import Actor
from packages.guardian.audit import AgentEventIn, write_event
from packages.guardian.redaction import redact

webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"])
operator_router = APIRouter(prefix="/cases", tags=["operator"])

_ROOT = Path(__file__).resolve().parents[2]
_REDACTED = "[REDACTED]"

# Atlas's documented values plus punctuation variants seen in integrations.
_EVENT_KINDS = {
    "order.schedulechange": "webhook_schedule_change",
    "order.schedule_change": "webhook_schedule_change",
    "schedule-change": "webhook_schedule_change",
    "schedule_change": "webhook_schedule_change",
    "email.schedulechange": "webhook_schedule_change",
    "abnormal.cancelled": "webhook_cancellation",
    "order.cancelled": "webhook_cancellation",
    "cancellation": "webhook_cancellation",
    "order.ticketed": "webhook_ticketing_complete",
    "ticketing-complete": "webhook_ticketing_complete",
    "ticketing_complete": "webhook_ticketing_complete",
    "order.void": "webhook_void",
    "void": "webhook_void",
    "airline.status": "webhook_airline_status",
    "airline-status": "webhook_airline_status",
    "airline_status": "webhook_airline_status",
    "email.all": "webhook_email",
    "email": "webhook_email",
    "incident": "webhook_incident",
}

_SENSITIVE_KEY_PARTS = (
    "address",
    "authorization",
    "birthday",
    "cardholder",
    "cardnum",
    "contact",
    "credential",
    "dateofbirth",
    "dob",
    "document",
    "email",
    "fullname",
    "identification",
    "link",
    "locator",
    "mobile",
    "name",
    "passport",
    "password",
    "phone",
    "pnr",
    "secret",
    "subject",
    "ticketno",
    "token",
)
_OPERATIONAL_TIMESTAMP_KEYS = frozenset(
    {
        "arrtime",
        "createtime",
        "deptime",
        "emailreceivingdate",
        "eventtime",
        "receivedat",
        "receivedtime",
        "updateime",  # Atlas incident schema typo
        "updatedtime",
        "updatetime",
    }
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")


class CaseRefResponse(BaseModel):
    case_ref: str


class ManualTriggerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    atlas_order_no: str = Field(min_length=1)


def database_path() -> str:
    """Use DB_PATH when supplied, with a local file for the API default."""
    return os.environ.get("DB_PATH") or str(_ROOT / "rebound.db")


@lru_cache
def get_watcher() -> Watcher:
    settings = get_settings()
    if settings.rebound_mode is ReboundMode.REPLAY:
        transport = ReplayTransport(
            CassettePlayer(_ROOT / "fixtures" / "cassettes")
        )
    else:
        transport = LiveTransport(
            settings.atlas_base_url,
            settings.atlas_client_id,
            settings.atlas_client_secret,
        )
    return Watcher(AtlasClient(transport), session_factory(database_path()))


@webhook_router.post("/atlas", response_model=CaseRefResponse)
async def atlas_webhook(
    payload: dict[str, Any],
    watcher: Watcher = Depends(get_watcher),
) -> CaseRefResponse:
    event_type = _event_type(payload)
    kind = _EVENT_KINDS.get(event_type)
    if kind is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unsupported Atlas webhook type: {event_type or '<missing>'}",
        )

    order_no = _order_no(payload)
    if not order_no:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Atlas webhook is missing data.orderNo",
        )

    fingerprint = Watcher.fingerprint(payload)
    signal = DisruptionSignal(
        kind=kind,
        atlas_order_no=order_no,
        fingerprint=fingerprint,
        received_at=datetime.now(UTC),
        raw=dict(payload),
    )
    case = await watcher.ingest(signal)
    if case.id is None:
        raise RuntimeError("Watcher returned an unpersisted RecoveryCase")

    await _log_delivery(
        case_id=case.id,
        event_type=event_type,
        fingerprint=fingerprint,
        payload=payload,
    )
    return CaseRefResponse(case_ref=case.case_ref)


@operator_router.post("/trigger", response_model=CaseRefResponse)
async def manual_trigger(
    http_request: Request,
    watcher: Watcher = Depends(get_watcher),
) -> CaseRefResponse:
    try:
        request = ManualTriggerRequest.model_validate_json(
            await http_request.body()
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(include_url=False),
        ) from exc
    order_no = request.atlas_order_no.strip()
    if not order_no:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="atlas_order_no must not be blank",
        )
    fingerprint_payload = {
        "kind": "manual_trigger",
        "atlas_order_no": order_no,
    }
    signal = DisruptionSignal(
        kind="manual_trigger",
        atlas_order_no=order_no,
        fingerprint=Watcher.fingerprint(fingerprint_payload),
        received_at=datetime.now(UTC),
        raw=fingerprint_payload,
    )
    case = await watcher.ingest(signal)
    return CaseRefResponse(case_ref=case.case_ref)


def _event_type(payload: dict[str, Any]) -> str:
    raw = payload.get("type") or payload.get("eventType")
    if raw is None and isinstance(payload.get("data"), dict):
        raw = payload["data"].get("type") or payload["data"].get("eventType")
    return str(raw or "").strip().lower()


def _order_no(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    candidates: list[Any] = []
    if isinstance(data, dict):
        candidates.extend(
            (data.get("orderNo"), data.get("order_no"), data.get("atlas_order_no"))
        )
    candidates.extend(
        (
            payload.get("orderNo"),
            payload.get("order_no"),
            payload.get("atlas_order_no"),
        )
    )
    for value in candidates:
        order_no = str(value or "").strip()
        if order_no:
            return order_no
    return ""


async def _log_delivery(
    *,
    case_id: int,
    event_type: str,
    fingerprint: str,
    payload: dict[str, Any],
) -> None:
    clean_payload = _redact_payload(payload)
    with session_factory(database_path())() as session:
        await write_event(
            session,
            AgentEventIn(
                case_id=case_id,
                actor=Actor.WATCHER,
                step="webhook.delivery",
                summary=f"accepted Atlas {event_type} delivery",
                payload={
                    "event_type": event_type,
                    "fingerprint": fingerprint,
                    "delivery": clean_payload,
                },
            ),
        )
        session.commit()


def _redact_payload(value: Any, *, key: str = "") -> Any:
    """Preserve delivery structure while removing sensitive values."""
    normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
    # Atlas operational timestamps are not passenger DOBs, but Guardian's
    # context-free assert_no_pii check rejects their YYYY-MM-DD prefix. Until
    # Guardian can distinguish field semantics, redact the whole value here
    # instead of corrupting it with a partial [[PAX_*_DOB]] substitution.
    if normalized_key in _OPERATIONAL_TIMESTAMP_KEYS and isinstance(value, str):
        return _REDACTED
    if normalized_key and any(
        part in normalized_key for part in _SENSITIVE_KEY_PARTS
    ):
        return _REDACTED
    if isinstance(value, dict):
        return {
            str(child_key): _redact_payload(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_payload(item, key=key) for item in value]
    if isinstance(value, str):
        clean = redact(value).text
        clean = _EMAIL_RE.sub(_REDACTED, clean)
        return _PHONE_RE.sub(_REDACTED, clean)
    return value
