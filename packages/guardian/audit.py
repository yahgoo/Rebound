"""Guardian append-only audit log (I8, I4).

AgentEvent rows are inserted only — never updated or deleted.
Every payload is assert_no_pii'd before it touches the database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from packages.domain.enums import Actor
from packages.domain.models import AgentEvent, RecoveryCase
from packages.guardian.redaction import assert_no_pii


class AgentEventIn(BaseModel):
    case_id: int
    actor: Actor
    step: str  # stable machine name; SSE streams this (I5)
    summary: str
    payload: dict = Field(default_factory=dict)  # redacted before it arrives here


def _elapsed_ms_since(opened_at: datetime, *, at: datetime) -> int:
    """Milliseconds from case.opened_at to `at`. Both sides normalised to UTC."""
    start = opened_at if opened_at.tzinfo is not None else opened_at.replace(tzinfo=UTC)
    end = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    return max(0, int((end - start).total_seconds() * 1000))


async def write_event(session: Session, event: AgentEventIn) -> int:
    """Append-only insert; returns the new id, which is also the SSE sequence
    number. Never updates, never deletes (I8)."""
    # Last line of defence before anything touches the database (I4).
    assert_no_pii(event.payload)

    case = session.get(RecoveryCase, event.case_id)
    if case is None:
        raise ValueError(f"RecoveryCase id={event.case_id} not found")

    at = datetime.now(UTC)
    row = AgentEvent(
        case_id=event.case_id,
        at=at,
        actor=str(event.actor),
        step=event.step,
        summary=event.summary,
        payload_json=json.dumps(event.payload, separators=(",", ":"), sort_keys=True),
        elapsed_ms=_elapsed_ms_since(case.opened_at, at=at),
    )
    session.add(row)
    session.flush()
    if row.id is None:
        raise RuntimeError("AgentEvent insert did not assign an id")
    return row.id


async def read_events(
    session: Session, *, case_id: int, after_id: int = 0
) -> list[AgentEvent]:
    """Ordered by id ascending. Backs both the SSE replay-on-reconnect and the
    Recovery Receipt."""
    statement = (
        select(AgentEvent)
        .where(AgentEvent.case_id == case_id)
        .where(col(AgentEvent.id) > after_id)
        .order_by(col(AgentEvent.id).asc())
    )
    return list(session.exec(statement).all())
