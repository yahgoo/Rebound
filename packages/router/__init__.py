"""Model router factory — register only backends whose credentials are present."""

from __future__ import annotations

from packages.router.base import (
    ModelBackend,
    ModelClient,
    ModelRouter,
    ModelSchemaError,
    ModelTimeoutError,
)
from packages.router.gemini import (
    GeminiClient,
    build_gemini_client,
    gemini_credentials_present,
)
from packages.router.openrouter_gemini import OpenRouterGeminiClient

__all__ = [
    "GeminiClient",
    "OpenRouterGeminiClient",
    "ModelBackend",
    "ModelClient",
    "ModelRouter",
    "ModelSchemaError",
    "ModelTimeoutError",
    "get_router",
]


def get_router(*, settings: object | None = None) -> ModelRouter:
    """Build a ModelRouter from settings.

    Registers only backends whose credentials are present. Stretch backends
    (Gemma / Kimi / Qwen) are not implemented yet (Task 26).

    GEMINI transport: direct google-genai by default; set GEMINI_VIA=openrouter
    and OPENROUTER_API_KEY to route the same ModelBackend.GEMINI via OpenRouter.
    """
    if settings is None:
        from apps.api.settings import get_settings

        settings = get_settings()

    clients: dict[ModelBackend, ModelClient] = {}

    if gemini_credentials_present(settings):
        clients[ModelBackend.GEMINI] = build_gemini_client(settings)

    default = getattr(settings, "model_router_default", ModelBackend.GEMINI)
    if not isinstance(default, ModelBackend):
        default = ModelBackend(str(default))

    if default not in clients:
        # Prefer an available backend rather than constructing an unusable router.
        if clients:
            default = next(iter(clients))
        else:
            raise RuntimeError(
                "No model backends configured. Set GEMINI_API_KEY, or "
                "GEMINI_VIA=openrouter with OPENROUTER_API_KEY."
            )

    return ModelRouter(clients, default=default)
