"""Cassette recorder/player for Atlas request/response fixtures (I4, I9)."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from packages.atlas.errors import CassetteMissError

# Volatile request fields excluded from key_for so replay matches on intent.
_VOLATILE_KEYS = frozenset(
    {
        "sessionid",
        "session_id",
        "timestamp",
        "timestamps",
        "time",
        "ts",
        "nonce",
        "nonces",
        "requesttime",
        "request_time",
        "recorded_at",
        "datetime",
        "date_time",
    }
)

# Keys whose values must never reach disk (I4).
_SENSITIVE_KEYS = frozenset(
    {
        "number",
        "cardnumber",
        "card_number",
        "pan",
        "cvv",
        "cvc",
        "securitycode",
        "security_code",
        "holder_given_name",
        "holder_surname",
        "holdername",
        "holder_name",
        "cardholder",
        "cardholdername",
        "card_holder",
        "card_holder_name",
        "cardholderfirstname",
        "cardholderlastname",
        "passport_number",
        "passportnumber",
        "passport",
        "cardnum",  # Atlas passenger passport field
        "date_of_birth",
        "dateofbirth",
        "birthday",
        "dob",
    }
)

_PAN_RE = re.compile(r"(?<!\d)(\d{13,19})(?!\d)")
_REDACTED = "[REDACTED]"


def _norm_key(key: str) -> str:
    return key.replace("-", "").replace("_", "").lower()


def _is_volatile(key: str) -> bool:
    return key.lower() in _VOLATILE_KEYS or _norm_key(key) in {
        "sessionid",
        "timestamp",
        "timestamps",
        "nonce",
        "nonces",
        "requesttime",
        "recordedat",
    }


def _is_sensitive_key(key: str) -> bool:
    nk = _norm_key(key)
    return key.lower() in _SENSITIVE_KEYS or nk in {
        _norm_key(k) for k in _SENSITIVE_KEYS
    }


def _luhn_ok(digits: str) -> bool:
    total = 0
    reverse = digits[::-1]
    for i, ch in enumerate(reverse):
        n = ord(ch) - 48
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _redact_string(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        digits = match.group(1)
        if _luhn_ok(digits):
            return _REDACTED
        return digits

    return _PAN_RE.sub(repl, value)


def _strip_volatile(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _strip_volatile(v)
            for k, v in obj.items()
            if not _is_volatile(str(k))
        }
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if _is_sensitive_key(str(k)):
                out[k] = _REDACTED
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, str):
        return _redact_string(obj)
    return obj


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class Cassette(BaseModel):
    key: str  # deterministic hash of (path, canonicalised payload)
    path: str
    request: dict  # redacted: never card data (I4)
    response: dict
    latency_ms: int
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CassetteRecorder:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(path: str, payload: dict) -> str:
        """Deterministic and stable. Volatile fields (timestamps, nonces,
        sessionId) are excluded from the key so replay matches on intent."""
        material = path + "\n" + _canonical_json(_strip_volatile(payload))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def record(
        self, *, path: str, payload: dict, response: dict, latency_ms: int
    ) -> None:
        key = self.key_for(path, payload)
        cassette = Cassette(
            key=key,
            path=path,
            request=_redact(deepcopy(payload)),
            response=_redact(deepcopy(response)),
            latency_ms=latency_ms,
            recorded_at=datetime.now(UTC),
        )
        target = self.directory / f"{key}.json"
        # Ensure I4: never write unredacted sensitive material.
        text = cassette.model_dump_json(indent=2)
        target.write_text(text + "\n", encoding="utf-8")


class CassettePlayer:
    def __init__(self, directory: Path, *, reproduce_latency: bool = True) -> None:
        self.directory = Path(directory)
        self.reproduce_latency = reproduce_latency
        self._by_key: dict[str, Cassette] = {}
        if self.directory.is_dir():
            for file in sorted(self.directory.glob("*.json")):
                data = json.loads(file.read_text(encoding="utf-8"))
                cassette = Cassette.model_validate(data)
                self._by_key[cassette.key] = cassette

    async def play(self, *, path: str, payload: dict) -> dict:
        """Raises CassetteMissError when no recording matches."""
        import asyncio

        key = CassetteRecorder.key_for(path, payload)
        cassette = self._by_key.get(key)
        if cassette is None:
            # Also accept file present but not loaded (hot add).
            file = self.directory / f"{key}.json"
            if file.is_file():
                cassette = Cassette.model_validate(
                    json.loads(file.read_text(encoding="utf-8"))
                )
                self._by_key[key] = cassette
            else:
                raise CassetteMissError(
                    code="cassette_miss",
                    message=f"no cassette for path={path!r} key={key}",
                )
        if self.reproduce_latency and cassette.latency_ms > 0:
            await asyncio.sleep(cassette.latency_ms / 1000.0)
        return deepcopy(cassette.response)

    def keys(self) -> list[str]:
        return sorted(self._by_key.keys())
