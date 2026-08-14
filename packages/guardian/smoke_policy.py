"""Smoke: Guardian spend cap + confirmation gate (Task 9).

Exercises all six required behaviours and prints the actual verdict or
exception for each rejection case.
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from packages.guardian.policy import (
    ConfirmationDecision,
    ConfirmationGate,
    ConfirmationRequest,
    check_spend_cap,
)


def main() -> int:
    # 1) over-cap rejection
    print("=== 1 over-cap rejection ===")
    over = check_spend_cap(
        amount_sgd=Decimal("900"),
        intent_ceiling_sgd=Decimal("1000"),
        env_cap_sgd=Decimal("800"),
    )
    print(over)
    assert over.allowed is False and over.reason == "over_cap"
    assert over.effective_cap_sgd == Decimal("800")

    # 2) under-cap approval
    print("=== 2 under-cap approval ===")
    under = check_spend_cap(
        amount_sgd=Decimal("500"),
        intent_ceiling_sgd=Decimal("1000"),
        env_cap_sgd=Decimal("800"),
    )
    print(under)
    assert under.allowed is True and under.reason is None
    assert under.effective_cap_sgd == Decimal("800")

    # 3) env cap winning (min = env)
    print("=== 3 env cap winning ===")
    env_wins = check_spend_cap(
        amount_sgd=Decimal("100"),
        intent_ceiling_sgd=Decimal("1000"),
        env_cap_sgd=Decimal("800"),
    )
    print(env_wins)
    assert env_wins.effective_cap_sgd == Decimal("800")

    # 4) intent ceiling winning (min = intent)
    print("=== 4 intent ceiling winning ===")
    intent_wins = check_spend_cap(
        amount_sgd=Decimal("100"),
        intent_ceiling_sgd=Decimal("400"),
        env_cap_sgd=Decimal("800"),
    )
    print(intent_wins)
    assert intent_wins.effective_cap_sgd == Decimal("400")

    gate = ConfirmationGate()
    now = datetime.now(UTC)

    req = ConfirmationRequest(
        case_id=1,
        candidate_ids=[10, 20, 30],
        recommended_candidate_id=10,
        effective_cap_sgd=Decimal("800"),
        expires_at=now + timedelta(minutes=4),
        nonce="",  # open issues a single-use nonce
    )
    gate.open(req)
    print("=== open issued ===")
    print(f"nonce={req.nonce!r} expires_at={req.expires_at.isoformat()}")

    gate.resolve(
        ConfirmationDecision(
            case_id=1,
            candidate_id=10,
            nonce=req.nonce,
            decided_by="operator",
            decided_at=datetime.now(UTC),
        )
    )
    assert gate.is_confirmed(case_id=1, candidate_id=10) is True
    assert gate.is_confirmed(case_id=1, candidate_id=20) is False
    print("is_confirmed(1,10)=", gate.is_confirmed(case_id=1, candidate_id=10))

    # 5) nonce replay rejected
    print("=== 5 nonce replay rejected ===")
    try:
        gate.resolve(
            ConfirmationDecision(
                case_id=1,
                candidate_id=10,
                nonce=req.nonce,
                decided_by="operator",
                decided_at=datetime.now(UTC),
            )
        )
        print("FAIL: replay did not raise")
        return 1
    except Exception as exc:
        print(f"raised {type(exc).__name__}: {exc}")
        traceback.print_exc()

    # 6) expired request rejected
    print("=== 6 expired request rejected ===")
    expired_gate = ConfirmationGate()
    expired_req = ConfirmationRequest(
        case_id=2,
        candidate_ids=[40],
        recommended_candidate_id=40,
        effective_cap_sgd=Decimal("800"),
        expires_at=now + timedelta(minutes=4),
        nonce="expire-me-once",
    )
    expired_gate.open(expired_req)
    # Force the registered challenge past expiry (in-memory only).
    stored = expired_gate._pending[expired_req.nonce]
    expired_gate._pending[expired_req.nonce] = stored.model_copy(
        update={"expires_at": now - timedelta(seconds=1)}
    )
    try:
        expired_gate.resolve(
            ConfirmationDecision(
                case_id=2,
                candidate_id=40,
                nonce=expired_req.nonce,
                decided_by="traveller",
                decided_at=datetime.now(UTC),
            )
        )
        print("FAIL: expired resolve did not raise")
        return 1
    except Exception as exc:
        print(f"raised {type(exc).__name__}: {exc}")
        traceback.print_exc()

    print("OK: smoke_policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
