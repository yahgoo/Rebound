"""Atlas client. Task 4–7: search → pay + queryOrderDetails + poll."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol

from packages.atlas.errors import (
    AtlasError,
    AtlasNoResultsError,
    AtlasPaymentDeclinedError,
    AtlasPriceMovedError,
    AtlasThreeDSRequiredError,
    AtlasTimeoutError,
)
from packages.atlas.models import (
    CardDetails,
    Offer,
    OrderDetails,
    OrderResult,
    Passenger,
    PayResult,
    SearchRequest,
    SearchResult,
    Segment,
    VerifyResult,
)
from packages.domain.enums import ChaosProfile

_log = logging.getLogger(__name__)

# Atlas queryOrderDetails.do orderStatus → domain status string [E].
_ORDER_STATUS_NAMES: dict[str, str] = {
    "0": "unpaid",
    "1": "ticketing_in_process",
    "2": "ticketed",
    "-3": "cancelled",
}

# Atlas SGT (GMT+8) for tktLimitTime [E].
_SGT = timezone(timedelta(hours=8))

# Wire / model keys that must never appear in persisted or exception payloads (I4).
_CARD_SECRET_KEYS = frozenset(
    {
        "number",
        "cardnumber",
        "card_number",
        "pan",
        "cvv",
        "cvc",
        "cardcvv",
        "securitycode",
        "security_code",
        "holder_given_name",
        "holder_surname",
        "holdername",
        "holder_name",
        "cardholder",
        "cardholdername",
        "card_holder",
        "card_holder_name",
        "cardholderfirstname",
        "cardholderlastname",
        "cardholdermiddle",
        "creditcard",
    }
)

_PAN_SHAPE = re.compile(r"(?<!\d)(\d{13,19})(?!\d)")


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


def _norm_key(key: str) -> str:
    return key.replace("-", "").replace("_", "").lower()


def _is_card_secret_key(key: str) -> bool:
    return _norm_key(key) in _CARD_SECRET_KEYS or key.lower() in _CARD_SECRET_KEYS


def _strip_card_secrets(obj: Any) -> Any:
    """Deep-copy with card fields removed — safe for cassette / logs (I4)."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if _is_card_secret_key(str(k)):
                continue
            out[str(k)] = _strip_card_secrets(v)
        return out
    if isinstance(obj, list):
        return [_strip_card_secrets(v) for v in obj]
    return obj


def _assert_no_card_secrets(obj: Any, *, context: str) -> None:
    """Raise if PAN-shaped digits or card keys remain (I4 — assert in code)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _is_card_secret_key(str(k)):
                raise AssertionError(f"I4: card field {k!r} leaked into {context}")
            _assert_no_card_secrets(v, context=context)
        return
    if isinstance(obj, list):
        for v in obj:
            _assert_no_card_secrets(v, context=context)
        return
    if isinstance(obj, str):
        for match in _PAN_SHAPE.finditer(obj):
            digits = match.group(1)
            # Luhn-valid runs are PANs; refuse them in any persisted/exception text.
            if _luhn_ok(digits):
                raise AssertionError(f"I4: PAN-shaped value leaked into {context}")


def _luhn_ok(digits: str) -> bool:
    total = 0
    reverse = digits[::-1]
    for i, ch in enumerate(reverse):
        n = ord(ch) - 48
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _passenger_wire_name(p: Passenger) -> str:
    # Atlas: Family Name/Given Name [E].
    return f"{p.surname.strip()}/{p.given_name.strip()}"


def _passenger_to_wire(p: Passenger) -> dict:
    body: dict[str, Any] = {
        "name": _passenger_wire_name(p),
        "passengerType": 0,  # adult — Task 6 smoke is one adult
        "gender": "M",  # required on wire; not on Passenger model
        "birthday": p.date_of_birth.strftime("%Y%m%d"),
    }
    if p.nationality:
        body["nationality"] = p.nationality.upper()
    if p.passport_number:
        body["cardType"] = "PP"
        body["cardNum"] = p.passport_number
        if p.nationality:
            body["cardIssuePlace"] = p.nationality.upper()
        # Expiry required by some carriers when a document is present [E].
        body["cardExpired"] = "20351231"
    return body


def _parse_tkt_limit_time(raw: object) -> datetime | None:
    """Parse order.do tktLimitTime (`yyyy-MM-dd HH:mm:ss` SGT) to UTC [E]."""
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            local = datetime.strptime(text, fmt).replace(tzinfo=_SGT)
            return local.astimezone(UTC)
        except ValueError:
            continue
    try:
        return _atlas_local_dt(text)
    except ValueError:
        return None


def _order_result_from_body(body: dict) -> OrderResult:
    order_no = body.get("orderNo")
    if order_no in (None, ""):
        raise AtlasNoResultsError(
            code=str(body.get("status", "missing_order_no")),
            message="order.do succeeded but returned no orderNo",
        )
    total = _decimal(body.get("totalPrice")) + _decimal(body.get("totalTransactionFee"))
    currency = str(body.get("currency") or "")
    status = str(body.get("status", "0"))
    return OrderResult(
        order_no=str(order_no),
        status=status,
        ticketing_deadline=_parse_tkt_limit_time(body.get("tktLimitTime")),
        total_amount=total,
        currency=currency,
        raw=dict(body),
    )


def _card_to_credit_card(card: CardDetails) -> dict:
    # pay.do creditCard fields from resources.atriptech.com OpenAPI [E].
    year = int(card.expiry_year)
    yy = year % 100 if year >= 100 else year
    return {
        "cardNumber": str(card.number),
        "cardCVV": str(card.cvv),
        "cardExpireMonth": f"{int(card.expiry_month):02d}",
        "cardExpireYear": f"{yy:02d}",
        "cardHolderLastName": card.holder_surname,
        "cardHolderFirstName": card.holder_given_name,
    }


def _ticket_numbers_from_pay(body: dict) -> list[str]:
    tickets: list[str] = []
    for key in ("ticketNos", "ticketNumbers", "ticket_numbers"):
        raw = body.get(key)
        if isinstance(raw, list):
            tickets.extend(str(t) for t in raw if t not in (None, ""))
    pax_infos = body.get("paxTicketInfos")
    if isinstance(pax_infos, list):
        for info in pax_infos:
            if not isinstance(info, dict):
                continue
            for t in info.get("ticketNos") or []:
                if t not in (None, ""):
                    tickets.append(str(t))
    # Preserve order, drop dupes.
    seen: set[str] = set()
    out: list[str] = []
    for t in tickets:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _pnr_from_pay(body: dict) -> str | None:
    for key in ("pnr", "pnrCode", "airlinePnr"):
        val = body.get(key)
        if val not in (None, ""):
            return str(val)
    return None


def _payment_methods_from_order(body: dict) -> list[int]:
    options = body.get("paymentOptions")
    methods: list[int] = []
    if isinstance(options, list):
        for opt in options:
            if not isinstance(opt, dict):
                continue
            pm = opt.get("paymentMethod")
            if pm is None:
                continue
            try:
                methods.append(int(pm))
            except (TypeError, ValueError):
                continue
    routing = body.get("routing") if isinstance(body.get("routing"), dict) else {}
    for pm in routing.get("supportPaymentMethods") or []:
        try:
            methods.append(int(pm))
        except (TypeError, ValueError):
            continue
    # Stable unique.
    return list(dict.fromkeys(methods))


def _order_status_name(raw: object) -> str:
    """Map Atlas orderStatus codes to names; unknown codes pass through [E]."""
    if raw in (None, ""):
        return ""
    code = str(raw)
    return _ORDER_STATUS_NAMES.get(code, code)


def _pnr_from_order_details(body: dict) -> str | None:
    for key in ("pnrCode", "pnr", "airlinePnr"):
        val = body.get(key)
        if val not in (None, ""):
            return str(val)
    bookings = body.get("airlineBookings")
    if isinstance(bookings, list):
        for b in bookings:
            if not isinstance(b, dict):
                continue
            pnr = b.get("airlinePnr")
            if pnr not in (None, ""):
                return str(pnr)
    pax_infos = body.get("paxTicketInfos")
    if isinstance(pax_infos, list):
        for info in pax_infos:
            if not isinstance(info, dict):
                continue
            for pnr in info.get("airlinePNRs") or []:
                if pnr not in (None, ""):
                    return str(pnr)
    return None


def _segments_from_routing(routing: dict) -> list[Segment]:
    from_segments = routing.get("fromSegments") or []
    ret_segments = routing.get("retSegments") or []
    if not isinstance(from_segments, list):
        from_segments = []
    if not isinstance(ret_segments, list):
        ret_segments = []
    segments = [_segment_from_atlas(s) for s in from_segments if isinstance(s, dict)]
    segments.extend(_segment_from_atlas(s) for s in ret_segments if isinstance(s, dict))
    return segments


def _order_details_from_body(body: dict, *, requested_order_no: str) -> OrderDetails:
    """Map queryOrderDetails.do onto OrderDetails. Status from orderStatus only (I7)."""
    order_raw = body.get("orderNo")
    if order_raw not in (None, ""):
        order_no = str(order_raw)
    else:
        # Sandbox may return status=0 with null fields for unknown orders [E observed].
        order_no = requested_order_no

    routing = body.get("routing") if isinstance(body.get("routing"), dict) else {}
    total = _decimal(body.get("totalPrice")) + _decimal(body.get("totalTransactionFee"))
    currency = str(body.get("currency") or "")

    return OrderDetails(
        order_no=order_no,
        status=_order_status_name(body.get("orderStatus")),
        pnr=_pnr_from_order_details(body),
        ticket_numbers=_ticket_numbers_from_pay(body),
        segments=_segments_from_routing(routing),
        total_amount=total,
        currency=currency,
        raw=dict(body),
    )


class AtlasClient:
    def __init__(
        self,
        transport: AtlasTransport,
        *,
        chaos: ChaosProfile = ChaosProfile.NONE,
    ) -> None:
        self._transport = transport
        self._chaos = chaos
        # (session_id, offer_id) pairs from successful verify.do only (I2).
        self._verified: set[tuple[str, str]] = set()
        self._last_order_payment_methods: list[int] = []

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
        result = _verify_result_from_body(body, fallback_routing_identifier=rid)
        # I2 gate: only verify-issued session_id + offer_id may unlock order.do.
        self._verified.add((result.session_id, result.offer_id))
        return result

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

    async def _post_pay_isolated(self, payload_full: dict) -> dict:
        """POST pay.do: Atlas sees card data; cassette/logs never do (I4)."""
        persisted = _strip_card_secrets(deepcopy(payload_full))
        _assert_no_card_secrets(persisted, context="pay cassette/log payload")

        recorder = getattr(self._transport, "recorder", None)
        if recorder is not None:
            # LiveTransport records the same dict it posts — disable that, then
            # record only the card-stripped copy so CVV/PAN never reach disk.
            self._transport.recorder = None
            try:
                started = time.perf_counter()
                body = await self._transport.post("pay.do", payload_full)
                latency_ms = max(0, int((time.perf_counter() - started) * 1000))
            finally:
                self._transport.recorder = recorder
            safe_response = _strip_card_secrets(deepcopy(body))
            _assert_no_card_secrets(safe_response, context="pay cassette response")
            await recorder.record(
                path="pay.do",
                payload=persisted,
                response=safe_response,
                latency_ms=latency_ms,
            )
            return body

        # ReplayTransport keys on payload — use the same stripped shape we record.
        if type(self._transport).__name__ == "ReplayTransport":
            return await self._transport.post("pay.do", persisted)

        return await self._transport.post("pay.do", payload_full)

    async def order(
        self,
        *,
        session_id: str,
        offer_id: str,
        passengers: list[Passenger],
        contact_email: str,
        contact_phone: str,
    ) -> OrderResult:
        """POST order.do. Caller MUST have a successful verify first (I2).

        session_id MUST be the sessionId newly issued by verify.do
        (not a search-time value — search.do does not return one) [E].
        """
        sid = str(session_id or "").strip()
        oid = str(offer_id or "").strip()
        if not sid or not oid:
            raise AtlasError(
                code="unverified_offer",
                message=(
                    "order.do refused: session_id and offer_id from a "
                    "successful verify.do are required (I2)"
                ),
            )
        if (sid, oid) not in self._verified:
            raise AtlasError(
                code="unverified_offer",
                message=(
                    "order.do refused: session_id/offer_id were not issued by "
                    "a successful verify.do on this client (I2)"
                ),
            )
        if not passengers:
            raise AtlasError(
                code="missing_passengers",
                message="order.do requires at least one passenger",
            )

        # Wire fields from create-order.md OpenAPI [E].
        # verify path: sessionId is required; offerId is for the get-offer path
        # only — do not send search fid as offerId (causes Atlas 9999).
        contact_name = _passenger_wire_name(passengers[0])
        payload: dict[str, Any] = {
            "sessionId": sid,
            "passengers": [_passenger_to_wire(p) for p in passengers],
            "contact": {
                "name": contact_name,
                "email": contact_email,
                "mobile": contact_phone,
            },
        }

        body = await self._transport.post("order.do", payload)
        result = _order_result_from_body(body)
        self._last_order_payment_methods = _payment_methods_from_order(body)
        return result

    async def pay(self, *, order_no: str, card: CardDetails) -> PayResult:
        """POST pay.do. Raises AtlasPaymentDeclinedError on 604 and
        AtlasThreeDSRequiredError on 616 [E]. Card data never logged (I4)."""
        ono = str(order_no or "").strip()
        if not ono:
            raise AtlasError(
                code="missing_order_no",
                message="pay.do requires a non-empty orderNo",
            )
        if not isinstance(card, CardDetails):
            raise TypeError("pay requires CardDetails")

        # I4: repr/str must already redact; assert before any I/O or raise path.
        _assert_no_card_secrets(repr(card), context="CardDetails.repr")
        _assert_no_card_secrets(str(card), context="CardDetails.str")

        methods = self._last_order_payment_methods
        # Prefer VCC (3) / MoR (5) when the order supports card; else deposit (1).
        # Current sandbox JKT→SUB fares are deposit-only [E observed].
        if 3 in methods:
            payment_method = 3
        elif 5 in methods:
            payment_method = 5
        elif 1 in methods:
            payment_method = 1
        else:
            payment_method = 3

        payload: dict[str, Any] = {
            "orderNo": ono,
            "paymentMethod": payment_method,
        }
        if payment_method in {3, 5}:
            payload["creditCard"] = _card_to_credit_card(card)
        else:
            # Deposit: do not put card fields on the wire or cassette (I4).
            payload["creditCard"] = None

        try:
            body = await self._post_pay_isolated(payload)
        except (AtlasPaymentDeclinedError, AtlasThreeDSRequiredError) as exc:
            # Surface code on a PayResult attached to the exception (INTERFACES).
            _assert_no_card_secrets(exc.message, context="pay exception message")
            _assert_no_card_secrets(repr(card), context="pay exception card repr")
            exc.pay_result = PayResult(  # type: ignore[attr-defined]
                order_no=ono,
                paid=False,
                ticket_numbers=[],
                pnr=None,
                error_code=str(exc.code),
                raw={},
            )
            raise

        code = str(body.get("status", body.get("errorCode", "0")))
        if code == "604":
            pay_result = PayResult(
                order_no=ono,
                paid=False,
                ticket_numbers=[],
                pnr=None,
                error_code="604",
                raw=dict(body),
            )
            err = AtlasPaymentDeclinedError(
                code="604",
                message=str(body.get("msg") or "Payment declined"),
            )
            err.pay_result = pay_result  # type: ignore[attr-defined]
            raise err
        if code == "616":
            pay_result = PayResult(
                order_no=ono,
                paid=False,
                ticket_numbers=[],
                pnr=None,
                error_code="616",
                raw=dict(body),
            )
            err = AtlasThreeDSRequiredError(
                code="616",
                message=str(body.get("msg") or "3DS required"),
            )
            err.pay_result = pay_result  # type: ignore[attr-defined]
            raise err

        return PayResult(
            order_no=str(body.get("orderNo") or ono),
            paid=code in {"0", "00"},
            ticket_numbers=_ticket_numbers_from_pay(body),
            pnr=_pnr_from_pay(body),
            error_code=None if code in {"0", "00"} else code,
            raw=dict(body),
        )

    async def query_order_details(self, *, order_no: str) -> OrderDetails:
        """POST queryOrderDetails.do. Authoritative state (I7)."""
        ono = str(order_no or "").strip()
        if not ono:
            raise AtlasError(
                code="missing_order_no",
                message="queryOrderDetails.do requires a non-empty orderNo",
            )
        # Wire field from resources.atriptech.com query-order OpenAPI [E].
        body = await self._transport.post("queryOrderDetails.do", {"orderNo": ono})
        return _order_details_from_body(body, requested_order_no=ono)

    async def poll_order_until(
        self,
        *,
        order_no: str,
        terminal_statuses: set[str],
        timeout_seconds: int = 120,
        interval_seconds: float = 3.0,
    ) -> OrderDetails:
        """Poll until status is terminal or timeout. The webhook safety net (I7).

        Cancellation-safe: each attempt is a discrete query_order_details call;
        only asyncio.sleep runs between attempts (no DB/session held across sleeps).
        """
        ono = str(order_no or "").strip()
        if not ono:
            raise AtlasError(
                code="missing_order_no",
                message="poll_order_until requires a non-empty orderNo",
            )
        if not terminal_statuses:
            raise AtlasError(
                code="missing_terminal_statuses",
                message="poll_order_until requires a non-empty terminal_statuses set",
            )

        deadline = time.monotonic() + float(timeout_seconds)
        last: OrderDetails | None = None
        while True:
            # Fresh call each iteration — never hold resources across the sleep (I7).
            last = await self.query_order_details(order_no=ono)
            _log.debug("order_no=%s status=%s", last.order_no, last.status)
            if last.status in terminal_statuses:
                return last

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Sleep is cancellable; CancelledError propagates to the caller.
            await asyncio.sleep(min(float(interval_seconds), remaining))
            if time.monotonic() >= deadline:
                break

        raise AtlasTimeoutError(
            code="timeout",
            message=(
                f"order {ono!r} did not reach {sorted(terminal_statuses)!r} "
                f"within {timeout_seconds}s"
                + (f" (last status={last.status!r})" if last is not None else "")
            ),
        )
