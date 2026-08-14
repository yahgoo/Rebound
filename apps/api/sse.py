"""Typed Server-Sent Events and per-case wake-up publishing."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel

from packages.domain.enums import Actor
from packages.executors.base import SandboxStatus
from packages.guardian.policy import ConfirmationRequest


class TraceEvent(BaseModel):
    id: int
    case_ref: str
    actor: Actor
    step: str
    summary: str
    elapsed_ms: int
    status: str
    data: dict = {}


class SandboxGridEvent(BaseModel):
    id: int
    case_ref: str
    slots: list[SandboxStatus]


class CandidatesEvent(BaseModel):
    id: int
    case_ref: str
    candidates: list[dict]
    recommended_offer_id: str | None


class ConfirmationEvent(BaseModel):
    id: int
    case_ref: str
    request: ConfirmationRequest


class ReceiptEvent(BaseModel):
    id: int
    case_ref: str
    receipt: dict


class CaseStatusEvent(BaseModel):
    id: int
    case_ref: str
    status: str


class CaseEventPublisher:
    """Wake live subscribers after a case receives a durable AgentEvent.

    The queue contains only wake-up signals. Consumers always re-read the
    append-only audit log, which keeps reconnect and queue-overflow behaviour
    lossless.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[None]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def subscribe(self, case_ref: str) -> AsyncIterator[asyncio.Queue[None]]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        async with self._lock:
            self._subscribers[case_ref].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(case_ref)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(case_ref, None)

    async def publish(self, case_ref: str) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers.get(case_ref, ()))
        for queue in subscribers:
            if queue.empty():
                queue.put_nowait(None)


publisher = CaseEventPublisher()


def encode_sse(event: str, data: BaseModel | dict[str, Any], *, event_id: int | None) -> str:
    if isinstance(data, BaseModel):
        payload = data.model_dump(mode="json")
    else:
        payload = data
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(
        "data: "
        + json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
    )
    return "\n".join(lines) + "\n\n"
