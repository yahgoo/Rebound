"""LocalExecutor — Zone B scoring in restricted subprocesses (I10)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, Awaitable, Callable

from packages.domain.enums import ExecutorKind
from packages.executors.base import (
    CandidateForScoring,
    ExecutorUnavailableError,
    SandboxStatus,
    ScoredCandidate,
    ScoringInput,
)
from packages.guardian.redaction import assert_no_pii

# Top-level + candidate keys that ScoringInput may serialise. Anything else is
# refused before a subprocess is spawned.
_SCORING_INPUT_KEYS = frozenset(
    {
        "case_ref",
        "candidates",
        "must_arrive_by",
        "budget_ceiling_sgd",
        "mobility_penalty_weight",
        "original_arrival_at",
    }
)
_CANDIDATE_KEYS = frozenset(
    {
        "offer_id",
        "price",
        "currency",
        "arrival_at",
        "stop_count",
        "min_transfer_minutes",
        "origin",
        "destination",
        "carriers",
    }
)
_FORBIDDEN_KEYS = frozenset(
    {
        "given_name",
        "surname",
        "passport_number",
        "nationality",
        "date_of_birth",
        "holder_given_name",
        "holder_surname",
        "cvv",
        "expiry_month",
        "expiry_year",
        "card",
        "card_details",
        "passengers",
        "passenger",
        "client_secret",
        "atlas_client_secret",
        "atlas_secret",
        "ATLAS_CLIENT_SECRET",
        "pan",
        "payment_card",
    }
)

# Child runner: install network / write FS guards, then exec scoring_code.
# Communicates via stdin (JSON request) / stdout (JSON response).
_CHILD_RUNNER = r"""
import builtins
import json
import os
import socket
import sys

def _deny_network(*_a, **_k):
    raise OSError("network disabled in LocalExecutor sandbox")

socket.socket = _deny_network  # type: ignore[misc, assignment]
socket.create_connection = _deny_network  # type: ignore[misc, assignment]
socket.create_server = _deny_network  # type: ignore[misc, assignment]
if hasattr(socket, "socketpair"):
    socket.socketpair = _deny_network  # type: ignore[misc, assignment]

_real_open = builtins.open

def _guarded_open(file, mode="r", *args, **kwargs):
    m = mode if isinstance(mode, str) else "r"
    # Deny write / append / exclusive-create / read-write. Read-only is allowed.
    if set(m) & set("wax") or "+" in m:
        raise PermissionError("filesystem write disabled in LocalExecutor sandbox")
    return _real_open(file, mode, *args, **kwargs)

builtins.open = _guarded_open

def _deny_write(*_a, **_k):
    raise PermissionError("filesystem write disabled in LocalExecutor sandbox")

os.remove = _deny_write  # type: ignore[assignment]
os.unlink = _deny_write  # type: ignore[assignment]
os.rename = _deny_write  # type: ignore[assignment]
os.replace = _deny_write  # type: ignore[assignment]
os.makedirs = _deny_write  # type: ignore[assignment]
os.mkdir = _deny_write  # type: ignore[assignment]
os.rmdir = _deny_write  # type: ignore[assignment]
if hasattr(os, "link"):
    os.link = _deny_write  # type: ignore[assignment]
if hasattr(os, "symlink"):
    os.symlink = _deny_write  # type: ignore[assignment]

req = json.load(sys.stdin)
ns: dict = {}
exec(req["scoring_code"], ns, ns)
if "score" not in ns or not callable(ns["score"]):
    raise RuntimeError("scoring_code must define callable score(payload)")
raw = ns["score"](req["payload"])
if not isinstance(raw, list):
    raise TypeError("score() must return a list")
json.dump({"ok": True, "results": raw}, sys.stdout)
"""


def _walk_forbidden_keys(obj: Any) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _FORBIDDEN_KEYS:
                raise ValueError(
                    f"Zone B allowlist violation: forbidden key {key!r}"
                )
            _walk_forbidden_keys(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _walk_forbidden_keys(item)


def assert_zone_b_allowlist(payload: ScoringInput) -> None:
    """Refuse anything outside ScoringInput before Zone B dispatch.

    Checks structural allowlist, PII shapes, and Atlas secret leakage.
    """
    data = payload.model_dump(mode="json")
    extra_top = set(data) - _SCORING_INPUT_KEYS
    if extra_top:
        raise ValueError(f"Zone B allowlist violation: extra keys {sorted(extra_top)}")
    missing_top = _SCORING_INPUT_KEYS - set(data)
    if missing_top:
        raise ValueError(
            f"Zone B allowlist violation: missing keys {sorted(missing_top)}"
        )
    for cand in data["candidates"]:
        if not isinstance(cand, dict):
            raise ValueError("Zone B allowlist violation: candidate must be a dict")
        extra_c = set(cand) - _CANDIDATE_KEYS
        if extra_c:
            raise ValueError(
                f"Zone B allowlist violation: candidate extra keys {sorted(extra_c)}"
            )
        missing_c = _CANDIDATE_KEYS - set(cand)
        if missing_c:
            raise ValueError(
                f"Zone B allowlist violation: candidate missing keys {sorted(missing_c)}"
            )
    _walk_forbidden_keys(data)

    # Flight timestamps are allowlisted on ScoringInput but look DOB-shaped to
    # assert_no_pii. Scrub only those known fields, then scan the rest for PII.
    pii_scan: dict[str, Any] = {
        "case_ref": data["case_ref"],
        "budget_ceiling_sgd": data["budget_ceiling_sgd"],
        "mobility_penalty_weight": data["mobility_penalty_weight"],
        "candidates": [
            {
                "offer_id": c["offer_id"],
                "price": c["price"],
                "currency": c["currency"],
                "stop_count": c["stop_count"],
                "min_transfer_minutes": c["min_transfer_minutes"],
                "origin": c["origin"],
                "destination": c["destination"],
                "carriers": c["carriers"],
            }
            for c in data["candidates"]
        ],
    }
    assert_no_pii(pii_scan)

    secret = os.environ.get("ATLAS_CLIENT_SECRET")
    if secret:
        blob = json.dumps(data, default=str)
        if secret and secret in blob:
            raise ValueError("Zone B allowlist violation: Atlas secret in payload")


def _distribute(
    candidates: list[CandidateForScoring], target_slots: int
) -> list[list[CandidateForScoring]]:
    slots: list[list[CandidateForScoring]] = [[] for _ in range(target_slots)]
    for i, cand in enumerate(candidates):
        slots[i % target_slots].append(cand)
    return slots


def _slot_payload(payload: ScoringInput, slot_candidates: list[CandidateForScoring]) -> dict:
    """Build the dict passed into score() for one slot — still ScoringInput-shaped."""
    subset = ScoringInput(
        case_ref=payload.case_ref,
        candidates=slot_candidates,
        must_arrive_by=payload.must_arrive_by,
        budget_ceiling_sgd=payload.budget_ceiling_sgd,
        mobility_penalty_weight=payload.mobility_penalty_weight,
        original_arrival_at=payload.original_arrival_at,
    )
    return subset.model_dump(mode="json")


def _parse_scored(raw: Any, allowed_ids: set[str]) -> list[ScoredCandidate]:
    """Validate ScoredCandidate shapes; silently drop unknown offer_ids (I1)."""
    if not isinstance(raw, list):
        return []
    out: list[ScoredCandidate] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        offer_id = item.get("offer_id")
        if not isinstance(offer_id, str) or offer_id not in allowed_ids:
            continue
        try:
            out.append(ScoredCandidate.model_validate(item))
        except Exception:
            continue
    return out


class LocalExecutor:
    kind = ExecutorKind.LOCAL

    def __init__(self, *, target_slots: int = 8, timeout_seconds: int = 20) -> None:
        """Runs scoring_code in a subprocess with no network and no filesystem
        write access. Emits SandboxStatus per slot so the UI is mode-agnostic."""
        if target_slots < 1:
            raise ValueError("target_slots must be >= 1")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be >= 1")
        self._target_slots = target_slots
        self._timeout_seconds = timeout_seconds

    async def close(self) -> None:
        return None

    async def score(
        self,
        payload: ScoringInput,
        scoring_code: str,
        *,
        on_status: Callable[[SandboxStatus], Awaitable[None]] | None = None,
    ) -> list[ScoredCandidate]:
        assert_zone_b_allowlist(payload)

        allowed_ids = {c.offer_id for c in payload.candidates}
        slots = _distribute(payload.candidates, self._target_slots)
        started = time.monotonic()

        async def emit(slot: int, state: str, sandbox_id: str | None) -> None:
            if on_status is None:
                return
            elapsed_ms = int((time.monotonic() - started) * 1000)
            await on_status(
                SandboxStatus(
                    slot=slot,
                    state=state,
                    sandbox_id=sandbox_id,
                    elapsed_ms=elapsed_ms,
                )
            )

        # Every slot begins pending so the UI grid is fully populated (I10).
        for slot in range(self._target_slots):
            await emit(slot, "pending", None)

        async def run_slot(slot: int) -> list[ScoredCandidate]:
            sandbox_id = f"local-{slot}"
            await emit(slot, "starting", sandbox_id)
            await emit(slot, "running", sandbox_id)
            request = {
                "scoring_code": scoring_code,
                "payload": _slot_payload(payload, slots[slot]),
            }
            try:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-c",
                        _CHILD_RUNNER,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except OSError as exc:
                    await emit(slot, "failed", sandbox_id)
                    raise ExecutorUnavailableError(
                        f"subprocess spawn failed for slot {slot}: {exc}"
                    ) from exc

                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(json.dumps(request).encode("utf-8")),
                        timeout=self._timeout_seconds,
                    )
                except asyncio.TimeoutError as exc:
                    proc.kill()
                    await proc.wait()
                    await emit(slot, "failed", sandbox_id)
                    raise ExecutorUnavailableError(
                        f"subprocess timeout for slot {slot}"
                    ) from exc

                if proc.returncode != 0:
                    await emit(slot, "failed", sandbox_id)
                    return []

                try:
                    body = json.loads(stdout.decode("utf-8") or "null")
                except json.JSONDecodeError:
                    await emit(slot, "failed", sandbox_id)
                    return []

                if not isinstance(body, dict) or not body.get("ok"):
                    await emit(slot, "failed", sandbox_id)
                    return []

                scored = _parse_scored(body.get("results"), allowed_ids)
                await emit(slot, "done", sandbox_id)
                return scored
            except ExecutorUnavailableError:
                raise
            except Exception:
                await emit(slot, "failed", sandbox_id)
                return []

        results_nested = await asyncio.gather(
            *[run_slot(s) for s in range(self._target_slots)]
        )
        merged: list[ScoredCandidate] = []
        for group in results_nested:
            merged.extend(group)

        # Deterministic: score descending, tie-break by offer_id ascending.
        merged.sort(key=lambda c: (-c.score, c.offer_id))
        return merged
