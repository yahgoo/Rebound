"""Smoke: ModelRouter + Gemini structured / timeout / schema (Task 13 Verify).

Deliberate exception to Task 13's file allowlist: the Verify block requires
`python -m packages.router.smoke_router`, which cannot run without this module.
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any

from pydantic import BaseModel, Field, field_validator

from packages.router import ModelSchemaError, ModelTimeoutError, get_router
from packages.router.base import ModelRequest, ModelResponse


class TwoField(BaseModel):
    city: str = Field(description="IATA city or airport code, uppercase")
    reason: str = Field(description="One short sentence")


class Impossible(BaseModel):
    """Any value fails a custom validator — forces ModelSchemaError after one retry.

    Schema shape stays Gemini-compatible (plain int). Validation always rejects.
    """

    n: int = Field(description="Any integer; host-side validation always rejects it")

    @field_validator("n")
    @classmethod
    def _never_valid(cls, value: int) -> int:
        raise ValueError(
            f"n={value!r} is never acceptable for Impossible (forced schema failure)"
        )


async def _main() -> None:
    router = get_router()

    # Capture latency_ms / call counts from generate without changing production API.
    calls: list[ModelResponse] = []
    original_generate = router.generate

    async def counting_generate(
        request: ModelRequest, *, backend: Any = None
    ) -> ModelResponse:
        resp = await original_generate(request, backend=backend)
        calls.append(resp)
        return resp

    router.generate = counting_generate  # type: ignore[method-assign]

    print("=== structured (TwoField) ===", flush=True)
    calls.clear()
    # Operational timeout: INTERFACES default 20s is tight for Gemini 3.6 thinking.
    obj = await router.generate_structured(
        ModelRequest(
            system="You are a careful JSON generator. Output only valid JSON.",
            prompt=(
                "Return JSON with city='SIN' and reason explaining why Singapore "
                "Changi is a common hub for Southeast Asia recovery flights."
            ),
            temperature=0.0,
            # Thinking tokens can consume hundreds of the output budget.
            max_output_tokens=2048,
            timeout_seconds=60.0,
        ),
        TwoField,
    )
    last = calls[-1]
    print(f"object={obj.model_dump()}", flush=True)
    print(f"latency_ms={last.latency_ms}", flush=True)
    print(f"backend={last.backend.value} model={last.model_name}", flush=True)
    print(
        f"input_tokens={last.input_tokens} output_tokens={last.output_tokens}",
        flush=True,
    )
    print(f"generate_calls={len(calls)}", flush=True)

    print("\n=== ModelTimeoutError (timeout_seconds=0.001) ===", flush=True)
    try:
        await router.generate(
            ModelRequest(
                system="You are a helpful assistant.",
                prompt="Reply with the word OK.",
                timeout_seconds=0.001,
            )
        )
        print("ERROR: expected ModelTimeoutError", flush=True)
    except ModelTimeoutError:
        traceback.print_exc()

    print(
        "\n=== ModelSchemaError (impossible schema after one retry) ===",
        flush=True,
    )
    calls.clear()
    try:
        await router.generate_structured(
            ModelRequest(
                system="Output only JSON matching the schema. No commentary.",
                prompt=(
                    "Fill field n with any integer. The schema requires n > 100 "
                    "and n < 0 simultaneously."
                ),
                temperature=0.0,
                max_output_tokens=2048,
                timeout_seconds=60.0,
            ),
            Impossible,
        )
        print("ERROR: expected ModelSchemaError", flush=True)
    except ModelSchemaError:
        traceback.print_exc()
        print(
            f"generate_calls_during_schema_error={len(calls)} (expect exactly 2)",
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(_main())
