"""SQLModel tables for SPEC.md §4 entities."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, Numeric
from sqlmodel import Field, SQLModel


class Order(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    atlas_order_no: str = Field(unique=True, index=True)
    pnr: str | None = None
    status: str
    passengers_json: str
    itinerary_json: str
    total_amount: Decimal = Field(sa_column=Column(Numeric(), nullable=False))
    currency: str
    created_at: datetime
    updated_at: datetime


class RecoveryCase(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    case_ref: str = Field(unique=True, index=True)
    order_id: int = Field(foreign_key="order.id")
    trigger_kind: str
    trigger_fingerprint: str = Field(unique=True, index=True)
    status: str
    opened_at: datetime
    resolved_at: datetime | None = None
    surface: str


class RecoveryIntent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="recoverycase.id")
    passenger_count: int
    must_arrive_by: datetime | None = None
    budget_ceiling_sgd: Decimal = Field(sa_column=Column(Numeric(), nullable=False))
    # Spec list[str] fields stored as JSON-encoded str columns (names exact).
    origin_candidates: str
    destination_candidates: str
    mobility_notes: str | None = None
    language: str
    confidence: float
    raw_input_kinds: str

    @property
    def origin_candidates_list(self) -> list[str]:
        return json.loads(self.origin_candidates)

    @property
    def destination_candidates_list(self) -> list[str]:
        return json.loads(self.destination_candidates)

    @property
    def raw_input_kinds_list(self) -> list[str]:
        return json.loads(self.raw_input_kinds)


class Candidate(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="recoverycase.id")
    offer_id: str
    # Atlas Offer.routing_identifier — required for verify.do (I2). Not in the
    # frozen SPEC/INTERFACES Candidate tables; added so segments_json stays a
    # bare segment list (Task 17) while verify still has an explicit handle.
    routing_identifier: str
    strategy: str
    segments_json: str
    price: Decimal = Field(sa_column=Column(Numeric(), nullable=False))
    currency: str
    arrival_delay_minutes: int
    stop_count: int
    min_transfer_minutes: int | None = None
    self_transfer_risk: float
    mobility_fit: float
    score: float | None = None
    score_components_json: str | None = None
    verified: bool
    verified_price: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(), nullable=True)
    )
    rejected_reason: str | None = None


class RecoveryReceipt(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="recoverycase.id")
    elapsed_seconds: int
    human_taps: int
    attempts_json: str
    final_offer_id: str | None = None
    amount_paid: Decimal = Field(sa_column=Column(Numeric(), nullable=False))
    currency: str
    counterfactual_cost_delta_sgd: Decimal = Field(
        sa_column=Column(Numeric(), nullable=False)
    )
    counterfactual_hours_delta: float
    event_ids_json: str


class AgentEvent(SQLModel, table=True):
    """Append-only audit log (I8). No update/delete helpers."""

    id: int | None = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="recoverycase.id")
    at: datetime
    actor: str
    step: str
    summary: str
    payload_json: str
    elapsed_ms: int
