# Rebound — INTERFACES (frozen contract)

> **This file is a frozen contract. If an implementing task believes an interface is wrong, it must write the objection to `docs/RISKS.md` and implement the interface as written anyway.**

Only the human owner edits this file. Signatures are normative: names, argument order, types and return types are all part of the contract. Implementations may add private helpers; they may not change or widen a public signature.

Stack: Python 3.12, Pydantic v2, `httpx` async, SQLModel, FastAPI.

Shared conventions:

- All I/O-bound public methods are `async def`.
- Money is `decimal.Decimal`, never `float`.
- Times are timezone-aware `datetime` in UTC.
- Domain models live in `packages/atlas/models.py` (Atlas wire types) and `packages/domain/models.py` (SQLModel tables from `SPEC.md` §4).

---

## 0. Shared enums and errors

```python
# packages/domain/enums.py
from enum import StrEnum


class ReboundMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


class ExecutorKind(StrEnum):
    DAYTONA = "daytona"
    LOCAL = "local"


class ChaosProfile(StrEnum):
    NONE = "none"
    DECLINE = "decline"   # Atlas 604 via cardholder first name "Reject"  [E]
    TIMEOUT = "timeout"
    THREE_DS = "3ds"      # Atlas 616 via cardholder first name "Three DS" [E]


class Surface(StrEnum):
    OPERATOR = "operator"
    TRAVELLER = "traveller"


class SearchStrategy(StrEnum):
    SAME_ROUTE_LATER = "same_route_later"
    NEARBY_AIRPORT = "nearby_airport"
    ONE_STOP_REROUTE = "one_stop_reroute"
    NEXT_MORNING_HOTEL = "next_morning_hotel"


class Actor(StrEnum):
    WATCHER = "watcher"
    INTERPRETER = "interpreter"
    STRATEGIST = "strategist"
    EXECUTOR = "executor"
    CARETAKER = "caretaker"
    GUARDIAN = "guardian"
    HUMAN = "human"
```

```python
# packages/atlas/errors.py
class AtlasError(Exception):
    """Base. Carries the Atlas error code verbatim; never swallow the code."""
    code: str
    message: str
    http_status: int | None


class AtlasAuthError(AtlasError): ...          # bad credentials, or source IP not allowlisted [E]
class AtlasNoResultsError(AtlasError): ...     # search returned zero offers
class AtlasPriceMovedError(AtlasError):        # verify.do returned a different price
    old_price: Decimal
    new_price: Decimal
class AtlasPaymentDeclinedError(AtlasError): ...   # code "604" [E]
class AtlasThreeDSRequiredError(AtlasError): ...   # code "616" [E]
class AtlasTimeoutError(AtlasError): ...
class CassetteMissError(AtlasError): ...        # replay mode, no recording for this request
```

---

## 1. `packages/atlas`

### 1.1 Wire models

```python
# packages/atlas/models.py
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field


class Segment(BaseModel):
    carrier: str
    flight_number: str
    origin: str                      # IATA
    destination: str                 # IATA
    departure_at: datetime
    arrival_at: datetime
    cabin: str | None = None


class Offer(BaseModel):
    """One purchasable option exactly as Atlas returned it.

    Invariant I1: no field here may ever be synthesised by a model.
    """
    offer_id: str
    routing_identifier: str          # MUST be preserved and echoed back [E]
    segments: list[Segment]
    price: Decimal
    currency: str
    stop_count: int
    min_transfer_minutes: int | None = None
    baggage_included: bool | None = None
    raw: dict                        # verbatim Atlas object, for the audit log


class SearchRequest(BaseModel):
    origin: str
    destination: str
    departure_date: datetime
    adults: int = 1
    children: int = 0
    infants: int = 0
    cabin: str | None = None
    currency: str = "USD"            # sandbox requires this explicitly [E]


class SearchResult(BaseModel):
    session_id: str                  # NOT a live search.do field [E]. search.do
                                     # does not issue sessionId; verify.do does
                                     # (§1.2). Kept only for backward compat with
                                     # Task 4 (empty string). Do not rely on it.
    offers: list[Offer]
    raw: dict


class VerifyResult(BaseModel):
    offer_id: str
    session_id: str
    verified: bool
    price: Decimal                   # authoritative; may differ from the search price
    currency: str
    price_changed: bool
    raw: dict


class Passenger(BaseModel):
    """Zone A only. Never serialised into a sandbox or a model prompt (I4)."""
    given_name: str
    surname: str
    date_of_birth: datetime
    passport_number: str | None = None
    nationality: str | None = None


class OrderResult(BaseModel):
    order_no: str                    # MUST be preserved [E]
    status: str
    ticketing_deadline: datetime | None = None   # 5-minute window after order [E]
    total_amount: Decimal
    currency: str
    raw: dict


class CardDetails(BaseModel):
    """NEVER logged, NEVER persisted, NEVER leaves Zone A (I4).

    `holder_given_name` is also the chaos lever: "Reject" -> 604,
    "Three DS" -> 616, both documented Atlas sandbox triggers [E].
    """
    holder_given_name: str
    holder_surname: str
    number: str
    expiry_month: int
    expiry_year: int
    cvv: str

    def __repr__(self) -> str: ...   # MUST redact; no PAN in any repr or traceback


class PayResult(BaseModel):
    order_no: str
    paid: bool
    ticket_numbers: list[str]
    pnr: str | None
    error_code: str | None           # "604" / "616" surface here as well as raising
    raw: dict


class OrderDetails(BaseModel):
    """Authoritative order state. The only trusted source (I7)."""
    order_no: str
    status: str
    pnr: str | None
    ticket_numbers: list[str]
    segments: list[Segment]
    total_amount: Decimal
    currency: str
    raw: dict
```

### 1.2 Client

```python
# packages/atlas/client.py
from typing import Protocol


class AtlasTransport(Protocol):
    """The single seam between live and replay (I9).

    Everything above this line is identical in both modes.
    """

    async def post(self, path: str, payload: dict) -> dict: ...


class AtlasClient:
    def __init__(
        self,
        transport: AtlasTransport,
        *,
        chaos: ChaosProfile = ChaosProfile.NONE,
    ) -> None: ...

    async def search(self, request: SearchRequest) -> SearchResult:
        """POST search.do. Raises AtlasNoResultsError on zero offers.

        search.do returns routings with routingIdentifier; it does not
        issue a sessionId [E]. See verify() for where sessionId appears.
        """

    async def verify(self, *, routing_identifier: str) -> VerifyResult:
        """POST verify.do with the Offer.routing_identifier from search [E].

        Required wire input is routingIdentifier (not offer_id / fid).
        routingIdentifier must be ≤6 hours old when verify is called [E].

        On success, Atlas issues a NEW sessionId on the verify response
        (valid ~2 hours for order.do) [E]. That sessionId is newly minted
        here — it is not echoed from search, which never returned one.

        Sets price_changed by comparing the verified price to the search
        price. Raises AtlasPriceMovedError only via verify_strict().
        """

    async def verify_strict(
        self, *, routing_identifier: str, expected_price: Decimal
    ) -> VerifyResult:
        """Like verify(), then raises AtlasPriceMovedError when the
        verified price differs from expected_price."""

    async def get_offer_price(self, *, offer_id: str) -> VerifyResult:
        """POST getOfferPrice.do. Preserves OfferId [E]."""

    async def order(
        self,
        *,
        session_id: str,
        offer_id: str,
        passengers: list[Passenger],
        contact_email: str,
        contact_phone: str,
    ) -> OrderResult:
        """POST order.do. Caller MUST have a successful verify first (I2).

        session_id MUST be the sessionId newly issued by verify.do
        (not a search-time value — search.do does not return one) [E].
        """

    async def pay(self, *, order_no: str, card: CardDetails) -> PayResult:
        """POST pay.do. Raises AtlasPaymentDeclinedError on 604 and
        AtlasThreeDSRequiredError on 616 [E]. Card data never logged (I4)."""

    async def query_order_details(self, *, order_no: str) -> OrderDetails:
        """POST queryOrderDetails.do. Authoritative state (I7)."""

    async def poll_order_until(
        self,
        *,
        order_no: str,
        terminal_statuses: set[str],
        timeout_seconds: int = 120,
        interval_seconds: float = 3.0,
    ) -> OrderDetails:
        """Poll until status is terminal or timeout. The webhook safety net (I7)."""
```

<!-- CONFIRMED (Task 4 live cassette + Atlas verify.md): Atlas flow is
     search → routingIdentifier (no sessionId at search time) → verify(routingIdentifier)
     → newly issued sessionId (~2h) → order(sessionId). routingIdentifier ≤6h at verify.
     Exact remaining wire shapes: read from https://resources.atriptech.com; do not invent. -->

### 1.3 Live and replay transports

```python
# packages/atlas/transport.py
class LiveTransport:
    """Sends x-atlas-client-id / x-atlas-client-secret headers, plus Accept,
    Content-Type and Accept-Encoding; handles gzip responses [E]."""

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        *,
        recorder: "CassetteRecorder | None" = None,
        timeout_seconds: float = 30.0,
    ) -> None: ...

    async def post(self, path: str, payload: dict) -> dict: ...


class ReplayTransport:
    """Serves recorded responses and reproduces recorded latency (I9)."""

    def __init__(self, player: "CassettePlayer") -> None: ...

    async def post(self, path: str, payload: dict) -> dict: ...
```

### 1.4 Cassettes

```python
# packages/atlas/cassette.py
class Cassette(BaseModel):
    key: str                 # deterministic hash of (path, canonicalised payload)
    path: str
    request: dict            # redacted: never card data (I4)
    response: dict
    latency_ms: int
    recorded_at: datetime


class CassetteRecorder:
    def __init__(self, directory: Path) -> None: ...

    @staticmethod
    def key_for(path: str, payload: dict) -> str:
        """Deterministic and stable. Volatile fields (timestamps, nonces,
        sessionId) are excluded from the key so replay matches on intent."""

    async def record(
        self, *, path: str, payload: dict, response: dict, latency_ms: int
    ) -> None: ...


class CassettePlayer:
    def __init__(self, directory: Path, *, reproduce_latency: bool = True) -> None: ...

    async def play(self, *, path: str, payload: dict) -> dict:
        """Raises CassetteMissError when no recording matches."""

    def keys(self) -> list[str]: ...
```

### 1.5 Chaos injection

```python
# packages/atlas/chaos.py
def apply_chaos(profile: ChaosProfile, card: CardDetails) -> CardDetails:
    """Returns a copy with holder_given_name rewritten to trigger Atlas's
    own documented sandbox failures [E]:
      DECLINE  -> "Reject"    -> error 604
      THREE_DS -> "Three DS"  -> error 616
      TIMEOUT  -> unchanged; the transport delays past its timeout instead
      NONE     -> unchanged
    Chaos is a product feature, not a test hook. It never bypasses Guardian.
    """
```

---

## 2. `packages/guardian`

Deterministic. **No import of `packages.router`, no model call, ever (I3).**

```python
# packages/guardian/policy.py
from decimal import Decimal
from pydantic import BaseModel


class SpendVerdict(BaseModel):
    allowed: bool
    effective_cap_sgd: Decimal        # min(env cap, intent ceiling)
    requested_sgd: Decimal
    reason: str | None                # e.g. "over_cap"


def check_spend_cap(
    *,
    amount_sgd: Decimal,
    intent_ceiling_sgd: Decimal,
    env_cap_sgd: Decimal,
) -> SpendVerdict:
    """Pure function. Both ceilings are data, never model output (I3)."""


class ConfirmationRequest(BaseModel):
    case_id: int
    candidate_ids: list[int]
    recommended_candidate_id: int
    effective_cap_sgd: Decimal
    expires_at: datetime              # aligned to Atlas's 5-minute window [E]
    nonce: str                        # single use


class ConfirmationDecision(BaseModel):
    case_id: int
    candidate_id: int
    nonce: str
    decided_by: str                   # "operator" | "traveller"
    decided_at: datetime


class ConfirmationGate:
    """The only door to order.do / pay.do (I6). No auto-approve path exists."""

    def open(self, request: ConfirmationRequest) -> None: ...

    def resolve(self, decision: ConfirmationDecision) -> None:
        """Raises on unknown nonce, reused nonce, expired request, or a
        candidate_id outside the original request."""

    def is_confirmed(self, *, case_id: int, candidate_id: int) -> bool: ...
```

```python
# packages/guardian/redaction.py
class RedactionMap(BaseModel):
    """Token -> real value. Zone A only. Never serialised outside the host (I4)."""
    tokens: dict[str, str]


class RedactionResult(BaseModel):
    text: str
    map: RedactionMap
    kinds_found: list[str]            # "passenger_name" | "passport" | "dob" | "pan"


def redact(text: str, *, passengers: list[Passenger] | None = None) -> RedactionResult:
    """Replaces passenger names, passport numbers, dates of birth and anything
    PAN-shaped with stable tokens like [[PAX_1_NAME]]. Deterministic: the same
    input yields the same tokens. Called on EVERY payload before Zone C egress."""


def redact_image_metadata(image_bytes: bytes) -> bytes:
    """Strips EXIF, including GPS, before any image reaches Zone C."""


def rehydrate(text: str, map: RedactionMap) -> str:
    """Zone A only. A model never sees the map."""


def assert_no_pii(payload: dict) -> None:
    """Raises if any PAN-shaped, passport-shaped or known-passenger-name value
    is present. Called immediately before Zone B and Zone C egress."""
```

```python
# packages/guardian/audit.py
class AgentEventIn(BaseModel):
    case_id: int
    actor: Actor
    step: str                         # stable machine name; SSE streams this (I5)
    summary: str
    payload: dict = {}                # redacted before it arrives here


async def write_event(session: Session, event: AgentEventIn) -> int:
    """Append-only insert; returns the new id, which is also the SSE sequence
    number. Never updates, never deletes (I8)."""


async def read_events(
    session: Session, *, case_id: int, after_id: int = 0
) -> list[AgentEvent]:
    """Ordered by id ascending. Backs both the SSE replay-on-reconnect and the
    Recovery Receipt."""
```

---

## 3. `packages/executors`

```python
# packages/executors/base.py
from typing import Protocol
from pydantic import BaseModel


class ScoringInput(BaseModel):
    """Exactly what may cross into Zone B. Nothing else.

    No passenger names, no passport numbers, no card data, no Atlas secret.
    """
    case_ref: str
    candidates: list["CandidateForScoring"]
    must_arrive_by: datetime | None
    budget_ceiling_sgd: Decimal
    mobility_penalty_weight: float    # derived from RecoveryIntent.mobility_notes
    original_arrival_at: datetime


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


class ScoredCandidate(BaseModel):
    offer_id: str
    score: float
    components: dict[str, float]      # must be explainable in the UI
    self_transfer_risk: float
    mobility_fit: float


class SandboxStatus(BaseModel):
    """Drives the UI grid. LocalExecutor emits these too, so the grid renders
    identically in both modes."""
    slot: int
    state: str                        # "pending" | "starting" | "running" | "done" | "failed"
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
```

```python
# packages/executors/local.py
class LocalExecutor:
    kind = ExecutorKind.LOCAL

    def __init__(self, *, target_slots: int = 8, timeout_seconds: int = 20) -> None:
        """Runs scoring_code in a subprocess with no network and no filesystem
        write access. Emits SandboxStatus per slot so the UI is mode-agnostic."""
```

```python
# packages/executors/daytona.py
class DaytonaExecutor:
    kind = ExecutorKind.DAYTONA

    def __init__(
        self,
        api_key: str,
        *,
        target_slots: int = 8,
        timeout_seconds: int = 20,
    ) -> None: ...

    async def _mint_scoped_token(self, slot: int) -> str:
        """Single-use, scoped, short-lived. NEVER the Atlas master secret (I4)."""
```

---

## 4. `packages/router`

```python
# packages/router/base.py
from typing import Protocol, TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class ModelBackend(StrEnum):
    GEMINI = "gemini"
    GEMMA = "gemma"
    KIMI = "kimi"
    QWEN = "qwen"


class ImagePart(BaseModel):
    mime_type: str
    data: bytes                       # EXIF already stripped by Guardian


class AudioPart(BaseModel):
    mime_type: str
    data: bytes
    duration_seconds: float


class ModelRequest(BaseModel):
    """Everything here has already passed Guardian redaction (I4)."""
    system: str
    prompt: str
    images: list[ImagePart] = []
    audio: list[AudioPart] = []
    temperature: float = 0.0
    max_output_tokens: int = 2048
    timeout_seconds: float = 20.0
    response_schema: dict | None = None   # JSON schema for structured output


class ModelResponse(BaseModel):
    text: str
    backend: ModelBackend
    model_name: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelClient(Protocol):
    backend: ModelBackend
    supports_images: bool
    supports_audio: bool

    async def generate(self, request: ModelRequest) -> ModelResponse: ...


class ModelRouter:
    def __init__(
        self,
        clients: dict[ModelBackend, ModelClient],
        *,
        default: ModelBackend,
    ) -> None: ...

    async def generate(
        self, request: ModelRequest, *, backend: ModelBackend | None = None
    ) -> ModelResponse:
        """Falls back to `default` when a requested backend is unconfigured or
        lacks a required modality. Raises ModelTimeoutError on timeout — never
        returns a fabricated response."""

    async def generate_structured(
        self, request: ModelRequest, schema: type[T], *, backend: ModelBackend | None = None
    ) -> T:
        """Validates against `schema`. On validation failure: retry once with the
        validation error appended, then raise ModelSchemaError. Never repair by
        guessing (I1)."""


class ModelTimeoutError(Exception): ...
class ModelSchemaError(Exception): ...
```

---

## 5. `packages/agents`

One class per agent. No agent talks to another directly; the orchestrator wires them. Every agent writes `AgentEvent`s through `guardian.audit`.

```python
# packages/agents/watcher.py
class DisruptionSignal(BaseModel):
    kind: str                         # "webhook_schedule_change" | "webhook_cancellation" | "manual_trigger"
    atlas_order_no: str
    fingerprint: str                  # idempotency key
    received_at: datetime
    raw: dict


class Watcher:
    """Deterministic. No model call."""

    def __init__(self, atlas: AtlasClient, session_factory: Callable[[], Session]) -> None: ...

    @staticmethod
    def fingerprint(payload: dict) -> str:
        """Stable across duplicate deliveries of the same logical event (I7)."""

    async def ingest(self, signal: DisruptionSignal) -> RecoveryCase:
        """Dedupes on fingerprint, loads the order via query_order_details
        (never trusts the webhook body), opens or returns the RecoveryCase."""
```

```python
# packages/agents/interpreter.py
class InterpreterInput(BaseModel):
    case_id: int
    text: str | None = None
    voice: AudioPart | None = None
    photo: ImagePart | None = None
    original_itinerary: list[Segment]


class Interpreter:
    """LLM. Emits constraints only — never an itinerary (I1)."""

    def __init__(self, router: ModelRouter) -> None: ...

    async def interpret(self, payload: InterpreterInput) -> RecoveryIntent:
        """Guardian.redact runs on all text and redact_image_metadata on all
        images BEFORE egress. Structured output only. When confidence is below
        threshold, sets needs_clarification and asks — never guesses."""

    async def clarification_question(self, intent: RecoveryIntent) -> str:
        """One short question in intent.language."""
```

```python
# packages/agents/strategist.py
class StrategyPlan(BaseModel):
    strategy: SearchStrategy
    search_request: SearchRequest
    rationale: str


class RankedSelection(BaseModel):
    """A model's ONLY permitted output about itineraries: existing offer IDs."""
    ordered_offer_ids: list[str]
    explanations: dict[str, str]      # offer_id -> one sentence


class Strategist:
    def __init__(self, router: ModelRouter, atlas: AtlasClient) -> None: ...

    async def plan(
        self, intent: RecoveryIntent, original: list[Segment]
    ) -> list[StrategyPlan]:
        """Returns up to 4 plans, one per SearchStrategy."""

    async def fan_out(self, plans: list[StrategyPlan]) -> list[Candidate]:
        """Concurrent search.do calls. Deduplicates by offer_id. An empty
        result for one strategy is not fatal; all-empty raises AtlasNoResultsError."""

    async def write_scoring_code(self, intent: RecoveryIntent) -> str:
        """Model-generated Python scored in Zone B. Must define
        `def score(payload: dict) -> list[dict]` and use only the stdlib."""

    async def select(
        self, candidates: list[Candidate], scored: list[ScoredCandidate], intent: RecoveryIntent
    ) -> RankedSelection:
        """Any offer_id not present in `candidates` is discarded silently (I1)."""
```

```python
# packages/agents/executor_agent.py
class ExecutionAttempt(BaseModel):
    candidate_id: int
    offer_id: str
    verified: bool
    order_no: str | None
    paid: bool
    error_code: str | None            # "604" / "616" recorded verbatim
    started_at: datetime
    finished_at: datetime


class ExecutionOutcome(BaseModel):
    succeeded: bool
    attempts: list[ExecutionAttempt]  # in order, including every failure
    final_order_no: str | None
    final_candidate_id: int | None


class ExecutorAgent:
    """Owns the money path. The only caller of order/pay."""

    def __init__(
        self,
        atlas: AtlasClient,
        executor: Executor,
        gate: ConfirmationGate,
        session_factory: Callable[[], Session],
    ) -> None: ...

    async def score_and_verify(
        self,
        *,
        case_id: int,
        candidates: list[Candidate],
        intent: RecoveryIntent,
        scoring_code: str,
        on_status: Callable[[SandboxStatus], Awaitable[None]] | None = None,
    ) -> list[Candidate]:
        """Scores in Zone B, verifies the top 3 (I2), applies check_spend_cap to
        each, and marks over-cap or unverifiable candidates rejected. Falls back
        to LocalExecutor on ExecutorUnavailableError."""

    async def execute(
        self,
        *,
        case_id: int,
        ordered_candidates: list[Candidate],
        passengers: list[Passenger],
        card: CardDetails,
        max_attempts: int = 3,
    ) -> ExecutionOutcome:
        """Requires gate.is_confirmed for the FIRST candidate (I6). On
        AtlasPaymentDeclinedError or AtlasThreeDSRequiredError, re-verifies the
        next candidate and retries automatically — the confirmed spend cap still
        binds every retry, and no new human tap is required for failover.
        Polls query_order_details after any success (I7)."""
```

```python
# packages/agents/caretaker.py
class DeliveryBundle(BaseModel):
    spoken_text: str                  # in intent.language
    audio_path: Path | None
    pdf_path: Path | None             # large-print, one page
    family_message: str
    telegram_sent: bool


class Caretaker:
    def __init__(self, router: ModelRouter, notifier: "Notifier") -> None: ...

    async def deliver(
        self,
        *,
        case: RecoveryCase,
        intent: RecoveryIntent,
        details: OrderDetails,
    ) -> DeliveryBundle:
        """Copy is model-written; every flight fact is interpolated from
        `details`, never generated (I1)."""

    async def build_receipt(
        self, *, case: RecoveryCase, outcome: ExecutionOutcome, events: list[AgentEvent]
    ) -> RecoveryReceipt:
        """Deterministic. Counterfactual deltas come from the DIY baseline in
        packages/agents/counterfactual.py, not from a model."""
```

```python
# packages/notify/base.py
class Notifier(Protocol):
    async def send_telegram(self, *, chat_id: str, text: str) -> bool: ...
    async def render_pdf(self, *, case_ref: str, context: dict) -> Path: ...
    async def synthesise_speech(self, *, text: str, language: str) -> Path: ...
```

---

## 6. SSE event schema

One event per agent step (I5). Never a token. The frontend consumes exactly this.

```python
# apps/api/sse.py
class TraceEvent(BaseModel):
    id: int                           # == AgentEvent.id; monotonic, used for Last-Event-ID
    case_ref: str
    actor: Actor
    step: str
    summary: str
    elapsed_ms: int
    status: str                       # "started" | "ok" | "failed"
    data: dict = {}                   # redacted; UI-ready


class SandboxGridEvent(BaseModel):
    id: int
    case_ref: str
    slots: list[SandboxStatus]        # full state, not a delta — idempotent render


class CandidatesEvent(BaseModel):
    id: int
    case_ref: str
    candidates: list[dict]            # offer_id, price, arrival, score, components, verified
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
    status: str                       # RecoveryCase.status
```

Wire format on `GET /cases/{case_ref}/stream`:

```
event: trace | sandboxes | candidates | confirmation | receipt | status | heartbeat
id: <TraceEvent.id>
data: <single-line JSON of the model above>
```

Rules:

- `id` is always the `AgentEvent.id`, so a client reconnecting with `Last-Event-ID` is replayed from `read_events(after_id=...)`.
- `heartbeat` every 15 seconds with `data: {}` to survive proxies.
- Every event is a full state snapshot for its concern, never a patch.

---

## 7. Ownership

| Path | Owner |
|---|---|
| `apps/`, `packages/` | implementing agent (Qoder / Cursor) |
| `tests/`, `fixtures/` | adversarial reviewer (CodeBuddy) |
| `docs/SPEC.md`, `docs/INTERFACES.md` | human owner only — frozen |
| `docs/RISKS.md` | implementing agent, append-only |
| `docs/REVIEW.md` | adversarial reviewer |
| `docs/QODER.md` | implementing agent, append-only decision log |
