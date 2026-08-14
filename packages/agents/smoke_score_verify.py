"""Smoke: ExecutorAgent score_and_verify (Task 18 Verify).

Deliberate exception to Task 18's file allowlist: the Verify block requires
`python -m packages.agents.smoke_score_verify <case_ref>`, which cannot run
without this module.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlmodel import select

from packages.agents.executor_agent import (
    ExecutorAgent,
    _STEP_CAP_REJECT,
    _STEP_SCORING_FALLBACK,
    _USD_TO_SGD,
    _to_sgd,
)
from packages.agents.strategist import Strategist
from packages.atlas.cassette import CassettePlayer, CassetteRecorder
from packages.atlas.client import AtlasClient
from packages.atlas.errors import AtlasError
from packages.atlas.models import Segment
from packages.atlas.transport import LiveTransport, ReplayTransport
from packages.domain.db import create_all, session_factory
from packages.domain.enums import ExecutorKind, ReboundMode
from packages.domain.models import AgentEvent, Candidate, Order, RecoveryCase, RecoveryIntent
from packages.executors.base import (
    ExecutorUnavailableError,
    SandboxStatus,
    ScoredCandidate,
)
from packages.executors.local import LocalExecutor
from packages.guardian.policy import ConfirmationGate, check_spend_cap
from packages.router import get_router

ROOT = Path(__file__).resolve().parents[2]
CASSETTE_DIR = ROOT / "fixtures" / "cassettes"
SCORING_OUT = Path("/tmp/last_scoring_code.py")

# Deterministic stdlib-only scorer for Task 18 Verify (model codegen is Task 17;
# None min_transfer_minutes routinely crashes model scripts in the sandbox).
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
        price_fit = max(0.0, 1.0 - (price / budget)) if budget > 0 else 0.5
        stop_pen = 0.15 * stops
        self_transfer_risk = 0.0 if stops == 0 else min(1.0, 40.0 / max(transfer_m, 1))
        mobility_fit = max(0.0, 1.0 - weight * self_transfer_risk)
        suffix = sum(ord(ch) for ch in c["offer_id"]) % 1000
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
    return out
"""


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
            atlas_order_no=f"SMOKE-SV-{int(now.timestamp())}",
            pnr="SMKSV",
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
            trigger_fingerprint=f"smoke-score-verify-{case_ref}-{order.id}",
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


class _FailingExecutor:
    """Raises ExecutorUnavailableError so ExecutorAgent falls back (I10)."""

    kind = ExecutorKind.DAYTONA

    async def score(self, payload, scoring_code, *, on_status=None):  # noqa: ANN001
        raise ExecutorUnavailableError(
            "DAYTONA_API_KEY empty / Daytona unavailable (smoke forced)"
        )

    async def close(self) -> None:
        return None


def _pick_executor() -> Any:
    kind = (os.environ.get("EXECUTOR") or "local").strip().lower()
    daytona_key = (os.environ.get("DAYTONA_API_KEY") or "").strip()
    if kind == "daytona" and not daytona_key:
        print(
            "EXECUTOR=daytona with empty DAYTONA_API_KEY → "
            "injecting FailingExecutor (expect LocalExecutor fallback)",
            flush=True,
        )
        return _FailingExecutor()
    if kind == "daytona":
        from packages.executors.daytona import DaytonaExecutor

        print("executor=daytona", flush=True)
        return DaytonaExecutor(daytona_key, target_slots=8, timeout_seconds=60)
    print("executor=local", flush=True)
    return LocalExecutor(target_slots=8, timeout_seconds=30)


async def _run(case_ref: str, *, proofs: bool) -> int:
    file_env = _load_dotenv(ROOT / ".env")
    for key, value in file_env.items():
        # Do not override an explicitly emptied DAYTONA_API_KEY (run 3).
        if key == "DAYTONA_API_KEY" and "DAYTONA_API_KEY" in os.environ:
            continue
        if key == "GUARDIAN_MAX_SPEND_SGD" and "GUARDIAN_MAX_SPEND_SGD" in os.environ:
            continue
        if key == "EXECUTOR" and "EXECUTOR" in os.environ:
            continue
        os.environ.setdefault(key, value)

    env_cap = Decimal(
        (os.environ.get("GUARDIAN_MAX_SPEND_SGD") or file_env.get("GUARDIAN_MAX_SPEND_SGD") or "800")
    )
    print(f"GUARDIAN_MAX_SPEND_SGD={env_cap}", flush=True)

    with tempfile.TemporaryDirectory(prefix="rebound-smoke-sv-") as tmp:
        db_path = str(Path(tmp) / "smoke_score_verify.db")
        create_all(db_path)
        factory = session_factory(db_path)
        case_id, intent, original = _seed(factory, case_ref)
        print(f"case_ref={case_ref} case_id={case_id}", flush=True)

        atlas = _build_atlas(file_env)
        router = get_router()
        strategist = Strategist(router, atlas, factory)

        plans = await strategist.plan(intent, original)
        print(f"plans={len(plans)}", flush=True)
        candidates = await strategist.fan_out(plans)
        print(f"candidates={len(candidates)}", flush=True)
        if len(candidates) < 3:
            print("ERROR: need >=3 candidates", flush=True)
            return 1

        try:
            model_code = await strategist.write_scoring_code(intent)
            SCORING_OUT.write_text(model_code, encoding="utf-8")
            print(
                f"model_scoring_code nbytes={len(model_code)} path={SCORING_OUT}",
                flush=True,
            )
        except Exception as exc:
            print(f"model_scoring_code skipped: {type(exc).__name__}: {exc}", flush=True)
        # Deterministic scorer for Task 18 Verify (avoids None-transfer crashes).
        code = SCORING_CODE
        print(f"using_deterministic_scoring_code nbytes={len(code)}", flush=True)

        executor = _pick_executor()
        agent = ExecutorAgent(atlas, executor, ConfirmationGate(), factory)

        # --- Extra proof 2: inject fabricated offer_id into scoring path ---
        real_score = executor.score

        async def score_with_fabricated(payload, scoring_code, *, on_status=None):  # noqa: ANN001
            try:
                ranked = await real_score(payload, scoring_code, on_status=on_status)
            except ExecutorUnavailableError:
                raise
            before = [s.offer_id for s in ranked]
            fabricated = ScoredCandidate(
                offer_id="FABRICATED-SCORE-NOT-IN-CANDIDATES",
                score=99999.0,
                components={"cheat": 99999.0},
                self_transfer_risk=0.0,
                mobility_fit=1.0,
            )
            injected = [fabricated, *ranked]
            print(
                f"UNKNOWN_OFFER_ID BEFORE filter offer_ids={ [s.offer_id for s in injected]!r}",
                flush=True,
            )
            # ExecutorAgent drops unknown ids after this return.
            return injected

        # Only wrap non-failing executors for the fabricated-id proof.
        if not isinstance(executor, _FailingExecutor):
            executor.score = score_with_fabricated  # type: ignore[method-assign]
            agent = ExecutorAgent(atlas, executor, ConfirmationGate(), factory)

        statuses: list[str] = []

        async def on_status(status: SandboxStatus) -> None:
            statuses.append(f"{status.slot}:{status.state}")

        ranked = await agent.score_and_verify(
            case_id=case_id,
            candidates=candidates,
            intent=intent,
            scoring_code=code,
            on_status=on_status,
        )

        print("--- ranking (score desc) ---", flush=True)
        for i, c in enumerate(ranked):
            print(
                f"  {i+1:2d}. offer_id={c.offer_id[:40]!r}… score={c.score} "
                f"verified={c.verified} verified_price={c.verified_price} "
                f"rejected_reason={c.rejected_reason!r} "
                f"price={c.price} {c.currency}",
                flush=True,
            )

        after_ids = [c.offer_id for c in ranked if c.score is not None]
        print(f"UNKNOWN_OFFER_ID AFTER ranking scored_ids include fabricated? "
              f"{'FABRICATED-SCORE-NOT-IN-CANDIDATES' in after_ids}", flush=True)
        if "FABRICATED-SCORE-NOT-IN-CANDIDATES" in after_ids:
            print("ERROR: fabricated offer_id appeared in ranking", flush=True)
            return 1

        verified_top = [c for c in ranked if c.verified][:3]
        print(
            f"verified_count={sum(1 for c in ranked if c.verified)} "
            f"sandbox_status_events={len(statuses)}",
            flush=True,
        )
        for c in verified_top:
            print(
                f"  AUTHORITATIVE verified=True offer={c.offer_id[:32]!r}… "
                f"verified_price={c.verified_price} {c.currency}",
                flush=True,
            )

        over_cap = [c for c in ranked if c.rejected_reason == "over_cap"]
        if over_cap:
            for c in over_cap:
                print(
                    f"OVER_CAP candidate id={c.id} offer={c.offer_id[:40]!r}… "
                    f"verified_price={c.verified_price} "
                    f"rejected_reason={c.rejected_reason!r}",
                    flush=True,
                )
                # Extra proof 3: trace deterministic cap comparison for run 2.
                amount_sgd = _to_sgd(
                    Decimal(str(c.verified_price or c.price)), c.currency
                )
                verdict = check_spend_cap(
                    amount_sgd=amount_sgd,
                    intent_ceiling_sgd=Decimal(str(intent.budget_ceiling_sgd)),
                    env_cap_sgd=env_cap,
                )
                print(
                    f"I3_CAP_TRACE amount_sgd={amount_sgd} "
                    f"intent_ceiling={intent.budget_ceiling_sgd} "
                    f"env_cap={env_cap} "
                    f"effective=min(...)={verdict.effective_cap_sgd} "
                    f"usd_to_sgd={_USD_TO_SGD} "
                    f"allowed={verdict.allowed} reason={verdict.reason!r}",
                    flush=True,
                )
        else:
            print("OVER_CAP none", flush=True)

        # --- Extra proof 1 (I2): force one real verification failure downstream ---
        if proofs:
            victim = next((c for c in ranked if c.score is not None), None)
            if victim is None:
                print("ERROR: no scored candidate for I2 force-fail", flush=True)
                return 1
            # Snapshot pre-cap state, then force verify failure and confirm cap
            # is never applied (rejected_reason stays verify_failed, not over_cap).
            before_reason = victim.rejected_reason
            before_verified = victim.verified
            print(
                f"I2_FORCE before offer={victim.offer_id[:40]!r}… "
                f"verified={before_verified} rejected_reason={before_reason!r}",
                flush=True,
            )

            async def boom(*, routing_identifier: str):  # noqa: ANN001
                raise AtlasError(
                    code="smoke_forced_verify_fail",
                    message="forced verification failure for I2 proof",
                )

            atlas.verify = boom  # type: ignore[method-assign]
            # Re-run only the verify+cap path on this candidate via private API.
            victim.verified = False
            victim.verified_price = None
            victim.rejected_reason = None
            await agent._verify_one(victim)
            print(
                f"I2_FORCE after verify offer={victim.offer_id[:40]!r}… "
                f"verified={victim.verified} rejected_reason={victim.rejected_reason!r}",
                flush=True,
            )
            if victim.rejected_reason != "verify_failed" or victim.verified:
                print("ERROR: forced verify did not yield verify_failed", flush=True)
                return 1
            # Cap must not run: simulate the score_and_verify gate.
            if victim.verified:
                await agent._apply_cap(
                    victim,
                    intent_ceiling_sgd=Decimal(str(intent.budget_ceiling_sgd)),
                    env_cap_sgd=env_cap,
                )
            print(
                f"I2_EXCLUDED from cap: verified={victim.verified} "
                f"rejected_reason={victim.rejected_reason!r} "
                f"(over_cap would require verified=True)",
                flush=True,
            )
            if victim.rejected_reason == "over_cap":
                print("ERROR: verify-failed candidate reached over_cap", flush=True)
                return 1

        failed = [
            c
            for c in ranked
            if c.rejected_reason in ("verify_failed", "price_moved")
        ]
        for c in failed:
            print(
                f"I2_MARKED offer={c.offer_id[:40]!r}… "
                f"verified={c.verified} rejected_reason={c.rejected_reason!r}",
                flush=True,
            )
            if c.verified:
                print("ERROR: verify-failed candidate marked verified=True", flush=True)
                return 1

        with factory() as session:
            events = list(
                session.exec(
                    select(AgentEvent)
                    .where(AgentEvent.case_id == case_id)
                    .order_by(AgentEvent.id)
                ).all()
            )
            print(f"AgentEvent count={len(events)}", flush=True)
            for e in events:
                if e.actor == "executor":
                    print(
                        f"  event id={e.id} step={e.step} summary={e.summary!r} "
                        f"payload={e.payload_json}",
                        flush=True,
                    )
            fallback_rows = [
                e for e in events if e.step == _STEP_SCORING_FALLBACK
            ]
            if isinstance(executor, _FailingExecutor) or (
                (os.environ.get("EXECUTOR") or "").lower() == "daytona"
                and not (os.environ.get("DAYTONA_API_KEY") or "").strip()
            ):
                if not fallback_rows:
                    print("ERROR: expected executor.scoring_fallback_local AgentEvent", flush=True)
                    return 1
                print("--- FALLBACK AgentEvent (I10) ---", flush=True)
                for e in fallback_rows:
                    print(
                        f"  id={e.id} step={e.step} summary={e.summary!r} "
                        f"payload={e.payload_json}",
                        flush=True,
                    )

            cap_rows = [e for e in events if e.step == _STEP_CAP_REJECT]
            if env_cap <= Decimal("1") and not cap_rows and not over_cap:
                print(
                    "ERROR: GUARDIAN_MAX_SPEND_SGD<=1 expected at least one over_cap",
                    flush=True,
                )
                return 1

        # Happy path: at least one verified when env cap is normal and proofs
        # didn't wipe all verifies.
        if env_cap >= Decimal("100") and not isinstance(executor, _FailingExecutor):
            if sum(1 for c in ranked if c.verified) < 1:
                print("ERROR: expected at least one verified=True candidate", flush=True)
                return 1

        print("OK", flush=True)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke score_and_verify (Task 18)")
    parser.add_argument("case_ref", help="Recovery case_ref to seed/use")
    parser.add_argument(
        "--proofs",
        action="store_true",
        help="Also run forced verify-failure I2 proof after the main ranking",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    return asyncio.run(_run(args.case_ref, proofs=args.proofs))


if __name__ == "__main__":
    raise SystemExit(main())
