"""Smoke: LocalExecutor vs DaytonaExecutor ranking parity (Task 12 Verify).

Deliberate exception to Task 12's file allowlist: the Verify block requires
`python -m packages.executors.smoke_parity`, which cannot run without this module.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from daytona import AsyncDaytona, DaytonaConfig

from packages.executors.daytona import DaytonaExecutor
from packages.executors.local import LocalExecutor
from packages.executors.smoke_local import SCORING_CODE, _fixtures


def _load_daytona_key() -> str:
    key = os.environ.get("DAYTONA_API_KEY")
    if key:
        return key
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == "DAYTONA_API_KEY":
                return v.strip().strip('"').strip("'")
    raise SystemExit("DAYTONA_API_KEY missing — required for smoke_parity")


async def _list_survivors(api_key: str) -> list[dict[str, str]]:
    client = AsyncDaytona(DaytonaConfig(api_key=api_key))
    try:
        out: list[dict[str, str]] = []
        async for sb in client.list():
            state = str(getattr(sb, "state", ""))
            # Destroyed/deleted entries (if any) do not count as survivors.
            if "DESTROY" in state.upper() or "DELETED" in state.upper():
                continue
            out.append({"id": str(sb.id), "state": state})
        return out
    finally:
        await client.close()


async def main() -> int:
    payload = _fixtures()
    api_key = _load_daytona_key()
    print(f"candidates={len(payload.candidates)} target_slots=8")

    local = LocalExecutor(target_slots=8, timeout_seconds=20)
    local_ranked = await local.score(payload, SCORING_CODE)
    await local.close()
    local_ids = [c.offer_id for c in local_ranked]

    daytona = DaytonaExecutor(api_key, target_slots=8, timeout_seconds=60)
    daytona_ranked = await daytona.score(payload, SCORING_CODE)
    await daytona.close()
    daytona_ids = [c.offer_id for c in daytona_ranked]

    print("RANKED offer_id lists (LocalExecutor | DaytonaExecutor):")
    width = max(len(local_ids), len(daytona_ids))
    for i in range(width):
        left = local_ids[i] if i < len(local_ids) else "-"
        right = daytona_ids[i] if i < len(daytona_ids) else "-"
        mark = "OK" if left == right else "DIFF"
        print(f"  {i+1:2d}. {left:12s} | {right:12s}  [{mark}]")

    print("LOCAL_OFFER_IDS  =", local_ids)
    print("DAYTONA_OFFER_IDS=", daytona_ids)

    survivors: list[dict[str, str]] = []
    for attempt in range(8):
        survivors = await _list_survivors(api_key)
        print(f"SURVIVING_SANDBOXES attempt={attempt+1} count={len(survivors)} detail={survivors}")
        if not survivors:
            break
        await asyncio.sleep(1.0)

    if local_ids != daytona_ids:
        print("PARITY FAIL")
        return 1
    if survivors:
        print("SANDBOX CLEANUP FAIL")
        return 1

    print("PARITY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
