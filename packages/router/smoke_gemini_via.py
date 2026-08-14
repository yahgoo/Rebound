"""Smoke: GEMINI transport parity — direct google-genai vs OpenRouter.

Usage:
  python -m packages.router.smoke_gemini_via
  GEMINI_VIA=openrouter python -m packages.router.smoke_gemini_via

Runs generate_structured(TwoField) on whichever transport the env selects.
"""

from __future__ import annotations

import asyncio
import os

from pydantic import BaseModel, Field

from apps.api.settings import get_settings
from packages.router import get_router
from packages.router.base import ModelRequest
from packages.router.gemini import build_gemini_client
from packages.router.openrouter_gemini import OpenRouterGeminiClient


class TwoField(BaseModel):
    city: str = Field(description="IATA city or airport code, uppercase")
    reason: str = Field(description="One short sentence")


async def _run() -> int:
    get_settings.cache_clear()
    settings = get_settings()
    via = (settings.gemini_via or "").strip().lower() or "direct"
    client = build_gemini_client(settings)
    transport = (
        "openrouter"
        if isinstance(client, OpenRouterGeminiClient)
        else "google-genai"
    )
    print(f"GEMINI_VIA_env={os.environ.get('GEMINI_VIA')!r}", flush=True)
    print(f"settings.gemini_via={settings.gemini_via!r} resolved={via!r}", flush=True)
    print(f"transport={transport} model={client.model_name}", flush=True)
    print(
        f"client_type={type(client).__name__} backend={client.backend.value}",
        flush=True,
    )

    router = get_router(settings=settings)
    obj = await router.generate_structured(
        ModelRequest(
            system="You are a careful JSON generator. Output only valid JSON.",
            prompt=(
                "Return JSON with city='SIN' and reason explaining why Singapore "
                "Changi is a common hub for Southeast Asia recovery flights."
            ),
            temperature=0.0,
            max_output_tokens=2048,
            timeout_seconds=90.0,
        ),
        TwoField,
    )
    print(f"structured={obj.model_dump()}", flush=True)
    print(f"city_ok={obj.city.upper() == 'SIN'}", flush=True)
    print(f"reason_nonempty={bool(obj.reason and obj.reason.strip())}", flush=True)
    return 0 if obj.city.upper() == "SIN" and obj.reason.strip() else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
