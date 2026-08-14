"""Watcher agent — deterministic disruption ingest (I7, I8).

No model call. Order facts come only from Atlas query_order_details;
every ingest (including duplicates) appends one AgentEvent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlmodel import Session, col, select

from packages.atlas.client import AtlasClient
from packages.atlas.models import OrderDetails
from packages.domain.enums import Actor
from packages.domain.models import Order, RecoveryCase
from packages.guardian.audit import AgentEventIn, write_event

# Delivery / receipt metadata — must not affect the idempotency key (I7).
_VOLATILE_KEYS = frozenset(
    {
        "notificationId",
        "notification_id",
        "deliveryId",
        "delivery_id",
        "deliveryIdStr",
        "received_at",
        "receivedAt",
        "eventTime",
        "createTime",
        "updateTime",
        "updateIme",  # Atlas incident typo observed in docs
        "updatedTime",
        "timestamp",
        "ts",
    }
)

_CASE_REF_RE = re.compile(r"^RC-(\d+)$")

_STEP_OPENED = "watcher.ingest"
_STEP_DUPLICATE = "watcher.ingest_duplicate"


class DisruptionSignal(BaseModel):
    kind: str  # webhook_schedule_change | webhook_cancellation | manual_trigger
    atlas_order_no: str
    fingerprint: str  # idempotency key
    received_at: datetime
    raw: dict


class Watcher:
    """Deterministic. No model call."""

    def __init__(
        self, atlas: AtlasClient, session_factory: Callable[[], Session]
    ) -> None:
        self._atlas = atlas
        self._session_factory = session_factory

    @staticmethod
    def fingerprint(payload: dict) -> str:
        """Stable across duplicate deliveries of the same logical event (I7).

        Excludes receipt time and delivery ids so retries with a new
        notificationId still collide on the same RecoveryCase.
        """
        cleaned = _strip_volatile(payload)
        canonical = json.dumps(
            cleaned, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def ingest(self, signal: DisruptionSignal) -> RecoveryCase:
        """Dedupes on fingerprint, loads the order via query_order_details
        (never trusts the webhook body), opens or returns the RecoveryCase."""
        with self._session_factory() as session:
            existing = session.exec(
                select(RecoveryCase).where(
                    RecoveryCase.trigger_fingerprint == signal.fingerprint
                )
            ).first()
            if existing is not None:
                await write_event(
                    session,
                    AgentEventIn(
                        case_id=existing.id,  # type: ignore[arg-type]
                        actor=Actor.WATCHER,
                        step=_STEP_DUPLICATE,
                        summary=(
                            f"duplicate {signal.kind} for "
                            f"{signal.atlas_order_no}; case unchanged"
                        ),
                        payload={
                            "kind": signal.kind,
                            "atlas_order_no": signal.atlas_order_no,
                            "fingerprint": signal.fingerprint,
                            "deduplicated": True,
                            "case_ref": existing.case_ref,
                        },
                    ),
                )
                session.commit()
                session.refresh(existing)
                return existing

        # Authoritative order facts only — never the webhook body (I7).
        details = await self._atlas.query_order_details(
            order_no=signal.atlas_order_no
        )

        with self._session_factory() as session:
            # Re-check inside the write session in case of a concurrent twin.
            existing = session.exec(
                select(RecoveryCase).where(
                    RecoveryCase.trigger_fingerprint == signal.fingerprint
                )
            ).first()
            if existing is not None:
                await write_event(
                    session,
                    AgentEventIn(
                        case_id=existing.id,  # type: ignore[arg-type]
                        actor=Actor.WATCHER,
                        step=_STEP_DUPLICATE,
                        summary=(
                            f"duplicate {signal.kind} for "
                            f"{signal.atlas_order_no}; case unchanged"
                        ),
                        payload={
                            "kind": signal.kind,
                            "atlas_order_no": signal.atlas_order_no,
                            "fingerprint": signal.fingerprint,
                            "deduplicated": True,
                            "case_ref": existing.case_ref,
                        },
                    ),
                )
                session.commit()
                session.refresh(existing)
                return existing

            order = _upsert_order(session, details)
            opened_at = datetime.now(UTC)
            case = RecoveryCase(
                case_ref=_next_case_ref(session),
                order_id=order.id,  # type: ignore[arg-type]
                trigger_kind=signal.kind,
                trigger_fingerprint=signal.fingerprint,
                status="open",
                opened_at=opened_at,
                resolved_at=None,
                surface=_default_surface(),
            )
            session.add(case)
            session.flush()

            await write_event(
                session,
                AgentEventIn(
                    case_id=case.id,  # type: ignore[arg-type]
                    actor=Actor.WATCHER,
                    step=_STEP_OPENED,
                    summary=(
                        f"opened {case.case_ref} from {signal.kind} "
                        f"on {signal.atlas_order_no}"
                    ),
                    payload={
                        "kind": signal.kind,
                        "atlas_order_no": signal.atlas_order_no,
                        "fingerprint": signal.fingerprint,
                        "deduplicated": False,
                        "case_ref": case.case_ref,
                        "order_status": details.status,
                    },
                ),
            )
            session.commit()
            session.refresh(case)
            return case


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): _strip_volatile(v)
            for k, v in value.items()
            if str(k) not in _VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def _default_surface() -> str:
    return os.environ.get("SURFACE", "operator") or "operator"


def _next_case_ref(session: Session) -> str:
    refs = list(session.exec(select(RecoveryCase.case_ref)).all())
    max_n = 0
    for ref in refs:
        match = _CASE_REF_RE.match(str(ref))
        if match:
            max_n = max(max_n, int(match.group(1)))
    return f"RC-{max_n + 1:04d}"


def _passengers_json(details: OrderDetails) -> str:
    raw = details.raw if isinstance(details.raw, dict) else {}
    passengers = raw.get("paxTicketInfos")
    if passengers is None:
        passengers = raw.get("passengers")
    if passengers is None:
        passengers = []
    return json.dumps(passengers, separators=(",", ":"), default=str)


def _itinerary_json(details: OrderDetails) -> str:
    segments = [s.model_dump(mode="json") for s in details.segments]
    return json.dumps(segments, separators=(",", ":"), default=str)


def _upsert_order(session: Session, details: OrderDetails) -> Order:
    now = datetime.now(UTC)
    existing = session.exec(
        select(Order).where(col(Order.atlas_order_no) == details.order_no)
    ).first()
    passengers_json = _passengers_json(details)
    itinerary_json = _itinerary_json(details)
    if existing is None:
        order = Order(
            atlas_order_no=details.order_no,
            pnr=details.pnr,
            status=details.status,
            passengers_json=passengers_json,
            itinerary_json=itinerary_json,
            total_amount=details.total_amount,
            currency=details.currency,
            created_at=now,
            updated_at=now,
        )
        session.add(order)
        session.flush()
        return order

    existing.pnr = details.pnr
    existing.status = details.status
    existing.passengers_json = passengers_json
    existing.itinerary_json = itinerary_json
    existing.total_amount = details.total_amount
    existing.currency = details.currency
    existing.updated_at = now
    session.add(existing)
    session.flush()
    return existing
