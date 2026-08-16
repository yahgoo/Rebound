"""Executor agent — score, verify, cap (Task 18) and execute (Task 19).

Owns the money path. The only caller of order/pay.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlmodel import Session, select

from packages.atlas.client import AtlasClient
from packages.atlas.errors import (
    AtlasDuplicateBookingError,
    AtlasError,
    AtlasPaymentDeclinedError,
    AtlasPriceMovedError,
    AtlasThreeDSRequiredError,
    AtlasTimeoutError,
)
from packages.atlas.models import CardDetails, Passenger, VerifyResult
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
_STEP_EXECUTE_STARTED = "executor.execute_started"
_STEP_ATTEMPT_STARTED = "executor.attempt_started"
_STEP_ORDERED = "executor.ordered"
_STEP_PAID = "executor.paid"
_STEP_PAY_FAILED = "executor.pay_failed"
_STEP_POLL = "executor.poll_result"
_STEP_ATTEMPT_FINISHED = "executor.attempt_finished"
_STEP_EXECUTE_FINISHED = "executor.execute_finished"

# I7: poll is authoritative; only this status counts as a recovered ticket.
_TICKETED_STATUSES = frozenset({"ticketed"})
_FAILOVER_ERROR_TYPES = (AtlasPaymentDeclinedError, AtlasThreeDSRequiredError)

# Zone A contact for order.do — not card data, not logged as PII keys.
_ORDER_CONTACT_EMAIL = "rebound.operator@example.com"
_ORDER_CONTACT_PHONE = "0065-91234567"

# Guardian assert_no_pii flags 13–19 Luhn digits as PAN and YYYYMMDD as DOB.
# Atlas sandbox orderNos look like TESTA20260814154123960 (date+time stamp).
_LONG_DIGIT_RUN = re.compile(r"\d{6,}")


def _audit_ref(value: str | None) -> str | None:
    """Keep a correlatable order token without PAN/DOB-shaped digit runs (I4).

    The live order_no stays on ExecutionAttempt and is passed to Atlas; only
    the audit payload/summary is rewritten.
    """
    if value is None:
        return None
    return _LONG_DIGIT_RUN.sub(
        lambda m: f"{m.group(0)[:2]}…{m.group(0)[-2:]}",
        str(value),
    )


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


class ConfirmationRequiredError(Exception):
    """execute() refused: no ConfirmationDecision for the first candidate (I6)."""


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
        """Requires gate.is_confirmed for the FIRST candidate (I6). On
        AtlasPaymentDeclinedError or AtlasThreeDSRequiredError, re-verifies the
        next candidate and retries automatically — the confirmed spend cap still
        binds every retry, and no new human tap is required for failover.
        Polls query_order_details after any success (I7)."""
        if not isinstance(card, CardDetails):
            raise TypeError("execute requires CardDetails")
        if not ordered_candidates:
            raise ValueError("execute requires at least one candidate")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not passengers:
            raise ValueError("execute requires at least one passenger")

        first = ordered_candidates[0]
        if first.id is None:
            raise ValueError("first candidate is unpersisted (no id)")
        # I6: raise, do not warn. No auto-approve. Failover does not re-check
        # later candidates against the gate — the original tap covers the case.
        if not self._gate.is_confirmed(case_id=case_id, candidate_id=first.id):
            raise ConfirmationRequiredError(
                f"execute refused: case_id={case_id} candidate_id={first.id} "
                "is not confirmed (I6)"
            )

        intent = self._load_intent(case_id)
        env_cap = _env_spend_cap_sgd()
        intent_ceiling = Decimal(str(intent.budget_ceiling_sgd))
        to_try = [_detach(c) for c in ordered_candidates[:max_attempts]]

        await self._set_case_status(case_id, "executing", resolved_at=None)
        await self._write_event(
            case_id=case_id,
            step=_STEP_EXECUTE_STARTED,
            summary=(
                f"execute confirmed candidate_id={first.id} "
                f"attempts_planned={len(to_try)}"
            ),
            payload={
                "confirmed_candidate_id": first.id,
                "planned_attempts": len(to_try),
                "max_attempts": max_attempts,
                "intent_ceiling_sgd": str(intent_ceiling),
                "env_cap_sgd": str(env_cap),
            },
        )

        attempts: list[ExecutionAttempt] = []
        for index, cand in enumerate(to_try):
            attempt = await self._attempt_one(
                case_id=case_id,
                cand=cand,
                passengers=passengers,
                card=card,
                attempt_index=index,
                intent_ceiling_sgd=intent_ceiling,
                env_cap_sgd=env_cap,
            )
            attempts.append(attempt)

            if attempt.paid and attempt.error_code is None:
                # Pay returned success — I7 poll already ran inside _attempt_one
                # and cleared error_code only when status was ticketed.
                await self._set_case_status(
                    case_id, "recovered", resolved_at=datetime.now(UTC)
                )
                await self._write_event(
                    case_id=case_id,
                    step=_STEP_EXECUTE_FINISHED,
                    summary=(
                        f"recovered order_no={_audit_ref(attempt.order_no)!r} "
                        f"attempts={len(attempts)}"
                    ),
                    payload={
                        "succeeded": True,
                        "final_order_no": _audit_ref(attempt.order_no),
                        "final_candidate_id": cand.id,
                        "attempt_count": len(attempts),
                    },
                )
                return ExecutionOutcome(
                    succeeded=True,
                    attempts=attempts,
                    final_order_no=attempt.order_no,
                    final_candidate_id=cand.id,
                )

            if attempt.paid and attempt.error_code is not None:
                # Pay succeeded but poll did not confirm ticketed — do not
                # spend again on the next candidate (I7). Case is not recovered.
                break

            # 604 / 616 / verify / cap / order failures: try the next candidate.
            # No new ConfirmationDecision is recorded (I6).

        await self._set_case_status(
            case_id, "failed", resolved_at=datetime.now(UTC)
        )
        await self._write_event(
            case_id=case_id,
            step=_STEP_EXECUTE_FINISHED,
            summary=f"failed after {len(attempts)} attempt(s)",
            payload={
                "succeeded": False,
                "final_order_no": None,
                "final_candidate_id": None,
                "attempt_count": len(attempts),
                "last_error_code": attempts[-1].error_code if attempts else None,
            },
        )
        return ExecutionOutcome(
            succeeded=False,
            attempts=attempts,
            final_order_no=None,
            final_candidate_id=None,
        )

    async def _attempt_one(
        self,
        *,
        case_id: int,
        cand: Candidate,
        passengers: list[Passenger],
        card: CardDetails,
        attempt_index: int,
        intent_ceiling_sgd: Decimal,
        env_cap_sgd: Decimal,
    ) -> ExecutionAttempt:
        started = datetime.now(UTC)
        cid = cand.id if cand.id is not None else -1
        offer_prefix = cand.offer_id[:32]
        await self._write_event(
            case_id=case_id,
            step=_STEP_ATTEMPT_STARTED,
            summary=f"attempt={attempt_index} offer={offer_prefix}",
            payload={
                "attempt": attempt_index,
                "candidate_id": cid,
                "offer_id_prefix": offer_prefix,
                "routing_identifier_prefix": (cand.routing_identifier or "")[:24],
                "routing_identifier_suffix": (cand.routing_identifier or "")[-16:],
            },
        )

        verified_result = await self._reverify_fresh(cand, attempt_index=attempt_index)
        if verified_result is None:
            finished = datetime.now(UTC)
            attempt = ExecutionAttempt(
                candidate_id=cid,
                offer_id=cand.offer_id,
                verified=False,
                order_no=None,
                paid=False,
                error_code=cand.rejected_reason or "verify_failed",
                started_at=started,
                finished_at=finished,
            )
            await self._persist_verify_and_cap([cand])
            await self._finish_attempt_event(case_id, attempt, attempt_index)
            return attempt

        # I3: originally confirmed cap (min(intent, env)) binds every retry,
        # applied to THIS candidate's freshly verified price — never a cached
        # price from a prior attempt.
        amount_sgd = _to_sgd(
            Decimal(str(verified_result.price)), verified_result.currency
        )
        await self._apply_cap(
            cand,
            intent_ceiling_sgd=intent_ceiling_sgd,
            env_cap_sgd=env_cap_sgd,
        )
        await self._persist_verify_and_cap([cand])
        if cand.rejected_reason == "over_cap":
            finished = datetime.now(UTC)
            attempt = ExecutionAttempt(
                candidate_id=cid,
                offer_id=cand.offer_id,
                verified=True,
                order_no=None,
                paid=False,
                error_code="over_cap",
                started_at=started,
                finished_at=finished,
            )
            await self._write_event(
                case_id=case_id,
                step=_STEP_ATTEMPT_FINISHED,
                summary=f"attempt={attempt_index} over_cap amount_sgd={amount_sgd}",
                payload={
                    "attempt": attempt_index,
                    "candidate_id": cid,
                    "offer_id_prefix": offer_prefix,
                    "verified": True,
                    "verified_price": str(verified_result.price),
                    "currency": verified_result.currency,
                    "amount_sgd": str(amount_sgd),
                    "intent_ceiling_sgd": str(intent_ceiling_sgd),
                    "env_cap_sgd": str(env_cap_sgd),
                    "paid": False,
                    "error_code": "over_cap",
                },
            )
            return attempt

        try:
            ordered = await self._atlas.order(
                session_id=verified_result.session_id,
                offer_id=verified_result.offer_id,
                passengers=passengers,
                contact_email=_ORDER_CONTACT_EMAIL,
                contact_phone=_ORDER_CONTACT_PHONE,
            )
        except AtlasDuplicateBookingError as exc:
            # 318 — Atlas correctly refuses a duplicate passenger+flight.
            # Record the typed error and advance to the next candidate.
            finished = datetime.now(UTC)
            attempt = ExecutionAttempt(
                candidate_id=cid,
                offer_id=cand.offer_id,
                verified=True,
                order_no=None,
                paid=False,
                error_code=str(exc.code),
                started_at=started,
                finished_at=finished,
            )
            cand.rejected_reason = "duplicate_booking"
            await self._write_event(
                case_id=case_id,
                step=_STEP_PAY_FAILED,
                summary=(
                    f"attempt={attempt_index} duplicate_booking"
                    f" duplicates={exc.duplicate_orders}"
                ),
                payload={
                    "attempt": attempt_index,
                    "candidate_id": cid,
                    "offer_id_prefix": offer_prefix,
                    "error_code": str(exc.code)[:64],
                    "error_type": type(exc).__name__,
                    "stage": "order",
                    "duplicate_orders": exc.duplicate_orders,
                    "rejected_reason": "duplicate_booking",
                },
            )
            await self._finish_attempt_event(case_id, attempt, attempt_index)
            return attempt
        except AtlasError as exc:
            finished = datetime.now(UTC)
            attempt = ExecutionAttempt(
                candidate_id=cid,
                offer_id=cand.offer_id,
                verified=True,
                order_no=None,
                paid=False,
                error_code=str(exc.code),
                started_at=started,
                finished_at=finished,
            )
            await self._write_event(
                case_id=case_id,
                step=_STEP_PAY_FAILED,
                summary=(
                    f"attempt={attempt_index} order_failed code={exc.code}"
                ),
                payload={
                    "attempt": attempt_index,
                    "candidate_id": cid,
                    "offer_id_prefix": offer_prefix,
                    "error_code": str(exc.code)[:64],
                    "error_type": type(exc).__name__,
                    "stage": "order",
                },
            )
            await self._finish_attempt_event(case_id, attempt, attempt_index)
            return attempt

        order_no = ordered.order_no
        audit_no = _audit_ref(order_no)
        await self._write_event(
            case_id=case_id,
            step=_STEP_ORDERED,
            summary=f"attempt={attempt_index} order_no={audit_no}",
            payload={
                "attempt": attempt_index,
                "candidate_id": cid,
                "offer_id_prefix": offer_prefix,
                "order_no": audit_no,
                "order_status": ordered.status,
                "verified_price": str(verified_result.price),
                "currency": verified_result.currency,
            },
        )

        try:
            # Card is passed only to AtlasClient.pay (Zone A). It must never
            # appear in this event payload, a log line, or a cassette (I4).
            paid = await self._atlas.pay(order_no=order_no, card=card)
        except _FAILOVER_ERROR_TYPES as exc:
            finished = datetime.now(UTC)
            attempt = ExecutionAttempt(
                candidate_id=cid,
                offer_id=cand.offer_id,
                verified=True,
                order_no=order_no,
                paid=False,
                error_code=str(exc.code),
                started_at=started,
                finished_at=finished,
            )
            await self._write_event(
                case_id=case_id,
                step=_STEP_PAY_FAILED,
                summary=(
                    f"attempt={attempt_index} pay_failed code={exc.code} "
                    f"order_no={audit_no}"
                ),
                payload={
                    "attempt": attempt_index,
                    "candidate_id": cid,
                    "offer_id_prefix": offer_prefix,
                    "order_no": audit_no,
                    "error_code": str(exc.code)[:64],
                    "error_type": type(exc).__name__,
                    "paid": False,
                    "stage": "pay",
                    # Orphan: order.do already succeeded. No cancel in this task.
                    "orphaned_order": True,
                },
            )
            await self._finish_attempt_event(case_id, attempt, attempt_index)
            return attempt
        except AtlasError as exc:
            finished = datetime.now(UTC)
            attempt = ExecutionAttempt(
                candidate_id=cid,
                offer_id=cand.offer_id,
                verified=True,
                order_no=order_no,
                paid=False,
                error_code=str(exc.code),
                started_at=started,
                finished_at=finished,
            )
            await self._write_event(
                case_id=case_id,
                step=_STEP_PAY_FAILED,
                summary=(
                    f"attempt={attempt_index} pay_failed code={exc.code} "
                    f"order_no={audit_no}"
                ),
                payload={
                    "attempt": attempt_index,
                    "candidate_id": cid,
                    "offer_id_prefix": offer_prefix,
                    "order_no": audit_no,
                    "error_code": str(exc.code)[:64],
                    "error_type": type(exc).__name__,
                    "paid": False,
                    "stage": "pay",
                    "orphaned_order": True,
                },
            )
            await self._finish_attempt_event(case_id, attempt, attempt_index)
            return attempt

        if not paid.paid:
            finished = datetime.now(UTC)
            code = paid.error_code or "pay_unsuccessful"
            attempt = ExecutionAttempt(
                candidate_id=cid,
                offer_id=cand.offer_id,
                verified=True,
                order_no=order_no,
                paid=False,
                error_code=str(code),
                started_at=started,
                finished_at=finished,
            )
            await self._write_event(
                case_id=case_id,
                step=_STEP_PAY_FAILED,
                summary=f"attempt={attempt_index} pay unpaid code={code}",
                payload={
                    "attempt": attempt_index,
                    "candidate_id": cid,
                    "offer_id_prefix": offer_prefix,
                    "order_no": audit_no,
                    "error_code": str(code)[:64],
                    "paid": False,
                    "stage": "pay",
                    "orphaned_order": True,
                },
            )
            await self._finish_attempt_event(case_id, attempt, attempt_index)
            return attempt

        await self._write_event(
            case_id=case_id,
            step=_STEP_PAID,
            summary=f"attempt={attempt_index} pay.do returned paid order_no={audit_no}",
            payload={
                "attempt": attempt_index,
                "candidate_id": cid,
                "offer_id_prefix": offer_prefix,
                "order_no": audit_no,
                "pay_reported_paid": True,
                "ticket_count": len(paid.ticket_numbers),
                # I7: not yet recovered — poll_order_until is authoritative.
                "authoritative": False,
            },
        )

        poll_status: str | None = None
        poll_error: str | None = None
        try:
            details = await self._atlas.poll_order_until(
                order_no=order_no,
                terminal_statuses=set(_TICKETED_STATUSES),
                timeout_seconds=120,
                interval_seconds=3.0,
            )
            poll_status = details.status
        except AtlasTimeoutError as exc:
            poll_error = str(exc.code)
            poll_status = None
        except AtlasError as exc:
            poll_error = str(exc.code)
            poll_status = None

        ticketed = poll_status in _TICKETED_STATUSES
        await self._write_event(
            case_id=case_id,
            step=_STEP_POLL,
            summary=(
                f"attempt={attempt_index} poll status={poll_status!r} "
                f"ticketed={ticketed} error={poll_error!r}"
            ),
            payload={
                "attempt": attempt_index,
                "candidate_id": cid,
                "order_no": audit_no,
                "poll_status": poll_status,
                "poll_error_code": poll_error,
                "ticketed": ticketed,
                "authoritative": True,
            },
        )

        finished = datetime.now(UTC)
        if ticketed:
            attempt = ExecutionAttempt(
                candidate_id=cid,
                offer_id=cand.offer_id,
                verified=True,
                order_no=order_no,
                paid=True,
                error_code=None,
                started_at=started,
                finished_at=finished,
            )
        else:
            attempt = ExecutionAttempt(
                candidate_id=cid,
                offer_id=cand.offer_id,
                verified=True,
                order_no=order_no,
                paid=True,
                error_code=poll_error or poll_status or "poll_not_ticketed",
                started_at=started,
                finished_at=finished,
            )
        await self._finish_attempt_event(case_id, attempt, attempt_index)
        return attempt

    async def _reverify_fresh(
        self, cand: Candidate, *, attempt_index: int
    ) -> VerifyResult | None:
        """Independent verify.do for this attempt (I2). Uses the fresh price."""
        rid = _routing_identifier(cand)
        if not rid:
            cand.verified = False
            cand.verified_price = None
            cand.rejected_reason = "verify_failed"
            await self._write_event(
                case_id=cand.case_id,
                step=_STEP_VERIFY,
                summary=(
                    f"attempt={attempt_index} verify_failed missing "
                    f"routing_identifier offer={cand.offer_id[:24]}"
                ),
                payload={
                    "attempt": attempt_index,
                    "offer_id_prefix": cand.offer_id[:32],
                    "verified": False,
                    "rejected_reason": "verify_failed",
                    "fresh": True,
                },
            )
            return None

        try:
            result = await self._atlas.verify(routing_identifier=rid)
        except AtlasPriceMovedError as exc:
            cand.verified = False
            cand.verified_price = None
            cand.rejected_reason = "price_moved"
            await self._write_event(
                case_id=cand.case_id,
                step=_STEP_VERIFY,
                summary=f"attempt={attempt_index} price_moved offer={cand.offer_id[:24]}",
                payload={
                    "attempt": attempt_index,
                    "offer_id_prefix": cand.offer_id[:32],
                    "routing_identifier_prefix": rid[:32],
                    "verified": False,
                    "rejected_reason": "price_moved",
                    "old_price": str(exc.old_price),
                    "new_price": str(exc.new_price),
                    "fresh": True,
                },
            )
            return None
        except AtlasError as exc:
            cand.verified = False
            cand.verified_price = None
            cand.rejected_reason = "verify_failed"
            await self._write_event(
                case_id=cand.case_id,
                step=_STEP_VERIFY,
                summary=(
                    f"attempt={attempt_index} verify_failed "
                    f"offer={cand.offer_id[:24]} code={exc.code}"
                ),
                payload={
                    "attempt": attempt_index,
                    "offer_id_prefix": cand.offer_id[:32],
                    "routing_identifier_prefix": rid[:32],
                    "verified": False,
                    "rejected_reason": "verify_failed",
                    "error_code": str(exc.code)[:64],
                    "fresh": True,
                },
            )
            return None
        except Exception as exc:
            cand.verified = False
            cand.verified_price = None
            cand.rejected_reason = "verify_failed"
            await self._write_event(
                case_id=cand.case_id,
                step=_STEP_VERIFY,
                summary=(
                    f"attempt={attempt_index} verify_failed "
                    f"offer={cand.offer_id[:24]}"
                ),
                payload={
                    "attempt": attempt_index,
                    "offer_id_prefix": cand.offer_id[:32],
                    "routing_identifier_prefix": rid[:32],
                    "verified": False,
                    "rejected_reason": "verify_failed",
                    "error_type": type(exc).__name__,
                    "fresh": True,
                },
            )
            return None

        if not result.verified:
            cand.verified = False
            cand.verified_price = None
            cand.rejected_reason = "verify_failed"
            await self._write_event(
                case_id=cand.case_id,
                step=_STEP_VERIFY,
                summary=(
                    f"attempt={attempt_index} verified=False "
                    f"offer={cand.offer_id[:24]}"
                ),
                payload={
                    "attempt": attempt_index,
                    "offer_id_prefix": cand.offer_id[:32],
                    "routing_identifier_prefix": rid[:32],
                    "verified": False,
                    "rejected_reason": "verify_failed",
                    "fresh": True,
                },
            )
            return None

        # verify.do succeeded: the fresh price is authoritative for I3 even
        # when it differs from search (price_changed). Cap runs on this price.
        cand.verified = True
        cand.verified_price = Decimal(str(result.price))
        if result.currency:
            cand.currency = result.currency
        cand.rejected_reason = None
        await self._write_event(
            case_id=cand.case_id,
            step=_STEP_VERIFY,
            summary=(
                f"attempt={attempt_index} verified=True "
                f"offer={cand.offer_id[:24]} price={result.price} "
                f"{result.currency}"
            ),
            payload={
                "attempt": attempt_index,
                "offer_id_prefix": cand.offer_id[:32],
                "routing_identifier_prefix": rid[:24],
                "routing_identifier_suffix": rid[-16:],
                "verified": True,
                "verified_price": str(result.price),
                "currency": result.currency,
                "price_changed": result.price_changed,
                "fresh": True,
            },
        )
        return result

    async def _finish_attempt_event(
        self, case_id: int, attempt: ExecutionAttempt, attempt_index: int
    ) -> None:
        await self._write_event(
            case_id=case_id,
            step=_STEP_ATTEMPT_FINISHED,
            summary=(
                f"attempt={attempt_index} paid={attempt.paid} "
                f"error_code={attempt.error_code!r} "
                f"order_no={_audit_ref(attempt.order_no)!r}"
            ),
            payload={
                "attempt": attempt_index,
                "candidate_id": attempt.candidate_id,
                "offer_id_prefix": attempt.offer_id[:32],
                "verified": attempt.verified,
                "order_no": _audit_ref(attempt.order_no),
                "paid": attempt.paid,
                "error_code": attempt.error_code,
            },
        )

    def _load_intent(self, case_id: int) -> RecoveryIntent:
        with self._session_factory() as session:
            intent = session.exec(
                select(RecoveryIntent).where(RecoveryIntent.case_id == case_id)
            ).first()
            if intent is None:
                raise ValueError(f"RecoveryIntent for case_id={case_id} not found")
            return RecoveryIntent.model_validate(intent.model_dump())

    async def _set_case_status(
        self,
        case_id: int,
        status: str,
        *,
        resolved_at: datetime | None,
    ) -> None:
        with self._session_factory() as session:
            case = session.get(RecoveryCase, case_id)
            if case is None:
                raise ValueError(f"RecoveryCase id={case_id} not found")
            case.status = status
            if resolved_at is not None:
                case.resolved_at = resolved_at
            await write_event(
                session,
                AgentEventIn(
                    case_id=case_id,
                    actor=Actor.GUARDIAN,
                    step="sse.status",
                    summary=f"case status is {status}",
                    payload={"status": status},
                ),
            )
            session.commit()

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
