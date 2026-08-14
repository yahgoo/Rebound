"""Smoke: Guardian append-only audit log (Task 10).

Writes five events for one case, reads them back (full + after_id), and
proves a Luhn-valid PAN in the payload is rejected before insert.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import traceback
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from packages.domain.db import create_all, session_factory
from packages.domain.enums import Actor
from packages.domain.models import Order, RecoveryCase
from packages.guardian.audit import AgentEventIn, read_events, write_event
from packages.guardian.redaction import PIIDetectedError


async def _run(db_path: str) -> int:
    create_all(db_path)
    factory = session_factory(db_path)

    opened_at = datetime.now(UTC)
    with factory() as session:
        order = Order(
            atlas_order_no="SMOKE-AUDIT-ORDER",
            pnr=None,
            status="TICKETED",
            passengers_json="[]",
            itinerary_json="{}",
            total_amount=Decimal("100.00"),
            currency="SGD",
            created_at=opened_at,
            updated_at=opened_at,
        )
        session.add(order)
        session.flush()
        case = RecoveryCase(
            case_ref="RC-SMOKE-AUDIT",
            order_id=order.id,  # type: ignore[arg-type]
            trigger_kind="schedule_change",
            trigger_fingerprint="smoke-audit-fp",
            status="open",
            opened_at=opened_at,
            resolved_at=None,
            surface="operator",
        )
        session.add(case)
        session.commit()
        session.refresh(case)
        case_id = case.id
        assert case_id is not None

    steps = [
        (Actor.WATCHER, "watcher.ingest", "webhook received", {"kind": "schedule_change"}),
        (Actor.INTERPRETER, "interpreter.start", "interpretation started", {"confidence": 0.9}),
        (Actor.STRATEGIST, "strategist.search", "search dispatched", {"strategies": 3}),
        (Actor.GUARDIAN, "guardian.cap_ok", "spend cap passed", {"amount_sgd": "120.00"}),
        (Actor.EXECUTOR, "executor.attempt", "order attempt", {"attempt": 1}),
    ]

    ids: list[int] = []
    with factory() as session:
        for actor, step, summary, payload in steps:
            # Small sleep so elapsed_ms is non-decreasing and usually increasing.
            time.sleep(0.02)
            event_id = await write_event(
                session,
                AgentEventIn(
                    case_id=case_id,
                    actor=actor,
                    step=step,
                    summary=summary,
                    payload=payload,
                ),
            )
            ids.append(event_id)
            session.commit()
        print("=== write_event ids ===")
        print(ids)

        rows = await read_events(session, case_id=case_id)
        print("=== read_events (all) ===")
        for row in rows:
            print(
                f"id={row.id} actor={row.actor} step={row.step} "
                f"elapsed_ms={row.elapsed_ms} summary={row.summary!r}"
            )

        assert len(rows) == 5, f"expected 5 events, got {len(rows)}"
        assert [r.id for r in rows] == ids
        assert ids == sorted(ids), "ids must be ascending"
        elapsed = [r.elapsed_ms for r in rows]
        assert elapsed == sorted(elapsed), f"elapsed_ms not non-decreasing: {elapsed}"

        after_id = ids[2]  # third id → expect last two
        filtered = await read_events(session, case_id=case_id, after_id=after_id)
        print(f"=== read_events after_id={after_id} ===")
        for row in filtered:
            print(
                f"id={row.id} actor={row.actor} step={row.step} "
                f"elapsed_ms={row.elapsed_ms} summary={row.summary!r}"
            )
        assert [r.id for r in filtered] == ids[3:], (
            f"after_id filter mismatch: got {[r.id for r in filtered]}, "
            f"expected {ids[3:]}"
        )

        print("=== write_event with Luhn-valid PAN (expect raise) ===")
        try:
            await write_event(
                session,
                AgentEventIn(
                    case_id=case_id,
                    actor=Actor.EXECUTOR,
                    step="executor.pay",
                    summary="must not land",
                    payload={"card_number": "4111111111111111"},
                ),
            )
            print("FAIL: write_event did not raise on Luhn-valid PAN")
            return 1
        except PIIDetectedError as exc:
            traceback.print_exc()
            print(f"raised: {type(exc).__name__}: {exc}")

        # Confirm the rejected write left no sixth row.
        again = await read_events(session, case_id=case_id)
        assert len(again) == 5, f"PAN write leaked a row; count={len(again)}"

    print("smoke_audit OK")
    return 0


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="rebound-smoke-audit-") as tmp:
        db_path = str(Path(tmp) / "smoke_audit.db")
        return asyncio.run(_run(db_path))


if __name__ == "__main__":
    raise SystemExit(main())
