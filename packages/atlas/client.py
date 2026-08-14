"""Atlas client. Task 4: search.do only; other methods stubbed."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from packages.atlas.errors import AtlasNoResultsError
from packages.atlas.models import Offer, SearchRequest, SearchResult, Segment
from packages.domain.enums import ChaosProfile

if TYPE_CHECKING:
    from packages.atlas.models import (
        CardDetails,
        OrderDetails,
        OrderResult,
        Passenger,
        PayResult,
        VerifyResult,
    )


class AtlasTransport(Protocol):
    """The single seam between live and replay (I9).

    Everything above this line is identical in both modes.
    """

    async def post(self, path: str, payload: dict) -> dict: ...


def _atlas_local_dt(value: str) -> datetime:
    """Parse Atlas segment times (`YYYYMMDDHHMM` or `YYYYMMDDHHMMSS`).

    Atlas documents these as airport-local wall clocks. We attach UTC tzinfo
    without shifting the clock so the wire value is preserved (I1).
    """
    raw = str(value).strip()
    for fmt in ("%Y%m%d%H%M", "%Y%m%d%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"unrecognised Atlas datetime: {value!r}")


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _segment_from_atlas(raw: dict) -> Segment:
    cabin = raw.get("cabin")
    if isinstance(cabin, str):
        cabin = cabin.strip() or None
    else:
        cabin = None
    return Segment(
        carrier=str(raw["carrier"]),
        flight_number=str(raw["flightNumber"]),
        origin=str(raw["depAirport"]),
        destination=str(raw["arrAirport"]),
        departure_at=_atlas_local_dt(raw["depTime"]),
        arrival_at=_atlas_local_dt(raw["arrTime"]),
        cabin=cabin,
    )


def _offer_from_routing(routing: dict) -> Offer:
    routing_identifier = str(routing.get("routingIdentifier") or "")
    if not routing_identifier:
        raise AtlasNoResultsError(
            code="missing_routing_identifier",
            message="Atlas routing missing routingIdentifier",
        )

    # Live sandbox routings carry `fid`; OpenAPI Routing schema does not list it.
    # Prefer fid for offer_id when present so routingIdentifier stays distinct.
    fid = routing.get("fid")
    offer_id = str(fid).strip() if fid not in (None, "") else routing_identifier

    from_segments = routing.get("fromSegments") or []
    ret_segments = routing.get("retSegments") or []
    if not isinstance(from_segments, list):
        from_segments = []
    if not isinstance(ret_segments, list):
        ret_segments = []

    segments = [_segment_from_atlas(s) for s in from_segments if isinstance(s, dict)]
    segments.extend(_segment_from_atlas(s) for s in ret_segments if isinstance(s, dict))

    # Docs: total for one adult = adultPrice + adultTax + transactionFeePerPax
    price = (
        _decimal(routing.get("adultPrice"))
        + _decimal(routing.get("adultTax"))
        + _decimal(routing.get("transactionFeePerPax"))
    )
    currency = str(routing.get("currency") or "")

    # Connection count from Atlas segment arrays (not invented schedule data).
    outbound_stops = max(0, len(from_segments) - 1) if from_segments else 0
    inbound_stops = max(0, len(ret_segments) - 1) if ret_segments else 0
    stop_count = outbound_stops + inbound_stops

    rule = routing.get("rule") if isinstance(routing.get("rule"), dict) else {}
    has_baggage = rule.get("hasBaggage")
    baggage_included: bool | None
    if has_baggage is None:
        baggage_included = None
    else:
        baggage_included = int(has_baggage) == 1

    return Offer(
        offer_id=offer_id,
        routing_identifier=routing_identifier,
        segments=segments,
        price=price,
        currency=currency,
        stop_count=stop_count,
        min_transfer_minutes=None,
        baggage_included=baggage_included,
        raw=dict(routing),
    )


def _search_payload(request: SearchRequest) -> dict:
    # Field names from resources.atriptech.com search.do OpenAPI (not invented).
    return {
        "tripType": "1",
        "adultNum": int(request.adults),
        "childNum": int(request.children),
        "infantNum": int(request.infants),
        "fromCity": request.origin.upper(),
        "toCity": request.destination.upper(),
        "fromDate": request.departure_date.strftime("%Y%m%d"),
        "currency": "USD",  # sandbox requires explicit USD [E]
    }


class AtlasClient:
    def __init__(
        self,
        transport: AtlasTransport,
        *,
        chaos: ChaosProfile = ChaosProfile.NONE,
    ) -> None:
        self._transport = transport
        self._chaos = chaos

    async def search(self, request: SearchRequest) -> SearchResult:
        """POST search.do. Raises AtlasNoResultsError on zero offers."""
        body = await self._transport.post("search.do", _search_payload(request))
        routings = body.get("routings") or []
        if not isinstance(routings, list) or len(routings) == 0:
            raise AtlasNoResultsError(
                code=str(body.get("status", "no_results")),
                message=str(body.get("msg") or "Atlas search returned zero offers"),
            )

        offers = [_offer_from_routing(r) for r in routings if isinstance(r, dict)]
        offers = [o for o in offers if o.offer_id and o.routing_identifier]
        if not offers:
            raise AtlasNoResultsError(
                code=str(body.get("status", "no_results")),
                message="Atlas search returned routings but no usable offers",
            )

        # sessionId is a verify.do response field per Atlas docs; search.do may
        # omit it. Preserve when present; otherwise empty string (never invent).
        session_raw = body.get("sessionId")
        if session_raw in (None, ""):
            session_id = ""
        else:
            session_id = str(session_raw)

        return SearchResult(session_id=session_id, offers=offers, raw=dict(body))

    async def verify(self, *, session_id: str, offer_id: str) -> VerifyResult:
        """POST verify.do. Sets price_changed; raises AtlasPriceMovedError
        only if the caller passed expected_price via verify_strict()."""
        raise NotImplementedError("Task 5")

    async def verify_strict(
        self, *, session_id: str, offer_id: str, expected_price: Decimal
    ) -> VerifyResult:
        """Raises AtlasPriceMovedError when the verified price differs."""
        raise NotImplementedError("Task 5")

    async def get_offer_price(self, *, offer_id: str) -> VerifyResult:
        """POST getOfferPrice.do. Preserves OfferId [E]."""
        raise NotImplementedError("Task 5")

    async def order(
        self,
        *,
        session_id: str,
        offer_id: str,
        passengers: list[Passenger],
        contact_email: str,
        contact_phone: str,
    ) -> OrderResult:
        """POST order.do. Caller MUST have a successful verify first (I2)."""
        raise NotImplementedError("Task 6")

    async def pay(self, *, order_no: str, card: CardDetails) -> PayResult:
        """POST pay.do. Raises AtlasPaymentDeclinedError on 604 and
        AtlasThreeDSRequiredError on 616 [E]. Card data never logged (I4)."""
        raise NotImplementedError("Task 6")

    async def query_order_details(self, *, order_no: str) -> OrderDetails:
        """POST queryOrderDetails.do. Authoritative state (I7)."""
        raise NotImplementedError("Task 7")

    async def poll_order_until(
        self,
        *,
        order_no: str,
        terminal_statuses: set[str],
        timeout_seconds: int = 120,
        interval_seconds: float = 3.0,
    ) -> OrderDetails:
        """Poll until status is terminal or timeout. The webhook safety net (I7)."""
        raise NotImplementedError("Task 7")
