"""Recovery-case JSON, confirmation, orchestration, and SSE routes."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlmodel import col, select

from apps.api.routes_webhook import database_path
from apps.api.settings import ReboundMode, get_settings
from apps.api.sse import (
    CandidatesEvent,
    CaseStatusEvent,
    ConfirmationEvent,
    ReceiptEvent,
    SandboxGridEvent,
    TraceEvent,
    encode_sse,
    publisher,
)
from packages.agents.executor_agent import ExecutorAgent
from packages.agents.interpreter import Interpreter, InterpreterInput
from packages.agents.strategist import Strategist
from packages.atlas.cassette import CassettePlayer
from packages.atlas.client import AtlasClient
from packages.atlas.models import CardDetails, Passenger, Segment
from packages.atlas.transport import LiveTransport, ReplayTransport
from packages.domain.db import session_factory
from packages.domain.enums import Actor
from packages.domain.models import (
    AgentEvent,
    Candidate,
    Order,
    RecoveryCase,
    RecoveryIntent,
    RecoveryReceipt,
)
from packages.executors import get_executor
from packages.executors.base import SandboxStatus
from packages.guardian.audit import AgentEventIn, read_events, write_event
from packages.guardian.policy import (
    ConfirmationDecision,
    ConfirmationError,
    ConfirmationGate,
    ConfirmationRequest,
)
from packages.router import get_router

case_router = APIRouter(prefix="/cases", tags=["cases"])
operator_case_router = APIRouter(prefix="/cases", tags=["operator"])

_ROOT = Path(__file__).resolve().parents[2]
_SSE_SANDBOXES = "sse.sandboxes"
_SSE_CANDIDATES = "sse.candidates"
_SSE_CONFIRMATION = "sse.confirmation"
_SSE_RECEIPT = "sse.receipt"
_SSE_STATUS = "sse.status"
_HEARTBEAT_SECONDS = 15.0
_AUDIT_POLL_SECONDS = 0.25
_running_case_ids: set[int] = set()
_SAFE_SCORING_CODE = """
def score(payload):
    budget = float(payload["budget_ceiling_sgd"]) or 1.0
    weight = float(payload["mobility_penalty_weight"])
    out = []
    for candidate in payload["candidates"]:
        price = float(candidate["price"])
        stops = int(candidate["stop_count"])
        transfer = candidate.get("min_transfer_minutes")
        transfer_minutes = int(transfer) if transfer is not None else 90
        price_fit = max(0.0, 1.0 - price / budget)
        risk = 0.0 if stops == 0 else min(1.0, 40.0 / max(transfer_minutes, 1))
        mobility_fit = max(0.0, 1.0 - weight * risk)
        stop_penalty = 0.15 * stops
        total = 0.55 * price_fit + 0.25 * mobility_fit + 0.20 * (1.0 - risk) - stop_penalty
        out.append({
            "offer_id": candidate["offer_id"],
            "score": total,
            "components": {
                "price_fit": price_fit,
                "mobility_fit": mobility_fit,
                "stop_penalty": stop_penalty,
            },
            "self_transfer_risk": risk,
            "mobility_fit": mobility_fit,
        })
    return out
"""


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: int = Field(gt=0)
    nonce: str = Field(min_length=1)


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=4000)


@lru_cache
def get_confirmation_gate() -> ConfirmationGate:
    return ConfirmationGate()


@lru_cache
def get_atlas_client() -> AtlasClient:
    settings = get_settings()
    if settings.rebound_mode is ReboundMode.REPLAY:
        return AtlasClient(
            ReplayTransport(CassettePlayer(_ROOT / "fixtures" / "cassettes"))
        )
    return AtlasClient(
        LiveTransport(
            settings.atlas_base_url,
            settings.atlas_client_id,
            settings.atlas_client_secret,
        )
    )


def _factory():  # noqa: ANN202
    return session_factory(database_path())


def _case_by_ref(case_ref: str) -> RecoveryCase:
    with _factory()() as session:
        case = session.exec(
            select(RecoveryCase).where(RecoveryCase.case_ref == case_ref)
        ).first()
        if case is None:
            raise HTTPException(status_code=404, detail=f"case {case_ref!r} not found")
        return RecoveryCase.model_validate(case.model_dump())


@case_router.get("/{case_ref}")
async def get_case(case_ref: str) -> dict[str, Any]:
    case = _case_by_ref(case_ref)
    assert case.id is not None
    with _factory()() as session:
        intent = session.exec(
            select(RecoveryIntent)
            .where(RecoveryIntent.case_id == case.id)
            .order_by(col(RecoveryIntent.id).desc())
        ).first()
        candidates = list(
            session.exec(
                select(Candidate)
                .where(Candidate.case_id == case.id)
                .order_by(col(Candidate.id).asc())
            ).all()
        )
        receipt = session.exec(
            select(RecoveryReceipt)
            .where(RecoveryReceipt.case_id == case.id)
            .order_by(col(RecoveryReceipt.id).desc())
        ).first()

        return {
            "case": case.model_dump(mode="json"),
            "intent": _intent_json(intent),
            "candidates": [_candidate_json(candidate) for candidate in candidates],
            "receipt": _receipt_json(receipt),
        }


@case_router.get("/{case_ref}/stream")
async def case_stream(
    case_ref: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    case = _case_by_ref(case_ref)
    assert case.id is not None
    try:
        cursor = int(last_event_id or "0")
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Last-Event-ID must be an integer"
        ) from exc
    if cursor < 0:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be >= 0")

    async def stream() -> Any:
        nonlocal cursor
        loop = asyncio.get_running_loop()
        heartbeat_at = loop.time() + _HEARTBEAT_SECONDS
        async with publisher.subscribe(case_ref) as wakeups:
            while True:
                with _factory()() as session:
                    rows = await read_events(
                        session, case_id=case.id, after_id=cursor
                    )
                for row in rows:
                    if row.id is None:
                        continue
                    cursor = row.id
                    event_name, event = _to_sse_event(row, case_ref)
                    yield encode_sse(event_name, event, event_id=row.id)

                if await request.is_disconnected():
                    return

                now = loop.time()
                if now >= heartbeat_at:
                    yield encode_sse("heartbeat", {}, event_id=None)
                    heartbeat_at = now + _HEARTBEAT_SECONDS
                    continue

                timeout = min(_AUDIT_POLL_SECONDS, heartbeat_at - now)
                try:
                    await asyncio.wait_for(wakeups.get(), timeout=timeout)
                except TimeoutError:
                    pass

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@operator_case_router.post("/{case_ref}/run")
async def run_case(
    case_ref: str,
    body: RunBody | None = None,
    gate: ConfirmationGate = Depends(get_confirmation_gate),
    atlas: AtlasClient = Depends(get_atlas_client),
) -> dict[str, Any]:
    case, order = _case_and_order(case_ref)
    assert case.id is not None
    with _factory()() as session:
        prior_intent = session.exec(
            select(RecoveryIntent).where(RecoveryIntent.case_id == case.id)
        ).first()
        prior_candidate = session.exec(
            select(Candidate).where(Candidate.case_id == case.id)
        ).first()
        prior_confirmation = session.exec(
            select(AgentEvent)
            .where(AgentEvent.case_id == case.id)
            .where(AgentEvent.step == "confirmation.resolved")
        ).first()
    if prior_confirmation is not None:
        raise HTTPException(status_code=409, detail="case has already been confirmed")
    if (
        case.status != "failed"
        and (prior_intent is not None or prior_candidate is not None)
    ):
        raise HTTPException(status_code=409, detail="case has already been run")
    if case.id in _running_case_ids:
        raise HTTPException(status_code=409, detail="case run is already in progress")
    _running_case_ids.add(case.id)

    executor: Any = None
    try:
        original = _segments(order.itinerary_json)
        text = (body.text if body is not None else None) or _default_run_text(original)
        await _set_case_status(case, "interpreting")

        router = get_router()
        interpreter = Interpreter(router, _factory())
        intent = await interpreter.interpret(
            InterpreterInput(
                case_id=case.id,
                text=text,
                original_itinerary=original,
            )
        )
        await publisher.publish(case_ref)
        if intent.confidence < 0.6 or intent.id is None:
            await _set_case_status(case, "needs_clarification")
            raise HTTPException(
                status_code=409,
                detail=await interpreter.clarification_question(intent),
            )
        intent = await _complete_intent_from_trusted_context(case, intent, original)

        await _set_case_status(case, "planning")
        strategist = Strategist(router, atlas, _factory())
        plans = await strategist.plan(intent, original)
        await _append_event(
            case,
            actor=Actor.STRATEGIST,
            step="strategist.plan_completed",
            summary=f"planned {len(plans)} search strategies",
            payload={
                "strategy_count": len(plans),
                "strategies": [plan.strategy.value for plan in plans],
            },
        )

        candidates = await strategist.fan_out(plans)
        await publisher.publish(case_ref)
        await _append_candidates_snapshot(case, candidates)

        scoring_code = await strategist.write_scoring_code(intent)
        settings = get_settings()
        executor = get_executor(settings=settings, timeout_seconds=30)
        agent = ExecutorAgent(atlas, executor, gate, _factory())
        slot_count = int(settings.daytona_target_sandboxes)
        slots = {
            slot: SandboxStatus(
                slot=slot,
                state="pending",
                sandbox_id=None,
                elapsed_ms=0,
            )
            for slot in range(slot_count)
        }
        slot_lock = asyncio.Lock()
        await _append_sandbox_snapshot(case, list(slots.values()))

        async def on_status(sandbox: SandboxStatus) -> None:
            async with slot_lock:
                slots[sandbox.slot] = sandbox.model_copy(deep=True)
                await _append_sandbox_snapshot(
                    case, [slots[index] for index in sorted(slots)]
                )

        ranked = await agent.score_and_verify(
            case_id=case.id,
            candidates=candidates,
            intent=intent,
            scoring_code=scoring_code,
            on_status=on_status,
        )
        if not any(candidate.score is not None for candidate in ranked):
            await _append_event(
                case,
                actor=Actor.EXECUTOR,
                step="executor.scoring_code_fallback",
                summary="generated scorer returned no scores; using deterministic fallback",
                payload={"reason": "no_scored_candidates"},
            )
            ranked = await agent.score_and_verify(
                case_id=case.id,
                candidates=candidates,
                intent=intent,
                scoring_code=_SAFE_SCORING_CODE,
                on_status=on_status,
            )
        await publisher.publish(case_ref)
        await _append_candidates_snapshot(case, ranked)

        eligible = [
            candidate
            for candidate in ranked
            if candidate.id is not None
            and candidate.verified
            and candidate.rejected_reason is None
        ][:3]
        if not eligible:
            await _set_case_status(case, "failed")
            raise HTTPException(
                status_code=409, detail="no verified candidate is eligible"
            )

        candidate_ids = [candidate.id for candidate in eligible]
        first_id = candidate_ids[0]
        assert first_id is not None
        request = ConfirmationRequest(
            case_id=case.id,
            candidate_ids=[candidate_id for candidate_id in candidate_ids if candidate_id],
            recommended_candidate_id=first_id,
            effective_cap_sgd=min(
                Decimal(str(intent.budget_ceiling_sgd)),
                Decimal(str(settings.guardian_max_spend_sgd)),
            ),
            expires_at=datetime.now(UTC) + timedelta(minutes=4),
            nonce="",
        )
        gate.open(request)
        await _append_confirmation_snapshot(case, request)
        await _set_case_status(case, "awaiting_confirmation")
        return {"status": "awaiting_confirmation", "confirmation": request}
    except HTTPException:
        raise
    except Exception as exc:
        await _append_event(
            case,
            actor=Actor.GUARDIAN,
            step="orchestrator.run_failed",
            summary="case run failed",
            payload={"error_type": type(exc).__name__},
        )
        await _set_case_status(case, "failed")
        raise HTTPException(status_code=502, detail="case run failed") from exc
    finally:
        _running_case_ids.discard(case.id)
        if executor is not None:
            await executor.close()


@case_router.post("/{case_ref}/confirm", status_code=status.HTTP_202_ACCEPTED)
async def confirm_case(
    case_ref: str,
    request: Request,
    background_tasks: BackgroundTasks,
    gate: ConfirmationGate = Depends(get_confirmation_gate),
    atlas: AtlasClient = Depends(get_atlas_client),
) -> dict[str, Any]:
    try:
        body = ConfirmBody.model_validate_json(await request.body())
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False)
        ) from exc
    case = _case_by_ref(case_ref)
    assert case.id is not None
    with _factory()() as session:
        candidate = session.get(Candidate, body.candidate_id)
        if candidate is None or candidate.case_id != case.id:
            raise HTTPException(
                status_code=422, detail="candidate does not belong to this case"
            )

    decision = ConfirmationDecision(
        case_id=case.id,
        candidate_id=body.candidate_id,
        nonce=body.nonce,
        decided_by="operator",
        decided_at=datetime.now(UTC),
    )
    try:
        gate.resolve(decision)
    except ConfirmationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await _append_event(
        case,
        actor=Actor.HUMAN,
        step="confirmation.resolved",
        summary=f"operator confirmed candidate_id={body.candidate_id}",
        payload={
            "candidate_id": body.candidate_id,
            "decided_by": decision.decided_by,
            "decided_at_epoch": decision.decided_at.timestamp(),
            "human_taps": 1,
        },
    )
    background_tasks.add_task(
        _execute_confirmed,
        case,
        selected_candidate_id=body.candidate_id,
        gate=gate,
        atlas=atlas,
    )
    return {
        "status": "accepted",
        "case_ref": case_ref,
        "candidate_id": body.candidate_id,
    }


async def _execute_confirmed(
    case: RecoveryCase,
    *,
    selected_candidate_id: int,
    gate: ConfirmationGate,
    atlas: AtlasClient,
) -> None:
    assert case.id is not None
    try:
        with _factory()() as session:
            order = session.get(Order, case.order_id)
            rows = list(
                session.exec(
                    select(Candidate).where(Candidate.case_id == case.id)
                ).all()
            )
            candidates = [
                Candidate.model_validate(candidate.model_dump()) for candidate in rows
            ]
        if order is None:
            raise ValueError("case order not found")
        ordered = sorted(
            (
                candidate
                for candidate in candidates
                if candidate.id == selected_candidate_id
                or (candidate.verified and candidate.rejected_reason is None)
            ),
            key=lambda candidate: (
                candidate.id != selected_candidate_id,
                -(candidate.score if candidate.score is not None else float("-inf")),
                candidate.offer_id,
            ),
        )
        agent = ExecutorAgent(atlas, None, gate, _factory())
        await agent.execute(
            case_id=case.id,
            ordered_candidates=ordered,
            passengers=_passengers(order.passengers_json),
            card=_sandbox_card(),
            max_attempts=3,
        )
        await publisher.publish(case.case_ref)
    except Exception as exc:
        await _append_event(
            case,
            actor=Actor.EXECUTOR,
            step="executor.background_failed",
            summary="background execution failed",
            payload={"error_type": type(exc).__name__},
        )
        await _set_case_status(case, "failed")


async def _append_event(
    case: RecoveryCase,
    *,
    actor: Actor,
    step: str,
    summary: str,
    payload: dict[str, Any],
) -> int:
    assert case.id is not None
    with _factory()() as session:
        event_id = await write_event(
            session,
            AgentEventIn(
                case_id=case.id,
                actor=actor,
                step=step,
                summary=summary,
                payload=payload,
            ),
        )
        session.commit()
    await publisher.publish(case.case_ref)
    return event_id


async def _set_case_status(case: RecoveryCase, value: str) -> None:
    assert case.id is not None
    with _factory()() as session:
        row = session.get(RecoveryCase, case.id)
        if row is None:
            raise ValueError(f"RecoveryCase id={case.id} not found")
        row.status = value
        session.add(row)
        await write_event(
            session,
            AgentEventIn(
                case_id=case.id,
                actor=Actor.GUARDIAN,
                step=_SSE_STATUS,
                summary=f"case status is {value}",
                payload={"status": value},
            ),
        )
        session.commit()
    case.status = value
    await publisher.publish(case.case_ref)


async def _append_sandbox_snapshot(
    case: RecoveryCase, slots: list[SandboxStatus]
) -> None:
    await _append_event(
        case,
        actor=Actor.EXECUTOR,
        step=_SSE_SANDBOXES,
        summary=f"sandbox grid: {len(slots)} slots",
        payload={"slots": [slot.model_dump(mode="json") for slot in slots]},
    )


async def _append_candidates_snapshot(
    case: RecoveryCase, candidates: list[Candidate]
) -> None:
    snapshot = [_candidate_event_json(candidate) for candidate in candidates]
    recommended = next(
        (
            candidate.offer_id
            for candidate in candidates
            if candidate.verified and candidate.rejected_reason is None
        ),
        None,
    )
    await _append_event(
        case,
        actor=Actor.EXECUTOR,
        step=_SSE_CANDIDATES,
        summary=f"candidate snapshot: {len(snapshot)} options",
        payload={
            "candidates": snapshot,
            "recommended_offer_id": recommended,
        },
    )


async def _append_confirmation_snapshot(
    case: RecoveryCase, request: ConfirmationRequest
) -> None:
    await _append_event(
        case,
        actor=Actor.GUARDIAN,
        step=_SSE_CONFIRMATION,
        summary=f"confirmation requested for candidate_id={request.recommended_candidate_id}",
        payload={
            "request": {
                "case_id": request.case_id,
                "candidate_ids": request.candidate_ids,
                "recommended_candidate_id": request.recommended_candidate_id,
                "effective_cap_sgd": str(request.effective_cap_sgd),
                "expires_at_epoch": request.expires_at.timestamp(),
                "nonce": request.nonce,
            }
        },
    )


def _to_sse_event(row: AgentEvent, case_ref: str) -> tuple[str, BaseModel]:
    payload = _json_object(row.payload_json)
    event_id = int(row.id or 0)
    if row.step == _SSE_SANDBOXES:
        slots = [
            SandboxStatus.model_validate(slot)
            for slot in payload.get("slots", [])
            if isinstance(slot, dict)
        ]
        return "sandboxes", SandboxGridEvent(
            id=event_id, case_ref=case_ref, slots=slots
        )
    if row.step == _SSE_CANDIDATES:
        candidates = payload.get("candidates", [])
        return "candidates", CandidatesEvent(
            id=event_id,
            case_ref=case_ref,
            candidates=candidates if isinstance(candidates, list) else [],
            recommended_offer_id=payload.get("recommended_offer_id"),
        )
    if row.step == _SSE_CONFIRMATION:
        raw_request = payload.get("request", {})
        if not isinstance(raw_request, dict):
            raw_request = {}
        request = ConfirmationRequest(
            case_id=int(raw_request.get("case_id", 0)),
            candidate_ids=list(raw_request.get("candidate_ids", [])),
            recommended_candidate_id=int(
                raw_request.get("recommended_candidate_id", 0)
            ),
            effective_cap_sgd=Decimal(
                str(raw_request.get("effective_cap_sgd", "0"))
            ),
            expires_at=datetime.fromtimestamp(
                float(raw_request.get("expires_at_epoch", 0)), tz=UTC
            ),
            nonce=str(raw_request.get("nonce", "")),
        )
        return "confirmation", ConfirmationEvent(
            id=event_id, case_ref=case_ref, request=request
        )
    if row.step == _SSE_RECEIPT:
        receipt = payload.get("receipt", {})
        return "receipt", ReceiptEvent(
            id=event_id,
            case_ref=case_ref,
            receipt=receipt if isinstance(receipt, dict) else {},
        )
    if row.step == _SSE_STATUS:
        return "status", CaseStatusEvent(
            id=event_id,
            case_ref=case_ref,
            status=str(payload.get("status", "")),
        )
    return "trace", TraceEvent(
        id=event_id,
        case_ref=case_ref,
        actor=Actor(row.actor),
        step=row.step,
        summary=row.summary,
        elapsed_ms=row.elapsed_ms,
        status=_trace_status(row.step, payload),
        data=payload,
    )


def _trace_status(step: str, payload: dict[str, Any]) -> str:
    explicit = payload.get("status")
    if explicit in {"started", "ok", "failed"}:
        return str(explicit)
    lowered = step.lower()
    if any(part in lowered for part in ("failed", "rejected", "error")):
        return "failed"
    if any(part in lowered for part in ("started", "dispatched")):
        return "started"
    return "ok"


def _case_and_order(case_ref: str) -> tuple[RecoveryCase, Order]:
    case = _case_by_ref(case_ref)
    with _factory()() as session:
        order = session.get(Order, case.order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="case order not found")
        return case, Order.model_validate(order.model_dump())


def _segments(raw: str) -> list[Segment]:
    try:
        values = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(values, list):
        return []
    segments: list[Segment] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        try:
            segments.append(Segment.model_validate(value))
        except Exception:
            continue
    return segments


def _default_run_text(original: list[Segment]) -> str:
    cap = get_settings().guardian_max_spend_sgd
    if original:
        origin = original[0].origin
        destination = original[-1].destination
        deadline = original[-1].arrival_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        deadline += timedelta(hours=12)
        return (
            f"Find a replacement from {origin} to {destination} as soon as possible. "
            f"I must arrive by {deadline.astimezone(UTC).strftime('%d %b %Y %H:%M UTC')}. "
            f"My budget ceiling is S${cap}. Prioritize a simple journey."
        )
    return (
        "Find a replacement journey for this disruption as soon as possible. "
        f"My budget ceiling is S${cap}."
    )


async def _complete_intent_from_trusted_context(
    case: RecoveryCase,
    intent: RecoveryIntent,
    original: list[Segment],
) -> RecoveryIntent:
    """Fill missing planning constraints from Atlas facts and Guardian config.

    These values never come from model-authored itinerary data: airports and
    timing come from the original Atlas order, while the fallback ceiling comes
    from Guardian configuration.
    """

    origins = list(intent.origin_candidates_list)
    destinations = list(intent.destination_candidates_list)
    fallbacks: list[str] = []
    if original and not origins:
        origins = [original[0].origin]
        fallbacks.append("origin_from_original_order")
    if original and not destinations:
        destinations = [original[-1].destination]
        fallbacks.append("destination_from_original_order")

    budget = Decimal(str(intent.budget_ceiling_sgd))
    if budget <= 0:
        budget = Decimal(str(get_settings().guardian_max_spend_sgd))
        fallbacks.append("budget_from_guardian_config")

    must_arrive_by = intent.must_arrive_by
    if must_arrive_by is None and original:
        must_arrive_by = original[-1].arrival_at + timedelta(hours=12)
        fallbacks.append("deadline_from_original_order")

    if not fallbacks:
        return intent

    assert intent.id is not None
    with _factory()() as session:
        row = session.get(RecoveryIntent, intent.id)
        if row is None:
            raise ValueError(f"RecoveryIntent id={intent.id} not found")
        row.origin_candidates = json.dumps(origins, separators=(",", ":"))
        row.destination_candidates = json.dumps(destinations, separators=(",", ":"))
        row.budget_ceiling_sgd = budget
        row.must_arrive_by = must_arrive_by
        session.add(row)
        session.commit()
        session.refresh(row)
        completed = RecoveryIntent.model_validate(row.model_dump())

    await _append_event(
        case,
        actor=Actor.GUARDIAN,
        step="orchestrator.intent_completed",
        summary="completed missing intent constraints from trusted sources",
        payload={
            "fallbacks": fallbacks,
            "origin_candidates": origins,
            "destination_candidates": destinations,
            "budget_ceiling_sgd": str(budget),
            "must_arrive_by": (
                must_arrive_by.astimezone(UTC).strftime("%d %b %Y %H:%M UTC")
                if must_arrive_by is not None
                else None
            ),
        },
    )
    return completed


def _candidate_json(candidate: Candidate) -> dict[str, Any]:
    data = candidate.model_dump(mode="json")
    try:
        data["segments"] = json.loads(candidate.segments_json or "[]")
    except json.JSONDecodeError:
        data["segments"] = []
    data.pop("segments_json", None)
    data["score_components"] = _json_object(candidate.score_components_json or "{}")
    data.pop("score_components_json", None)
    return data


def _candidate_event_json(candidate: Candidate) -> dict[str, Any]:
    segments = _segments(candidate.segments_json)
    arrival = None
    if segments:
        value = segments[-1].arrival_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        arrival = value.astimezone(UTC).strftime("%d %b %Y %H:%M UTC")
    return {
        "candidate_id": candidate.id,
        "offer_id": candidate.offer_id,
        "strategy": candidate.strategy,
        "price": str(candidate.price),
        "currency": candidate.currency,
        "arrival": arrival,
        "arrival_delay_minutes": candidate.arrival_delay_minutes,
        "stop_count": candidate.stop_count,
        "min_transfer_minutes": candidate.min_transfer_minutes,
        "score": candidate.score,
        "components": _json_object(candidate.score_components_json or "{}"),
        "self_transfer_risk": candidate.self_transfer_risk,
        "mobility_fit": candidate.mobility_fit,
        "verified": candidate.verified,
        "verified_price": (
            str(candidate.verified_price)
            if candidate.verified_price is not None
            else None
        ),
        "rejected_reason": candidate.rejected_reason,
    }


def _intent_json(intent: RecoveryIntent | None) -> dict[str, Any] | None:
    if intent is None:
        return None
    data = intent.model_dump(mode="json")
    data["origin_candidates"] = intent.origin_candidates_list
    data["destination_candidates"] = intent.destination_candidates_list
    data["raw_input_kinds"] = intent.raw_input_kinds_list
    return data


def _receipt_json(receipt: RecoveryReceipt | None) -> dict[str, Any] | None:
    if receipt is None:
        return None
    data = receipt.model_dump(mode="json")
    data["attempts"] = json.loads(receipt.attempts_json or "[]")
    data["event_ids"] = json.loads(receipt.event_ids_json or "[]")
    data.pop("attempts_json", None)
    data.pop("event_ids_json", None)
    return data


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _passengers(raw: str) -> list[Passenger]:
    try:
        values = json.loads(raw or "[]")
    except json.JSONDecodeError:
        values = []
    passengers: list[Passenger] = []
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, dict):
                continue
            name = str(value.get("name") or "Passenger/Test")
            if "/" in name:
                surname, given = name.split("/", 1)
            else:
                parts = name.split(maxsplit=1)
                given = parts[0]
                surname = parts[1] if len(parts) > 1 else "Passenger"
            birthday = value.get("birthday") or value.get("dateOfBirth")
            try:
                born = datetime.fromisoformat(str(birthday).replace("Z", "+00:00"))
            except ValueError:
                born = datetime(1990, 1, 15, tzinfo=UTC)
            passengers.append(
                Passenger(
                    given_name=given.title(),
                    surname=surname.title(),
                    date_of_birth=born,
                    passport_number=value.get("cardNum") or value.get("passport_number"),
                    nationality=value.get("nationality") or "SG",
                )
            )
    return passengers or [
        Passenger(
            given_name="Test",
            surname="Passenger",
            date_of_birth=datetime(1990, 1, 15, tzinfo=UTC),
            passport_number="A12345678",
            nationality="SG",
        )
    ]


def _sandbox_card() -> CardDetails:
    settings = get_settings()
    if (
        settings.rebound_mode is not ReboundMode.REPLAY
        and "sandbox" not in settings.atlas_base_url.lower()
    ):
        raise RuntimeError("automatic payment is restricted to an Atlas sandbox")
    return CardDetails(
        holder_given_name=os.environ.get("ATLAS_CARD_GIVEN_NAME", "Test"),
        holder_surname=os.environ.get("ATLAS_CARD_SURNAME", "User"),
        number=os.environ.get("ATLAS_CARD_NUMBER", "4532015112830366"),
        expiry_month=int(os.environ.get("ATLAS_CARD_EXPIRY_MONTH", "12")),
        expiry_year=int(os.environ.get("ATLAS_CARD_EXPIRY_YEAR", "2030")),
        cvv=os.environ.get("ATLAS_CARD_CVV", "123"),
    )
