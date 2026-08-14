"""Smoke: Interpreter text-only (Task 15 Verify).

Deliberate exception to Task 15's file allowlist: the Verify block requires
`python -m packages.agents.smoke_interpreter`, which cannot run without this module.

Also asserts I4 egress ordering: redact → assert_no_pii → generate_structured.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from packages.agents.interpreter import Interpreter, InterpreterInput
from packages.atlas.models import Segment
from packages.domain.db import create_all, session_factory
from packages.domain.models import Order, RecoveryCase
from packages.guardian import redaction as redaction_mod
from packages.router import get_router
from packages.router.base import ModelRouter


def _seed_case(factory) -> int:  # noqa: ANN001
    now = datetime.now(UTC)
    with factory() as session:
        order = Order(
            atlas_order_no=f"SMOKE-INT-{int(now.timestamp())}",
            pnr="SMKINT",
            status="TICKETED",
            passengers_json="[]",
            itinerary_json="[]",
            total_amount=Decimal("100.00"),
            currency="USD",
            created_at=now,
            updated_at=now,
        )
        session.add(order)
        session.flush()
        case = RecoveryCase(
            case_ref=f"RC-SMOKE-{order.id}",
            order_id=order.id,  # type: ignore[arg-type]
            trigger_kind="manual_trigger",
            trigger_fingerprint=f"smoke-interpreter-{order.id}",
            status="open",
            opened_at=now,
            resolved_at=None,
            surface="operator",
        )
        session.add(case)
        session.commit()
        session.refresh(case)
        assert case.id is not None
        return case.id


def _sample_itinerary() -> list[Segment]:
    now = datetime.now(UTC)
    return [
        Segment(
            carrier="XX",
            flight_number="0000",
            origin="CGK",
            destination="SIN",
            departure_at=now,
            arrival_at=now + timedelta(hours=2),
        )
    ]


def _install_order_probes(interpreter: Interpreter, router: ModelRouter) -> list[str]:
    """Monkeypatch to prove redact → assert_no_pii → generate_structured order."""
    order: list[str] = []
    real_redact = redaction_mod.redact
    real_assert = redaction_mod.assert_no_pii
    real_structured = router.generate_structured

    def tracked_redact(text: str, *, passengers=None):  # noqa: ANN001
        order.append("redact")
        return real_redact(text, passengers=passengers)

    def tracked_assert(payload: dict) -> None:
        order.append("assert_no_pii")
        return real_assert(payload)

    async def tracked_structured(request, schema, *, backend=None):  # noqa: ANN001
        order.append("generate_structured")
        return await real_structured(request, schema, backend=backend)

    # Patch the names Interpreter imported/binds via module references.
    import packages.agents.interpreter as interp_mod

    interp_mod.redact = tracked_redact  # type: ignore[assignment]
    interp_mod.assert_no_pii = tracked_assert  # type: ignore[assignment]
    router.generate_structured = tracked_structured  # type: ignore[method-assign]
    interpreter._router = router
    return order


async def _run(
    text: str, *, original_itinerary: list[Segment] | None = None
) -> int:
    itinerary = (
        _sample_itinerary() if original_itinerary is None else original_itinerary
    )
    with tempfile.TemporaryDirectory(prefix="rebound-smoke-interpreter-") as tmp:
        db_path = str(Path(tmp) / "smoke_interpreter.db")
        create_all(db_path)
        factory = session_factory(db_path)
        case_id = _seed_case(factory)

        router = get_router()
        interpreter = Interpreter(router, factory)
        call_order = _install_order_probes(interpreter, router)

        print(
            f"original_itinerary_len={len(itinerary)} "
            f"segments={[ (s.origin, s.destination) for s in itinerary ]}",
            flush=True,
        )
        intent = await interpreter.interpret(
            InterpreterInput(
                case_id=case_id,
                text=text,
                original_itinerary=itinerary,
            )
        )

        print(f"egress_call_order={call_order}", flush=True)
        expected = ["redact", "assert_no_pii", "generate_structured"]
        if call_order != expected:
            print(
                f"ERROR: expected egress order {expected}, got {call_order}",
                flush=True,
            )
            return 1

        origins = intent.origin_candidates_list
        print(f"origin_candidates={origins!r}", flush=True)
        if not itinerary and origins:
            print(
                "ERROR: origin_candidates must be empty when "
                "original_itinerary=[] and text has no origin evidence",
                flush=True,
            )
            return 1
        if not itinerary:
            print(
                f"origin_candidates empty with no itinerary evidence → {origins == []}",
                flush=True,
            )

        if intent.confidence < 0.6:
            question = await interpreter.clarification_question(intent)
            print("=== clarification (confidence < 0.6) ===", flush=True)
            print(f"confidence={intent.confidence}", flush=True)
            print(f"language={intent.language!r}", flush=True)
            print(f"clarification_question={question!r}", flush=True)
            print(
                "RecoveryIntent (not treated as actionable): "
                f"must_arrive_by={intent.must_arrive_by!r} "
                f"budget_ceiling_sgd={intent.budget_ceiling_sgd!r} "
                f"mobility_notes={intent.mobility_notes!r} "
                f"origin_candidates={origins!r} "
                f"destination_candidates={intent.destination_candidates_list!r} "
                f"raw_input_kinds={intent.raw_input_kinds_list!r}",
                flush=True,
            )
            return 0

        print("=== RecoveryIntent ===", flush=True)
        printed = {
            "id": intent.id,
            "case_id": intent.case_id,
            "passenger_count": intent.passenger_count,
            "must_arrive_by": intent.must_arrive_by,
            "budget_ceiling_sgd": intent.budget_ceiling_sgd,
            "origin_candidates": origins,
            "destination_candidates": intent.destination_candidates_list,
            "mobility_notes": intent.mobility_notes,
            "language": intent.language,
            "confidence": intent.confidence,
            "raw_input_kinds": intent.raw_input_kinds_list,
        }
        print(repr(printed), flush=True)
        print(f"budget_ceiling_sgd == 400 → {intent.budget_ceiling_sgd == Decimal('400')}", flush=True)
        print(f"must_arrive_by set → {intent.must_arrive_by is not None}", flush=True)
        print(
            f"mobility_notes non-empty → {bool(intent.mobility_notes and intent.mobility_notes.strip())}",
            flush=True,
        )
        print(f"language starts with zh → {intent.language.lower().startswith('zh')}", flush=True)
        print(f"raw_input_kinds == ['text'] → {intent.raw_input_kinds_list == ['text']}", flush=True)
        return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    empty_itinerary = False
    if "--empty-itinerary" in args:
        empty_itinerary = True
        args = [a for a in args if a != "--empty-itinerary"]
    if not args:
        print(
            "usage: python -m packages.agents.smoke_interpreter "
            "[--empty-itinerary] <text>",
            file=sys.stderr,
        )
        return 2
    text = " ".join(args)
    itinerary: list[Segment] | None = [] if empty_itinerary else None
    return asyncio.run(_run(text, original_itinerary=itinerary))


if __name__ == "__main__":
    raise SystemExit(main())
