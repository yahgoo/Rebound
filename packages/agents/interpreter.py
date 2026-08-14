"""Interpreter agent — multimodal → RecoveryIntent constraints (I1, I4).

Zone C egress order (mandatory):
  1. guardian.redaction.redact on all text (when text is supplied)
  2. redact_image_metadata on every image; assert no EXIF remains
  3. build ModelRequest (redacted text + stripped images + audio)
  4. assert_no_pii on the final request payload
  5. only then router.generate_structured

Safe to run concurrently with an early search when an orchestrator allows it;
search must never depend on an unvalidated / low-confidence intent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session

from packages.atlas.models import Segment
from packages.domain.enums import Actor
from packages.domain.models import RecoveryIntent
from packages.guardian.audit import AgentEventIn, write_event
from packages.guardian.redaction import assert_no_pii, redact, redact_image_metadata
from packages.router.base import AudioPart, ImagePart, ModelRequest, ModelRouter

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "interpreter.md"
_CONFIDENCE_THRESHOLD = 0.6
_STEP_STARTED = "interpreter.started"
_STEP_SUCCEEDED = "interpreter.succeeded"
_STEP_NEEDS_CLARIFICATION = "interpreter.needs_clarification"

# Itinerary-shaped keys: drop if a model sneaks them into structured output (I1).
_ITINERARY_SHAPED_KEYS = frozenset(
    {
        "flight_number",
        "flightNumber",
        "flight_numbers",
        "carrier",
        "carriers",
        "airline",
        "airlines",
        "offer_id",
        "offerId",
        "offer_ids",
        "price",
        "prices",
        "amount",
        "currency",
        "segments",
        "itinerary",
        "legs",
        "pnr",
        "booking_code",
        "ticket_number",
    }
)


class InterpreterInput(BaseModel):
    case_id: int
    text: str | None = None
    voice: AudioPart | None = None
    photo: ImagePart | None = None
    original_itinerary: list[Segment]


class InterpreterIntentDraft(BaseModel):
    """Structured model output — constraints only (I1). Not a DB row."""

    passenger_count: int = Field(default=1, ge=1)
    must_arrive_by: datetime | None = None
    budget_ceiling_sgd: Decimal = Field(default=Decimal("0"))
    origin_candidates: list[str] = Field(default_factory=list)
    destination_candidates: list[str] = Field(default_factory=list)
    mobility_notes: str | None = None
    language: str = "en"
    confidence: float = Field(ge=0.0, le=1.0)
    clarification_question: str | None = None

    @field_validator("origin_candidates", "destination_candidates", mode="before")
    @classmethod
    def _coerce_iata_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)

    @field_validator("budget_ceiling_sgd", mode="before")
    @classmethod
    def _coerce_budget(cls, value: Any) -> Decimal:
        if value is None or value == "":
            return Decimal("0")
        return Decimal(str(value))


class Interpreter:
    """LLM. Emits constraints only — never an itinerary (I1)."""

    def __init__(
        self,
        router: ModelRouter,
        session_factory: Callable[[], Session],
    ) -> None:
        self._router = router
        self._session_factory = session_factory
        self._system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
        # case_id -> last clarification question produced during interpret()
        self._clarifications: dict[int, str] = {}

    async def interpret(self, payload: InterpreterInput) -> RecoveryIntent:
        """Guardian.redact on text and redact_image_metadata on images BEFORE egress.
        Structured output only. When confidence is below threshold, sets
        needs_clarification and asks — never guesses.
        """
        raw_kinds = _raw_input_kinds(payload)
        if not raw_kinds:
            raise ValueError(
                "InterpreterInput requires at least one of text, voice, or photo"
            )

        await self._write_event(
            case_id=payload.case_id,
            step=_STEP_STARTED,
            summary="interpretation started",
            payload={"raw_input_kinds": raw_kinds},
        )

        # --- I4 egress gate: redact → strip EXIF → assert_no_pii → router ---
        redacted_text = ""
        kinds_found: list[str] = []
        if payload.text is not None and payload.text.strip():
            redacted = redact(payload.text)
            redacted_text = redacted.text
            kinds_found = list(redacted.kinds_found)

        images: list[ImagePart] = []
        if payload.photo is not None:
            clean = redact_image_metadata(payload.photo.data)
            _assert_no_exif(clean)
            images.append(
                ImagePart(mime_type=payload.photo.mime_type, data=clean)
            )

        audio: list[AudioPart] = []
        if payload.voice is not None:
            audio.append(payload.voice)

        user_prompt = _build_user_prompt(
            redacted_text=redacted_text,
            original_itinerary=payload.original_itinerary,
            kinds_found=kinds_found,
            has_voice=bool(audio),
            has_photo=bool(images),
        )
        request = ModelRequest(
            system=self._system_prompt,
            prompt=user_prompt,
            images=images,
            audio=audio,
            temperature=0.0,
            max_output_tokens=2048,
            # Operational override: INTERFACES default 20s is tight for Gemini
            # 3.6 + multimodal (esp. audio).
            timeout_seconds=90.0,
        )
        egress_payload = _request_payload_for_pii_check(request)
        assert_no_pii(egress_payload)

        draft = await self._router.generate_structured(
            request, InterpreterIntentDraft
        )
        draft = _drop_itinerary_shaped(draft)

        if draft.confidence < _CONFIDENCE_THRESHOLD:
            question = (draft.clarification_question or "").strip() or (
                _fallback_clarification(draft.language)
            )
            self._clarifications[payload.case_id] = question
            intent = _draft_to_intent(
                case_id=payload.case_id,
                draft=draft,
                raw_input_kinds=raw_kinds,
                persist=False,
            )
            await self._write_event(
                case_id=payload.case_id,
                step=_STEP_NEEDS_CLARIFICATION,
                summary="interpretation needs clarification",
                payload={
                    "confidence": draft.confidence,
                    "language": draft.language,
                    "clarification_question": question,
                    "raw_input_kinds": raw_kinds,
                },
            )
            return intent

        with self._session_factory() as session:
            intent = _draft_to_intent(
                case_id=payload.case_id,
                draft=draft,
                raw_input_kinds=raw_kinds,
                persist=True,
                session=session,
            )
            session.commit()
            session.refresh(intent)
            intent_out = RecoveryIntent(
                id=intent.id,
                case_id=intent.case_id,
                passenger_count=intent.passenger_count,
                must_arrive_by=intent.must_arrive_by,
                budget_ceiling_sgd=intent.budget_ceiling_sgd,
                origin_candidates=intent.origin_candidates,
                destination_candidates=intent.destination_candidates,
                mobility_notes=intent.mobility_notes,
                language=intent.language,
                confidence=intent.confidence,
                raw_input_kinds=intent.raw_input_kinds,
            )

        await self._write_event(
            case_id=payload.case_id,
            step=_STEP_SUCCEEDED,
            summary="interpretation succeeded",
            payload={
                "confidence": intent_out.confidence,
                "language": intent_out.language,
                "budget_ceiling_sgd": str(intent_out.budget_ceiling_sgd),
                # Avoid ISO date shapes — assert_no_pii treats YYYY-MM-DD as DOB.
                "must_arrive_by": (
                    _fmt_when(intent_out.must_arrive_by)
                    if intent_out.must_arrive_by
                    else None
                ),
                "mobility_notes": intent_out.mobility_notes,
                "raw_input_kinds": intent_out.raw_input_kinds_list,
                "intent_id": intent_out.id,
            },
        )
        return intent_out

    async def clarification_question(self, intent: RecoveryIntent) -> str:
        """One short question in intent.language."""
        cached = self._clarifications.get(intent.case_id)
        if cached:
            return cached
        return _fallback_clarification(intent.language)

    async def _write_event(
        self,
        *,
        case_id: int,
        step: str,
        summary: str,
        payload: dict,
    ) -> None:
        with self._session_factory() as session:
            await write_event(
                session,
                AgentEventIn(
                    case_id=case_id,
                    actor=Actor.INTERPRETER,
                    step=step,
                    summary=summary,
                    payload=payload,
                ),
            )
            session.commit()


def _raw_input_kinds(payload: InterpreterInput) -> list[str]:
    """Exact modalities supplied — SPEC: text | voice | photo."""
    kinds: list[str] = []
    if payload.text is not None and payload.text.strip():
        kinds.append("text")
    if payload.voice is not None:
        kinds.append("voice")
    if payload.photo is not None:
        kinds.append("photo")
    return kinds


def _assert_no_exif(image_bytes: bytes) -> None:
    """Raise if JPEG still carries an APP1 Exif segment after redaction (I4)."""
    if len(image_bytes) < 2 or image_bytes[:2] != b"\xff\xd8":
        return
    i = 2
    n = len(image_bytes)
    while i < n:
        if image_bytes[i] != 0xFF:
            break
        while i < n and image_bytes[i] == 0xFF:
            i += 1
        if i >= n:
            break
        marker = image_bytes[i]
        i += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        if i + 2 > n:
            break
        seglen = int.from_bytes(image_bytes[i : i + 2], "big")
        if seglen < 2 or i + seglen > n:
            break
        payload = image_bytes[i + 2 : i + seglen]
        if marker == 0xE1 and (
            payload.startswith(b"Exif\x00\x00") or payload.startswith(b"Exif\x00")
        ):
            raise AssertionError(
                "EXIF remains after redact_image_metadata — refusing Zone C egress (I4)"
            )
        if marker == 0xDA:
            break
        i += seglen


def _request_payload_for_pii_check(request: ModelRequest) -> dict:
    """Final Zone C request shape — strings/metadata only for assert_no_pii."""
    return {
        "system": request.system,
        "prompt": request.prompt,
        "temperature": request.temperature,
        "max_output_tokens": request.max_output_tokens,
        "timeout_seconds": request.timeout_seconds,
        "images": [
            {"mime_type": img.mime_type, "nbytes": len(img.data)}
            for img in request.images
        ],
        "audio": [
            {
                "mime_type": part.mime_type,
                "nbytes": len(part.data),
                "duration_seconds": part.duration_seconds,
            }
            for part in request.audio
        ],
    }


def _fmt_when(dt: datetime) -> str:
    """Human UTC stamp that does not match Guardian DOB shapes (I4 assert_no_pii)."""
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%d %b %Y %H:%M UTC")


def _build_user_prompt(
    *,
    redacted_text: str,
    original_itinerary: list[Segment],
    kinds_found: list[str],
    has_voice: bool,
    has_photo: bool,
) -> str:
    # Context for origin/destination candidates only — no flight numbers/carriers (I1).
    airports: list[dict[str, str]] = []
    for seg in original_itinerary:
        airports.append(
            {
                "origin": seg.origin,
                "destination": seg.destination,
                "departure_at": _fmt_when(seg.departure_at),
                "arrival_at": _fmt_when(seg.arrival_at),
            }
        )
    parts = [
        f"Current time (UTC): {_fmt_when(datetime.now(UTC))}",
        f"Redaction kinds applied: {kinds_found or []}",
        (
            "Original itinerary airports (constraints context only; "
            "do NOT emit flights/carriers/prices/offer ids):"
        ),
        json.dumps(airports, separators=(",", ":")),
        "",
    ]
    if redacted_text.strip():
        parts.append("Traveller message (already Guardian-redacted):")
        parts.append(redacted_text)
        parts.append("")
    if has_voice:
        parts.append(
            "A voice note is attached to this request. Transcribe it and extract "
            "constraints only. Do not invent destinations, deadlines, budgets, or "
            "mobility needs that the voice does not state."
        )
        parts.append("")
    if has_photo:
        parts.append(
            "A departure-board (or similar) photo is attached. Read it for "
            "disruption facts as evidence only — cancelled/delayed status, "
            "airports shown, times shown. Photo-derived text is never an offer "
            "(I1): do not emit flight numbers, carriers, prices, or itineraries "
            "as recommendations; airport codes may appear only in "
            "origin_candidates / destination_candidates when clearly visible."
        )
        parts.append("")
    return "\n".join(parts)


def _drop_itinerary_shaped(draft: InterpreterIntentDraft) -> InterpreterIntentDraft:
    """Any itinerary-shaped field in a model response is dropped, not stored (I1)."""
    raw = draft.model_dump(mode="python")
    cleaned = {k: v for k, v in raw.items() if k not in _ITINERARY_SHAPED_KEYS}
    extra = getattr(draft, "__pydantic_extra__", None) or {}
    for key in list(extra):
        if key in _ITINERARY_SHAPED_KEYS:
            extra.pop(key, None)
    return InterpreterIntentDraft.model_validate(cleaned)


def _draft_to_intent(
    *,
    case_id: int,
    draft: InterpreterIntentDraft,
    raw_input_kinds: list[str],
    persist: bool,
    session: Session | None = None,
) -> RecoveryIntent:
    intent = RecoveryIntent(
        case_id=case_id,
        passenger_count=draft.passenger_count,
        must_arrive_by=draft.must_arrive_by,
        budget_ceiling_sgd=draft.budget_ceiling_sgd,
        origin_candidates=json.dumps(
            list(draft.origin_candidates), separators=(",", ":")
        ),
        destination_candidates=json.dumps(
            list(draft.destination_candidates), separators=(",", ":")
        ),
        mobility_notes=draft.mobility_notes,
        language=draft.language,
        confidence=float(draft.confidence),
        raw_input_kinds=json.dumps(list(raw_input_kinds), separators=(",", ":")),
    )
    if persist:
        if session is None:
            raise ValueError("session required when persist=True")
        session.add(intent)
        session.flush()
    return intent


def _fallback_clarification(language: str) -> str:
    lang = (language or "en").lower()
    if lang.startswith("zh"):
        return "请问您需要抵达哪个目的地，以及最晚什么时候到达？"
    if lang.startswith("ms"):
        return "Ke mana anda perlu tiba, dan sebelum bilakah?"
    return "Where do you need to arrive, and by when?"
