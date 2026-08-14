"""Smoke: LocalExecutor scores 12 fixtures across 8 slots (Task 11 Verify).

Deliberate exception to Task 11's file allowlist: the Verify block requires
`python -m packages.executors.smoke_local`, which cannot run without this module.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from packages.executors.base import CandidateForScoring, SandboxStatus, ScoringInput
from packages.executors.local import LocalExecutor

# Hand-written stdlib-only scoring function (model-codegen comes later).
SCORING_CODE = r"""
def score(payload: dict) -> list[dict]:
    budget = float(payload["budget_ceiling_sgd"])
    weight = float(payload["mobility_penalty_weight"])
    out = []
    for c in payload["candidates"]:
        price = float(c["price"])
        stops = int(c["stop_count"])
        transfer = c.get("min_transfer_minutes")
        transfer_m = int(transfer) if transfer is not None else 90
        # Prefer cheaper, fewer stops, shorter self-transfer.
        price_fit = max(0.0, 1.0 - (price / budget))
        stop_pen = 0.15 * stops
        self_transfer_risk = 0.0 if stops == 0 else min(1.0, 40.0 / max(transfer_m, 1))
        mobility_fit = max(0.0, 1.0 - weight * self_transfer_risk)
        suffix = int("".join(ch for ch in c["offer_id"] if ch.isdigit()) or "0")
        total = (
            0.55 * price_fit
            + 0.25 * mobility_fit
            + 0.20 * (1.0 - self_transfer_risk)
            - stop_pen
            + suffix * 1e-6
        )
        out.append(
            {
                "offer_id": c["offer_id"],
                "score": float(total),
                "components": {
                    "price_fit": float(price_fit),
                    "mobility_fit": float(mobility_fit),
                    "stop_penalty": float(stop_pen),
                },
                "self_transfer_risk": float(self_transfer_risk),
                "mobility_fit": float(mobility_fit),
            }
        )
    # Inject a fabricated offer_id that must be silently dropped by the executor.
    out.append(
        {
            "offer_id": "FABRICATED-NOT-IN-INPUT",
            "score": 9999.0,
            "components": {"cheat": 9999.0},
            "self_transfer_risk": 0.0,
            "mobility_fit": 1.0,
        }
    )
    return out
"""


def _fixtures() -> ScoringInput:
    base_arr = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)
    original = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    candidates: list[CandidateForScoring] = []
    for i in range(12):
        candidates.append(
            CandidateForScoring(
                offer_id=f"OFFER-{i:02d}",
                price=Decimal(str(200 + i * 35)),
                currency="USD",
                arrival_at=base_arr + timedelta(hours=i),
                stop_count=i % 3,
                min_transfer_minutes=None if i % 3 == 0 else 45 + 5 * i,
                origin="SIN",
                destination="BKK",
                carriers=["SQ"] if i % 2 == 0 else ["FD", "VQ"],
            )
        )
    return ScoringInput(
        case_ref="RC-SMOKE-11",
        candidates=candidates,
        must_arrive_by=datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
        budget_ceiling_sgd=Decimal("800"),
        mobility_penalty_weight=0.4,
        original_arrival_at=original,
    )


async def main() -> int:
    payload = _fixtures()
    print(f"candidates={len(payload.candidates)} target_slots=8")

    # What score() itself would return (including fabricated) — BEFORE filter.
    ns: dict = {}
    exec(SCORING_CODE, ns, ns)
    before_raw = ns["score"](payload.model_dump(mode="json"))
    before_ids = [r["offer_id"] for r in before_raw]
    print("BEFORE filter (raw score() offer_ids):")
    print(before_ids)

    transitions: dict[int, list[str]] = {i: [] for i in range(8)}

    async def on_status(status: SandboxStatus) -> None:
        transitions[status.slot].append(status.state)
        print(
            f"status slot={status.slot} state={status.state} "
            f"sandbox_id={status.sandbox_id!r} elapsed_ms={status.elapsed_ms}"
        )

    ex = LocalExecutor(target_slots=8, timeout_seconds=20)
    ranked = await ex.score(payload, SCORING_CODE, on_status=on_status)
    await ex.close()

    print("AFTER filter (LocalExecutor ranking):")
    for i, c in enumerate(ranked):
        print(
            f"  {i+1:2d}. offer_id={c.offer_id} score={c.score:.6f} "
            f"self_transfer_risk={c.self_transfer_risk:.3f} "
            f"mobility_fit={c.mobility_fit:.3f} components={c.components}"
        )

    after_ids = [c.offer_id for c in ranked]
    print("AFTER offer_ids:", after_ids)

    assert "FABRICATED-NOT-IN-INPUT" in before_ids, "fabricated missing from BEFORE"
    assert "FABRICATED-NOT-IN-INPUT" not in after_ids, "fabricated leaked into AFTER"
    assert len(ranked) == 12, f"expected 12 scored candidates, got {len(ranked)}"
    assert len(transitions) == 8
    for slot, seq in transitions.items():
        assert seq[0] == "pending", (slot, seq)
        assert "starting" in seq and "running" in seq, (slot, seq)
        assert seq[-1] in ("done", "failed"), (slot, seq)
        print(f"slot {slot} transitions: {' -> '.join(seq)}")

    for a, b in zip(ranked, ranked[1:]):
        assert (-a.score, a.offer_id) <= (-b.score, b.offer_id)

    print("SMOKE_LOCAL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
