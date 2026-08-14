"""Atlas client. Task 4–5: search.do + verify.do + getOfferPrice.do."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from packages.atlas.errors import AtlasNoResultsError, AtlasPriceMovedError
from packages.atlas.models import (
    Offer,
    SearchRequest,
    SearchResult,
    Segment,
    VerifyResult,
)
from packages.domain.enums import ChaosProfile

if TYPE_CHECKING:
    from packages.atlas.models import (
        CardDetails,
        OrderDetails,
        OrderResult,
        Passenger,
        PayResult,
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


def _routing_total_price(routing: dict) -> Decimal:
    """Single-adult purchase total — same formula as search (I1, no invention)."""
    return (
        _decimal(routing.get("adultPrice"))
        + _decimal(routing.get("adultTax"))
        + _decimal(routing.get("transactionFeePerPax"))
    )


def _price_changed_from_verify(body: dict) -> bool:
    """Compare verified price to search price using Atlas priceChange [E]."""
    pc = body.get("priceChange")
    if not isinstance(pc, dict):
        return False
    if "isPriceChange" in pc:
        return bool(pc["isPriceChange"])
    # Fallback: reconstruct original vs new adult totals from priceChange fields.
    original = _decimal(pc.get("originalAdultPrice")) + _decimal(
        pc.get("originalAdultTax")
    )
    new = _decimal(pc.get("newAdultPrice")) + _decimal(pc.get("newAdultTax"))
    if original or new:
        return original != new
    return False


def _offer_id_from_routing(routing: dict, *, fallback: str) -> str:
    fid = routing.get("fid")
    if fid not in (None, ""):
        return str(fid).strip()
    rid = routing.get("routingIdentifier")
    if rid not in (None, ""):
        return str(rid)
    return fallback


def _verify_result_from_body(
    body: dict, *, fallback_routing_identifier: str
) -> VerifyResult:
    routing = body.get("routing") if isinstance(body.get("routing"), dict) else {}
    session_raw = body.get("sessionId")
    if session_raw in (None, ""):
        raise AtlasNoResultsError(
            code=str(body.get("status", "missing_session")),
            message="verify.do succeeded but returned no sessionId",
        )
    session_id = str(session_raw)
    price = _routing_total_price(routing)
    currency = str(routing.get("currency") or "")
    offer_id = _offer_id_from_routing(
        routing, fallback=fallback_routing_identifier
    )
    price_changed = _price_changed_from_verify(body)
    return VerifyResult(
        offer_id=offer_id,
        session_id=session_id,
        verified=True,
        price=price,
        currency=currency,
        price_changed=price_changed,
        raw=dict(body),
    )


def _verify_result_from_offer_price(body: dict, *, offer_id: str) -> VerifyResult:
    """Map getOfferPrice.do Fulfilment response onto VerifyResult.

    Preserves OfferId from the response when present; otherwise echoes the
    request OfferId unchanged [E]. Ticketing-window fields (e.g. expireTime)
    stay in raw for later order/pay gates.
    """
    data = body.get("data")
    offer: dict = {}
    session_id = ""
    price = Decimal("0")
    currency = ""
    returned_id = offer_id

    if isinstance(data, list) and data:
        first = data[0] if isinstance(data[0], dict) else {}
        maybe_offer = first.get("offer") if isinstance(first.get("offer"), dict) else first
        if isinstance(maybe_offer, dict):
            offer = maybe_offer
        # Fulfilment issues OfferId, not sessionId; keep session empty.
        session_id = str(first.get("sessionId") or body.get("sessionId") or "")

    if offer:
        rid = offer.get("offerID") or offer.get("offerId") or offer.get("OfferId")
        if rid not in (None, ""):
            returned_id = str(rid)  # preserve Atlas OfferId unchanged
        pax_fares = offer.get("paxFares") or []
        if isinstance(pax_fares, list):
            for pf in pax_fares:
                if not isinstance(pf, dict):
                    continue
                if str(pf.get("paxType") or "").upper() != "ADT":
                    continue
                price_obj = pf.get("price") if isinstance(pf.get("price"), dict) else {}
                price = _decimal(price_obj.get("baseAmount")) + _decimal(
                    price_obj.get("taxes")
                )
                currency = str(price_obj.get("currency") or currency)
                break

    if not currency:
        currency = str(body.get("currency") or offer.get("currency") or "")

    return VerifyResult(
        offer_id=returned_id,
        session_id=session_id,
        verified=True,
        price=price,
        currency=currency,
        price_changed=False,
        raw=dict(body),
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

    async def verify(self, *, routing_identifier: str) -> VerifyResult:
        """POST verify.do with the Offer.routing_identifier from search [E].

        Required wire input is routingIdentifier (not offer_id / fid).
        routingIdentifier must be ≤6 hours old when verify is called [E].

        On success, Atlas issues a NEW sessionId on the verify response
        (valid ~2 hours for order.do) [E]. That sessionId is newly minted
        here — it is not echoed from search, which never returned one.

        Sets price_changed by comparing the verified price to the search
        price. Raises AtlasPriceMovedError only via verify_strict().
        """
        rid = str(routing_identifier or "").strip()
        if not rid:
            raise AtlasNoResultsError(
                code="missing_routing_identifier",
                message="verify requires a non-empty routingIdentifier from search",
            )

        # Wire field from resources.atriptech.com verify.do OpenAPI (required).
        body = await self._transport.post(
            "verify.do",
            {"routingIdentifier": rid},
        )
        return _verify_result_from_body(body, fallback_routing_identifier=rid)

    async def verify_strict(
        self, *, routing_identifier: str, expected_price: Decimal
    ) -> VerifyResult:
        """Like verify(), then raises AtlasPriceMovedError when the
        verified price differs from expected_price."""
        result = await self.verify(routing_identifier=routing_identifier)
        expected = _decimal(expected_price)
        if result.price != expected:
            raise AtlasPriceMovedError(
                code="price_moved",
                message=(
                    f"verified price {result.price} differs from "
                    f"expected {expected}"
                ),
                old_price=expected,
                new_price=result.price,
            )
        return result

    async def get_offer_price(self, *, offer_id: str) -> VerifyResult:
        """POST getOfferPrice.do. Preserves OfferId [E].

        Fulfilment path: Atlas returns offerID on the response; we carry it
        through unchanged on VerifyResult.offer_id. The 5-minute ticketing
        window is a Fulfilment/order constraint [E] — preserved in raw when
        Atlas surfaces deadline fields (e.g. expireTime).
        """
        oid = str(offer_id or "").strip()
        if not oid:
            raise AtlasNoResultsError(
                code="missing_offer_id",
                message="get_offer_price requires a non-empty OfferId",
            )
        # Preserve OfferId on the wire exactly as provided (do not invent
        # segment payloads here). Live OpenAPI also documents a segments-
        # based create path; OfferId preservation is the Task 5 contract.
        body = await self._transport.post("getOfferPrice.do", {"offerId": oid})
        return _verify_result_from_offer_price(body, offer_id=oid)

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
