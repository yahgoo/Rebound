"""Model router contracts and routing logic (INTERFACES.md §4).

Zone C: callers must already have redacted inputs. This module never redacts
and must never receive or forward card data (I4).
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class ModelBackend(StrEnum):
    GEMINI = "gemini"
    GEMMA = "gemma"
    KIMI = "kimi"
    QWEN = "qwen"


class ImagePart(BaseModel):
    mime_type: str
    data: bytes  # EXIF already stripped by Guardian


class AudioPart(BaseModel):
    mime_type: str
    data: bytes
    duration_seconds: float


class ModelRequest(BaseModel):
    """Everything here has already passed Guardian redaction (I4)."""

    system: str
    prompt: str
    images: list[ImagePart] = []
    audio: list[AudioPart] = []
    temperature: float = 0.0
    max_output_tokens: int = 2048
    # Frozen INTERFACES default is 20.0. Gemini 3.6 thinking + audio often need
    # more; callers should pass an explicit higher value (see docs/QODER.md).
    timeout_seconds: float = 20.0
    response_schema: dict | None = None  # JSON schema for structured output


class ModelResponse(BaseModel):
    text: str
    backend: ModelBackend
    model_name: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelClient(Protocol):
    backend: ModelBackend
    supports_images: bool
    supports_audio: bool

    async def generate(self, request: ModelRequest) -> ModelResponse: ...


class ModelTimeoutError(Exception):
    """Raised when a model call exceeds timeout_seconds. Never return partial text."""


class ModelSchemaError(Exception):
    """Raised when structured output fails schema validation after one retry (I1)."""


class ModelRouter:
    def __init__(
        self,
        clients: dict[ModelBackend, ModelClient],
        *,
        default: ModelBackend,
    ) -> None:
        self._clients = dict(clients)
        self._default = default

    def _supports(self, client: ModelClient, request: ModelRequest) -> bool:
        if request.images and not client.supports_images:
            return False
        if request.audio and not client.supports_audio:
            return False
        return True

    def _resolve(
        self, request: ModelRequest, *, backend: ModelBackend | None
    ) -> ModelClient:
        requested = backend if backend is not None else self._default
        client = self._clients.get(requested)
        if client is not None and self._supports(client, request):
            return client

        # Fall back only when unconfigured or missing a required modality.
        if requested != self._default:
            fallback = self._clients.get(self._default)
            if fallback is not None and self._supports(fallback, request):
                return fallback

        reason = "unconfigured" if client is None else "lacks required modality"
        raise RuntimeError(
            f"No usable model backend for {requested.value} ({reason}); "
            f"default={self._default.value}"
        )

    async def generate(
        self, request: ModelRequest, *, backend: ModelBackend | None = None
    ) -> ModelResponse:
        """Falls back to `default` when a requested backend is unconfigured or
        lacks a required modality. Raises ModelTimeoutError on timeout — never
        returns a fabricated response."""
        client = self._resolve(request, backend=backend)
        return await client.generate(request)

    async def generate_structured(
        self,
        request: ModelRequest,
        schema: type[T],
        *,
        backend: ModelBackend | None = None,
    ) -> T:
        """Validates against `schema`. On validation failure: retry once with the
        validation error appended, then raise ModelSchemaError. Never repair by
        guessing (I1)."""
        schema_dict = request.response_schema or schema.model_json_schema()
        base = request.model_copy(update={"response_schema": schema_dict})

        response = await self.generate(base, backend=backend)
        try:
            return self._parse_structured(response.text, schema)
        except (ValidationError, json.JSONDecodeError, ValueError) as first_err:
            retry_prompt = (
                f"{base.prompt}\n\n"
                f"Your previous reply failed schema validation:\n{first_err}\n"
                f"Reply again with JSON that satisfies the schema exactly. "
                f"Do not include markdown fences or commentary."
            )
            retry = base.model_copy(update={"prompt": retry_prompt})
            response = await self.generate(retry, backend=backend)
            try:
                return self._parse_structured(response.text, schema)
            except (ValidationError, json.JSONDecodeError, ValueError) as second_err:
                raise ModelSchemaError(
                    f"Structured output failed schema validation after one retry: "
                    f"{second_err}"
                ) from second_err

    @staticmethod
    def _parse_structured(text: str, schema: type[T]) -> T:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError(f"Expected a JSON object, got {type(payload).__name__}")
        return schema.model_validate(payload)
