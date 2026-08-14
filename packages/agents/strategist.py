"""Strategist agent — search strategies, fan-out, scoring codegen, select (I1).

Models choose among Atlas offer ids and write Zone B scoring code.
They never author itineraries. Generated code is untrusted.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session

from packages.atlas.client import AtlasClient
from packages.atlas.errors import AtlasNoResultsError
from packages.atlas.models import Offer, SearchRequest, Segment
from packages.domain.enums import Actor, SearchStrategy
from packages.domain.models import Candidate, RecoveryIntent
from packages.executors.base import ScoredCandidate
from packages.guardian.audit import AgentEventIn, write_event
from packages.router.base import ModelRequest, ModelRouter

_STRATEGIST_PROMPT = Path(__file__).resolve().parent / "prompts" / "strategist.md"
_SCORING_PROMPT = Path(__file__).resolve().parent / "prompts" / "scoring_codegen.md"

_STEP_DISPATCHED = "strategist.strategy_dispatched"
_STEP_RETURNED = "strategist.search_returned"
_STEP_SCORING_CODE = "strategist.scoring_code_written"
_STEP_SELECTED = "strategist.selected"

# Stdlib modules that remain dangerous in Zone B — Task 17 still allows them
# under "stdlib only"; verification must flag residual sandbox risk.
_DANGEROUS_STDLIB = frozenset(
    {"os", "subprocess", "socket", "sys", "shutil", "ctypes", "multiprocessing"}
)

# Test seam: when set, first write_scoring_code generation uses this bad source
# instead of the model, then clears itself so the retry path can recover.
_FORCE_BAD_SCORING_ONCE: str | None = None


class StrategyPlan(BaseModel):
    strategy: SearchStrategy
    search_request: SearchRequest
    rationale: str


class RankedSelection(BaseModel):
    """A model's ONLY permitted output about itineraries: existing offer IDs."""

    ordered_offer_ids: list[str]
    explanations: dict[str, str]  # offer_id -> one sentence


class _PlanItemDraft(BaseModel):
    strategy: SearchStrategy
    origin: str
    destination: str
    departure_date: str
    rationale: str = ""

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def _upper_iata(cls, value: Any) -> str:
        return str(value or "").strip().upper()


class _PlanOutputDraft(BaseModel):
    plans: list[_PlanItemDraft] = Field(default_factory=list)


class _SelectDraft(BaseModel):
    """Tokens only — explanations are filled locally (OpenRouter truncates long JSON)."""

    ordered_tokens: list[str] = Field(default_factory=list)

    @field_validator("ordered_tokens", mode="before")
    @classmethod
    def _coerce_tokens(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value]


class Strategist:
    def __init__(
        self,
        router: ModelRouter,
        atlas: AtlasClient,
        session_factory: Callable[[], Session],
    ) -> None:
        self._router = router
        self._atlas = atlas
        self._session_factory = session_factory
        self._strategist_prompt = _STRATEGIST_PROMPT.read_text(encoding="utf-8")
        self._scoring_prompt = _SCORING_PROMPT.read_text(encoding="utf-8")
        self._case_id: int | None = None
        self._original_arrival_at: datetime | None = None

    async def plan(
        self, intent: RecoveryIntent, original: list[Segment]
    ) -> list[StrategyPlan]:
        """Returns up to 4 plans, one per SearchStrategy."""
        self._case_id = intent.case_id
        self._original_arrival_at = (
            original[-1].arrival_at if original else datetime.now(UTC)
        )

        origins = _iata_list(intent.origin_candidates_list)
        destinations = _iata_list(intent.destination_candidates_list)
        if not origins or not destinations:
            raise ValueError(
                "RecoveryIntent must include non-empty origin_candidates "
                "and destination_candidates before plan()"
            )

        base_dep = _base_departure(original)
        adults = max(1, int(intent.passenger_count))

        user_prompt = _build_plan_prompt(
            intent=intent,
            origins=origins,
            destinations=destinations,
            original=original,
            base_departure=base_dep,
        )
        request = ModelRequest(
            system=self._strategist_prompt,
            prompt=user_prompt,
            temperature=0.0,
            max_output_tokens=2048,
            timeout_seconds=60.0,
        )

        try:
            draft = await self._router.generate_structured(request, _PlanOutputDraft)
            plans = _plans_from_draft(
                draft,
                origins=origins,
                destinations=destinations,
                base_departure=base_dep,
                adults=adults,
            )
        except Exception:
            plans = []

        if not plans:
            plans = _deterministic_plans(
                origins=origins,
                destinations=destinations,
                base_departure=base_dep,
                adults=adults,
            )
        return plans

    async def fan_out(self, plans: list[StrategyPlan]) -> list[Candidate]:
        """Concurrent search.do calls. Deduplicates by offer_id. An empty
        result for one strategy is not fatal; all-empty raises AtlasNoResultsError.
        """
        if self._case_id is None:
            raise RuntimeError("fan_out requires plan() first (case_id unset)")
        case_id = self._case_id
        original_arrival = self._original_arrival_at or datetime.now(UTC)

        for plan in plans:
            await self._write_event(
                case_id=case_id,
                step=_STEP_DISPATCHED,
                summary=f"dispatched {plan.strategy.value}",
                payload={
                    "strategy": plan.strategy.value,
                    "origin": plan.search_request.origin,
                    "destination": plan.search_request.destination,
                    "adults": plan.search_request.adults,
                    # Human date — avoid YYYY-MM-DD / YYYYMMDD DOB shapes (I4).
                    "departure_day": _fmt_day(plan.search_request.departure_date),
                },
            )

        async def _one(
            plan: StrategyPlan,
        ) -> tuple[StrategyPlan, list[Offer], str | None]:
            try:
                result = await self._atlas.search(plan.search_request)
                return plan, list(result.offers), None
            except AtlasNoResultsError as exc:
                return plan, [], str(exc.code)

        gathered = await asyncio.gather(*[_one(p) for p in plans])

        by_offer: dict[str, Candidate] = {}
        any_offers = False

        with self._session_factory() as session:
            for plan, offers, err_code in gathered:
                any_offers = any_offers or bool(offers)
                await write_event(
                    session,
                    AgentEventIn(
                        case_id=case_id,
                        actor=Actor.STRATEGIST,
                        step=_STEP_RETURNED,
                        summary=(
                            f"search returned {len(offers)} for {plan.strategy.value}"
                        ),
                        payload={
                            "strategy": plan.strategy.value,
                            "offer_count": len(offers),
                            "empty": len(offers) == 0,
                            "error_code": err_code,
                        },
                    ),
                )
                for offer in offers:
                    if offer.offer_id in by_offer:
                        continue
                    candidate = _offer_to_candidate(
                        case_id=case_id,
                        offer=offer,
                        strategy=plan.strategy,
                        original_arrival=original_arrival,
                    )
                    session.add(candidate)
                    session.flush()
                    by_offer[offer.offer_id] = candidate

            # Copy while the session is open — SQLModel expires attrs on commit.
            out = [_detach_candidate(c) for c in by_offer.values()]
            session.commit()

        if not any_offers or not out:
            raise AtlasNoResultsError(
                code="no_results",
                message="every Strategist search strategy returned zero offers",
            )
        return out

    async def write_scoring_code(self, intent: RecoveryIntent) -> str:
        """Model-generated Python scored in Zone B. Must define
        `def score(payload: dict) -> list[dict]` and use only the stdlib.
        """
        global _FORCE_BAD_SCORING_ONCE

        user_prompt = _build_scoring_prompt(intent)
        request = ModelRequest(
            system=self._scoring_prompt,
            prompt=user_prompt,
            temperature=0.0,
            max_output_tokens=8192,
            timeout_seconds=90.0,
        )

        forced = _FORCE_BAD_SCORING_ONCE
        if forced is not None:
            _FORCE_BAD_SCORING_ONCE = None
            code = _extract_python(forced)
        else:
            response = await self._router.generate(request)
            code = _extract_python(response.text)

        err = _scoring_code_error(code)
        if err is not None:
            retry = request.model_copy(
                update={
                    "prompt": (
                        f"{user_prompt}\n\n"
                        f"Your previous reply was rejected: {err}\n"
                        "Reply again with only valid Python source that defines "
                        "score(payload) and imports nothing outside the stdlib. "
                        "No markdown fences."
                    )
                }
            )
            response = await self._router.generate(retry)
            code = _extract_python(response.text)
            err2 = _scoring_code_error(code)
            if err2 is not None:
                raise ValueError(f"scoring code rejected after one retry: {err2}")

        if intent.case_id:
            await self._write_event(
                case_id=intent.case_id,
                step=_STEP_SCORING_CODE,
                summary="scoring code accepted",
                payload={
                    "nbytes": len(code.encode("utf-8")),
                    "defines_score": True,
                },
            )
        return code

    async def select(
        self,
        candidates: list[Candidate],
        scored: list[ScoredCandidate],
        intent: RecoveryIntent,
    ) -> RankedSelection:
        """Any offer_id not present in `candidates` is discarded silently (I1)."""
        allowed = {c.offer_id for c in candidates}
        # Keep the model prompt small — long candidate lists truncate JSON.
        scored_sorted = sorted(
            (s for s in scored if s.offer_id in allowed),
            key=lambda s: s.score,
            reverse=True,
        )[:8]
        shortlist_ids = {s.offer_id for s in scored_sorted}
        if not shortlist_ids:
            shortlist_ids = {c.offer_id for c in candidates[:8]}

        token_to_id: dict[str, str] = {}
        id_to_token: dict[str, str] = {}
        shortlist = [c for c in candidates if c.offer_id in shortlist_ids]
        for i, c in enumerate(shortlist):
            token = f"CAND_{i}"
            token_to_id[token] = c.offer_id
            id_to_token[c.offer_id] = token

        scored_payload = [
            {
                "token": id_to_token[s.offer_id],
                "score": round(float(s.score), 4),
                "self_transfer_risk": round(float(s.self_transfer_risk), 4),
                "mobility_fit": round(float(s.mobility_fit), 4),
            }
            for s in scored_sorted
            if s.offer_id in id_to_token
        ]
        cand_payload = [
            {
                "token": id_to_token[c.offer_id],
                "strategy": c.strategy,
                "price": str(c.price),
                "arrival_delay_minutes": c.arrival_delay_minutes,
                "stop_count": c.stop_count,
            }
            for c in shortlist
        ]
        prompt = (
            "Rank these candidates best-first by token. Return JSON only with "
            "one field: ordered_tokens (array of CAND_N, best first). "
            "No explanations field.\n\n"
            f"budget_ceiling_sgd={intent.budget_ceiling_sgd}\n"
            f"mobility_notes={intent.mobility_notes!r}\n"
            f"candidates={json.dumps(cand_payload, separators=(',', ':'))}\n"
            f"scored={json.dumps(scored_payload, separators=(',', ':'))}\n"
        )
        request = ModelRequest(
            system=(
                "Select among existing CAND_N tokens only (I1). "
                "Never invent tokens or itineraries. "
                "Reply with compact JSON: {\"ordered_tokens\":[...]} only."
            ),
            prompt=prompt,
            temperature=0.0,
            max_output_tokens=2048,
            timeout_seconds=60.0,
        )
        draft = await self._router.generate_structured(request, _SelectDraft)

        by_id = {c.offer_id: c for c in candidates}
        ordered: list[str] = []
        explanations: dict[str, str] = {}
        for raw in draft.ordered_tokens:
            oid = token_to_id.get(raw, raw)
            if oid not in allowed or oid in ordered:
                continue
            ordered.append(oid)
            cand = by_id.get(oid)
            score = next((s.score for s in scored if s.offer_id == oid), None)
            explanations[oid] = (
                f"strategy={cand.strategy if cand else '?'} "
                f"score={score if score is not None else '?'} "
                f"delay_min={cand.arrival_delay_minutes if cand else '?'}"
            )

        selection = RankedSelection(
            ordered_offer_ids=ordered,
            explanations=explanations,
        )
        await self._write_event(
            case_id=intent.case_id,
            step=_STEP_SELECTED,
            summary=f"selected {len(ordered)} offer ids",
            payload={"selected_count": len(ordered)},
        )
        return selection

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
                    actor=Actor.STRATEGIST,
                    step=step,
                    summary=summary,
                    payload=payload,
                ),
            )
            session.commit()


def _iata_list(raw: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        code = str(item or "").strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def _base_departure(original: list[Segment]) -> datetime:
    now = datetime.now(UTC)
    if not original:
        return (now + timedelta(days=30)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    dep = original[0].departure_at
    if dep.tzinfo is None:
        dep = dep.replace(tzinfo=UTC)
    day = dep.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if day < today:
        return today + timedelta(days=30)
    return day


def _fmt_when(dt: datetime) -> str:
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%d %b %Y %H:%M UTC")


def _fmt_day(dt: datetime) -> str:
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%d %b %Y")


def _parse_departure_date(raw: str, *, fallback: datetime) -> datetime:
    text = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%Y%m%d"):
        try:
            if fmt == "%d %b %Y":
                d = datetime.strptime(text, fmt)
            elif fmt == "%Y%m%d":
                d = datetime.strptime(text[:8], fmt)
            else:
                d = datetime.strptime(text[:10], fmt)
            return d.replace(tzinfo=UTC, hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    except ValueError:
        return fallback


def _build_plan_prompt(
    *,
    intent: RecoveryIntent,
    origins: list[str],
    destinations: list[str],
    original: list[Segment],
    base_departure: datetime,
) -> str:
    airports = [
        {
            "origin": s.origin,
            "destination": s.destination,
            "departure_at": _fmt_when(s.departure_at),
            "arrival_at": _fmt_when(s.arrival_at),
        }
        for s in original
    ]
    return "\n".join(
        [
            f"Current time (UTC): {_fmt_when(datetime.now(UTC))}",
            f"passenger_count={intent.passenger_count}",
            f"must_arrive_by="
            f"{_fmt_when(intent.must_arrive_by) if intent.must_arrive_by else None}",
            f"budget_ceiling_sgd={intent.budget_ceiling_sgd}",
            f"mobility_notes={intent.mobility_notes!r}",
            f"language={intent.language!r}",
            f"origin_candidates={json.dumps(origins)}",
            f"destination_candidates={json.dumps(destinations)}",
            f"base_departure_day={_fmt_day(base_departure)}",
            "Original itinerary airports (date context only; no flights to emit):",
            json.dumps(airports, separators=(",", ":")),
            "",
            "Emit up to four plans, one per SearchStrategy.",
        ]
    )


def _build_scoring_prompt(intent: RecoveryIntent) -> str:
    return "\n".join(
        [
            "Write score(payload) for this recovery intent context:",
            f"passenger_count={intent.passenger_count}",
            f"must_arrive_by="
            f"{_fmt_when(intent.must_arrive_by) if intent.must_arrive_by else None}",
            f"budget_ceiling_sgd={intent.budget_ceiling_sgd}",
            f"mobility_notes={intent.mobility_notes!r}",
            f"origin_candidates={intent.origin_candidates_list!r}",
            f"destination_candidates={intent.destination_candidates_list!r}",
            "",
            "Return only Python source.",
        ]
    )


def _plans_from_draft(
    draft: _PlanOutputDraft,
    *,
    origins: list[str],
    destinations: list[str],
    base_departure: datetime,
    adults: int,
) -> list[StrategyPlan]:
    origin_set = set(origins)
    dest_set = set(destinations)
    by_strategy: dict[SearchStrategy, StrategyPlan] = {}

    for item in draft.plans:
        if item.origin not in origin_set or item.destination not in dest_set:
            continue
        dep = _parse_departure_date(item.departure_date, fallback=base_departure)
        plan = StrategyPlan(
            strategy=item.strategy,
            search_request=SearchRequest(
                origin=item.origin,
                destination=item.destination,
                departure_date=dep,
                adults=adults,
                currency="USD",
            ),
            rationale=(item.rationale or item.strategy.value).strip(),
        )
        by_strategy.setdefault(item.strategy, plan)

    for plan in _deterministic_plans(
        origins=origins,
        destinations=destinations,
        base_departure=base_departure,
        adults=adults,
    ):
        by_strategy.setdefault(plan.strategy, plan)

    return [by_strategy[s] for s in SearchStrategy if s in by_strategy]


def _deterministic_plans(
    *,
    origins: list[str],
    destinations: list[str],
    base_departure: datetime,
    adults: int,
) -> list[StrategyPlan]:
    primary_o, primary_d = origins[0], destinations[0]
    alt_o = next((o for o in origins if o != primary_o), primary_o)
    alt_d = next((d for d in destinations if d != primary_d), primary_d)
    d0 = base_departure
    d1 = base_departure + timedelta(days=1)

    nearby_origin = alt_o
    nearby_dest = primary_d if alt_o != primary_o else alt_d

    specs: list[tuple[SearchStrategy, str, str, datetime, str]] = [
        (
            SearchStrategy.SAME_ROUTE_LATER,
            primary_o,
            primary_d,
            d0,
            "Same route on the original travel day.",
        ),
        (
            SearchStrategy.NEARBY_AIRPORT,
            nearby_origin,
            nearby_dest,
            d0,
            "Nearby-airport substitution from intent candidates.",
        ),
        (
            SearchStrategy.ONE_STOP_REROUTE,
            primary_o,
            primary_d,
            d0,
            "Same city pair; connecting inventory eligible.",
        ),
        (
            SearchStrategy.NEXT_MORNING_HOTEL,
            primary_o,
            primary_d,
            d1,
            "Next-morning continuation after overnight.",
        ),
    ]
    plans: list[StrategyPlan] = []
    for strategy, origin, dest, dep, rationale in specs:
        plans.append(
            StrategyPlan(
                strategy=strategy,
                search_request=SearchRequest(
                    origin=origin,
                    destination=dest,
                    departure_date=dep,
                    adults=adults,
                    currency="USD",
                ),
                rationale=rationale,
            )
        )
    return plans


def _detach_candidate(c: Candidate) -> Candidate:
    """Plain Candidate copy safe to use after the Session closes."""
    return Candidate(
        id=c.id,
        case_id=c.case_id,
        offer_id=c.offer_id,
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


def _offer_to_candidate(
    *,
    case_id: int,
    offer: Offer,
    strategy: SearchStrategy,
    original_arrival: datetime,
) -> Candidate:
    segs = offer.segments
    if segs:
        arrival = segs[-1].arrival_at
        if arrival.tzinfo is None:
            arrival = arrival.replace(tzinfo=UTC)
        orig = original_arrival
        if orig.tzinfo is None:
            orig = orig.replace(tzinfo=UTC)
        delay = int((arrival - orig).total_seconds() // 60)
    else:
        delay = 0

    segments_json = json.dumps(
        [s.model_dump(mode="json") for s in segs],
        separators=(",", ":"),
        default=str,
    )
    return Candidate(
        case_id=case_id,
        offer_id=offer.offer_id,
        strategy=strategy.value,
        segments_json=segments_json,
        price=offer.price,
        currency=offer.currency,
        arrival_delay_minutes=delay,
        stop_count=offer.stop_count,
        min_transfer_minutes=offer.min_transfer_minutes,
        self_transfer_risk=0.0,
        mobility_fit=0.0,
        score=None,
        score_components_json=None,
        verified=False,
        verified_price=None,
        rejected_reason=None,
    )


_FENCE_RE = re.compile(
    r"```(?:python)?\s*([\s\S]*?)```",
    re.IGNORECASE,
)


def _extract_python(text: str) -> str:
    raw = (text or "").strip()
    match = _FENCE_RE.search(raw)
    if match:
        return match.group(1).strip()
    return raw


def _scoring_code_error(code: str) -> str | None:
    """Return a rejection reason, or None if the code is acceptable."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"syntax error: {exc}"

    stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root not in stdlib:
                    return f"non-stdlib import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                return "invalid relative import"
            root = node.module.split(".", 1)[0]
            if root not in stdlib:
                return f"non-stdlib import: {node.module}"
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"eval", "exec", "__import__"}:
                return f"forbidden call: {func.id}"

    has_score = False
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "score":
            return "score must be a sync def, not async"
        if isinstance(node, ast.FunctionDef) and node.name == "score":
            has_score = True
            has_return = any(isinstance(n, ast.Return) for n in ast.walk(node))
            if not has_return:
                return "score() has no return (possibly truncated)"
            break
    if not has_score:
        return "missing def score(...)"
    return None
