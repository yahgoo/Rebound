"""Live and replay Atlas transports (INTERFACES.md §1.3; I9)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from urllib.parse import urljoin

import httpx

from packages.atlas.errors import (
    AtlasAuthError,
    AtlasDuplicateBookingError,
    AtlasError,
    AtlasPaymentDeclinedError,
    AtlasThreeDSRequiredError,
    AtlasTimeoutError,
)

if TYPE_CHECKING:
    from packages.atlas.cassette import CassettePlayer, CassetteRecorder

_AUTH_HINTS = (
    "allowlist",
    "allow list",
    "whitelist",
    "source ip",
    "client ip",
    "unauthorized",
    "unauthorised",
    "authentication",
    "credential",
    "invalid client",
    "access denied",
    "forbidden",
    "not authenticated",
)


def _extract_code_and_message(body: dict) -> tuple[str, str]:
    raw_code = body.get("status", body.get("errorCode", body.get("code", "unknown")))
    code = str(raw_code)
    message = (
        body.get("msg")
        or body.get("message")
        or body.get("errorMsg")
        or body.get("errorMessage")
        or body.get("msgInfo")
        or ""
    )
    if not isinstance(message, str):
        message = str(message)
    return code, message


def _looks_like_auth_failure(*, code: str, message: str, http_status: int | None) -> bool:
    # Known non-auth Atlas status codes that may arrive on HTTP 403
    # (e.g. deposit-only fare 403, or 318 duplicate booking). Do not
    # mis-classify these as auth failures.
    _NON_AUTH_CODES = {"318"}
    if code in _NON_AUTH_CODES:
        return False
    if http_status in {401, 403}:
        return True
    lowered = message.lower()
    if any(hint in lowered for hint in _AUTH_HINTS):
        return True
    # Common Atlas-style auth/IP failure codes when surfaced as status.
    if code in {"401", "403", "1001", "1002", "1003"}:
        return True
    return False


def raise_for_atlas_response(body: dict, *, http_status: int | None = None) -> None:
    """Map non-success Atlas responses onto typed errors. Success is status 0."""
    if not isinstance(body, dict):
        raise AtlasError(
            code="invalid_response",
            message="Atlas response was not a JSON object",
            http_status=http_status,
        )

    if "status" not in body and "errorCode" not in body:
        # Some endpoints may return bare payloads; treat as success.
        return

    code, message = _extract_code_and_message(body)
    if code in {"0", "00"}:
        return

    if code == "318":
        dup_orders = body.get("duplicateOrders") or []
        if not isinstance(dup_orders, list):
            dup_orders = [str(dup_orders)]
        raise AtlasDuplicateBookingError(
            code=code,
            message=message or "Duplicate booking",
            duplicate_orders=[str(o) for o in dup_orders],
            http_status=http_status,
        )
    if code == "604":
        raise AtlasPaymentDeclinedError(code=code, message=message or "Payment declined", http_status=http_status)
    if code == "616":
        raise AtlasThreeDSRequiredError(
            code=code, message=message or "3DS required", http_status=http_status
        )
    if _looks_like_auth_failure(code=code, message=message, http_status=http_status):
        raise AtlasAuthError(
            code=code,
            message=message or "Atlas authentication failed",
            http_status=http_status,
        )
    raise AtlasError(code=code, message=message or "Atlas error", http_status=http_status)


class LiveTransport:
    """Sends x-atlas-client-id / x-atlas-client-secret headers, plus Accept,
    Content-Type and Accept-Encoding; handles gzip responses [E]."""

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        *,
        recorder: CassetteRecorder | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.client_id = client_id
        self.client_secret = client_secret
        self.recorder = recorder
        self.timeout_seconds = timeout_seconds
        self._headers = {
            "x-atlas-client-id": client_id,
            "x-atlas-client-secret": client_secret,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
        }

    async def post(self, path: str, payload: dict) -> dict:
        url = urljoin(self.base_url, path.lstrip("/"))
        timeout = httpx.Timeout(self.timeout_seconds)
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                headers=self._headers,
                trust_env=False,
            ) as client:
                response = await client.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise AtlasTimeoutError(
                code="timeout",
                message=f"Atlas request timed out after {self.timeout_seconds}s",
                http_status=None,
            ) from exc

        latency_ms = max(0, int((time.perf_counter() - started) * 1000))

        try:
            body = response.json()
        except ValueError as exc:
            raise AtlasError(
                code="invalid_json",
                message=f"Atlas returned non-JSON body (HTTP {response.status_code})",
                http_status=response.status_code,
            ) from exc

        if not isinstance(body, dict):
            raise AtlasError(
                code="invalid_response",
                message="Atlas response was not a JSON object",
                http_status=response.status_code,
            )

        if self.recorder is not None:
            await self.recorder.record(
                path=path,
                payload=payload,
                response=body,
                latency_ms=latency_ms,
            )

        if response.status_code in {401, 403}:
            code, message = _extract_code_and_message(body)
            raise AtlasAuthError(
                code=code if code not in {"0", "00", "unknown"} else str(response.status_code),
                message=message or f"HTTP {response.status_code}",
                http_status=response.status_code,
            )

        raise_for_atlas_response(body, http_status=response.status_code)
        return body


class ReplayTransport:
    """Serves recorded responses and reproduces recorded latency (I9)."""

    def __init__(self, player: CassettePlayer) -> None:
        self.player = player

    async def post(self, path: str, payload: dict) -> dict:
        body = await self.player.play(path=path, payload=payload)
        raise_for_atlas_response(body, http_status=None)
        return body
