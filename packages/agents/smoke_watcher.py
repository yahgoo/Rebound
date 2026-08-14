"""Smoke: Watcher ingest + dedupe (Task 14 Verify).

Deliberate exception to Task 14's file allowlist: the Verify block requires
`python -m packages.agents.smoke_watcher`, which cannot run without this module.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlmodel import select

from packages.agents.watcher import DisruptionSignal, Watcher
from packages.atlas.models import OrderDetails, Segment
from packages.domain.db import create_all, session_factory
from packages.domain.models import AgentEvent, Order, RecoveryCase

# Fixture webhook — schedule change. notificationId differs across deliveries
# to prove fingerprint ignores delivery id.
_FIXTURE_BASE: dict[str, Any] = {
    "cid": "SMOKE_CID",
    "notificationId": "DELIVERY-001",
    "status": 0,
    "type": "order.schedulechange",
    "data": {
        "orderNo": "SMOKE-ORDER-14",
        "scheduleChangeType": 1,
        "previousSegs": [
            {
                "depAirport": "CGK",
                "arrAirport": "SUB",
                "flightNumber": "QG2407",
                "depTime": "2026-09-13 06:00:00",
                "arrTime": "2026-09-13 08:00:00",
            }
        ],
        "revisedSegs": [
            {
                "depAirport": "CGK",
                "arrAirport": "SUB",
                "flightNumber": "QG2407",
                "depTime": "2026-09-13 10:00:00",
                "arrTime": "2026-09-13 12:00:00",
            }
        ],
        "originalSegs": [],
    },
}


class _FakeAtlas:
    """Minimal stand-in: query_order_details only (I7 path)."""

    def __init__(self) -> None:
        self.calls = 0

    async def query_order_details(self, *, order_no: str) -> OrderDetails:
        self.calls += 1
        now = datetime.now(UTC)
        return OrderDetails(
            order_no=order_no,
            status="TICKETED",
            pnr="SMKPNR",
            ticket_numbers=["SMOKE-TKT-1"],
            segments=[
                Segment(
                    carrier="QG",
                    flight_number="2407",
                    origin="CGK",
                    destination="SUB",
                    departure_at=now,
                    arrival_at=now,
                )
            ],
            total_amount=Decimal("78.24"),
            currency="USD",
            raw={
                "orderNo": order_no,
                "orderStatus": "2",
                "paxTicketInfos": [{"name": "PASSENGER/TEST", "passengerType": 0}],
            },
        )


def _signal_from_fixture(payload: dict[str, Any]) -> DisruptionSignal:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    order_no = str(data.get("orderNo") or "")
    return DisruptionSignal(
        kind="webhook_schedule_change",
        atlas_order_no=order_no,
        fingerprint=Watcher.fingerprint(payload),
        received_at=datetime.now(UTC),
        raw=dict(payload),
    )


async def _run(db_path: str) -> int:
    create_all(db_path)
    factory = session_factory(db_path)
    atlas = _FakeAtlas()
    watcher = Watcher(atlas, factory)  # type: ignore[arg-type]

    cases: list[RecoveryCase] = []
    for i in range(3):
        payload = {**_FIXTURE_BASE, "notificationId": f"DELIVERY-{i + 1:03d}"}
        signal = _signal_from_fixture(payload)
        case = await watcher.ingest(signal)
        cases.append(case)
        print(
            f"ingest[{i + 1}] case_ref={case.case_ref} "
            f"fingerprint={signal.fingerprint[:12]}… id={case.id}"
        )

    assert len({c.id for c in cases}) == 1
    assert cases[0].case_ref == "RC-0001", cases[0].case_ref
    assert atlas.calls == 1, f"query_order_details should run once, got {atlas.calls}"

    fp_a = Watcher.fingerprint({**_FIXTURE_BASE, "notificationId": "A"})
    fp_b = Watcher.fingerprint({**_FIXTURE_BASE, "notificationId": "B"})
    assert fp_a == fp_b
    print(f"fingerprint_stable_across_delivery_ids={fp_a == fp_b}")

    # Manual trigger shares the ingest path (kind differs → new fingerprint/case).
    manual_payload = {"kind": "manual_trigger", "atlas_order_no": "SMOKE-ORDER-14"}
    manual = DisruptionSignal(
        kind="manual_trigger",
        atlas_order_no="SMOKE-ORDER-14",
        fingerprint=Watcher.fingerprint(manual_payload),
        received_at=datetime.now(UTC),
        raw=manual_payload,
    )
    manual_case = await watcher.ingest(manual)
    print(f"manual_trigger case_ref={manual_case.case_ref}")
    assert manual_case.case_ref == "RC-0002"

    with factory() as session:
        # Re-open a clean count for the webhook-only claim: delete is forbidden
        # on AgentEvent helpers, so filter by case instead.
        webhook_case_id = cases[0].id
        n_orders = len(list(session.exec(select(Order)).all()))
        webhook_cases = list(
            session.exec(
                select(RecoveryCase).where(
                    RecoveryCase.trigger_kind == "webhook_schedule_change"
                )
            ).all()
        )
        webhook_events = list(
            session.exec(
                select(AgentEvent).where(AgentEvent.case_id == webhook_case_id)
            ).all()
        )
        print("=== smoke print (webhook fixture ×3) ===")
        print(
            f"RecoveryCase(webhook)={len(webhook_cases)} "
            f"Order={n_orders} "
            f"AgentEvent(webhook case)={len(webhook_events)}"
        )
        print(f"steps={[e.step for e in webhook_events]}")
        assert len(webhook_cases) == 1
        assert n_orders == 1
        assert len(webhook_events) == 3

    print("smoke_watcher OK")
    return 0


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="rebound-smoke-watcher-") as tmp:
        db_path = str(Path(tmp) / "smoke_watcher.db")
        print(f"db_path={db_path}")
        return asyncio.run(_run(db_path))


if __name__ == "__main__":
    raise SystemExit(main())
