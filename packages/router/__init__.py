"""Model router factory — register only backends whose credentials are present."""

from __future__ import annotations

from packages.router.base import (
    ModelBackend,
    ModelClient,
    ModelRouter,
    ModelSchemaError,
    ModelTimeoutError,
)
from packages.router.gemini import GeminiClient

__all__ = [
    "GeminiClient",
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
    """
    if settings is None:
        from apps.api.settings import get_settings

        settings = get_settings()

    clients: dict[ModelBackend, ModelClient] = {}

    gemini_key = getattr(settings, "gemini_api_key", None)
    if gemini_key:
        clients[ModelBackend.GEMINI] = GeminiClient(gemini_key)

    default = getattr(settings, "model_router_default", ModelBackend.GEMINI)
    if not isinstance(default, ModelBackend):
        default = ModelBackend(str(default))

    if default not in clients:
        # Prefer an available backend rather than constructing an unusable router.
        if clients:
            default = next(iter(clients))
        else:
            raise RuntimeError(
                "No model backends configured. Set GEMINI_API_KEY (or another "
                "supported backend credential)."
            )

    return ModelRouter(clients, default=default)
