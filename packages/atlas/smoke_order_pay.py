"""Smoke: search → verify → order → pay (Task 6 Verify)."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packages.atlas.cassette import CassettePlayer, CassetteRecorder
from packages.atlas.client import AtlasClient
from packages.atlas.errors import AtlasError
from packages.atlas.models import CardDetails, Passenger, SearchRequest
from packages.atlas.transport import LiveTransport, ReplayTransport
from packages.domain.enums import ReboundMode

ROOT = Path(__file__).resolve().parents[2]
CASSETTE_DIR = ROOT / "fixtures" / "cassettes"

# Sandbox Visa from Atlas pay.do docs [E] — never print the full PAN.
_SANDBOX_VISA = "4532015112830366"


def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def _mode() -> ReboundMode:
    raw = (os.environ.get("REBOUND_MODE") or "live").strip().lower()
    try:
        return ReboundMode(raw)
    except ValueError:
        return ReboundMode.LIVE


def _search_request() -> SearchRequest:
    departure = datetime.now(tz=UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=30)
    return SearchRequest(
        origin="JKT",
        destination="SUB",
        departure_date=departure,
        adults=1,
        children=0,
        infants=0,
        currency="USD",
    )


def _build_client(mode: ReboundMode, file_env: dict[str, str]) -> AtlasClient:
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    if mode is ReboundMode.REPLAY:
        print("mode=replay")
        transport = ReplayTransport(
            CassettePlayer(CASSETTE_DIR, reproduce_latency=True)
        )
        return AtlasClient(transport)

    base_url = os.environ.get("ATLAS_BASE_URL") or file_env.get("ATLAS_BASE_URL")
    client_id = os.environ.get("ATLAS_CLIENT_ID") or file_env.get("ATLAS_CLIENT_ID")
    client_secret = (
        os.environ.get("ATLAS_CLIENT_SECRET") or file_env.get("ATLAS_CLIENT_SECRET")
    )
    if not base_url or not client_id or not client_secret:
        print(
            "missing ATLAS_BASE_URL / ATLAS_CLIENT_ID / ATLAS_CLIENT_SECRET",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print("mode=live")
    recorder = CassetteRecorder(CASSETTE_DIR)
    transport = LiveTransport(
        base_url,
        client_id,
        client_secret,
        recorder=recorder,
        timeout_seconds=90.0,
    )
    return AtlasClient(transport)


def _pick_offer(search_offers: list) -> object:
    """Prefer a VCC-capable fare when present; else first offer (deposit-only)."""
    for offer in search_offers:
        methods = offer.raw.get("supportPaymentMethods") or []
        try:
            methods_i = {int(m) for m in methods}
        except (TypeError, ValueError):
            methods_i = set()
        if 3 in methods_i or 5 in methods_i:
            return offer
    return search_offers[0]


async def main() -> int:
    file_env = _load_dotenv(ROOT / ".env")
    mode = _mode()
    client = _build_client(mode, file_env)
    request = _search_request()

    print(
        f"search {request.origin}->{request.destination} "
        f"on {request.departure_date.strftime('%Y%m%d')} …"
    )
    search = await client.search(request)
    offer = _pick_offer(search.offers)
    print(
        f"Offer offer_id={offer.offer_id!r} "
        f"routing_identifier={offer.routing_identifier!r} "
        f"price={offer.price} {offer.currency} "
        f"search_session_id={search.session_id!r} "
        f"supportPaymentMethods={offer.raw.get('supportPaymentMethods')!r}"
    )

    # I2 negative: order without verify must refuse.
    try:
        await client.order(
            session_id=search.session_id or "not-from-verify",
            offer_id=offer.offer_id,
            passengers=[
                Passenger(
                    given_name="Test",
                    surname="Passenger",
                    date_of_birth=datetime(1990, 1, 15, tzinfo=UTC),
                )
            ],
            contact_email="rebound.smoke@example.com",
            contact_phone="0065-91234567",
        )
        print("FAIL: order accepted unverified session/offer", file=sys.stderr)
        return 1
    except AtlasError as exc:
        print(f"OK refused unverified order: [{exc.code}] {exc.message}")

    print(f"verify routing_identifier={offer.routing_identifier!r} …")
    verified = await client.verify(routing_identifier=offer.routing_identifier)
    print(
        f"verified={verified.verified} price={verified.price} {verified.currency} "
        f"session_id={verified.session_id!r} offer_id={verified.offer_id!r}"
    )
    if not verified.session_id:
        print("FAIL: empty verify session_id", file=sys.stderr)
        return 1
    if search.session_id and verified.session_id == search.session_id:
        print(
            "FAIL: verify session_id equals search session_id",
            file=sys.stderr,
        )
        return 1

    passenger = Passenger(
        given_name="Test",
        surname="Passenger",
        date_of_birth=datetime(1990, 1, 15, tzinfo=UTC),
        passport_number="A12345678",
        nationality="SG",
    )
    print(
        f"order session_id=<verify-issued> offer_id={verified.offer_id!r} …"
    )
    ordered = await client.order(
        session_id=verified.session_id,
        offer_id=verified.offer_id,
        passengers=[passenger],
        contact_email="rebound.smoke@example.com",
        contact_phone="0065-91234567",
    )
    atlas_pnr = ordered.raw.get("pnrCode")
    print(
        f"order_no={ordered.order_no!r} status={ordered.status!r} "
        f"total={ordered.total_amount} {ordered.currency} "
        f"pnrCode={atlas_pnr!r} "
        f"ticketing_deadline={ordered.ticketing_deadline!r} "
        f"payment_methods={client._last_order_payment_methods!r}"
    )
    if not ordered.order_no:
        print("FAIL: empty order_no", file=sys.stderr)
        return 1

    card = CardDetails(
        holder_given_name="Test",
        holder_surname="User",
        number=_SANDBOX_VISA,
        expiry_month=12,
        expiry_year=2030,
        cvv="123",
    )
    print(f"pay order_no={ordered.order_no!r} card={card!r} …")
    paid = await client.pay(order_no=ordered.order_no, card=card)
    print(
        f"paid={paid.paid} error_code={paid.error_code!r} "
        f"ticket_numbers={paid.ticket_numbers!r} pnr={paid.pnr!r}"
    )

    issued = None
    if paid.ticket_numbers:
        issued = f"ticket={paid.ticket_numbers[0]}"
    elif paid.pnr:
        issued = f"pnr={paid.pnr}"
    elif atlas_pnr:
        issued = f"pnrCode={atlas_pnr}"
    if not issued:
        print(
            "FAIL: no ticket number or PNR after order/pay",
            file=sys.stderr,
        )
        return 1
    if not paid.paid:
        print("FAIL: pay did not report paid=True", file=sys.stderr)
        return 1

    # Confirm 604/616 mapping exists on the client/transport path (not triggered).
    assert hasattr(AtlasClient.pay, "__call__")
    from packages.atlas.transport import raise_for_atlas_response

    try:
        raise_for_atlas_response({"status": 604, "msg": "probe"})
    except Exception as exc:  # noqa: BLE001 — probe only
        assert type(exc).__name__ == "AtlasPaymentDeclinedError", type(exc)
        assert exc.code == "604"
    try:
        raise_for_atlas_response({"status": 616, "msg": "probe"})
    except Exception as exc:  # noqa: BLE001 — probe only
        assert type(exc).__name__ == "AtlasThreeDSRequiredError", type(exc)
        assert exc.code == "616"
    print("OK error mapping 604→AtlasPaymentDeclinedError, 616→AtlasThreeDSRequiredError")

    print(f"OK search→verify→order→pay issued {issued}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
