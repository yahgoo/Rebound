"""Executor agent — score, verify, spend-cap (Task 18).

Owns the money path. score_and_verify only in this task; execute() is Task 19.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlmodel import Session

from packages.atlas.client import AtlasClient
from packages.atlas.errors import AtlasError, AtlasPriceMovedError
from packages.atlas.models import CardDetails, Passenger
from packages.domain.enums import Actor
from packages.domain.models import Candidate, Order, RecoveryCase, RecoveryIntent
from packages.executors.base import (
    CandidateForScoring,
    ExecutorUnavailableError,
    SandboxStatus,
    ScoredCandidate,
    ScoringInput,
)
from packages.executors.local import LocalExecutor, assert_zone_b_allowlist
from packages.guardian.audit import AgentEventIn, write_event
from packages.guardian.policy import ConfirmationGate, check_spend_cap

# Hard-coded USD→SGD — deterministic, never from a model (I3 / SPEC §5).
_USD_TO_SGD = Decimal("1.35")

_STEP_SCORING_STARTED = "executor.scoring_started"
_STEP_SCORING_DONE = "executor.scoring_done"
_STEP_SCORING_FALLBACK = "executor.scoring_fallback_local"
_STEP_VERIFY = "executor.verified"
_STEP_CAP_REJECT = "executor.cap_rejected"


class ExecutionAttempt(BaseModel):
    candidate_id: int
    offer_id: str
    verified: bool
    order_no: str | None
    paid: bool
    error_code: str | None  # "604" / "616" recorded verbatim
    started_at: datetime
    finished_at: datetime


class ExecutionOutcome(BaseModel):
    succeeded: bool
    attempts: list[ExecutionAttempt]  # in order, including every failure
    final_order_no: str | None
    final_candidate_id: int | None


class ExecutorAgent:
    """Owns the money path. The only caller of order/pay."""

    def __init__(
        self,
        atlas: AtlasClient,
        executor: Any,
        gate: ConfirmationGate,
        session_factory: Callable[[], Session],
    ) -> None:
        self._atlas = atlas
        self._executor = executor
        self._gate = gate
        self._session_factory = session_factory

    async def score_and_verify(
        self,
        *,
        case_id: int,
        candidates: list[Candidate],
        intent: RecoveryIntent,
        scoring_code: str,
        on_status: Callable[[SandboxStatus], Awaitable[None]] | None = None,
    ) -> list[Candidate]:
        """Scores in Zone B, verifies the top 3 (I2), applies check_spend_cap to
        each, and marks over-cap or unverifiable candidates rejected. Falls back
        to LocalExecutor on ExecutorUnavailableError."""
        if not candidates:
            return []

        case_ref, original_arrival_at = self._case_context(case_id, candidates)

        scoring_candidates = [
            _to_scoring_candidate(c) for c in candidates if c.offer_id
        ]
        known_ids = {c.offer_id for c in scoring_candidates}
        weight = _mobility_penalty_weight(intent.mobility_notes)

        payload = ScoringInput(
            case_ref=case_ref,
            candidates=scoring_candidates,
            must_arrive_by=intent.must_arrive_by,
            budget_ceiling_sgd=Decimal(str(intent.budget_ceiling_sgd)),
            mobility_penalty_weight=weight,
            original_arrival_at=original_arrival_at,
        )
        # Zone B allowlist — refuse anything outside ScoringInput before dispatch.
        assert_zone_b_allowlist(payload)

        await self._write_event(
            case_id=case_id,
            step=_STEP_SCORING_STARTED,
            summary=f"scoring {len(scoring_candidates)} candidates",
            payload={
                "candidate_count": len(scoring_candidates),
                "mobility_penalty_weight": weight,
                "executor_kind": str(getattr(self._executor, "kind", "unknown")),
            },
        )

        async def _forward(status: SandboxStatus) -> None:
            if on_status is not None:
                await on_status(status)

        scored: list[ScoredCandidate]
        try:
            scored = await self._executor.score(
                payload, scoring_code, on_status=_forward
            )
        except ExecutorUnavailableError as exc:
            await self._write_event(
                case_id=case_id,
                step=_STEP_SCORING_FALLBACK,
                summary="executor unavailable; falling back to LocalExecutor",
                payload={
                    "reason": str(exc)[:200],
                    "from_kind": str(getattr(self._executor, "kind", "unknown")),
                    "to_kind": "local",
                },
            )
            local = LocalExecutor(target_slots=8, timeout_seconds=20)
            try:
                scored = await local.score(
                    payload, scoring_code, on_status=_forward
                )
            finally:
                await local.close()

        # Drop scores for unknown offer ids (I1) — same pattern as Strategist.select.
        by_score: dict[str, ScoredCandidate] = {}
        for s in scored:
            if s.offer_id not in known_ids:
                continue
            prev = by_score.get(s.offer_id)
            if prev is None or s.score > prev.score:
                by_score[s.offer_id] = s

        # Persist score fields onto each Candidate (in-memory + DB).
        working = [_detach(c) for c in candidates]
        for c in working:
            s = by_score.get(c.offer_id)
            if s is None:
                continue
            c.score = float(s.score)
            c.score_components_json = json.dumps(
                s.components, separators=(",", ":"), sort_keys=True
            )
            c.self_transfer_risk = float(s.self_transfer_risk)
            c.mobility_fit = float(s.mobility_fit)

        ranked = sorted(
            working,
            key=lambda c: (
                -(c.score if c.score is not None else float("-inf")),
                c.offer_id,
            ),
        )

        await self._persist_scores(ranked)
        await self._write_event(
            case_id=case_id,
            step=_STEP_SCORING_DONE,
            summary=f"scored {len(by_score)} candidates",
            payload={
                "scored_count": len(by_score),
                "dropped_unknown": max(0, len(scored) - len(by_score)),
                "top_offer_ids": [c.offer_id for c in ranked[:3] if c.score is not None],
            },
        )

        # Verify top three only (I2). Unverified candidates never reach the cap.
        top = [c for c in ranked if c.score is not None][:3]
        env_cap = _env_spend_cap_sgd()
        intent_ceiling = Decimal(str(intent.budget_ceiling_sgd))

        for cand in top:
            await self._verify_one(cand)
            if not cand.verified or cand.verified_price is None:
                # I2: nothing unverified proceeds to the spend cap.
                continue
            await self._apply_cap(
                cand,
                intent_ceiling_sgd=intent_ceiling,
                env_cap_sgd=env_cap,
            )

        await self._persist_verify_and_cap(top)
        # Re-sort after verify/cap annotations; verified+under-cap float naturally
        # via score; rejected stay in ranking but are marked.
        return ranked

    async def execute(
        self,
        *,
        case_id: int,
        ordered_candidates: list[Candidate],
        passengers: list[Passenger],
        card: CardDetails,
        max_attempts: int = 3,
    ) -> ExecutionOutcome:
        raise NotImplementedError("Task 19: confirm, order, pay, failover")

    def _case_context(
        self, case_id: int, candidates: list[Candidate]
    ) -> tuple[str, datetime]:
        with self._session_factory() as session:
            case = session.get(RecoveryCase, case_id)
            if case is None:
                raise ValueError(f"RecoveryCase id={case_id} not found")
            case_ref = case.case_ref
            original_arrival = _original_arrival_from_order(session, case.order_id)
        if original_arrival is None:
            original_arrival = _infer_original_arrival(candidates)
        return case_ref, original_arrival

    async def _verify_one(self, cand: Candidate) -> None:
        rid = _routing_identifier(cand)
        if not rid:
            cand.verified = False
            cand.verified_price = None
            cand.rejected_reason = "verify_failed"
            await self._write_event(
                case_id=cand.case_id,
                step=_STEP_VERIFY,
                summary=f"verify_failed missing routing_identifier offer={cand.offer_id[:24]}",
                payload={
                    "offer_id_prefix": cand.offer_id[:32],
                    "verified": False,
                    "rejected_reason": "verify_failed",
                },
            )
            return

        try:
            result = await self._atlas.verify(routing_identifier=rid)
        except AtlasPriceMovedError as exc:
            cand.verified = False
            cand.verified_price = None
            cand.rejected_reason = "price_moved"
            await self._write_event(
                case_id=cand.case_id,
                step=_STEP_VERIFY,
                summary=f"price_moved offer={cand.offer_id[:24]}",
                payload={
                    "offer_id_prefix": cand.offer_id[:32],
                    "verified": False,
                    "rejected_reason": "price_moved",
                    "old_price": str(exc.old_price),
                    "new_price": str(exc.new_price),
                },
            )
            return
        except AtlasError as exc:
            cand.verified = False
            cand.verified_price = None
            cand.rejected_reason = "verify_failed"
            await self._write_event(
                case_id=cand.case_id,
                step=_STEP_VERIFY,
                summary=f"verify_failed offer={cand.offer_id[:24]} code={exc.code}",
                payload={
                    "offer_id_prefix": cand.offer_id[:32],
                    "verified": False,
                    "rejected_reason": "verify_failed",
                    "error_code": str(exc.code)[:64],
                },
            )
            return
        except Exception as exc:
            cand.verified = False
            cand.verified_price = None
            cand.rejected_reason = "verify_failed"
            await self._write_event(
                case_id=cand.case_id,
                step=_STEP_VERIFY,
                summary=f"verify_failed offer={cand.offer_id[:24]}",
                payload={
                    "offer_id_prefix": cand.offer_id[:32],
                    "verified": False,
                    "rejected_reason": "verify_failed",
                    "error_type": type(exc).__name__,
                },
            )
            return

        if not result.verified:
            cand.verified = False
            cand.verified_price = None
            cand.rejected_reason = "verify_failed"
            reason = "verify_failed"
        elif result.price_changed:
            # verify.do succeeded on the wire but the fare moved — reject, do not
            # pass to the spend cap or treat as orderable (I2).
            cand.verified = False
            cand.verified_price = None
            cand.rejected_reason = "price_moved"
            reason = "price_moved"
        else:
            cand.verified = True
            cand.verified_price = Decimal(str(result.price))
            cand.rejected_reason = None
            reason = None

        await self._write_event(
            case_id=cand.case_id,
            step=_STEP_VERIFY,
            summary=(
                f"verified={cand.verified} offer={cand.offer_id[:24]}"
                + (f" reason={reason}" if reason else "")
            ),
            payload={
                "offer_id_prefix": cand.offer_id[:32],
                "verified": cand.verified,
                "verified_price": (
                    str(cand.verified_price) if cand.verified_price is not None else None
                ),
                "rejected_reason": reason,
                "currency": result.currency,
            },
        )

    async def _apply_cap(
        self,
        cand: Candidate,
        *,
        intent_ceiling_sgd: Decimal,
        env_cap_sgd: Decimal,
    ) -> None:
        """I3: cap uses min(intent ceiling, env cap); amount from verified price only."""
        assert cand.verified and cand.verified_price is not None
        amount_sgd = _to_sgd(cand.verified_price, cand.currency)
        verdict = check_spend_cap(
            amount_sgd=amount_sgd,
            intent_ceiling_sgd=intent_ceiling_sgd,
            env_cap_sgd=env_cap_sgd,
        )
        if verdict.allowed:
            return
        cand.rejected_reason = "over_cap"
        await self._write_event(
            case_id=cand.case_id,
            step=_STEP_CAP_REJECT,
            summary=f"over_cap offer={cand.offer_id[:24]}",
            payload={
                "offer_id_prefix": cand.offer_id[:32],
                "rejected_reason": "over_cap",
                "amount_sgd": str(verdict.requested_sgd),
                "effective_cap_sgd": str(verdict.effective_cap_sgd),
                "intent_ceiling_sgd": str(intent_ceiling_sgd),
                "env_cap_sgd": str(env_cap_sgd),
                "verified_price": str(cand.verified_price),
                "currency": cand.currency,
            },
        )

    async def _persist_scores(self, candidates: list[Candidate]) -> None:
        with self._session_factory() as session:
            for c in candidates:
                if c.id is None:
                    continue
                row = session.get(Candidate, c.id)
                if row is None:
                    continue
                row.score = c.score
                row.score_components_json = c.score_components_json
                row.self_transfer_risk = c.self_transfer_risk
                row.mobility_fit = c.mobility_fit
            session.commit()

    async def _persist_verify_and_cap(self, candidates: list[Candidate]) -> None:
        with self._session_factory() as session:
            for c in candidates:
                if c.id is None:
                    continue
                row = session.get(Candidate, c.id)
                if row is None:
                    continue
                row.verified = c.verified
                row.verified_price = c.verified_price
                row.rejected_reason = c.rejected_reason
            session.commit()

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
                    actor=Actor.EXECUTOR,
                    step=step,
                    summary=summary,
                    payload=payload,
                ),
            )
            session.commit()


def _env_spend_cap_sgd() -> Decimal:
    raw = (os.environ.get("GUARDIAN_MAX_SPEND_SGD") or "").strip()
    if raw:
        return Decimal(raw)
    try:
        from apps.api.settings import get_settings

        return Decimal(str(get_settings().guardian_max_spend_sgd))
    except Exception:
        return Decimal("800")


def _to_sgd(amount: Decimal, currency: str) -> Decimal:
    cur = (currency or "").strip().upper()
    if cur in ("", "SGD"):
        return Decimal(str(amount))
    if cur == "USD":
        return (Decimal(str(amount)) * _USD_TO_SGD).quantize(Decimal("0.01"))
    # Unknown settlement currency: treat as SGD face value (deterministic).
    return Decimal(str(amount))


def _mobility_penalty_weight(notes: str | None) -> float:
    """Derive Zone B mobility_penalty_weight from RecoveryIntent.mobility_notes."""
    text = (notes or "").strip()
    if not text:
        return 0.0
    # Non-empty notes → fixed mid weight; longer notes nudge up, capped at 1.0.
    words = len(text.split())
    return float(min(Decimal("1.0"), Decimal("0.35") + Decimal(words) * Decimal("0.05")))


def _parse_segments(segments_json: str) -> list[dict]:
    """segments_json is a bare JSON list of Atlas segments (Task 17 shape)."""
    raw = json.loads(segments_json or "[]")
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    return []


def _routing_identifier(cand: Candidate) -> str | None:
    rid = (cand.routing_identifier or "").strip()
    return rid or None


def _to_scoring_candidate(cand: Candidate) -> CandidateForScoring:
    segs = _parse_segments(cand.segments_json)
    if segs:
        origin = str(segs[0].get("origin") or "")
        destination = str(segs[-1].get("destination") or "")
        arrival_raw = segs[-1].get("arrival_at")
        carriers = []
        seen: set[str] = set()
        for s in segs:
            carrier = str(s.get("carrier") or "").strip()
            if carrier and carrier not in seen:
                seen.add(carrier)
                carriers.append(carrier)
        arrival_at = _parse_dt(arrival_raw) or datetime.now(UTC)
    else:
        origin = ""
        destination = ""
        carriers = []
        arrival_at = datetime.now(UTC)

    return CandidateForScoring(
        offer_id=cand.offer_id,
        price=Decimal(str(cand.price)),
        currency=cand.currency,
        arrival_at=arrival_at,
        stop_count=int(cand.stop_count),
        min_transfer_minutes=cand.min_transfer_minutes,
        origin=origin,
        destination=destination,
        carriers=carriers,
    )


def _parse_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _original_arrival_from_order(session: Session, order_id: int) -> datetime | None:
    order = session.get(Order, order_id)
    if order is None:
        return None
    try:
        itinerary = json.loads(order.itinerary_json or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(itinerary, list) or not itinerary:
        return None
    last = itinerary[-1]
    if not isinstance(last, dict):
        return None
    return _parse_dt(last.get("arrival_at"))


def _infer_original_arrival(candidates: list[Candidate]) -> datetime:
    for c in candidates:
        segs = _parse_segments(c.segments_json)
        if not segs:
            continue
        arr = _parse_dt(segs[-1].get("arrival_at"))
        if arr is None:
            continue
        # arrival_delay_minutes = arrival - original → original = arrival - delay
        return arr.replace(microsecond=0)
    return datetime.now(UTC)


def _detach(c: Candidate) -> Candidate:
    return Candidate(
        id=c.id,
        case_id=c.case_id,
        offer_id=c.offer_id,
        routing_identifier=c.routing_identifier,
        strategy=c.strategy,
        segments_json=c.segments_json,
        price=c.price,
        currency=c.currency,
        arrival_delay_minutes=c.arrival_delay_minutes,
        stop_count=c.stop_count,
        min_transfer_minutes=c.min_transfer_minutes,
        self_transfer_risk=c.self_transfer_risk,
        mobility_fit=c.mobility_fit,
        score=c.score,
        score_components_json=c.score_components_json,
        verified=c.verified,
        verified_price=c.verified_price,
        rejected_reason=c.rejected_reason,
    )
