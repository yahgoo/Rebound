"""Smoke: Strategist plan / fan_out / scoring codegen / select (Task 17 Verify).

Deliberate exception to Task 17's file allowlist: the Verify block requires
`python -m packages.agents.smoke_strategist <case_ref>`, which cannot run
without this module.
"""

from __future__ import annotations

import argparse
import asyncio
import ast
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlmodel import select

from packages.agents import strategist as strategist_mod
from packages.agents.strategist import RankedSelection, Strategist
from packages.atlas.cassette import CassetteRecorder
from packages.atlas.client import AtlasClient
from packages.atlas.models import Segment
from packages.atlas.transport import LiveTransport, ReplayTransport
from packages.atlas.cassette import CassettePlayer
from packages.domain.db import create_all, session_factory
from packages.domain.enums import ReboundMode
from packages.domain.models import Candidate, Order, RecoveryCase, RecoveryIntent
from packages.executors.base import ScoredCandidate
from packages.router import get_router

ROOT = Path(__file__).resolve().parents[2]
CASSETTE_DIR = ROOT / "fixtures" / "cassettes"
SCORING_OUT = Path("/tmp/last_scoring_code.py")

_DANGEROUS_RE = re.compile(
    r"\b(os|subprocess|socket|sys|shutil|eval|exec)\b"
)


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


def _build_atlas(file_env: dict[str, str]) -> AtlasClient:
    mode = _mode()
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    if mode is ReboundMode.REPLAY:
        print("mode=replay", flush=True)
        transport = ReplayTransport(
            CassettePlayer(CASSETTE_DIR, reproduce_latency=True)
        )
    else:
        base_url = os.environ.get("ATLAS_BASE_URL") or file_env.get("ATLAS_BASE_URL")
        client_id = os.environ.get("ATLAS_CLIENT_ID") or file_env.get("ATLAS_CLIENT_ID")
        client_secret = (
            os.environ.get("ATLAS_CLIENT_SECRET")
            or file_env.get("ATLAS_CLIENT_SECRET")
        )
        if not base_url or not client_id or not client_secret:
            raise SystemExit(
                "missing ATLAS_BASE_URL / ATLAS_CLIENT_ID / ATLAS_CLIENT_SECRET"
            )
        print("mode=live", flush=True)
        transport = LiveTransport(
            base_url,
            client_id,
            client_secret,
            recorder=CassetteRecorder(CASSETTE_DIR),
            timeout_seconds=60.0,
        )
    return AtlasClient(transport)


def _seed(factory, case_ref: str) -> tuple[int, RecoveryIntent, list[Segment]]:  # noqa: ANN001
    now = datetime.now(UTC)
    dep = (now + timedelta(days=30)).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    arr = dep + timedelta(hours=2)
    original = [
        Segment(
            carrier="XX",
            flight_number="0000",
            origin="JKT",
            destination="SUB",
            departure_at=dep,
            arrival_at=arr,
        )
    ]
    with factory() as session:
        order = Order(
            atlas_order_no=f"SMOKE-STR-{int(now.timestamp())}",
            pnr="SMKSTR",
            status="TICKETED",
            passengers_json="[]",
            itinerary_json=json.dumps(
                [s.model_dump(mode="json") for s in original], default=str
            ),
            total_amount=Decimal("100.00"),
            currency="USD",
            created_at=now,
            updated_at=now,
        )
        session.add(order)
        session.flush()
        case = RecoveryCase(
            case_ref=case_ref,
            order_id=order.id,  # type: ignore[arg-type]
            trigger_kind="manual_trigger",
            trigger_fingerprint=f"smoke-strategist-{case_ref}-{order.id}",
            status="open",
            opened_at=now,
            resolved_at=None,
            surface="operator",
        )
        session.add(case)
        session.flush()
        intent = RecoveryIntent(
            case_id=case.id,  # type: ignore[arg-type]
            passenger_count=1,
            must_arrive_by=arr + timedelta(hours=6),
            budget_ceiling_sgd=Decimal("400"),
            origin_candidates=json.dumps(["JKT", "HLP"]),
            destination_candidates=json.dumps(["SUB"]),
            mobility_notes="Walks with a cane",
            language="en",
            confidence=0.9,
            raw_input_kinds=json.dumps(["text"]),
        )
        session.add(intent)
        session.commit()
        session.refresh(intent)
        session.refresh(case)
        assert case.id is not None
        intent_out = RecoveryIntent.model_validate(intent.model_dump())
        return case.id, intent_out, original


async def _run(case_ref: str, *, force_bad_scoring: bool) -> int:
    file_env = _load_dotenv(ROOT / ".env")
    # Ensure settings see Gemini / OpenRouter keys for get_router().
    for key, value in file_env.items():
        os.environ.setdefault(key, value)

    with tempfile.TemporaryDirectory(prefix="rebound-smoke-strategist-") as tmp:
        db_path = str(Path(tmp) / "smoke_strategist.db")
        create_all(db_path)
        factory = session_factory(db_path)
        case_id, intent, original = _seed(factory, case_ref)
        print(f"case_ref={case_ref} case_id={case_id}", flush=True)

        atlas = _build_atlas(file_env)
        router = get_router()
        strategist = Strategist(router, atlas, factory)

        plans = await strategist.plan(intent, original)
        print(f"plans={len(plans)}", flush=True)
        for p in plans:
            req = p.search_request
            print(
                f"  DISPATCHED strategy={p.strategy.value} "
                f"{req.origin}->{req.destination} "
                f"day={req.departure_date.strftime('%d %b %Y')} "
                f"rationale={p.rationale!r}",
                flush=True,
            )
        if len(plans) != 4:
            print(f"ERROR: expected 4 plans, got {len(plans)}", flush=True)
            return 1

        candidates = await strategist.fan_out(plans)
        offer_ids = [c.offer_id for c in candidates]
        print(
            f"deduplicated_candidates={len(candidates)} "
            f"distinct_offer_ids={len(set(offer_ids))}",
            flush=True,
        )
        for c in candidates[:12]:
            print(
                f"  Candidate offer_id={c.offer_id!r} strategy={c.strategy} "
                f"price={c.price} {c.currency} stops={c.stop_count} "
                f"delay_min={c.arrival_delay_minutes}",
                flush=True,
            )
        if len(candidates) < 6 or len(set(offer_ids)) < 6:
            print(
                "ERROR: need >=6 deduplicated candidates with distinct offer_ids",
                flush=True,
            )
            return 1

        if force_bad_scoring:
            strategist_mod._FORCE_BAD_SCORING_ONCE = (
                "import requests\n\ndef not_score(payload: dict):\n    return []\n"
            )
            print(
                "forcing bad first scoring generation "
                "(non-stdlib import + missing score)",
                flush=True,
            )

        code = await strategist.write_scoring_code(intent)
        SCORING_OUT.write_text(code, encoding="utf-8")
        print(f"wrote_scoring_code path={SCORING_OUT} nbytes={len(code)}", flush=True)
        print("--- scoring code begin ---", flush=True)
        print(code, flush=True)
        print("--- scoring code end ---", flush=True)

        # ast / exec checks
        tree = ast.parse(code)
        print(f"ast.parse ok nodes={len(list(ast.walk(tree)))}", flush=True)
        ns: dict = {}
        exec(compile(code, "<scoring>", "exec"), ns, ns)  # noqa: S102
        print(f"score defined → {callable(ns.get('score'))}", flush=True)
        if not callable(ns.get("score")):
            print("ERROR: score not defined after exec", flush=True)
            return 1

        # Non-stdlib import grep (stdlib modules are OK for this gate).
        import_lines = [
            line
            for line in code.splitlines()
            if re.match(r"^\s*(import |from )", line)
        ]
        print(f"import_lines={import_lines!r}", flush=True)
        stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
        for line in import_lines:
            m = re.match(r"^\s*import\s+([\w\.]+)", line) or re.match(
                r"^\s*from\s+([\w\.]+)\s+import", line
            )
            if not m:
                continue
            root = m.group(1).split(".", 1)[0]
            if root not in stdlib:
                print(f"ERROR: non-stdlib import: {m.group(1)}", flush=True)
                return 1

        dangerous_hits = sorted(set(_DANGEROUS_RE.findall(code)))
        print(f"dangerous_stdlib_or_eval_hits={dangerous_hits!r}", flush=True)

        # Fabricate scores from candidates (running scoring is Task 18).
        scored = [
            ScoredCandidate(
                offer_id=c.offer_id,
                score=float(1000 - i),
                components={"rank": float(1000 - i)},
                self_transfer_risk=0.1,
                mobility_fit=0.9,
            )
            for i, c in enumerate(candidates)
        ]

        # Inject fabricated offer_id via model output wrap.
        real_structured = router.generate_structured

        async def inject_fabricated(request, schema, *, backend=None):  # noqa: ANN001
            draft = await real_structured(request, schema, backend=backend)
            data = draft.model_dump()
            tokens = list(data.get("ordered_tokens") or [])
            tokens = ["FABRICATED-NOT-IN-CANDIDATES", *tokens]
            data["ordered_tokens"] = tokens
            print(f"select BEFORE filter tokens={tokens!r}", flush=True)
            return schema.model_validate(data)

        router.generate_structured = inject_fabricated  # type: ignore[method-assign]
        strategist._router = router

        selection: RankedSelection = await strategist.select(
            candidates, scored, intent
        )
        print(
            f"select AFTER filter offer_ids={selection.ordered_offer_ids!r}",
            flush=True,
        )
        if "FABRICATED-NOT-IN-CANDIDATES" in selection.ordered_offer_ids:
            print("ERROR: fabricated offer_id was not discarded (I1)", flush=True)
            return 1
        if not selection.ordered_offer_ids:
            print("ERROR: select returned empty ordered_offer_ids", flush=True)
            return 1

        with factory() as session:
            rows = list(session.exec(select(Candidate)).all())
            print(f"persisted_candidate_rows={len(rows)}", flush=True)

        print("OK", flush=True)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke Strategist (Task 17)")
    parser.add_argument("case_ref", help="Recovery case_ref to seed/use")
    parser.add_argument(
        "--force-bad-scoring",
        action="store_true",
        help="Force a bad first scoring generation to exercise reject-and-retry",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    return asyncio.run(
        _run(args.case_ref, force_bad_scoring=args.force_bad_scoring)
    )


if __name__ == "__main__":
    raise SystemExit(main())
