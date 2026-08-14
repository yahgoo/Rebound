"""Gemini Flash 3.6 backend for the model router (Zone C).

Assumes inputs are already Guardian-redacted. Never redacts. Never handles
card data (I4).
"""

from __future__ import annotations

import asyncio
import json
import time

from google import genai
from google.genai import types

from packages.router.base import (
    ModelBackend,
    ModelRequest,
    ModelResponse,
    ModelTimeoutError,
)

# Confirmed working in the pre-Task-13 gate test.
_DEFAULT_MODEL = "gemini-3.6-flash"


class GeminiClient:
    backend = ModelBackend.GEMINI
    supports_images = True
    supports_audio = True

    def __init__(self, api_key: str, *, model_name: str = _DEFAULT_MODEL) -> None:
        self._api_key = api_key
        self.model_name = model_name
        self._client = genai.Client(api_key=api_key)

    def _build_contents(self, request: ModelRequest) -> list[types.Part]:
        parts: list[types.Part] = [types.Part.from_text(text=request.prompt)]
        for image in request.images:
            parts.append(
                types.Part.from_bytes(data=image.data, mime_type=image.mime_type)
            )
        for audio in request.audio:
            parts.append(
                types.Part.from_bytes(data=audio.data, mime_type=audio.mime_type)
            )
        return parts

    def _build_config(self, request: ModelRequest) -> types.GenerateContentConfig:
        # HttpOptions.timeout is milliseconds.
        timeout_ms = max(1, int(request.timeout_seconds * 1000))
        config_kwargs: dict = {
            "http_options": types.HttpOptions(timeout=timeout_ms),
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
            "system_instruction": request.system or None,
            # Structured JSON should not trigger AFC tool loops.
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        }
        if request.response_schema is not None:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_schema"] = request.response_schema
        return types.GenerateContentConfig(**config_kwargs)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        contents = self._build_contents(request)
        config = self._build_config(request)
        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                ),
                timeout=request.timeout_seconds,
            )
        except TimeoutError as exc:
            # Includes asyncio.TimeoutError. Never return partial / fabricated text.
            raise ModelTimeoutError(
                f"Gemini call timed out after {request.timeout_seconds}s "
                f"(model={self.model_name})"
            ) from exc
        except Exception as exc:
            # google-genai may surface deadline/timeout as other exception types.
            name = type(exc).__name__.lower()
            msg = str(exc).lower()
            if "timeout" in name or "timed out" in msg or "deadline" in msg:
                raise ModelTimeoutError(
                    f"Gemini call timed out after {request.timeout_seconds}s "
                    f"(model={self.model_name}): {exc}"
                ) from exc
            raise

        latency_ms = int((time.perf_counter() - started) * 1000)
        text = response.text
        if not text and response.parsed is not None:
            # Prefer SDK-parsed structured payload when .text is empty.
            text = (
                response.parsed
                if isinstance(response.parsed, str)
                else json.dumps(response.parsed)
            )
        text = text or ""
        usage = response.usage_metadata
        input_tokens = usage.prompt_token_count if usage else None
        output_tokens = usage.candidates_token_count if usage else None

        return ModelResponse(
            text=text,
            backend=self.backend,
            model_name=self.model_name,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
