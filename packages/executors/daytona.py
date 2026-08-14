"""DaytonaExecutor — Zone B scoring in Daytona sandboxes (I4, I10).

The Atlas master secret must never appear in this module (I4).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any, Awaitable, Callable

from daytona import (
    AsyncDaytona,
    AsyncSandbox,
    CreateSandboxFromSnapshotParams,
    DaytonaConfig,
)

from packages.domain.enums import ExecutorKind
from packages.executors.base import (
    ExecutorUnavailableError,
    SandboxStatus,
    ScoredCandidate,
    ScoringInput,
)
from packages.executors.local import (
    _distribute,
    _parse_scored,
    _slot_payload,
    assert_zone_b_allowlist,
)


def _build_runner(scoring_code: str, payload: dict[str, Any]) -> str:
    """Wrap model-generated scoring_code so stdout is a JSON result envelope."""
    payload_literal = json.dumps(json.dumps(payload))
    return (
        "import json\n"
        f"{scoring_code}\n"
        f"_payload = json.loads({payload_literal})\n"
        "if 'score' not in globals() or not callable(score):\n"
        "    raise RuntimeError('scoring_code must define callable score(payload)')\n"
        "_raw = score(_payload)\n"
        "if not isinstance(_raw, list):\n"
        "    raise TypeError('score() must return a list')\n"
        "print(json.dumps({'ok': True, 'results': _raw}))\n"
    )


class DaytonaExecutor:
    kind = ExecutorKind.DAYTONA

    def __init__(
        self,
        api_key: str,
        *,
        target_slots: int = 8,
        timeout_seconds: int = 20,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if target_slots < 1:
            raise ValueError("target_slots must be >= 1")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be >= 1")
        self._api_key = api_key
        self._target_slots = target_slots
        self._timeout_seconds = timeout_seconds
        self._client: AsyncDaytona | None = None
        # Per-process key for scoped tokens — never the Atlas master secret.
        self._token_secret = secrets.token_urlsafe(24)

    def _client_or_raise(self) -> AsyncDaytona:
        if self._client is None:
            try:
                self._client = AsyncDaytona(DaytonaConfig(api_key=self._api_key))
            except Exception as exc:
                raise ExecutorUnavailableError(
                    f"daytona auth/init failed: {exc}"
                ) from exc
        return self._client

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    async def _mint_scoped_token(self, slot: int) -> str:
        """Single-use, scoped, short-lived. NEVER the Atlas master secret (I4)."""
        nonce = secrets.token_urlsafe(12)
        expires_at = int(time.time()) + min(60, self._timeout_seconds + 30)
        # Opaque token bound to this executor instance + slot; not derived from Atlas.
        return f"rb.slot{slot}.{expires_at}.{nonce}.{self._token_secret[:8]}"

    async def _safe_delete(self, client: AsyncDaytona, sandbox: AsyncSandbox) -> None:
        """Always attempt delete/archive — stopped sandboxes still bill for disk [O]."""
        last_err: Exception | None = None
        for _ in range(3):
            try:
                await client.delete(sandbox, timeout=60, wait=True)
                return
            except Exception as exc:
                last_err = exc
                msg = str(exc).lower()
                if "not found" in msg or "404" in msg:
                    return
            try:
                await sandbox.delete(timeout=60, wait=True)
                return
            except Exception as exc:
                last_err = exc
                msg = str(exc).lower()
                if "not found" in msg or "404" in msg:
                    return
            await asyncio.sleep(0.5)
        # Best-effort only — never raise from cleanup.
        _ = last_err

    async def _sweep_labeled(
        self, client: AsyncDaytona, *, case_ref: str
    ) -> None:
        """Delete any leftover sandboxes labeled for this case (delete races)."""
        label_case = case_ref[:64]
        try:
            async for sb in client.list():
                labels = getattr(sb, "labels", None) or {}
                if not isinstance(labels, dict):
                    continue
                if labels.get("rebound") != "executor":
                    continue
                if labels.get("case_ref") != label_case:
                    continue
                await self._safe_delete(client, sb)
        except Exception:
            pass

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
        client = self._client_or_raise()
        live: list[AsyncSandbox] = []
        live_lock = asyncio.Lock()

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

        for slot in range(self._target_slots):
            await emit(slot, "pending", None)

        async def run_slot(slot: int) -> list[ScoredCandidate]:
            sandbox: AsyncSandbox | None = None
            sandbox_id: str | None = None
            try:
                token = await self._mint_scoped_token(slot)
                await emit(slot, "starting", None)
                # Default snapshot sizing is 1 vCPU / 1 GiB / 3 GiB; never request GPU.
                params = CreateSandboxFromSnapshotParams(
                    language="python",
                    env_vars={"REBOUND_SCOPED_TOKEN": token},
                    labels={
                        "rebound": "executor",
                        "slot": str(slot),
                        "case_ref": payload.case_ref[:64],
                    },
                    ephemeral=True,
                )
                try:
                    sandbox = await asyncio.wait_for(
                        client.create(
                            params,
                            timeout=float(self._timeout_seconds),
                        ),
                        timeout=float(self._timeout_seconds) + 30.0,
                    )
                except asyncio.TimeoutError as exc:
                    await emit(slot, "failed", None)
                    raise ExecutorUnavailableError(
                        f"daytona create timeout for slot {slot}"
                    ) from exc
                except Exception as exc:
                    await emit(slot, "failed", None)
                    raise ExecutorUnavailableError(
                        f"daytona create failed for slot {slot}: {exc}"
                    ) from exc

                sandbox_id = str(sandbox.id)
                async with live_lock:
                    live.append(sandbox)
                await emit(slot, "running", sandbox_id)

                runner = _build_runner(
                    scoring_code, _slot_payload(payload, slots[slot])
                )
                try:
                    try:
                        resp = await asyncio.wait_for(
                            sandbox.process.code_run(
                                runner, timeout=self._timeout_seconds
                            ),
                            timeout=float(self._timeout_seconds) + 5.0,
                        )
                    except asyncio.TimeoutError as exc:
                        await emit(slot, "failed", sandbox_id)
                        raise ExecutorUnavailableError(
                            f"daytona code_run timeout for slot {slot}"
                        ) from exc

                    if getattr(resp, "exit_code", 1) != 0:
                        await emit(slot, "failed", sandbox_id)
                        return []

                    stdout = getattr(resp, "result", None) or ""
                    try:
                        body = json.loads(
                            stdout if isinstance(stdout, str) else str(stdout)
                        )
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
            except ExecutorUnavailableError:
                raise
            except Exception:
                await emit(slot, "failed", sandbox_id)
                return []
            finally:
                # Non-negotiable: always delete, including on exception/cancellation.
                if sandbox is not None:
                    await self._safe_delete(client, sandbox)
                    async with live_lock:
                        if sandbox in live:
                            live.remove(sandbox)

        try:
            results_nested = await asyncio.gather(
                *[run_slot(s) for s in range(self._target_slots)]
            )
        finally:
            # Belt-and-suspenders for any sandbox that escaped a slot finally
            # (e.g. cancellation between create and append).
            async with live_lock:
                leftovers = list(live)
                live.clear()
            for sb in leftovers:
                await self._safe_delete(client, sb)
            await self._sweep_labeled(client, case_ref=payload.case_ref)

        merged: list[ScoredCandidate] = []
        for group in results_nested:
            merged.extend(group)

        # Identical to LocalExecutor: score descending, tie-break by offer_id.
        merged.sort(key=lambda c: (-c.score, c.offer_id))
        return merged
