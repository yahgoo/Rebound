"""Server-rendered operator console and traveller-surface routes."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import col, select

from apps.api.routes_webhook import database_path
from apps.api.settings import Surface, get_settings
from packages.domain.db import session_factory
from packages.domain.models import AgentEvent, Order, RecoveryCase, RecoveryIntent

web_router = APIRouter(tags=["web"])

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "web" / "templates"
_DELIVERY_DIR = Path(__file__).resolve().parents[1] / "web" / "static" / "deliveries"
_TOKEN_TTL = timedelta(days=7)
_TOKEN_NS = b"rebound.traveller.v1"
_TOKEN_MAX_LEN = 512
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


@web_router.get("/", response_class=HTMLResponse)
async def landing_page(request: Request) -> Response:
    """Render the operator shell or the traveller view via SURFACE (I10)."""
    if get_settings().surface is Surface.TRAVELLER:
        return await _traveller_landing(request)

    with session_factory(database_path())() as session:
        case = session.exec(
            select(RecoveryCase).order_by(col(RecoveryCase.opened_at).desc())
        ).first()
        if case is None:
            context = _empty_context()
        else:
            order = session.get(Order, case.order_id)
            intent = _intent_for_case(session, case)
            events = _events_for_case(session, case)
            context = _case_context(case, order, intent, events)
    return templates.TemplateResponse(
        request=request,
        name="case.html",
        context=context,
    )


@web_router.get("/cases/{case_ref}", response_class=HTMLResponse)
async def case_page(request: Request, case_ref: str) -> Response:
    """Render one recovery case as the three-pane operator console."""
    with session_factory(database_path())() as session:
        case = session.exec(
            select(RecoveryCase).where(RecoveryCase.case_ref == case_ref)
        ).first()
        if case is None:
            raise HTTPException(status_code=404, detail=f"case {case_ref!r} not found")
        order = session.get(Order, case.order_id)
        intent = _intent_for_case(session, case)
        events = _events_for_case(session, case)
        context = _case_context(case, order, intent, events)
    return templates.TemplateResponse(
        request=request,
        name="case.html",
        context=context,
    )


@web_router.get("/t/{token}", response_class=HTMLResponse)
async def traveller_page(request: Request, token: str) -> Response:
    """Serve the traveller view from a signed magic link — no account, no login."""
    case_ref = verify_traveller_token(token)
    with session_factory(database_path())() as session:
        case = session.exec(
            select(RecoveryCase).where(RecoveryCase.case_ref == case_ref)
        ).first()
        if case is None:
            raise HTTPException(status_code=404, detail=f"case {case_ref!r} not found")
        order = session.get(Order, case.order_id)
        intent = _intent_for_case(session, case)
        events = _events_for_case(session, case)
        context = _traveller_context(case, order, intent, events)
    return templates.TemplateResponse(
        request=request,
        name="traveller.html",
        context=context,
    )


def mint_traveller_token(case_ref: str, *, now: datetime | None = None) -> str:
    """Return an HMAC-signed, time-limited magic-link token for case_ref."""
    issued = now or datetime.now(UTC)
    expires_at = int((issued + _TOKEN_TTL).timestamp())
    payload = f"{case_ref}.{expires_at}".encode("utf-8")
    signature = hmac.new(
        _token_key(), _TOKEN_NS + b"\n" + payload, hashlib.sha256
    ).digest()
    return f"{_b64url(payload)}.{_b64url(signature)}"


def verify_traveller_token(token: str, *, now: datetime | None = None) -> str:
    """Return case_ref if the magic link is authentic and unexpired; else 403."""
    if not token or len(token) > _TOKEN_MAX_LEN or "." not in token:
        raise HTTPException(status_code=403, detail="invalid traveller link")
    try:
        payload_b64, signature_b64 = token.rsplit(".", 1)
        payload = _b64url_decode(payload_b64)
        signature = _b64url_decode(signature_b64)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=403, detail="invalid traveller link") from exc

    expected = hmac.new(
        _token_key(), _TOKEN_NS + b"\n" + payload, hashlib.sha256
    ).digest()
    if len(signature) != len(expected) or not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=403, detail="invalid traveller link")

    try:
        decoded = payload.decode("utf-8")
        case_ref, exp_raw = decoded.rsplit(".", 1)
        expires_at = int(exp_raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="invalid traveller link") from exc

    current = int((now or datetime.now(UTC)).timestamp())
    if current >= expires_at:
        raise HTTPException(status_code=403, detail="traveller link expired")
    if not case_ref.strip():
        raise HTTPException(status_code=403, detail="invalid traveller link")
    return case_ref


async def _traveller_landing(request: Request) -> Response:
    with session_factory(database_path())() as session:
        case = session.exec(
            select(RecoveryCase).order_by(col(RecoveryCase.opened_at).desc())
        ).first()
        if case is None:
            context = _empty_traveller_context()
        else:
            order = session.get(Order, case.order_id)
            intent = _intent_for_case(session, case)
            events = _events_for_case(session, case)
            context = _traveller_context(case, order, intent, events)
    return templates.TemplateResponse(
        request=request,
        name="traveller.html",
        context=context,
    )


def _empty_context() -> dict[str, Any]:
    context = {
        "case_ref": "No active case",
        "status": "waiting",
        "status_label": "Waiting",
        "traveller_name": "No active traveller",
        "passenger_count": 0,
        "itinerary": [],
        "opened_at": "—",
        "stream_url": "",
        "embedded": True,
    }
    context.update(_traveller_fields("waiting", "en", {}))
    return context


def _empty_traveller_context() -> dict[str, Any]:
    context = {
        "case_ref": "No active case",
        "status": "waiting",
        "status_label": "Waiting",
        "traveller_name": "Traveller",
        "stream_url": "",
        "embedded": False,
    }
    context.update(_traveller_fields("waiting", "en", {}))
    return context


def _case_context(
    case: RecoveryCase,
    order: Order | None,
    intent: RecoveryIntent | None,
    events: list[AgentEvent],
) -> dict[str, Any]:
    passengers = _json_list(order.passengers_json if order is not None else "[]")
    itinerary = _json_list(order.itinerary_json if order is not None else "[]")
    status = case.status.strip().lower()
    language = (intent.language if intent is not None else "en") or "en"
    context = {
        "case_ref": case.case_ref,
        "status": status,
        "status_label": status.replace("_", " "),
        "traveller_name": _traveller_name(passengers),
        "passenger_count": len(passengers),
        "itinerary": [_segment_view(segment) for segment in itinerary],
        "opened_at": _display_time(case.opened_at),
        "stream_url": f"/cases/{case.case_ref}/stream",
        "embedded": True,
        "language": language,
    }
    context.update(_traveller_fields(status, language, _delivery_artifacts(case.case_ref, events)))
    return context


def _traveller_context(
    case: RecoveryCase,
    order: Order | None,
    intent: RecoveryIntent | None,
    events: list[AgentEvent],
) -> dict[str, Any]:
    passengers = _json_list(order.passengers_json if order is not None else "[]")
    status = case.status.strip().lower()
    language = (intent.language if intent is not None else "en") or "en"
    context = {
        "case_ref": case.case_ref,
        "status": status,
        "status_label": status.replace("_", " "),
        "traveller_name": _traveller_name(passengers),
        "stream_url": f"/cases/{case.case_ref}/stream",
        "embedded": False,
        "language": language,
    }
    context.update(_traveller_fields(status, language, _delivery_artifacts(case.case_ref, events)))
    return context


def _traveller_fields(status: str, language: str, artifacts: dict[str, Any]) -> dict[str, Any]:
    copy = _traveller_copy(language)
    stage = _stage_for_status(status)
    stage_copy = copy["stages"][stage]
    audio_url = str(artifacts.get("audio_url") or "")
    pdf_url = str(artifacts.get("pdf_url") or "")
    family_sent = bool(artifacts.get("family_sent"))
    return {
        "language": language,
        "audio_url": audio_url,
        "pdf_url": pdf_url,
        "family_sent": family_sent,
        "headline": stage_copy["headline"],
        "detail": stage_copy["detail"],
        "play_label": copy["play"],
        "pause_label": copy["pause"],
        "play_pending_label": copy["play_pending"],
        "pdf_label": copy["pdf"],
        "pdf_pending_label": copy["pdf_pending"],
        "family_sent_label": copy["family_sent"],
        "family_pending_label": copy["family_pending"],
        "copy_json": json.dumps(
            {"stages": copy["stages"], "language": language},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def _stage_for_status(status: str) -> str:
    normalized = (status or "waiting").strip().lower()
    if normalized == "recovered":
        return "recovered"
    if normalized == "failed":
        return "failed"
    if normalized == "awaiting_confirmation":
        return "awaiting_confirmation"
    if normalized == "executing":
        return "executing"
    if normalized in {"waiting", ""}:
        return "waiting"
    return "working"


def _traveller_copy(language: str) -> dict[str, Any]:
    if language.lower().startswith("zh"):
        return {
            "play": "播放行程说明",
            "pause": "暂停播放",
            "play_pending": "语音行程尚未准备好",
            "pdf": "打开大字行程单",
            "pdf_pending": "大字行程单尚未准备好",
            "family_sent": "已通知家人。",
            "family_pending": "家人通知尚未发送。",
            "stages": {
                "waiting": {
                    "headline": "我们正在帮助您。",
                    "detail": "请稍等。我们在查看您的航班。",
                },
                "working": {
                    "headline": "我们正在为您找航班。",
                    "detail": "请稍等。您不用做任何事。",
                },
                "awaiting_confirmation": {
                    "headline": "我们找到了新航班。",
                    "detail": "助手正在确认。请稍等。",
                },
                "executing": {
                    "headline": "正在为您出票。",
                    "detail": "请稍等。很快就好。",
                },
                "recovered": {
                    "headline": "新行程已准备好。",
                    "detail": "请按播放，收听您的新行程。",
                },
                "failed": {
                    "headline": "我们仍在帮助您。",
                    "detail": "请留在原地。帮助正在路上。",
                },
            },
        }
    return {
        "play": "Play spoken plan",
        "pause": "Pause spoken plan",
        "play_pending": "Spoken plan not ready yet",
        "pdf": "Open large-print itinerary",
        "pdf_pending": "Large-print itinerary not ready yet",
        "family_sent": "Message sent to your family.",
        "family_pending": "Family message not sent yet.",
        "stages": {
            "waiting": {
                "headline": "Help is on the way.",
                "detail": "Sit tight. We are looking at your flight.",
            },
            "working": {
                "headline": "We are finding your flight.",
                "detail": "One moment. You do not need to do anything.",
            },
            "awaiting_confirmation": {
                "headline": "We found a new flight.",
                "detail": "Your helper is confirming it now.",
            },
            "executing": {
                "headline": "We are booking your new flight.",
                "detail": "Please wait. This will only take a moment.",
            },
            "recovered": {
                "headline": "Your new plan is ready.",
                "detail": "Play the spoken plan. That is the only step.",
            },
            "failed": {
                "headline": "We are still helping you.",
                "detail": "Please stay where you are. Help is coming.",
            },
        },
    }


def _delivery_artifacts(case_ref: str, events: list[AgentEvent]) -> dict[str, Any]:
    """Surface Caretaker outputs when present; otherwise leave them pending.

    TTS / PDF / Telegram are Task 26. This only reads files or audit payloads.
    """
    audio_url = ""
    pdf_url = ""
    family_sent = False

    folder = _DELIVERY_DIR / case_ref
    audio_file = folder / "spoken-plan.mp3"
    pdf_file = folder / "itinerary.pdf"
    family_file = folder / "family-sent"
    if audio_file.is_file():
        audio_url = f"/static/deliveries/{case_ref}/spoken-plan.mp3"
    if pdf_file.is_file():
        pdf_url = f"/static/deliveries/{case_ref}/itinerary.pdf"
    if family_file.is_file():
        family_sent = True

    for row in events:
        actor = (row.actor or "").strip().lower()
        step = (row.step or "").strip().lower()
        if actor != "caretaker" and "deliver" not in step:
            continue
        payload = _json_object(row.payload_json)
        if payload.get("telegram_sent") or payload.get("family_sent"):
            family_sent = True
        audio_url = _public_artifact_url(
            payload.get("audio_url") or payload.get("audio_path"),
            fallback=audio_url,
        )
        pdf_url = _public_artifact_url(
            payload.get("pdf_url") or payload.get("pdf_path"),
            fallback=pdf_url,
        )
    return {
        "audio_url": audio_url,
        "pdf_url": pdf_url,
        "family_sent": family_sent,
    }


def _public_artifact_url(raw: Any, *, fallback: str) -> str:
    if raw is None:
        return fallback
    text = str(raw).strip()
    if not text:
        return fallback
    if text.startswith("/"):
        return text
    path = Path(text)
    if not path.is_file():
        return fallback
    try:
        relative = path.resolve().relative_to(_DELIVERY_DIR.resolve())
    except ValueError:
        return fallback
    return "/static/deliveries/" + relative.as_posix()


def _intent_for_case(session: Any, case: RecoveryCase) -> RecoveryIntent | None:
    if case.id is None:
        return None
    return session.exec(
        select(RecoveryIntent).where(RecoveryIntent.case_id == case.id)
    ).first()


def _events_for_case(session: Any, case: RecoveryCase) -> list[AgentEvent]:
    if case.id is None:
        return []
    return list(
        session.exec(
            select(AgentEvent)
            .where(AgentEvent.case_id == case.id)
            .order_by(col(AgentEvent.id).desc())
        ).all()
    )


def _token_key() -> bytes:
    settings = get_settings()
    secret = settings.operator_token or settings.atlas_client_secret
    return secret.encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(text.encode("ascii") + padding.encode("ascii"))


def _json_list(raw: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _traveller_name(passengers: list[dict[str, Any]]) -> str:
    names: list[str] = []
    for passenger in passengers:
        raw_name = (
            passenger.get("name")
            or passenger.get("fullName")
            or passenger.get("full_name")
        )
        if not raw_name:
            parts = [
                passenger.get("firstName") or passenger.get("first_name"),
                passenger.get("lastName") or passenger.get("last_name"),
            ]
            raw_name = " ".join(str(part) for part in parts if part)
        name = str(raw_name or "").replace("/", " ").strip()
        if name:
            names.append(name.title())
    return ", ".join(names) if names else "Traveller"


def _segment_view(segment: dict[str, Any]) -> dict[str, str]:
    carrier = str(segment.get("carrier") or "").strip()
    flight_number = str(segment.get("flight_number") or "").strip()
    flight = " ".join(part for part in (carrier, flight_number) if part)
    return {
        "origin": str(segment.get("origin") or "—"),
        "destination": str(segment.get("destination") or "—"),
        "flight": flight or "Flight",
        "departure": _display_time(segment.get("departure_at")),
        "arrival": _display_time(segment.get("arrival_at")),
    }


def _display_time(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return "—"
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    return parsed.strftime("%d %b · %H:%M")
