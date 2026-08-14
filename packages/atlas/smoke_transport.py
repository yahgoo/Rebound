"""Smoke: live POST → cassette → byte-identical replay (Task 3 Verify)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from packages.atlas.cassette import CassettePlayer, CassetteRecorder
from packages.atlas.transport import LiveTransport, ReplayTransport

ROOT = Path(__file__).resolve().parents[2]
CASSETTE_DIR = ROOT / "fixtures" / "cassettes"
TEST_PAN = "4111111111111111"


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


def _future_yyyymmdd(*, days_ahead: int = 30) -> str:
    return (date.today() + timedelta(days=days_ahead)).strftime("%Y%m%d")


async def main() -> int:
    file_env = _load_dotenv(ROOT / ".env")
    base_url = os.environ.get("ATLAS_BASE_URL") or file_env.get("ATLAS_BASE_URL")
    client_id = os.environ.get("ATLAS_CLIENT_ID") or file_env.get("ATLAS_CLIENT_ID")
    client_secret = (
        os.environ.get("ATLAS_CLIENT_SECRET") or file_env.get("ATLAS_CLIENT_SECRET")
    )
    if not base_url or not client_id or not client_secret:
        print("missing ATLAS_BASE_URL / ATLAS_CLIENT_ID / ATLAS_CLIENT_SECRET", file=sys.stderr)
        return 1

    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    recorder = CassetteRecorder(CASSETTE_DIR)

    # Prove I4 redaction: a pay-shaped payload with a test PAN must never hit disk raw.
    await recorder.record(
        path="pay.do",
        payload={
            "cid": client_id,
            "orderNo": "SMOKE-REDACT-ORDER",
            "cardNumber": TEST_PAN,
            "cvv": "123",
            "holder_given_name": "Test",
            "holder_surname": "User",
            "cardNum": "A12345678",
            "birthday": "19900101",
            "requestSource": "smoke-transport-redact",
        },
        response={"status": 0, "msg": "redaction-probe"},
        latency_ms=1,
    )

    path = "search.do"
    payload = {
        "cid": client_id,
        "tripType": "1",
        "adultNum": 1,
        "childNum": 0,
        "infantNum": 0,
        "fromCity": "JKT",
        "toCity": "SUB",
        "fromDate": _future_yyyymmdd(),
        "currency": "USD",
        "requestSource": "smoke-transport",
    }

    live = LiveTransport(
        base_url,
        client_id,
        client_secret,
        recorder=recorder,
        timeout_seconds=60.0,
    )
    print("live POST", path, "…")
    live_body = await live.post(path, payload)
    live_bytes = json.dumps(live_body, sort_keys=True, separators=(",", ":")).encode()
    print("live status=", live_body.get("status"), "bytes=", len(live_bytes))

    key = CassetteRecorder.key_for(path, payload)
    cassette_file = CASSETTE_DIR / f"{key}.json"
    if not cassette_file.is_file():
        print(f"cassette missing: {cassette_file}", file=sys.stderr)
        return 1
    print("cassette written:", cassette_file.relative_to(ROOT))

    player = CassettePlayer(CASSETTE_DIR, reproduce_latency=True)
    replay = ReplayTransport(player)
    print("replay POST", path, "(no network) …")
    replay_body = await replay.post(path, payload)
    replay_bytes = json.dumps(replay_body, sort_keys=True, separators=(",", ":")).encode()

    if live_bytes != replay_bytes:
        print("FAIL: replay payload is not byte-identical to live", file=sys.stderr)
        return 1

    # Confirm redaction cassette has no raw PAN.
    redact_key = CassetteRecorder.key_for(
        "pay.do",
        {
            "cid": client_id,
            "orderNo": "SMOKE-REDACT-ORDER",
            "cardNumber": TEST_PAN,
            "cvv": "123",
            "holder_given_name": "Test",
            "holder_surname": "User",
            "cardNum": "A12345678",
            "birthday": "19900101",
            "requestSource": "smoke-transport-redact",
        },
    )
    redact_text = (CASSETTE_DIR / f"{redact_key}.json").read_text(encoding="utf-8")
    if TEST_PAN in redact_text:
        print("FAIL: test PAN leaked into cassette", file=sys.stderr)
        return 1

    print("OK: live + replay byte-identical; PAN redacted from cassette")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
