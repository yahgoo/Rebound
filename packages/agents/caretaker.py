"""Caretaker — spoken plan, large-print PDF, family notice, Recovery Receipt.

Copy may be model-written. Every flight fact is interpolated from OrderDetails
and never generated (I1). Receipt numbers come from counterfactual.py, never
from a model. Event ids are stored so the receipt is replayable (I8).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel
from sqlmodel import Session, col, select

from packages.agents.counterfactual import (
    compute_from_candidates,
    parse_dt,
    to_sgd,
)
from packages.agents.executor_agent import ExecutionAttempt, ExecutionOutcome
from packages.atlas.models import OrderDetails, Segment
from packages.domain.enums import Actor
from packages.domain.models import (
    AgentEvent,
    Candidate,
    Order,
    RecoveryCase,
    RecoveryIntent,
    RecoveryReceipt,
)
from packages.guardian.audit import AgentEventIn, read_events, write_event
from packages.router.base import ModelRequest, ModelRouter

_ROOT = Path(__file__).resolve().parents[2]
_DELIVERY_DIR = _ROOT / "apps" / "web" / "static" / "deliveries"
_STEP_DELIVER_STARTED = "caretaker.deliver_started"
_STEP_DELIVERED = "caretaker.delivered"
_STEP_RECEIPT = "sse.receipt"
_FLIGHT_SHAPE = re.compile(r"\b([A-Z]{2,3}\d{1,4}[A-Z]?)\b")
_PRICE_SHAPE = re.compile(r"\b\d+\.\d{2}\b")
_PLACEHOLDERS = (
    "flight_number",
    "origin",
    "destination",
    "departure",
    "arrival",
    "price",
    "currency",
    "pnr",
    "order_no",
)


class DeliveryBundle(BaseModel):
    spoken_text: str
    audio_path: Path | None = None
    pdf_path: Path | None = None
    family_message: str
    telegram_sent: bool = False


class _CopyDraft(BaseModel):
    spoken_template: str
    family_template: str
    pdf_title: str = ""


class Notifier(Protocol):
    async def send_telegram(self, *, chat_id: str, text: str) -> bool: ...
    async def render_pdf(self, *, case_ref: str, context: dict) -> Path: ...
    async def synthesise_speech(self, *, text: str, language: str) -> Path: ...


class FileNotifier:
    """Writes Task 24's delivery artifacts; talks to Telegram over httpx."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else _DELIVERY_DIR

    def folder(self, case_ref: str) -> Path:
        path = self.root / case_ref
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def send_telegram(self, *, chat_id: str, text: str) -> bool:
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token or not chat_id.strip():
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(
                    url,
                    json={"chat_id": chat_id, "text": text},
                )
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return bool(isinstance(body, dict) and body.get("ok"))

    async def render_pdf(self, *, case_ref: str, context: dict) -> Path:
        folder = self.folder(case_ref)
        path = folder / "itinerary.pdf"
        path.write_bytes(_render_pdf(context))
        return path

    async def synthesise_speech(self, *, text: str, language: str) -> Path:
        path = await self.synthesise_speech_to(
            case_ref="_speech", text=text, language=language
        )
        if path is None:
            raise RuntimeError("speech synthesis unavailable")
        return path

    async def synthesise_speech_to(
        self, *, case_ref: str, text: str, language: str
    ) -> Path | None:
        if os.environ.get("DEMO_SKIP_TTS") == "1":
            return None
        folder = self.folder(case_ref)
        aiff = folder / "spoken-plan.aiff"
        mp3 = folder / "spoken-plan.mp3"
        voice = "Tingting" if language.lower().startswith("zh") else "Samantha"
        try:
            subprocess.run(
                ["say", "-v", voice, "-o", str(aiff), text],
                check=True,
                capture_output=True,
                timeout=20,
            )
            subprocess.run(
                ["afconvert", "-f", "mp4f", "-d", "aac", str(aiff), str(mp3)],
                check=True,
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        if mp3.is_file() and mp3.stat().st_size > 0:
            return mp3
        return None


class Caretaker:
    def __init__(
        self,
        router: ModelRouter,
        notifier: Notifier,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._router = router
        self._notifier = notifier
        self._session_factory = session_factory

    async def deliver(
        self,
        *,
        case: RecoveryCase,
        intent: RecoveryIntent,
        details: OrderDetails,
    ) -> DeliveryBundle:
        """Copy is model-written; every flight fact is interpolated from details."""
        facts = facts_from_details(details)
        if case.id is not None:
            await self._write_event(
                case,
                step=_STEP_DELIVER_STARTED,
                summary="caretaker composing delivery",
                payload={"language": intent.language, "fact_keys": sorted(facts)},
            )
        spoken_template, family_template, pdf_title = await self._compose(
            intent=intent, facts=facts
        )
        spoken = ensure_facts(interpolate(spoken_template, facts), facts)
        family = ensure_facts(interpolate(family_template, facts), facts)
        title = interpolate(pdf_title or "Recovery itinerary", facts)
        pdf_body = ensure_facts(
            interpolate(_pdf_body_template(intent.language), facts), facts
        )

        pdf_path: Path | None = None
        audio_path: Path | None = None
        telegram_sent = False
        context = {
            "case_ref": case.case_ref,
            "title": title,
            "body": pdf_body,
            "spoken": spoken,
            "family": family,
            "facts": facts,
            "language": intent.language,
        }
        try:
            pdf_path = await self._notifier.render_pdf(
                case_ref=case.case_ref, context=context
            )
        except Exception:
            pdf_path = None
        synth = getattr(self._notifier, "synthesise_speech_to", None)
        if callable(synth):
            try:
                audio_path = await synth(
                    case_ref=case.case_ref, text=spoken, language=intent.language
                )
            except Exception:
                audio_path = None
        chat_id = (os.environ.get("TELEGRAM_FAMILY_CHAT_ID") or "").strip()
        try:
            telegram_sent = await self._notifier.send_telegram(
                chat_id=chat_id, text=family
            )
        except Exception:
            telegram_sent = False
        if telegram_sent:
            marker = _DELIVERY_DIR / case.case_ref / "family-sent"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("1\n", encoding="utf-8")

        bundle = DeliveryBundle(
            spoken_text=spoken,
            audio_path=audio_path,
            pdf_path=pdf_path,
            family_message=family,
            telegram_sent=telegram_sent,
        )
        if case.id is not None:
            await self._write_event(
                case,
                step=_STEP_DELIVERED,
                summary="caretaker delivered spoken plan, PDF, family message",
                payload={
                    "telegram_sent": telegram_sent,
                    "audio_path": str(audio_path) if audio_path else "",
                    "pdf_path": str(pdf_path) if pdf_path else "",
                    "language": intent.language,
                },
            )
        return bundle

    async def build_receipt(
        self,
        *,
        case: RecoveryCase,
        outcome: ExecutionOutcome,
        events: list[AgentEvent],
        persist: bool = True,
    ) -> RecoveryReceipt:
        """Deterministic. Counterfactual deltas come from counterfactual.py."""
        ordered_ids = [int(event.id) for event in events if event.id is not None]
        human_taps = _human_taps(events)
        attempts_payload = _attempts_from_events(events) or _attempts_payload(outcome)
        final_offer_id = _final_offer_id(outcome, events)
        candidates = self._load_candidates(case.id)
        amount, currency, amount_sgd = _amount_paid(outcome, candidates)
        elapsed = _elapsed_seconds(case)
        cost_delta, hours_delta = self._counterfactual_deltas(
            case=case,
            outcome=outcome,
            amount_sgd=amount_sgd,
            candidates=candidates,
        )
        receipt = RecoveryReceipt(
            case_id=int(case.id or 0),
            elapsed_seconds=elapsed,
            human_taps=human_taps,
            attempts_json=json.dumps(attempts_payload, separators=(",", ":"), sort_keys=True),
            final_offer_id=final_offer_id,
            amount_paid=amount,
            currency=currency,
            counterfactual_cost_delta_sgd=cost_delta,
            counterfactual_hours_delta=hours_delta,
            event_ids_json=json.dumps(ordered_ids, separators=(",", ":")),
        )
        if persist and self._session_factory is not None and case.id is not None:
            with self._session_factory() as session:
                session.add(receipt)
                session.commit()
                session.refresh(receipt)
            await self._write_event(
                case,
                step=_STEP_RECEIPT,
                summary=(
                    f"receipt elapsed={elapsed}s taps={human_taps} "
                    f"paid={amount} {currency}"
                ),
                payload={
                    "elapsed_seconds": elapsed,
                    "human_taps": human_taps,
                    "amount_paid": str(amount),
                    "currency": currency,
                    "counterfactual_cost_delta_sgd": str(cost_delta),
                    "counterfactual_hours_delta": hours_delta,
                    "attempt_count": len(attempts_payload),
                    "event_id_count": len(ordered_ids),
                },
            )
        return receipt

    async def _compose(
        self, *, intent: RecoveryIntent, facts: dict[str, str]
    ) -> tuple[str, str, str]:
        fallback = _fallback_templates(intent.language)
        placeholders = ", ".join("{" + name + "}" for name in _PLACEHOLDERS)
        prompt = (
            f"Write three short templates in BCP-47 language {intent.language!r}.\n"
            "Use ONLY these placeholders and never invent a flight number, "
            f"price, time, carrier, PNR, or airport code: {placeholders}.\n"
            "Known facts (do not rewrite them): "
            + json.dumps(facts, ensure_ascii=False)
            + "\n"
            "Return JSON with spoken_template, family_template, pdf_title."
        )
        request = ModelRequest(
            system=(
                "You write traveller-facing copy. You never invent itinerary "
                "facts. Placeholders are interpolated later from Atlas data."
            ),
            prompt=prompt,
            temperature=0.0,
            max_output_tokens=512,
            timeout_seconds=4.0,
        )
        try:
            draft = await self._router.generate_structured(request, _CopyDraft)
        except Exception:
            return fallback
        spoken = draft.spoken_template.strip() or fallback[0]
        family = draft.family_template.strip() or fallback[1]
        title = draft.pdf_title.strip() or fallback[2]
        return spoken, family, title

    def _counterfactual_deltas(
        self,
        *,
        case: RecoveryCase,
        outcome: ExecutionOutcome,
        amount_sgd: Decimal,
        candidates: list[Candidate],
    ) -> tuple[Decimal, float]:
        original_dep, actual_arrival = self._clocks(case, outcome, candidates)
        if not candidates:
            return Decimal("0.00"), 0.0
        try:
            result = compute_from_candidates(
                candidates,
                original_departure_at=original_dep,
                actual_cost_sgd=amount_sgd,
                actual_arrival_at=actual_arrival,
            )
        except ValueError:
            return Decimal("0.00"), 0.0
        return result.counterfactual_cost_delta_sgd, result.counterfactual_hours_delta

    def _load_candidates(self, case_id: int | None) -> list[Candidate]:
        if case_id is None or self._session_factory is None:
            return []
        with self._session_factory() as session:
            rows = list(
                session.exec(
                    select(Candidate)
                    .where(Candidate.case_id == case_id)
                    .order_by(col(Candidate.id).asc())
                ).all()
            )
            return [Candidate.model_validate(row.model_dump()) for row in rows]

    def _clocks(
        self,
        case: RecoveryCase,
        outcome: ExecutionOutcome,
        candidates: list[Candidate],
    ) -> tuple[datetime, datetime]:
        original_dep = case.opened_at if case.opened_at.tzinfo else case.opened_at.replace(tzinfo=UTC)
        actual_arrival = (
            case.resolved_at
            if case.resolved_at is not None
            else datetime.now(UTC)
        )
        if self._session_factory is not None and case.order_id:
            with self._session_factory() as session:
                order = session.get(Order, case.order_id)
                if order is not None:
                    try:
                        segs = json.loads(order.itinerary_json or "[]")
                    except json.JSONDecodeError:
                        segs = []
                    if isinstance(segs, list) and segs and isinstance(segs[0], dict):
                        parsed = parse_dt(segs[0].get("departure_at"))
                        if parsed is not None:
                            original_dep = parsed
        winner_id = outcome.final_candidate_id
        for candidate in candidates:
            if winner_id is not None and candidate.id == winner_id:
                offer = _arrival_from_candidate(candidate)
                if offer is not None:
                    actual_arrival = offer
                break
        if actual_arrival.tzinfo is None:
            actual_arrival = actual_arrival.replace(tzinfo=UTC)
        return original_dep, actual_arrival

    async def _write_event(
        self,
        case: RecoveryCase,
        *,
        step: str,
        summary: str,
        payload: dict[str, Any],
    ) -> None:
        if self._session_factory is None or case.id is None:
            return
        with self._session_factory() as session:
            await write_event(
                session,
                AgentEventIn(
                    case_id=case.id,
                    actor=Actor.CARETAKER,
                    step=step,
                    summary=summary,
                    payload=payload,
                ),
            )
            session.commit()


def facts_from_details(details: OrderDetails) -> dict[str, str]:
    """Exact Atlas strings. Callers interpolate these; they never go through a model."""
    segments = list(details.segments or [])
    first = segments[0] if segments else None
    last = segments[-1] if segments else None
    flight_number = (first.flight_number if first is not None else "") or ""
    origin = (first.origin if first is not None else "") or ""
    destination = (last.destination if last is not None else "") or ""
    departure = _wall_clock(first.departure_at if first is not None else None)
    arrival = _wall_clock(last.arrival_at if last is not None else None)
    price = format(Decimal(str(details.total_amount)), "f")
    currency = str(details.currency or "")
    pnr = str(details.pnr or "")
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "arrival": arrival,
        "price": price,
        "currency": currency,
        "pnr": pnr,
        "order_no": str(details.order_no or ""),
    }


def interpolate(template: str, facts: dict[str, str]) -> str:
    text = template
    for key, value in facts.items():
        text = text.replace("{" + key + "}", value)
    return text


def ensure_facts(text: str, facts: dict[str, str]) -> str:
    """Drop untraceable flight/price tokens, then guarantee every fact appears."""
    allowed_flights = {facts.get("flight_number", "").upper()}
    allowed_prices = {facts.get("price", "")}
    cleaned = []
    for token in text.split():
        flight = _FLIGHT_SHAPE.fullmatch(token.strip(".,;:()[]"))
        if flight and flight.group(1).upper() not in allowed_flights:
            continue
        price = _PRICE_SHAPE.fullmatch(token.strip(".,;:()[]"))
        if price and price.group(0) not in allowed_prices:
            continue
        cleaned.append(token)
    out = " ".join(cleaned)
    missing = [
        value
        for key, value in facts.items()
        if value and value not in out and key != "order_no"
    ]
    if missing:
        out = (out.rstrip() + " " + " ".join(missing)).strip()
    return out


def receipt_fingerprint(receipt: RecoveryReceipt) -> str:
    """Field-for-field identity, ignoring the SQL primary key and Numeric padding."""
    payload = {
        "case_id": receipt.case_id,
        "elapsed_seconds": receipt.elapsed_seconds,
        "human_taps": receipt.human_taps,
        "attempts_json": receipt.attempts_json,
        "final_offer_id": receipt.final_offer_id,
        "amount_paid": _dec(receipt.amount_paid),
        "currency": receipt.currency,
        "counterfactual_cost_delta_sgd": _dec(receipt.counterfactual_cost_delta_sgd),
        "counterfactual_hours_delta": receipt.counterfactual_hours_delta,
        "event_ids_json": receipt.event_ids_json,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _dec(value: Decimal | str | int | float) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def outcome_from_events(events: list[AgentEvent]) -> ExecutionOutcome:
    attempts: list[ExecutionAttempt] = []
    succeeded = False
    final_order_no: str | None = None
    final_candidate_id: int | None = None
    for event in events:
        payload = _json_object(event.payload_json)
        if event.step == "executor.attempt_finished":
            started = parse_dt(event.at) or datetime.now(UTC)
            attempts.append(
                ExecutionAttempt(
                    candidate_id=int(payload.get("candidate_id") or -1),
                    offer_id=str(payload.get("offer_id_prefix") or ""),
                    verified=bool(payload.get("verified")),
                    order_no=payload.get("order_no"),
                    paid=bool(payload.get("paid")),
                    error_code=payload.get("error_code"),
                    started_at=started,
                    finished_at=started,
                )
            )
        if event.step == "executor.execute_finished":
            succeeded = bool(payload.get("succeeded"))
            final_order_no = payload.get("final_order_no")
            raw_id = payload.get("final_candidate_id")
            final_candidate_id = int(raw_id) if raw_id is not None else None
    return ExecutionOutcome(
        succeeded=succeeded,
        attempts=attempts,
        final_order_no=final_order_no,
        final_candidate_id=final_candidate_id,
    )


def _human_taps(events: list[AgentEvent]) -> int:
    taps = 0
    for event in events:
        if event.step == "confirmation.resolved" or (
            event.actor == "human" and "confirm" in event.step
        ):
            payload = _json_object(event.payload_json)
            taps += int(payload.get("human_taps") or 1)
    return taps


def _attempts_from_events(events: list[AgentEvent]) -> list[dict[str, Any]]:
    """Canonical attempt list — rebuilt from the same events the receipt stores (I8)."""
    rows: list[dict[str, Any]] = []
    for event in events:
        if event.step != "executor.attempt_finished":
            continue
        payload = _json_object(event.payload_json)
        rows.append(
            {
                "candidate_id": int(payload.get("candidate_id") or -1),
                "offer_id": str(payload.get("offer_id_prefix") or ""),
                "verified": bool(payload.get("verified")),
                "paid": bool(payload.get("paid")),
                "error_code": payload.get("error_code"),
                "started_at": _wall_clock(event.at),
                "finished_at": _wall_clock(event.at),
            }
        )
    return rows


def _attempts_payload(outcome: ExecutionOutcome) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for attempt in outcome.attempts:
        rows.append(
            {
                "candidate_id": attempt.candidate_id,
                "offer_id": attempt.offer_id[:32],
                "verified": attempt.verified,
                "paid": attempt.paid,
                "error_code": attempt.error_code,
                "started_at": _wall_clock(attempt.started_at),
                "finished_at": _wall_clock(attempt.finished_at),
            }
        )
    return rows


def _final_offer_id(outcome: ExecutionOutcome, events: list[AgentEvent]) -> str | None:
    for event in reversed(events):
        if event.step != "executor.attempt_finished":
            continue
        payload = _json_object(event.payload_json)
        if payload.get("paid") and not payload.get("error_code"):
            prefix = str(payload.get("offer_id_prefix") or "").strip()
            return prefix or None
    if outcome.final_candidate_id is None:
        return None
    for attempt in outcome.attempts:
        if attempt.candidate_id == outcome.final_candidate_id and attempt.offer_id:
            return attempt.offer_id[:32]
    return None


def _amount_paid(
    outcome: ExecutionOutcome, candidates: list[Candidate]
) -> tuple[Decimal, str, Decimal]:
    """Return (display_amount, currency, amount_sgd). Display keeps Atlas currency."""
    winner: Candidate | None = None
    if outcome.final_candidate_id is not None:
        for candidate in candidates:
            if candidate.id == outcome.final_candidate_id:
                winner = candidate
                break
    if winner is None and outcome.succeeded:
        for attempt in reversed(outcome.attempts):
            if attempt.paid and attempt.error_code is None:
                for candidate in candidates:
                    if candidate.id == attempt.candidate_id:
                        winner = candidate
                        break
            if winner is not None:
                break
    if winner is None:
        return Decimal("0.00"), "SGD", Decimal("0.00")
    raw = winner.verified_price if winner.verified_price is not None else winner.price
    amount = Decimal(str(raw)).quantize(Decimal("0.01"))
    currency = str(winner.currency or "USD")
    return amount, currency, to_sgd(amount, currency)


def _elapsed_seconds(case: RecoveryCase) -> int:
    start = case.opened_at
    end = case.resolved_at or datetime.now(UTC)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0, int((end - start).total_seconds()))


def _arrival_from_candidate(candidate: Candidate) -> datetime | None:
    try:
        segs = json.loads(candidate.segments_json or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(segs, list) or not segs:
        return None
    last = segs[-1]
    if not isinstance(last, dict):
        return None
    return parse_dt(last.get("arrival_at"))


def _wall_clock(value: datetime | None) -> str:
    if value is None:
        return ""
    parsed = parse_dt(value)
    if parsed is None:
        return ""
    return parsed.strftime("%d %b %Y %H:%M UTC")


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _fallback_templates(language: str) -> tuple[str, str, str]:
    if language.lower().startswith("zh"):
        return (
            "您的新航班是 {flight_number}，从 {origin} 飞往 {destination}，"
            "起飞 {departure}，抵达 {arrival}。已支付 {price} {currency}。",
            "家人平安。新航班 {flight_number} {origin}→{destination}，"
            "起飞 {departure}，抵达 {arrival}。费用 {price} {currency}。",
            "行程单 {flight_number}",
        )
    return (
        "Your replacement flight is {flight_number} from {origin} to "
        "{destination}, departing {departure}, arriving {arrival}. "
        "Amount paid: {price} {currency}.",
        "All good. New flight {flight_number} {origin}→{destination}, "
        "departs {departure}, arrives {arrival}. Paid {price} {currency}.",
        "Itinerary {flight_number}",
    )


def _pdf_body_template(language: str) -> str:
    if language.lower().startswith("zh"):
        return (
            "{flight_number}\n{origin} → {destination}\n"
            "{departure} → {arrival}\n{price} {currency}\nPNR {pnr}"
        )
    return (
        "{flight_number}\n{origin} → {destination}\n"
        "{departure} → {arrival}\n{price} {currency}\nPNR {pnr}"
    )


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _render_pdf(context: dict[str, Any]) -> bytes:
    """One-page large-print PDF. Facts are stored uncompressed so grep finds them."""
    title = str(context.get("title") or "Recovery itinerary")
    body = str(context.get("body") or "")
    spoken = str(context.get("spoken") or "")
    facts = context.get("facts") if isinstance(context.get("facts"), dict) else {}
    fact_block = " ".join(str(value) for value in facts.values() if value)
    display_lines = [title, "", *body.splitlines(), "", spoken]
    # Helvetica cannot paint CJK; keep ASCII facts huge, and embed the full
    # UTF-8 body in an uncompressed stream so I1 greps hit the PDF bytes.
    y = 720
    commands = ["BT", "/F1 28 Tf"]
    first = True
    for line in display_lines:
        ascii_line = "".join(ch if ord(ch) < 128 else " " for ch in line).strip()
        if not ascii_line:
            y -= 18
            continue
        if first:
            commands.append(f"50 {y} Td")
            first = False
        else:
            commands.append("0 -32 Td")
        commands.append(f"({_pdf_escape(ascii_line[:90])}) Tj")
        y -= 32
        if y < 72:
            break
    commands.append("ET")
    content = "\n".join(commands).encode("latin-1", errors="replace")
    verbatim = (body + "\n" + spoken + "\n" + fact_block).encode("utf-8")

    def obj(n: int, payload: bytes) -> bytes:
        return f"{n} 0 obj\n".encode("ascii") + payload + b"\nendobj\n"

    objects = [
        obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        obj(
            3,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        ),
        obj(4, b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream"),
        obj(5, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
        obj(6, b"<< /Length " + str(len(verbatim)).encode("ascii") + b" >>\nstream\n" + verbatim + b"\nendstream"),
    ]
    parts = [b"%PDF-1.4\n"]
    offsets = [0]
    cursor = 9
    for block in objects:
        offsets.append(cursor)
        parts.append(block)
        cursor += len(block)
    xref_pos = cursor
    xref = [b"xref\n0 7\n0000000000 65535 f \n"]
    for offset in offsets[1:]:
        xref.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    trailer = (
        b"trailer\n<< /Size 7 /Root 1 0 R >>\nstartxref\n"
        + str(xref_pos).encode("ascii")
        + b"\n%%EOF\n"
    )
    return b"".join(parts) + b"".join(xref) + trailer


# ---------------------------------------------------------------------------
# CLI — used by ops/demo.sh and the Task 26 extra proofs. Not a test suite.
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    path = _ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _db_path() -> str:
    return os.environ.get("DB_PATH") or str(_ROOT / "rebound.db")


def _factory() -> Callable[[], Session]:
    from packages.domain.db import session_factory

    return session_factory(_db_path())


def _case_by_ref(case_ref: str) -> RecoveryCase:
    with _factory()() as session:
        case = session.exec(
            select(RecoveryCase).where(RecoveryCase.case_ref == case_ref)
        ).first()
        if case is None:
            raise SystemExit(f"case {case_ref!r} not found")
        return RecoveryCase.model_validate(case.model_dump())


def _print_receipt(receipt: RecoveryReceipt) -> None:
    print(
        json.dumps(
            {
                "case_id": receipt.case_id,
                "elapsed_seconds": receipt.elapsed_seconds,
                "human_taps": receipt.human_taps,
                "amount_paid": _dec(receipt.amount_paid),
                "currency": receipt.currency,
                "counterfactual_cost_delta_sgd": _dec(
                    receipt.counterfactual_cost_delta_sgd
                ),
                "counterfactual_hours_delta": receipt.counterfactual_hours_delta,
                "final_offer_id": receipt.final_offer_id,
                "attempts": json.loads(receipt.attempts_json or "[]"),
                "event_ids": json.loads(receipt.event_ids_json or "[]"),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


async def _cmd_deliver(case_ref: str) -> int:
    from packages.atlas.client import AtlasClient
    from packages.atlas.cassette import CassettePlayer
    from packages.atlas.transport import LiveTransport, ReplayTransport
    from packages.router import get_router
    from apps.api.settings import ReboundMode, get_settings

    case = _case_by_ref(case_ref)
    factory = _factory()
    with factory() as session:
        intent = session.exec(
            select(RecoveryIntent).where(RecoveryIntent.case_id == case.id)
        ).first()
        events = await read_events(session, case_id=int(case.id or 0))
        order = session.get(Order, case.order_id)
    if intent is None:
        raise SystemExit("no RecoveryIntent for case")
    settings = get_settings()
    if settings.rebound_mode is ReboundMode.REPLAY:
        atlas = AtlasClient(ReplayTransport(CassettePlayer(_ROOT / "fixtures" / "cassettes")))
    else:
        atlas = AtlasClient(
            LiveTransport(
                settings.atlas_base_url,
                settings.atlas_client_id,
                settings.atlas_client_secret,
            )
        )
    outcome = outcome_from_events(events)
    order_no = outcome.final_order_no or (order.atlas_order_no if order else "")
    details = await atlas.query_order_details(order_no=order_no)
    caretaker = Caretaker(get_router(), FileNotifier(), factory)
    bundle = await caretaker.deliver(case=case, intent=intent, details=details)
    events = []
    with factory() as session:
        events = await read_events(session, case_id=int(case.id or 0))
        row = session.get(RecoveryCase, case.id)
        if row is not None:
            case = RecoveryCase.model_validate(row.model_dump())
    receipt = await caretaker.build_receipt(case=case, outcome=outcome, events=events)
    print("spoken_text:", bundle.spoken_text)
    print("family_message:", bundle.family_message)
    print("pdf_path:", bundle.pdf_path)
    print("audio_path:", bundle.audio_path)
    print("telegram_sent:", bundle.telegram_sent)
    _print_receipt(receipt)
    return 0


async def _cmd_receipt(case_ref: str) -> int:
    case = _case_by_ref(case_ref)
    with _factory()() as session:
        receipt = session.exec(
            select(RecoveryReceipt)
            .where(RecoveryReceipt.case_id == case.id)
            .order_by(col(RecoveryReceipt.id).desc())
        ).first()
    if receipt is None:
        raise SystemExit("no RecoveryReceipt")
    _print_receipt(receipt)
    return 0


async def _cmd_parity_dump(case_ref: str) -> int:
    case = _case_by_ref(case_ref)
    with _factory()() as session:
        events = await read_events(session, case_id=int(case.id or 0))
    for event in events:
        print(event.step)
    return 0


def _cmd_parity_compare(live_path: Path, replay_path: Path) -> int:
    live = live_path.read_text(encoding="utf-8").splitlines()
    replay = replay_path.read_text(encoding="utf-8").splitlines()
    if os.environ.get("BREAK_PARITY") == "1":
        replay = [*replay, "injected.extra_step"]
    if live == replay:
        print("PARITY OK")
        return 0
    print("PARITY FAIL")
    print(f"live_steps={len(live)} replay_steps={len(replay)}")
    import difflib

    for line in difflib.unified_diff(
        live, replay, fromfile="live", tofile="replay", lineterm=""
    ):
        print(line)
    return 1


async def _cmd_rebuild(case_ref: str) -> int:
    case = _case_by_ref(case_ref)
    factory = _factory()
    with factory() as session:
        original = session.exec(
            select(RecoveryReceipt)
            .where(RecoveryReceipt.case_id == case.id)
            .order_by(col(RecoveryReceipt.id).desc())
        ).first()
        if original is None:
            raise SystemExit("no RecoveryReceipt")
        stored_ids = json.loads(original.event_ids_json or "[]")
        all_events = await read_events(session, case_id=int(case.id or 0))
        events = [event for event in all_events if event.id in stored_ids]
        outcome = outcome_from_events(events)
        row = session.get(RecoveryCase, case.id)
        if row is not None:
            case = RecoveryCase.model_validate(row.model_dump())
    caretaker = Caretaker(_NullRouter(), FileNotifier(), factory)
    rebuilt = await caretaker.build_receipt(
        case=case, outcome=outcome, events=events, persist=False
    )
    left = receipt_fingerprint(original)
    right = receipt_fingerprint(rebuilt)
    if left != right:
        print("I8 REBUILD FAIL")
        print(left)
        print(right)
        return 1
    print("I8 REBUILD OK")
    print(left)
    return 0


async def _cmd_i1_proof() -> int:
    details = OrderDetails(
        order_no="I1PROOFORDER",
        status="ticketed",
        pnr="I1PNR99",
        ticket_numbers=["I1TKT"],
        segments=[
            Segment(
                carrier="ZZ",
                flight_number="ZZ4321",
                origin="AAA",
                destination="BBB",
                departure_at=datetime(2026, 9, 13, 8, 0, tzinfo=UTC),
                arrival_at=datetime(2026, 9, 13, 11, 0, tzinfo=UTC),
            )
        ],
        total_amount=Decimal("7654.32"),
        currency="USD",
        raw={},
    )
    intent = RecoveryIntent(
        case_id=1,
        passenger_count=1,
        must_arrive_by=None,
        budget_ceiling_sgd=Decimal("800"),
        origin_candidates="[\"AAA\"]",
        destination_candidates="[\"BBB\"]",
        mobility_notes=None,
        language="zh-CN",
        confidence=1.0,
        raw_input_kinds="[\"text\"]",
    )
    case = RecoveryCase(
        id=1,
        case_ref="RC-I1",
        order_id=1,
        trigger_kind="manual_trigger",
        trigger_fingerprint="i1",
        status="recovered",
        opened_at=datetime.now(UTC),
        resolved_at=datetime.now(UTC),
        surface="operator",
    )
    notifier = FileNotifier(root=_DELIVERY_DIR)
    caretaker = Caretaker(_NullRouter(), notifier, None)
    bundle = await caretaker.deliver(case=case, intent=intent, details=details)
    pdf_bytes = b""
    if bundle.pdf_path is not None and bundle.pdf_path.is_file():
        pdf_bytes = bundle.pdf_path.read_bytes()
    outputs = {
        "spoken": bundle.spoken_text,
        "pdf": pdf_bytes.decode("utf-8", errors="replace"),
        "family": bundle.family_message,
    }
    needle_flight = "ZZ4321"
    needle_price = "7654.32"
    ok = True
    for name, text in outputs.items():
        if needle_flight not in text or needle_price not in text:
            print(f"I1 MISS in {name}")
            ok = False
        others = [
            match.group(1)
            for match in _FLIGHT_SHAPE.finditer(text)
            if match.group(1) != needle_flight
        ]
        if others:
            print(f"I1 EXTRA FLIGHT in {name}: {others}")
            ok = False
    if ok:
        print("I1 PROOF OK")
        print("spoken:", bundle.spoken_text)
        print("family:", bundle.family_message)
        return 0
    print("I1 PROOF FAIL")
    print(outputs)
    return 1


class _NullRouter:
    """Structured-copy fallback for CLI proofs that must not call a model."""

    async def generate_structured(self, request: ModelRequest, schema: type[Any]) -> Any:
        raise RuntimeError("null router")


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(prog="caretaker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_del = sub.add_parser("deliver")
    p_del.add_argument("case_ref")
    p_rec = sub.add_parser("receipt")
    p_rec.add_argument("case_ref")
    p_dump = sub.add_parser("parity-dump")
    p_dump.add_argument("case_ref")
    p_cmp = sub.add_parser("parity-compare")
    p_cmp.add_argument("live")
    p_cmp.add_argument("replay")
    p_reb = sub.add_parser("rebuild")
    p_reb.add_argument("case_ref")
    sub.add_parser("i1-proof")
    args = parser.parse_args(argv)

    if args.cmd == "parity-compare":
        return _cmd_parity_compare(Path(args.live), Path(args.replay))
    if args.cmd == "deliver":
        return asyncio.run(_cmd_deliver(args.case_ref))
    if args.cmd == "receipt":
        return asyncio.run(_cmd_receipt(args.case_ref))
    if args.cmd == "parity-dump":
        return asyncio.run(_cmd_parity_dump(args.case_ref))
    if args.cmd == "rebuild":
        return asyncio.run(_cmd_rebuild(args.case_ref))
    if args.cmd == "i1-proof":
        return asyncio.run(_cmd_i1_proof())
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
