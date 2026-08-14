"""Smoke: AtlasClient.verify live + verify_strict price-moved (Task 5 Verify)."""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from packages.atlas.cassette import CassettePlayer, CassetteRecorder
from packages.atlas.client import AtlasClient
from packages.atlas.errors import AtlasPriceMovedError
from packages.atlas.models import SearchRequest
from packages.atlas.transport import LiveTransport, ReplayTransport
from packages.domain.enums import ReboundMode

ROOT = Path(__file__).resolve().parents[2]
CASSETTE_DIR = ROOT / "fixtures" / "cassettes"


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
    # Fresh search so routingIdentifier is ≤6h old for verify [E].
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
        timeout_seconds=60.0,
    )
    return AtlasClient(transport)


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
    offer = search.offers[0]
    print(
        f"Offer[0] offer_id={offer.offer_id!r} "
        f"routing_identifier={offer.routing_identifier!r} "
        f"search_price={offer.price} {offer.currency} "
        f"search_session_id={search.session_id!r}"
    )
    if not offer.routing_identifier:
        print("FAIL: empty routing_identifier", file=sys.stderr)
        return 1

    print(f"verify routing_identifier={offer.routing_identifier!r} …")
    verified = await client.verify(routing_identifier=offer.routing_identifier)
    print(
        f"verified={verified.verified} price={verified.price} {verified.currency} "
        f"price_changed={verified.price_changed} "
        f"session_id={verified.session_id!r} offer_id={verified.offer_id!r}"
    )

    if not verified.verified:
        print("FAIL: verified is False", file=sys.stderr)
        return 1
    if not verified.session_id:
        print("FAIL: empty session_id from verify", file=sys.stderr)
        return 1
    # sessionId must be newly minted by verify — search never issues one [E].
    if search.session_id and verified.session_id == search.session_id:
        print(
            "FAIL: verify session_id equals search session_id "
            "(expected newly issued)",
            file=sys.stderr,
        )
        return 1
    if verified.session_id == offer.routing_identifier:
        print(
            "FAIL: session_id equals routing_identifier "
            "(session must be newly issued)",
            file=sys.stderr,
        )
        return 1
    if verified.session_id == offer.offer_id:
        print(
            "FAIL: session_id equals offer_id (session must be newly issued)",
            file=sys.stderr,
        )
        return 1

    print("OK happy path")

    # Negative path: deliberately wrong expected_price must raise.
    wrong = verified.price + Decimal("1.00")
    print(f"verify_strict expected_price={wrong} (deliberately wrong) …")
    try:
        await client.verify_strict(
            routing_identifier=offer.routing_identifier,
            expected_price=wrong,
        )
    except AtlasPriceMovedError as exc:
        print("AtlasPriceMovedError raised as expected:")
        traceback.print_exc()
        print(f"old_price={exc.old_price} new_price={exc.new_price}")
        if exc.old_price != wrong:
            print(
                f"FAIL: AtlasPriceMovedError.old_price={exc.old_price} "
                f"expected {wrong}",
                file=sys.stderr,
            )
            return 1
        if exc.new_price == wrong:
            print(
                "FAIL: AtlasPriceMovedError.new_price equals deliberate wrong price",
                file=sys.stderr,
            )
            return 1
        print("OK price-moved path")
        return 0

    print("FAIL: verify_strict did not raise AtlasPriceMovedError", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
