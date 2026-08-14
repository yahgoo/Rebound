# packages/executors/base.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Awaitable, Callable, Protocol

from pydantic import BaseModel

from packages.domain.enums import ExecutorKind


class CandidateForScoring(BaseModel):
    offer_id: str
    price: Decimal
    currency: str
    arrival_at: datetime
    stop_count: int
    min_transfer_minutes: int | None
    origin: str
    destination: str
    carriers: list[str]


class ScoringInput(BaseModel):
    """Exactly what may cross into Zone B. Nothing else.

    No passenger names, no passport numbers, no card data, no Atlas secret.
    """

    case_ref: str
    candidates: list[CandidateForScoring]
    must_arrive_by: datetime | None
    budget_ceiling_sgd: Decimal
    mobility_penalty_weight: float  # derived from RecoveryIntent.mobility_notes
    original_arrival_at: datetime


class ScoredCandidate(BaseModel):
    offer_id: str
    score: float
    components: dict[str, float]  # must be explainable in the UI
    self_transfer_risk: float
    mobility_fit: float


class SandboxStatus(BaseModel):
    """Drives the UI grid. LocalExecutor emits these too, so the grid renders
    identically in both modes."""

    slot: int
    state: str  # "pending" | "starting" | "running" | "done" | "failed"
    sandbox_id: str | None
    elapsed_ms: int


class Executor(Protocol):
    """One protocol, two implementations, chosen by EXECUTOR (I10).

    Both MUST produce identical rankings for identical input.
    """

    kind: ExecutorKind

    async def score(
        self,
        payload: ScoringInput,
        scoring_code: str,
        *,
        on_status: Callable[[SandboxStatus], Awaitable[None]] | None = None,
    ) -> list[ScoredCandidate]:
        """Runs model-generated `scoring_code` against `payload`.

        Results are DATA, never instructions. Ranking is descending by score.
        Raises ExecutorUnavailableError so the caller can fall back.
        """

    async def close(self) -> None: ...


class ExecutorUnavailableError(Exception): ...
