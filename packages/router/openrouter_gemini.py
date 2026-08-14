"""OpenRouter transport for ModelBackend.GEMINI (env GEMINI_VIA=openrouter).

OpenAI-compatible Chat Completions against https://openrouter.ai/api/v1.
Same ModelClient surface as GeminiClient — agents do not change.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx

from packages.router.base import (
    ModelBackend,
    ModelRequest,
    ModelResponse,
    ModelTimeoutError,
)

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "google/gemini-3.6-flash"

# ModelRequest.mime_type → OpenRouter input_audio.format
_AUDIO_FORMAT_BY_MIME: dict[str, str] = {
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/aac": "aac",
    "audio/x-aac": "aac",
    "audio/webm": "wav",  # OpenRouter has no webm; wav is the safe fallback label
}


def _audio_format(mime_type: str) -> str:
    key = (mime_type or "").strip().lower()
    if key in _AUDIO_FORMAT_BY_MIME:
        return _AUDIO_FORMAT_BY_MIME[key]
    # Last path segment after / (e.g. "m4a" from a custom type) or default.
    if "/" in key:
        subtype = key.split("/", 1)[1]
        if subtype in {"m4a", "mp3", "wav", "ogg", "flac", "aac", "aiff", "pcm16", "pcm24"}:
            return subtype
    return "m4a"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class OpenRouterGeminiClient:
    """GEMINI backend via OpenRouter PAYGO — drop-in for GeminiClient."""

    backend = ModelBackend.GEMINI
    supports_images = True
    supports_audio = True

    def __init__(
        self,
        api_key: str,
        *,
        model_name: str = _DEFAULT_MODEL,
        base_url: str = _OPENROUTER_BASE,
    ) -> None:
        if not api_key:
            raise ValueError("OpenRouterGeminiClient requires OPENROUTER_API_KEY")
        self._api_key = api_key
        self.model_name = model_name
        self._base_url = base_url.rstrip("/")

    def _build_user_content(self, request: ModelRequest) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
        for image in request.images:
            mime = image.mime_type or "image/jpeg"
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{_b64(image.data)}",
                    },
                }
            )
        for audio in request.audio:
            parts.append(
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": _b64(audio.data),
                        "format": _audio_format(audio.mime_type),
                    },
                }
            )
        return parts

    def _build_messages(self, request: ModelRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append(
            {"role": "user", "content": self._build_user_content(request)}
        )
        return messages

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": self._build_messages(request),
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        if request.response_schema is not None:
            # OpenAI-compatible structured outputs; ModelRouter still validates.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": False,
                    "schema": request.response_schema,
                },
            }
        return payload

    async def generate(self, request: ModelRequest) -> ModelResponse:
        timeout = max(0.001, float(request.timeout_seconds))
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/yahgoo/Rebound",
            "X-Title": "Rebound",
        }
        payload = self._build_payload(request)
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(
                f"OpenRouter Gemini call timed out after {request.timeout_seconds}s "
                f"(model={self.model_name})"
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            # Surface provider errors plainly; never fabricate a completion.
            raise RuntimeError(
                f"OpenRouter error HTTP {response.status_code}: "
                f"{response.text[:800]}"
            )

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenRouter returned no choices: {body!r}"[:800])
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):
            # Some providers return content parts; join text fragments.
            text = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in text
            )

        usage = body.get("usage") or {}
        return ModelResponse(
            text=text,
            backend=self.backend,
            model_name=self.model_name,
            latency_ms=latency_ms,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
