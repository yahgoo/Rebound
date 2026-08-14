"""Smoke: queryOrderDetails.do + poll_order_until timeout (Task 7 Verify)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from packages.atlas.cassette import CassettePlayer, CassetteRecorder
from packages.atlas.client import AtlasClient
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


async def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].strip():
        print(
            "usage: python -m packages.atlas.smoke_poll <order_no>",
            file=sys.stderr,
        )
        return 1

    order_no = argv[1].strip()
    file_env = _load_dotenv(ROOT / ".env")
    mode = _mode()
    client = _build_client(mode, file_env)

    # One debug line per poll attempt (order_no + status only) when level allows.
    logging.getLogger("packages.atlas.client").setLevel(logging.DEBUG)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    print(f"query_order_details order_no={order_no!r} …")
    details = await client.query_order_details(order_no=order_no)
    print(
        f"authoritative status={details.status!r} "
        f"order_no={details.order_no!r} "
        f"pnr={details.pnr!r} "
        f"ticket_numbers={details.ticket_numbers!r} "
        f"segments={len(details.segments)} "
        f"total={details.total_amount} {details.currency}"
    )

    # Also prove poll_order_until returns promptly when already terminal.
    if details.status == "ticketed":
        polled = await client.poll_order_until(
            order_no=order_no,
            terminal_statuses={"ticketed"},
            interval_seconds=1,
            timeout_seconds=5,
        )
        print(f"poll_order_until terminal ok status={polled.status!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
