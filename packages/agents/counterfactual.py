"""DIY baseline for the Recovery Receipt — deterministic, no model (I1, I3).

Definition (also shown on the receipt UI):

The do-it-yourself path is the cheapest *same-day* offer from the unfiltered
search set (every Candidate the Strategist persisted from ``search.do``).
If no same-day offer exists, it is the cheapest ``next_morning_hotel`` offer.
If that set is also empty, it is the cheapest offer of any day.

Same-day means the offer's first-segment departure calendar day (UTC) matches
the original itinerary's first-segment departure calendar day.

Deltas (positive = Rebound is better than DIY):

* ``counterfactual_cost_delta_sgd`` = DIY cost in SGD − actual amount paid in SGD
* ``counterfactual_hours_delta`` = DIY arrival − actual arrival, in hours

USD amounts convert with the same hard-coded rate as the executor
(``1 USD = 1.35 SGD``). Never a model estimate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

# Identical to packages.agents.executor_agent._USD_TO_SGD — never from a model.
USD_TO_SGD = Decimal("1.35")
_NEXT_MORNING = "next_morning_hotel"


class SearchOffer(BaseModel):
    """One Atlas-originated option. Every field is search/verify data, not copy."""

    offer_id: str
    price: Decimal
    currency: str
    departure_at: datetime
    arrival_at: datetime
    strategy: str = ""


class DiyBaseline(BaseModel):
    offer_id: str
    cost_sgd: Decimal
    arrival_at: datetime
    source: str  # cheapest_same_day | next_morning | cheapest_any
    currency: str
    price_original: Decimal


class Counterfactual(BaseModel):
    diy: DiyBaseline
    actual_cost_sgd: Decimal
    actual_arrival_at: datetime
    counterfactual_cost_delta_sgd: Decimal
    counterfactual_hours_delta: float
    definition: str = Field(
        default=(
            "DIY baseline: cheapest same-day offer from the unfiltered search "
            "set; next-morning offer if no same-day offer exists. Cost delta "
            "is DIY SGD minus actual SGD. Hours delta is DIY arrival minus "
            "actual arrival."
        )
    )


def to_sgd(amount: Decimal, currency: str) -> Decimal:
    """Deterministic USD→SGD. Unknown currencies are treated as SGD face value."""
    cur = (currency or "").strip().upper()
    value = Decimal(str(amount))
    if cur in ("", "SGD"):
        return value.quantize(Decimal("0.01"))
    if cur == "USD":
        return (value * USD_TO_SGD).quantize(Decimal("0.01"))
    return value.quantize(Decimal("0.01"))


def parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def offer_from_candidate(candidate: Any) -> SearchOffer | None:
    """Lift a persisted Candidate (or dict) into a SearchOffer. Drops junk."""
    if hasattr(candidate, "model_dump"):
        data = candidate.model_dump()
        segments_raw = getattr(candidate, "segments_json", "") or ""
    elif isinstance(candidate, dict):
        data = candidate
        segments_raw = str(data.get("segments_json") or "")
    else:
        return None

    offer_id = str(data.get("offer_id") or "").strip()
    if not offer_id:
        return None
    try:
        price = Decimal(str(data.get("price") if data.get("price") is not None else "0"))
    except Exception:
        return None
    currency = str(data.get("currency") or "USD")
    strategy = str(data.get("strategy") or "")

    segments: list[dict[str, Any]] = []
    if segments_raw:
        try:
            loaded = json.loads(segments_raw)
        except (TypeError, json.JSONDecodeError):
            loaded = []
        if isinstance(loaded, list):
            segments = [item for item in loaded if isinstance(item, dict)]
    if not segments and isinstance(data.get("segments"), list):
        segments = [item for item in data["segments"] if isinstance(item, dict)]

    departure = parse_dt(segments[0].get("departure_at") if segments else None)
    arrival = parse_dt(segments[-1].get("arrival_at") if segments else None)
    if arrival is None:
        arrival = parse_dt(data.get("arrival_at"))
    if departure is None or arrival is None:
        return None
    return SearchOffer(
        offer_id=offer_id,
        price=price,
        currency=currency,
        departure_at=departure,
        arrival_at=arrival,
        strategy=strategy,
    )


def _day(value: datetime) -> datetime:
    utc = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    utc = utc.astimezone(UTC)
    return utc.replace(hour=0, minute=0, second=0, microsecond=0)


def _cheapest(offers: list[SearchOffer]) -> SearchOffer:
    return min(
        offers,
        key=lambda offer: (to_sgd(offer.price, offer.currency), offer.offer_id),
    )


def pick_diy_baseline(
    offers: list[SearchOffer], *, original_departure_at: datetime
) -> DiyBaseline:
    """Choose the DIY offer. Pure function of offers + original departure day."""
    original_day = _day(original_departure_at)
    same_day = [offer for offer in offers if _day(offer.departure_at) == original_day]
    if same_day:
        chosen = _cheapest(same_day)
        source = "cheapest_same_day"
    else:
        morning = [offer for offer in offers if offer.strategy == _NEXT_MORNING]
        if morning:
            chosen = _cheapest(morning)
            source = "next_morning"
        else:
            chosen = _cheapest(offers)
            source = "cheapest_any"
    return DiyBaseline(
        offer_id=chosen.offer_id,
        cost_sgd=to_sgd(chosen.price, chosen.currency),
        arrival_at=chosen.arrival_at,
        source=source,
        currency=chosen.currency,
        price_original=Decimal(str(chosen.price)),
    )


def compute_counterfactual(
    *,
    offers: list[SearchOffer],
    original_departure_at: datetime,
    actual_cost_sgd: Decimal,
    actual_arrival_at: datetime,
) -> Counterfactual:
    """Return DIY-minus-actual deltas. Raises if there is no offer to compare."""
    if not offers:
        raise ValueError("counterfactual requires at least one Atlas offer")
    actual_cost = Decimal(str(actual_cost_sgd)).quantize(Decimal("0.01"))
    actual_arrival = parse_dt(actual_arrival_at)
    if actual_arrival is None:
        raise ValueError("actual_arrival_at is required")
    diy = pick_diy_baseline(offers, original_departure_at=original_departure_at)
    hours = (diy.arrival_at - actual_arrival).total_seconds() / 3600.0
    hours = float(Decimal(str(hours)).quantize(Decimal("0.0001")))
    return Counterfactual(
        diy=diy,
        actual_cost_sgd=actual_cost,
        actual_arrival_at=actual_arrival,
        counterfactual_cost_delta_sgd=(diy.cost_sgd - actual_cost).quantize(
            Decimal("0.01")
        ),
        counterfactual_hours_delta=hours,
    )


def canonical_dumps(counterfactual: Counterfactual) -> str:
    """Stable JSON for the byte-for-byte determinism check."""
    return counterfactual.model_dump_json(indent=2) + "\n"


def compute_from_candidates(
    candidates: list[Any],
    *,
    original_departure_at: datetime,
    actual_cost_sgd: Decimal,
    actual_arrival_at: datetime,
) -> Counterfactual:
    offers = [
        offer
        for offer in (offer_from_candidate(item) for item in candidates)
        if offer is not None
    ]
    return compute_counterfactual(
        offers=offers,
        original_departure_at=original_departure_at,
        actual_cost_sgd=actual_cost_sgd,
        actual_arrival_at=actual_arrival_at,
    )


def _self_check() -> int:
    """Run twice on identical input and require byte-identical JSON."""
    offers = [
        SearchOffer(
            offer_id="same-day-cheap",
            price=Decimal("80.00"),
            currency="USD",
            departure_at=datetime(2026, 9, 13, 8, 0, tzinfo=UTC),
            arrival_at=datetime(2026, 9, 13, 11, 0, tzinfo=UTC),
            strategy="same_route_later",
        ),
        SearchOffer(
            offer_id="same-day-dear",
            price=Decimal("400.00"),
            currency="USD",
            departure_at=datetime(2026, 9, 13, 9, 0, tzinfo=UTC),
            arrival_at=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
            strategy="same_route_later",
        ),
        SearchOffer(
            offer_id="next-morning",
            price=Decimal("70.00"),
            currency="USD",
            departure_at=datetime(2026, 9, 14, 8, 0, tzinfo=UTC),
            arrival_at=datetime(2026, 9, 14, 11, 0, tzinfo=UTC),
            strategy="next_morning_hotel",
        ),
    ]
    kwargs = {
        "offers": offers,
        "original_departure_at": datetime(2026, 9, 13, 6, 0, tzinfo=UTC),
        "actual_cost_sgd": Decimal("120.00"),
        "actual_arrival_at": datetime(2026, 9, 13, 10, 0, tzinfo=UTC),
    }
    first = canonical_dumps(compute_counterfactual(**kwargs))
    second = canonical_dumps(compute_counterfactual(**kwargs))
    if first != second:
        print("COUNTERFACTUAL DRIFT")
        print(first)
        print("---")
        print(second)
        return 1
    print(first, end="")
    print("COUNTERFACTUAL DETERMINISTIC OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
