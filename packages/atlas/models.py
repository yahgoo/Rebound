"""Atlas wire models (INTERFACES.md §1.1). Task 4: search types only."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class Segment(BaseModel):
    carrier: str
    flight_number: str
    origin: str  # IATA
    destination: str  # IATA
    departure_at: datetime
    arrival_at: datetime
    cabin: str | None = None


class Offer(BaseModel):
    """One purchasable option exactly as Atlas returned it.

    Invariant I1: no field here may ever be synthesised by a model.
    """

    offer_id: str
    routing_identifier: str  # MUST be preserved and echoed back [E]
    segments: list[Segment]
    price: Decimal
    currency: str
    stop_count: int
    min_transfer_minutes: int | None = None
    baggage_included: bool | None = None
    raw: dict  # verbatim Atlas object, for the audit log


class SearchRequest(BaseModel):
    origin: str
    destination: str
    departure_date: datetime
    adults: int = 1
    children: int = 0
    infants: int = 0
    cabin: str | None = None
    currency: str = "USD"  # sandbox requires this explicitly [E]


class SearchResult(BaseModel):
    session_id: str  # MUST be preserved for verify.do [E]
    offers: list[Offer]
    raw: dict
