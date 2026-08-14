"""Guardian spend cap and confirmation gate (I3, I6).

Deterministic policy only: no model call, no network, and no import from
the agent orchestration or LLM packages. Both spend ceilings arrive as
Decimal arguments — never from model output (I3). Confirmation is granted
only by a recorded ConfirmationDecision (I6).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel

# Frozen INTERFACES.md §2 / Task 9: confirmation TTL capped at Atlas's cited
# 5-minute window. See docs/RISKS.md (Task 9) — that figure is the post-order
# ticketing deadline, not the verify sessionId lifetime (~2h). We keep the
# stricter frozen cap rather than silently widening it.
_MAX_CONFIRMATION_WINDOW = timedelta(minutes=5)


class SpendVerdict(BaseModel):
    allowed: bool
    effective_cap_sgd: Decimal  # min(env cap, intent ceiling)
    requested_sgd: Decimal
    reason: str | None  # e.g. "over_cap"


def check_spend_cap(
    *,
    amount_sgd: Decimal,
    intent_ceiling_sgd: Decimal,
    env_cap_sgd: Decimal,
) -> SpendVerdict:
    """Pure function. Both ceilings are data, never model output (I3)."""
    effective = min(intent_ceiling_sgd, env_cap_sgd)
    if amount_sgd > effective:
        return SpendVerdict(
            allowed=False,
            effective_cap_sgd=effective,
            requested_sgd=amount_sgd,
            reason="over_cap",
        )
    return SpendVerdict(
        allowed=True,
        effective_cap_sgd=effective,
        requested_sgd=amount_sgd,
        reason=None,
    )


class ConfirmationRequest(BaseModel):
    case_id: int
    candidate_ids: list[int]
    recommended_candidate_id: int
    effective_cap_sgd: Decimal
    expires_at: datetime  # aligned to Atlas's 5-minute window [E]
    nonce: str  # single use


class ConfirmationDecision(BaseModel):
    case_id: int
    candidate_id: int
    nonce: str
    decided_by: str  # "operator" | "traveller"
    decided_at: datetime


class ConfirmationError(Exception):
    """Base error for ConfirmationGate.resolve / open failures."""


class UnknownNonceError(ConfirmationError):
    pass


class NonceReuseError(ConfirmationError):
    pass


class ExpiredConfirmationError(ConfirmationError):
    pass


class InvalidCandidateError(ConfirmationError):
    pass


class ConfirmationGate:
    """The only door to order.do / pay.do (I6). No auto-approve path exists."""

    def __init__(self) -> None:
        self._pending: dict[str, ConfirmationRequest] = {}
        self._used_nonces: set[str] = set()
        self._decisions: dict[tuple[int, int], ConfirmationDecision] = {}

    def open(self, request: ConfirmationRequest) -> None:
        """Issue a single-use nonce and register the confirmation challenge.

        Mutates ``request`` in place with the issued ``nonce`` and a clamped
        ``expires_at`` no longer than the 5-minute confirmation window
        (frozen INTERFACES.md §2 / Task 9). See docs/RISKS.md Task 9 for the
        5-minute vs verify-session (~2h) clock conflation.
        """
        now = datetime.now(UTC)

        if request.recommended_candidate_id not in request.candidate_ids:
            raise InvalidCandidateError(
                f"recommended_candidate_id {request.recommended_candidate_id} "
                f"not in candidate_ids {request.candidate_ids}"
            )

        nonce = (request.nonce or "").strip() or secrets.token_urlsafe(24)

        expires_at = request.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        else:
            expires_at = expires_at.astimezone(UTC)

        max_expiry = now + _MAX_CONFIRMATION_WINDOW
        if expires_at > max_expiry:
            raise ConfirmationError(
                f"expires_at {expires_at.isoformat()} exceeds the "
                f"{int(_MAX_CONFIRMATION_WINDOW.total_seconds())}-second "
                "confirmation window"
            )
        if expires_at <= now:
            expires_at = max_expiry

        if nonce in self._pending or nonce in self._used_nonces:
            raise NonceReuseError(f"nonce already registered or used: {nonce!r}")

        request.nonce = nonce
        request.expires_at = expires_at
        self._pending[nonce] = request.model_copy(deep=True)

    def resolve(self, decision: ConfirmationDecision) -> None:
        """Raises on unknown nonce, reused nonce, expired request, or a
        candidate_id outside the original request."""
        nonce = decision.nonce
        if nonce in self._used_nonces:
            raise NonceReuseError(f"nonce already used: {nonce!r}")

        request = self._pending.get(nonce)
        if request is None:
            raise UnknownNonceError(f"unknown nonce: {nonce!r}")

        now = datetime.now(UTC)
        expires_at = request.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        else:
            expires_at = expires_at.astimezone(UTC)

        if now >= expires_at:
            raise ExpiredConfirmationError(
                f"confirmation expired at {expires_at.isoformat()}"
            )

        if decision.case_id != request.case_id:
            raise InvalidCandidateError(
                f"case_id {decision.case_id} does not match request "
                f"case_id {request.case_id}"
            )

        if decision.candidate_id not in request.candidate_ids:
            raise InvalidCandidateError(
                f"candidate_id {decision.candidate_id} not in "
                f"candidate_ids {request.candidate_ids}"
            )

        del self._pending[nonce]
        self._used_nonces.add(nonce)
        self._decisions[(decision.case_id, decision.candidate_id)] = decision

    def is_confirmed(self, *, case_id: int, candidate_id: int) -> bool:
        return (case_id, candidate_id) in self._decisions
