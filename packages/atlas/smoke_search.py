"""Smoke: AtlasClient.search live + replay (Task 4 Verify)."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packages.atlas.cassette import CassettePlayer, CassetteRecorder
from packages.atlas.client import AtlasClient
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
    # Fixed relative future date so live→replay in one session share a cassette key.
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


def _print_result(result) -> None:
    print(
        f"SearchResult session_id={result.session_id!r} "
        f"offers={len(result.offers)} status={result.raw.get('status')!r}"
    )
    offer = result.offers[0]
    print(
        f"Offer[0] offer_id={offer.offer_id!r} "
        f"routing_identifier={offer.routing_identifier!r} "
        f"price={offer.price} {offer.currency} "
        f"stops={offer.stop_count} segs={len(offer.segments)}"
    )
    if not offer.offer_id or not offer.routing_identifier:
        raise SystemExit("FAIL: offer_id or routing_identifier empty")


async def main() -> int:
    file_env = _load_dotenv(ROOT / ".env")
    mode = _mode()
    request = _search_request()
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)

    if mode is ReboundMode.REPLAY:
        print("mode=replay")
        transport = ReplayTransport(CassettePlayer(CASSETTE_DIR, reproduce_latency=True))
    else:
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
            return 1
        print("mode=live")
        recorder = CassetteRecorder(CASSETTE_DIR)
        transport = LiveTransport(
            base_url,
            client_id,
            client_secret,
            recorder=recorder,
            timeout_seconds=60.0,
        )

    client = AtlasClient(transport)
    print(
        f"search {request.origin}->{request.destination} "
        f"on {request.departure_date.strftime('%Y%m%d')} …"
    )
    result = await client.search(request)
    _print_result(result)
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
