"""Atlas wire models (INTERFACES.md §1.1). Task 4–7: search → pay + order details."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


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
    session_id: str  # NOT a live search.do field [E]. search.do
    # does not issue sessionId; verify.do does (§1.2). Kept only for
    # backward compat with Task 4 (empty string). Do not rely on it.
    offers: list[Offer]
    raw: dict


class VerifyResult(BaseModel):
    offer_id: str
    session_id: str  # newly issued by verify.do for order.do (~2h) [E]
    verified: bool
    price: Decimal  # authoritative; may differ from the search price
    currency: str
    price_changed: bool
    raw: dict


class Passenger(BaseModel):
    """Zone A only. Never serialised into a sandbox or a model prompt (I4)."""

    given_name: str
    surname: str
    date_of_birth: datetime
    passport_number: str | None = None
    nationality: str | None = None


class OrderResult(BaseModel):
    order_no: str  # MUST be preserved [E]
    status: str
    ticketing_deadline: datetime | None = None  # 5-minute window after order [E]
    total_amount: Decimal
    currency: str
    raw: dict


class CardDetails(BaseModel):
    """NEVER logged, NEVER persisted, NEVER leaves Zone A (I4).

    `holder_given_name` is also the chaos lever: "Reject" -> 604,
    "Three DS" -> 616, both documented Atlas sandbox triggers [E].
    """

    holder_given_name: str
    holder_surname: str
    number: str
    expiry_month: int
    expiry_year: int
    cvv: str

    def __repr__(self) -> str:
        # MUST redact; no PAN/CVV in any repr or traceback (I4).
        last4 = self.number[-4:] if len(self.number) >= 4 else "****"
        return (
            "CardDetails("
            f"holder_given_name={self.holder_given_name!r}, "
            f"holder_surname={self.holder_surname!r}, "
            f"number='****{last4}', "
            f"expiry_month={int(self.expiry_month)}, "
            f"expiry_year={int(self.expiry_year)}, "
            "cvv='***')"
        )

    def __str__(self) -> str:
        return repr(self)


class PayResult(BaseModel):
    order_no: str
    paid: bool
    ticket_numbers: list[str] = Field(default_factory=list)
    pnr: str | None = None
    error_code: str | None = None  # "604" / "616" surface here as well as raising
    raw: dict


class OrderDetails(BaseModel):
    """Authoritative order state. The only trusted source (I7)."""

    order_no: str
    status: str
    pnr: str | None
    ticket_numbers: list[str]
    segments: list[Segment]
    total_amount: Decimal
    currency: str
    raw: dict
