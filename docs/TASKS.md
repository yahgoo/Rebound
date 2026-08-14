# Rebound — TASKS (the build ledger)

26 tasks, ordered by dependency, following `hackathon-strategy.md` §7.5. Work one task at a time. Every task assumes `docs/SPEC.md` and `docs/INTERFACES.md` are read first and are immutable.

**Phase map**

| Phase | Tasks | Strategy checkpoint |
|---|---|---|
| 0 — repo skeleton | 1–2 | first 4 hours |
| 1 — Atlas client | 3–7 | first 4 hours |
| 2 — Guardian | 8–10 | first 12 hours |
| 3 — Executors | 11–12 | 24 hours |
| 4 — Agents | 13–19 | first 12 → 24 hours |
| 5 — API + SSE | 20–21 | first 12 hours |
| 6 — UI | 22–24 | first 12 → 24 hours |
| 7 — Chaos + polish | 25–26 | 24 hours |

**Gate before Task 3:** run the Atlas Newman happy-path kit (`Atlas_UAT_HappyPath.postman_collection.json` with `--delay-request 10000`) and confirm `Search`/`Verify`/`Order`/`Pay` pass **[E]**. If `Search` fails, stop — no code matters until credentials and IP allowlisting work.

---

## Phase 0 — Repo skeleton

### Task 1 — Repo skeleton, settings, health check

**Depends on:** none
**Files to create/modify:** `pyproject.toml`, `apps/api/main.py`, `apps/api/settings.py`, `.env.example`
**Files you must NOT touch:** `docs/`, `tests/`, anything under `packages/`
**Read first:** `docs/SPEC.md` §5, `docs/INTERFACES.md` §0

**Do:**

- Create `pyproject.toml` for Python 3.12 with exactly these runtime dependencies: `fastapi`, `uvicorn[standard]`, `httpx`, `sqlmodel`, `pydantic>=2`, `pydantic-settings`, `jinja2`, `python-multipart`. Nothing else.
- Create empty package directories with `__init__.py` for `packages/atlas`, `packages/domain`, `packages/guardian`, `packages/executors`, `packages/agents`, `packages/router`, `packages/notify`.
- Write `apps/api/settings.py` as a single `pydantic-settings` `Settings` class covering **every** variable in `SPEC.md` §5, with the enum types from `INTERFACES.md` §0 where applicable and the documented defaults (`DAYTONA_TARGET_SANDBOXES=8`, `EXECUTOR=local`, `REBOUND_MODE=live`, `CHAOS_PROFILE=none`, `SURFACE=operator`). Expose a cached `get_settings()`.
- Optional variables (`GEMMA_ENDPOINT`, `KIMI_API_KEY`, `DAYTONA_API_KEY`, `NOSANA_API_KEY`, `TELEGRAM_*`) must be `None`-able so the app boots without them.
- Write `apps/api/main.py` with a FastAPI app and `GET /healthz` returning `{"status": "ok", "mode": <REBOUND_MODE>, "executor": <EXECUTOR>, "surface": <SURFACE>, "chaos": <CHAOS_PROFILE>}`.
- Write `.env.example` listing every variable with a one-line comment, secrets left blank.

**Constraints:** No database server, no Redis, no build step (SPEC §6.6–6.7). Settings must never contain a default for a secret.

**Done when:** `uvicorn apps.api.main:app` starts and `curl localhost:8000/healthz` returns all five fields, with the app booting successfully when only `ATLAS_BASE_URL`, `ATLAS_CLIENT_ID`, `ATLAS_CLIENT_SECRET` and `GUARDIAN_MAX_SPEND_SGD` are set.

**Do not implement yet:** any Atlas call, any route beyond `/healthz`, any template, Docker, Caddy.

---

### Task 2 — SQLModel schema and engine

**Depends on:** Task 1
**Files to create/modify:** `packages/domain/models.py`, `packages/domain/enums.py`, `packages/domain/db.py`
**Files you must NOT touch:** `apps/api/main.py`, `packages/atlas/`, `docs/`
**Read first:** `docs/SPEC.md` §4, `docs/INTERFACES.md` §0

**Do:**

- Write `packages/domain/enums.py` verbatim from `INTERFACES.md` §0.
- Write `packages/domain/models.py` as SQLModel tables for all six entities in `SPEC.md` §4: `Order`, `RecoveryCase`, `RecoveryIntent`, `Candidate`, `RecoveryReceipt`, `AgentEvent`. Every field in those tables, with the stated types, nullability and unique constraints.
- Store `Decimal` money columns as `Numeric`; store list-valued intent fields as JSON-encoded `str` columns named exactly as in the spec (`origin_candidates` and `destination_candidates` may be JSON text with typed accessor properties).
- Add unique indexes on `Order.atlas_order_no`, `RecoveryCase.case_ref` and `RecoveryCase.trigger_fingerprint`.
- Write `packages/domain/db.py` with an engine factory that enables WAL (`PRAGMA journal_mode=WAL`) and `foreign_keys=ON` on connect, a `create_all()`, and a `session_factory()`.
- Give `AgentEvent` an `id` that is a plain autoincrement integer primary key — it doubles as the SSE sequence number.

**Constraints:** `AgentEvent` is append-only (I8): define no update or delete helper on it. No Alembic — `create_all()` only.

**Done when:** a script that calls `create_all()` against a temp file, inserts one row per table with valid foreign keys, and re-reads them, exits 0; and `sqlite3 <file> "PRAGMA journal_mode;"` prints `wal`.

**Do not implement yet:** business logic, validation rules beyond types, any repository or service layer.

---

## Phase 1 — Atlas client

### Task 3 — Atlas transport, auth, errors, cassette recorder/player

**Depends on:** Task 2
**Files to create/modify:** `packages/atlas/transport.py`, `packages/atlas/cassette.py`, `packages/atlas/errors.py`
**Files you must NOT touch:** `packages/atlas/client.py`, `packages/domain/`, `apps/`
**Read first:** `docs/SPEC.md` §2–3, `docs/INTERFACES.md` §1.1, §1.3, §1.4, `hackathon-strategy.md` Appendix A

**Do:**

- Write `packages/atlas/errors.py` exactly as `INTERFACES.md` §0 specifies, preserving the Atlas error code verbatim on every exception.
- Write `LiveTransport` sending `x-atlas-client-id`, `x-atlas-client-secret`, `Accept`, `Content-Type` and `Accept-Encoding`, handling gzip responses, with a configurable timeout that raises `AtlasTimeoutError` **[E]**.
- Map non-success Atlas codes onto the typed errors: `604` → `AtlasPaymentDeclinedError`, `616` → `AtlasThreeDSRequiredError`, auth/IP failures → `AtlasAuthError`, everything else → `AtlasError` with the code intact.
- Write `CassetteRecorder` and `CassettePlayer` per `INTERFACES.md` §1.4. `key_for` must exclude volatile fields (timestamps, nonces, `sessionId`) so replay matches on request intent, and must be stable across processes.
- `CassetteRecorder.record` must redact before writing: no PAN, CVV, cardholder name, passport number or date of birth may appear in any file under `fixtures/cassettes/` (I4).
- Write `ReplayTransport` that serves recordings and sleeps the recorded `latency_ms`, raising `CassetteMissError` on a miss.
- Wire recording so that in `live` mode every response is persisted on first call.

**Constraints:** I4 — nothing card-shaped may reach disk. I9 — `ReplayTransport` and `LiveTransport` must be substitutable through the `AtlasTransport` protocol with no caller change.

**Done when:** `python -m packages.atlas.smoke_transport` performs one live `POST` to `ATLAS_BASE_URL`, writes a cassette file, then re-runs the same request through `ReplayTransport` and returns a byte-identical payload without network access; and a grep for a test PAN across `fixtures/cassettes/` returns nothing.

**Do not implement yet:** `AtlasClient` or any endpoint-specific method, chaos injection.

---

### Task 4 — Atlas `search.do`

**Depends on:** Task 3
**Files to create/modify:** `packages/atlas/models.py`, `packages/atlas/client.py`, `packages/atlas/smoke_search.py`
**Files you must NOT touch:** `packages/atlas/transport.py`, `packages/atlas/cassette.py`, `packages/guardian/`
**Read first:** `docs/SPEC.md` §2, `docs/INTERFACES.md` §1.1–1.2, `hackathon-strategy.md` Appendix A

**Do:**

- Write `packages/atlas/models.py` with `Segment`, `Offer`, `SearchRequest`, `SearchResult` exactly as `INTERFACES.md` §1.1 defines them, including the `raw: dict` passthrough on `Offer` and `SearchResult`.
- Read the real request and response field names from the Atlas docs (`https://resources.atriptech.com`, `llms-full.txt` or the GitBook MCP endpoint) — do not invent them. Record what you used in `docs/QODER.md`.
- Create `AtlasClient` with only `__init__` and `search` implemented; leave other methods as `raise NotImplementedError`.
- `search` must send `"currency": "USD"` explicitly, preserve `routingIdentifier` on every `Offer`, and preserve `sessionId` on `SearchResult` **[E]**.
- Raise `AtlasNoResultsError` when Atlas returns zero offers — never return an empty `SearchResult`.

**Constraints:** I1 — every `Offer` field must come from the Atlas response; no computed or defaulted flight data.

**Done when:** `python -m packages.atlas.smoke_search` searches a real future-dated sandbox route and prints a parsed `SearchResult` with at least one `Offer` carrying a non-empty `offer_id` and `routing_identifier`, without raising; the same script passes with `REBOUND_MODE=replay`.

**Do not implement yet:** `verify`, `order`, `pay`, `query_order_details`, `get_offer_price`.

---

### Task 5 — Atlas `verify.do` and `getOfferPrice.do`

**Depends on:** Task 4
**Files to create/modify:** `packages/atlas/models.py`, `packages/atlas/client.py`, `packages/atlas/smoke_verify.py`
**Files you must NOT touch:** `packages/atlas/transport.py`, `packages/atlas/cassette.py`
**Read first:** `docs/SPEC.md` §2 (I1, I2), `docs/INTERFACES.md` §1.1–1.2

**Do:**

- Add `VerifyResult` to `packages/atlas/models.py`.
- Implement `verify`, `verify_strict` and `get_offer_price` per `INTERFACES.md` §1.2.
- `verify` must echo the `sessionId` from search and the `OfferId` unchanged **[E]**, and set `price_changed` by comparing the returned price to the offer's search price.
- `verify_strict` raises `AtlasPriceMovedError` carrying both `old_price` and `new_price`.
- Capture `ticketing_deadline` semantics where Atlas returns them — the 5-minute ticketing window is a real constraint **[E]**.

**Constraints:** I2 — `verify` is the gate before any order. The verified price, not the search price, is authoritative from here on.

**Done when:** `python -m packages.atlas.smoke_verify` takes an `offer_id` from Task 4's smoke run, verifies it, and prints `verified=True` with an authoritative price; and forcing a mismatched `expected_price` into `verify_strict` raises `AtlasPriceMovedError`.

**Do not implement yet:** `order`, `pay`, chaos injection.

---

### Task 6 — Atlas `order.do` and `pay.do`

**Depends on:** Task 5
**Files to create/modify:** `packages/atlas/models.py`, `packages/atlas/client.py`, `packages/atlas/smoke_order_pay.py`
**Files you must NOT touch:** `packages/atlas/chaos.py`, `packages/guardian/`, `packages/agents/`
**Read first:** `docs/SPEC.md` §2 (I1, I2, I4), §3, `docs/INTERFACES.md` §1.1–1.2

**Do:**

- Add `Passenger`, `OrderResult`, `CardDetails` and `PayResult` to `packages/atlas/models.py`.
- Implement `CardDetails.__repr__` (and `__str__`) to redact the PAN and CVV so no traceback, log line or exception message can leak them (I4).
- Implement `order`, preserving `orderNo` on the result **[E]**, and raise if the caller has not supplied a `session_id` and `offer_id` originating from a successful verify.
- Implement `pay`, raising `AtlasPaymentDeclinedError` on code `604` and `AtlasThreeDSRequiredError` on code `616`, and also surfacing the code on `PayResult.error_code` **[E]**.
- Ensure no card field is ever passed to the cassette recorder, any logger, or any exception payload.

**Constraints:** I2 — refuse to build an order request for an unverified offer. I4 — card data stays in Zone A; assert this in code, not in a comment.

**Done when:** `python -m packages.atlas.smoke_order_pay` completes search → verify → order → pay against the sandbox for one passenger and prints an issued ticket number or PNR; `repr(card)` contains no digit sequence longer than 4; and the written cassette contains no card field.

**Do not implement yet:** the `Reject` / `Three DS` chaos rewrite (Task 25), Guardian checks, any retry or failover logic.

---

### Task 7 — Atlas `queryOrderDetails.do` and polling

**Depends on:** Task 6
**Files to create/modify:** `packages/atlas/models.py`, `packages/atlas/client.py`, `packages/atlas/smoke_poll.py`
**Files you must NOT touch:** `apps/`, `packages/agents/`
**Read first:** `docs/SPEC.md` §2 (I7), `docs/INTERFACES.md` §1.2

**Do:**

- Add `OrderDetails` to `packages/atlas/models.py`.
- Implement `query_order_details` returning the authoritative order state.
- Implement `poll_order_until` with the signature in `INTERFACES.md` §1.2: poll every `interval_seconds` until `status` is in `terminal_statuses` or `timeout_seconds` elapses, then raise `AtlasTimeoutError`.
- Polling must be cancellation-safe and must not hold a database session open across sleeps.
- Log one line per poll attempt at debug level with the order number and status only.

**Constraints:** I7 — this is the safety net for best-effort webhooks. Never infer state from anything but this call.

**Done when:** `python -m packages.atlas.smoke_poll <order_no>` prints the authoritative status of the Task 6 order, and `poll_order_until` with an unreachable terminal status raises `AtlasTimeoutError` within its timeout rather than hanging.

**Do not implement yet:** the webhook receiver, reconciliation between webhook and poll.

---

## Phase 2 — Guardian

### Task 8 — Guardian redaction and re-hydration

**Depends on:** Task 6
**Files to create/modify:** `packages/guardian/redaction.py`
**Files you must NOT touch:** `packages/guardian/policy.py`, `packages/guardian/audit.py`, `packages/router/`
**Read first:** `docs/SPEC.md` §2 (I4), §3 (Zone C), `docs/INTERFACES.md` §2

**Do:**

- Implement `redact`, `rehydrate`, `redact_image_metadata` and `assert_no_pii` per `INTERFACES.md` §2.
- Tokens must be stable and deterministic (`[[PAX_1_NAME]]`, `[[PAX_1_PASSPORT]]`, `[[PAX_1_DOB]]`), so the same input always yields the same token, and re-hydration is exact.
- Detect and replace: known passenger given names and surnames, passport-shaped strings, dates of birth, and any PAN-shaped digit run (13–19 digits, Luhn-valid) regardless of whether a passenger list was supplied.
- `redact_image_metadata` strips all EXIF including GPS, returning re-encoded bytes with pixel data preserved.
- `assert_no_pii` raises on any remaining PII and is cheap enough to call on every egress.
- Return `kinds_found` so the UI's Guardian pane can show what was redacted.

**Constraints:** Pure functions, no database, no model call, no network (I3). Never log the input or the map.

**Done when:** `python -m packages.guardian.smoke_redaction` proves round-trip fidelity — `rehydrate(redact(text).text, map) == text` — for a fixture containing two passenger names, a passport number, a date of birth and a test PAN; `assert_no_pii` raises on the raw text and passes on the redacted text; and an EXIF-bearing JPEG comes back with no GPS tag.

**Do not implement yet:** calling redaction from any agent, the spend cap, the audit log.

---

### Task 9 — Guardian spend cap and confirmation gate

**Depends on:** Task 8
**Files to create/modify:** `packages/guardian/policy.py`
**Files you must NOT touch:** `packages/guardian/redaction.py`, `packages/router/`, `packages/agents/`
**Read first:** `docs/SPEC.md` §2 (I3, I6), `docs/INTERFACES.md` §2

**Do:**

- Implement `SpendVerdict` and `check_spend_cap` as a pure function taking `min(intent_ceiling_sgd, env_cap_sgd)` as the effective cap, returning `reason="over_cap"` on rejection.
- Implement `ConfirmationRequest`, `ConfirmationDecision` and `ConfirmationGate` per `INTERFACES.md` §2.
- `open` issues a single-use `nonce` and an `expires_at` no longer than Atlas's 5-minute ticketing window **[E]**.
- `resolve` must raise on: unknown nonce, already-used nonce, expired request, and a `candidate_id` not present in the original `candidate_ids`.
- `is_confirmed` returns `True` only for a `(case_id, candidate_id)` pair backed by a resolved decision.
- The module must import nothing from `packages.router` or `packages.agents`, and must contain no network or model call — enforce this by having no such import present.

**Constraints:** I3 — the cap is never influenced by model output; both ceilings arrive as `Decimal` arguments only. I6 — there is no code path, flag or default that grants confirmation without a `ConfirmationDecision`.

**Done when:** `python -m packages.guardian.smoke_policy` demonstrates all six behaviours — over-cap rejection, under-cap approval, env cap winning, intent ceiling winning, nonce replay rejected, expired request rejected — and `grep -rn "router\|generate\|httpx" packages/guardian/policy.py` returns nothing.

**Do not implement yet:** persisting decisions to the database, the HTTP confirm endpoint, the UI.

---

### Task 10 — Guardian append-only audit log

**Depends on:** Task 9
**Files to create/modify:** `packages/guardian/audit.py`
**Files you must NOT touch:** `packages/guardian/policy.py`, `packages/domain/models.py`
**Read first:** `docs/SPEC.md` §2 (I8), §4 (`AgentEvent`), `docs/INTERFACES.md` §2

**Do:**

- Implement `AgentEventIn`, `write_event` and `read_events` per `INTERFACES.md` §2.
- `write_event` computes `elapsed_ms` from the case's `opened_at`, inserts, and returns the new id.
- `write_event` must call `assert_no_pii` on the payload before insert and must reject card-shaped data outright.
- `read_events` returns rows ordered by `id` ascending, filtered by `case_id` and `after_id`, and is the single source for both SSE replay and the receipt.
- Expose no update or delete function of any kind (I8).

**Constraints:** I8 — append-only. I4 — a redacted payload only.

**Done when:** `python -m packages.guardian.smoke_audit` writes five events for one case, reads them back in insertion order with monotonically increasing ids and non-decreasing `elapsed_ms`, returns exactly the last two for `after_id=<3rd id>`, and raises when handed a payload containing a Luhn-valid PAN.

**Do not implement yet:** the SSE endpoint, the receipt builder.

---

## Phase 3 — Executors

### Task 11 — Executor protocol and `LocalExecutor`

**Depends on:** Task 10
**Files to create/modify:** `packages/executors/base.py`, `packages/executors/local.py`
**Files you must NOT touch:** `packages/executors/daytona.py`, `packages/agents/`
**Read first:** `docs/SPEC.md` §3 (Zone B), `docs/INTERFACES.md` §3

**Do:**

- Write `packages/executors/base.py` with `ScoringInput`, `CandidateForScoring`, `ScoredCandidate`, `SandboxStatus`, the `Executor` protocol and `ExecutorUnavailableError`, exactly as `INTERFACES.md` §3 defines them.
- Implement `LocalExecutor.score`: distribute candidates across `target_slots`, run `scoring_code` in a subprocess per slot with no network and no filesystem write access, and collect results.
- Emit `SandboxStatus` through `on_status` for every slot transition (`pending → starting → running → done|failed`), so the UI grid renders identically regardless of executor (I10).
- Validate results against `ScoredCandidate` and **discard any `offer_id` not present in the input** — a sandbox result is data, never instructions.
- Return the list sorted by `score` descending, deterministically tie-broken by `offer_id`.
- Raise `ExecutorUnavailableError` on subprocess spawn failure or timeout.

**Constraints:** `ScoringInput` is the complete allowlist of what may cross into Zone B — no passenger names, no passport numbers, no card data, no Atlas secret. Assert this before dispatch.

**Done when:** `python -m packages.executors.smoke_local` scores 12 fixture candidates with a hand-written `score()` function across 8 slots, prints a descending ranking, emits at least 8 distinct `SandboxStatus` sequences, and silently drops an injected result whose `offer_id` was not in the input.

**Do not implement yet:** Daytona, model-generated scoring code, fallback selection logic.

---

### Task 12 — `DaytonaExecutor`

**Depends on:** Task 11
**Files to create/modify:** `packages/executors/daytona.py`, `packages/executors/__init__.py`, `pyproject.toml`
**Files you must NOT touch:** `packages/executors/base.py`, `packages/executors/local.py`, `packages/agents/`
**Read first:** `docs/SPEC.md` §3 (Zone B), `docs/INTERFACES.md` §3, `hackathon-strategy.md` Appendix B

**Do:**

- Add the Daytona Python SDK to `pyproject.toml`.
- Implement `DaytonaExecutor.score` satisfying the same `Executor` protocol: create up to `target_slots` sandboxes concurrently via `daytona.create(...)`, run `scoring_code` with `sandbox.process.code_run(...)`, collect and validate results identically to `LocalExecutor`.
- Implement `_mint_scoped_token` returning a single-use, short-lived, scoped token. The Atlas master secret must never be referenced in this module — do not import settings fields that contain it (I4).
- Emit `SandboxStatus` with the real `sandbox_id` per slot, on the same transitions as `LocalExecutor`.
- Always delete or archive every sandbox in a `finally` block, including on exception and cancellation — stopped sandboxes still bill for disk **[O]**.
- Request default-sized sandboxes only (1 vCPU / 1 GiB / 3 GiB) and never a GPU sandbox — free credits do not cover GPU **[O]**.
- Raise `ExecutorUnavailableError` on create failure, auth failure or timeout, so the caller can fall back.
- Add a factory in `packages/executors/__init__.py` selecting the implementation from the `EXECUTOR` setting.

**Constraints:** Rankings must be **identical** to `LocalExecutor` for identical input — same sort, same tie-break, same score arithmetic. Zone B allowlist as in Task 11.

**Done when:** `EXECUTOR=daytona python -m packages.executors.smoke_parity` runs the same 12 fixture candidates through both executors and prints `PARITY OK` because the two ranked `offer_id` lists are equal; the Daytona dashboard shows zero surviving sandboxes afterwards; and `grep -n "ATLAS_CLIENT_SECRET" packages/executors/daytona.py` returns nothing.

**Do not implement yet:** the fallback decision itself (Task 18), the UI grid.

---

## Phase 4 — Agents

### Task 13 — Model router with Gemini backend

**Depends on:** Task 8
**Files to create/modify:** `packages/router/base.py`, `packages/router/gemini.py`, `packages/router/__init__.py`, `pyproject.toml`
**Files you must NOT touch:** `packages/guardian/`, `packages/agents/`
**Read first:** `docs/SPEC.md` §3 (Zone C), `docs/INTERFACES.md` §4

**Do:**

- Write `packages/router/base.py` with `ModelBackend`, `ImagePart`, `AudioPart`, `ModelRequest`, `ModelResponse`, the `ModelClient` protocol, `ModelRouter`, `ModelTimeoutError` and `ModelSchemaError`, exactly as `INTERFACES.md` §4 defines them.
- Implement `GeminiClient` against Gemini Flash 3.6 with `supports_images=True` and `supports_audio=True`, honouring `temperature`, `max_output_tokens`, `timeout_seconds` and `response_schema`.
- Implement `ModelRouter.generate`: route to the requested backend, fall back to `default` when a backend is unconfigured or lacks a required modality, and raise `ModelTimeoutError` on timeout — never return a fabricated or partial response.
- Implement `generate_structured`: validate against the Pydantic schema; on failure retry exactly once with the validation error appended to the prompt; on second failure raise `ModelSchemaError`. Never repair by guessing (I1).
- Add a factory in `__init__.py` building the router from settings, registering only backends whose credentials are present.
- Record `latency_ms` on every response.

**Constraints:** Zone C — this module assumes its input is already redacted and must not itself redact. It must never receive or forward card data.

**Done when:** `python -m packages.router.smoke_router` returns a schema-valid structured object from Gemini for a two-field Pydantic model, prints `latency_ms`, raises `ModelTimeoutError` with `timeout_seconds=0.001`, and raises `ModelSchemaError` for a prompt that cannot satisfy the schema after one retry.

**Do not implement yet:** Gemma, Kimi and Qwen backends (Task 26 stretch), any agent, prompt content for a real agent.

---

### Task 14 — Watcher

**Depends on:** Task 10, Task 13
**Files to create/modify:** `packages/agents/watcher.py`
**Files you must NOT touch:** `apps/api/`, `packages/atlas/`, other files in `packages/agents/`
**Read first:** `docs/SPEC.md` §2 (I7), §4 (`RecoveryCase`), `docs/INTERFACES.md` §5

**Do:**

- Implement `DisruptionSignal` and `Watcher` per `INTERFACES.md` §5. No model call — this agent is deterministic.
- `fingerprint` must be stable across duplicate deliveries of the same logical event and must not incorporate receipt time or delivery id.
- `ingest` must: look up an existing case by `trigger_fingerprint` and return it unchanged if found; otherwise load the order via `query_order_details` (never trust the webhook body, I7), upsert the `Order` row, and open exactly one `RecoveryCase` with a sequential `case_ref` (`RC-0001`, …) and `status="open"`.
- Write one `AgentEvent` per ingest with `actor=watcher`, including a distinct `step` for the deduplicated case.
- Handle a manual trigger through the same path with `kind="manual_trigger"`.

**Constraints:** I7 — order facts come only from `query_order_details`. I8 — audit every ingest, including duplicates.

**Done when:** `python -m packages.agents.smoke_watcher` ingests the same fixture webhook payload three times and the database holds exactly one `RecoveryCase`, one `Order`, and three `AgentEvent` rows.

**Do not implement yet:** the HTTP webhook route, interpretation, search.

---

### Task 15 — Interpreter, text only

**Depends on:** Task 13, Task 14
**Files to create/modify:** `packages/agents/interpreter.py`, `packages/agents/prompts/interpreter.md`
**Files you must NOT touch:** `packages/guardian/`, `packages/router/`, other files in `packages/agents/`
**Read first:** `docs/SPEC.md` §2 (I1), §4 (`RecoveryIntent`), `docs/INTERFACES.md` §5

**Do:**

- Implement `InterpreterInput` and `Interpreter` per `INTERFACES.md` §5, handling the `text` input only; leave `voice` and `photo` accepted but ignored with an explicit `NotImplementedError` guard if supplied.
- Call `guardian.redaction.redact` on all text and `assert_no_pii` on the final request payload before it reaches the router (I4).
- Use `generate_structured` with a `RecoveryIntent`-shaped Pydantic schema at `temperature=0.0`.
- The prompt must forbid producing flight numbers, carriers, prices or offer ids — constraints only (I1). Keep the prompt in `packages/agents/prompts/interpreter.md` and load it, so it is reviewable as a file.
- Set `confidence`; when it falls below `0.6`, do not guess — implement `clarification_question` returning one short question in `intent.language`.
- Write `AgentEvent` rows for interpretation started, succeeded or needs-clarification.

**Constraints:** I1 — the intent contains constraints only. Any itinerary-shaped field in a model response is dropped, not stored.

**Done when:** `python -m packages.agents.smoke_interpreter "My flight was cancelled, I must reach Singapore before 2pm tomorrow, I can spend up to 400 dollars, I walk with a cane and I speak Mandarin"` prints a `RecoveryIntent` with `must_arrive_by` set, `budget_ceiling_sgd == 400`, non-empty `mobility_notes`, `language` starting with `zh`, and `raw_input_kinds == ["text"]`; and a two-word vague input yields a clarification question instead of an intent.

**Do not implement yet:** voice, photo, EXIF handling, Strategist.

---

### Task 16 — Interpreter, multimodal

**Depends on:** Task 15
**Files to create/modify:** `packages/agents/interpreter.py`, `packages/agents/prompts/interpreter.md`, `fixtures/personas/README.md`
**Files you must NOT touch:** `packages/guardian/redaction.py`, `packages/router/`, other files in `packages/agents/`
**Read first:** `docs/SPEC.md` §2 (I1, I4), §3 (Zone C), `docs/INTERFACES.md` §5

**Do:**

- Extend `Interpreter.interpret` to accept `voice` and `photo`, sending them through `ModelRequest.audio` and `ModelRequest.images` in a single call alongside the text prompt.
- Call `redact_image_metadata` on every image before egress and assert the returned bytes carry no EXIF (I4).
- Set `raw_input_kinds` to reflect exactly which modalities were supplied.
- Extend the prompt file to instruct: transcribe the voice note, read the departure board photo for the disruption facts, and still emit constraints only — never an itinerary (I1).
- Document in `fixtures/personas/README.md` the three personas and the file naming for their pre-recorded voice notes and photos, so the reviewer can supply them.
- Run interpretation concurrently with the first search where the orchestrator allows it, but never make search depend on unvalidated intent.

**Constraints:** I4 — no EXIF, no GPS, no unredacted name reaches Zone C. I1 — photo-derived text is evidence, never an offer.

**Done when:** `python -m packages.agents.smoke_interpreter --voice fixtures/personas/tan_voice.m4a --photo fixtures/personas/tan_board.jpg` returns a `RecoveryIntent` with `language` starting with `zh`, `must_arrive_by` set, and `raw_input_kinds == ["voice", "photo"]`; and the bytes sent to the router contain no EXIF marker.

**Do not implement yet:** Gemma sovereign path, Strategist, TTS output.

---

### Task 17 — Strategist

**Depends on:** Task 4, Task 15
**Files to create/modify:** `packages/agents/strategist.py`, `packages/agents/prompts/strategist.md`, `packages/agents/prompts/scoring_codegen.md`
**Files you must NOT touch:** `packages/atlas/`, `packages/executors/`, other files in `packages/agents/`
**Read first:** `docs/SPEC.md` §2 (I1), §4 (`Candidate`), `docs/INTERFACES.md` §5

**Do:**

- Implement `StrategyPlan`, `RankedSelection` and `Strategist` per `INTERFACES.md` §5.
- `plan` returns up to four `StrategyPlan`s, one per `SearchStrategy`, each with a concrete `SearchRequest` derived from the intent's origin and destination candidates.
- `fan_out` issues the searches concurrently, converts every `Offer` into a persisted `Candidate` tagged with its `strategy`, deduplicates by `offer_id`, tolerates a per-strategy `AtlasNoResultsError`, and raises `AtlasNoResultsError` only when every strategy is empty.
- `write_scoring_code` returns model-generated Python defining `def score(payload: dict) -> list[dict]`, stdlib only, no network, no imports outside the stdlib. Reject and retry once if the generated code imports anything else or fails to define `score`.
- `select` returns a `RankedSelection` and must discard any `offer_id` not present in `candidates`, silently (I1).
- Persist `segments_json`, `price`, `arrival_delay_minutes`, `stop_count` and `min_transfer_minutes` verbatim from Atlas.
- Write `AgentEvent` rows per strategy dispatched and per search returned, with counts.

**Constraints:** I1 — the model chooses among offer ids and writes scoring code; it never authors an itinerary. Generated code is untrusted and will run only in Zone B.

**Done when:** `python -m packages.agents.smoke_strategist <case_ref>` prints four dispatched strategies, at least six deduplicated `Candidate` rows persisted with distinct `offer_id`s, and a `write_scoring_code` output that compiles, defines `score`, and imports nothing outside the stdlib; and a `select` call containing one fabricated `offer_id` returns a list excluding it.

**Do not implement yet:** running the scoring code, verification, ordering.

---

### Task 18 — Executor agent: score, verify, cap

**Depends on:** Task 5, Task 9, Task 12, Task 17
**Files to create/modify:** `packages/agents/executor_agent.py`
**Files you must NOT touch:** `packages/executors/`, `packages/guardian/`, `packages/atlas/`
**Read first:** `docs/SPEC.md` §2 (I1, I2, I3), §3, `docs/INTERFACES.md` §3, §5

**Do:**

- Implement `ExecutionAttempt`, `ExecutionOutcome` and `ExecutorAgent.__init__` plus `score_and_verify` per `INTERFACES.md` §5. Leave `execute` as `NotImplementedError`.
- Build `ScoringInput` from the candidates and intent, deriving `mobility_penalty_weight` from `mobility_notes`, and assert the Zone B allowlist before dispatch.
- Call `executor.score`; on `ExecutorUnavailableError`, construct a `LocalExecutor` and retry once, writing an `AgentEvent` that records the fallback (I10).
- Persist `score`, `score_components_json`, `self_transfer_risk` and `mobility_fit` onto each `Candidate`; drop scores for unknown offer ids.
- Verify the top three via `atlas.verify` (I2), persisting `verified` and `verified_price`, and set `rejected_reason="verify_failed"` or `"price_moved"` on failures.
- Run `check_spend_cap` against each candidate's **verified** price and set `rejected_reason="over_cap"` on rejection (I3).
- Forward every `SandboxStatus` to `on_status`, and write `AgentEvent` rows for scoring started, scoring done, each verification, and each cap rejection.

**Constraints:** I2 — nothing unverified proceeds. I3 — the cap uses `min(intent ceiling, env cap)` and no model output.

**Done when:** `python -m packages.agents.smoke_score_verify <case_ref>` prints a descending ranking, shows the top three marked `verified=True` with authoritative prices, marks at least one candidate `over_cap` when run with `GUARDIAN_MAX_SPEND_SGD=1`, and completes with `EXECUTOR=daytona` and with the Daytona key removed, logging the local fallback in both the trace and the audit log.

**Do not implement yet:** `execute`, ordering, payment, failover.

---

### Task 19 — Executor agent: confirm, order, pay, failover

**Depends on:** Task 6, Task 7, Task 18
**Files to create/modify:** `packages/agents/executor_agent.py`
**Files you must NOT touch:** `packages/guardian/policy.py`, `packages/atlas/client.py`
**Read first:** `docs/SPEC.md` §2 (I2, I4, I6, I7), §7, `docs/INTERFACES.md` §5

**Do:**

- Implement `ExecutorAgent.execute` per `INTERFACES.md` §5.
- Refuse to proceed unless `gate.is_confirmed(case_id, first_candidate.id)` is true — raise, do not warn (I6).
- For each attempt in order: re-verify the candidate (I2), re-run `check_spend_cap` on the freshly verified price (I3), then `order.do` then `pay.do`, recording an `ExecutionAttempt` with the verbatim `error_code` on failure.
- On `AtlasPaymentDeclinedError` (`604`) or `AtlasThreeDSRequiredError` (`616`), automatically advance to the next candidate up to `max_attempts` — no new human tap is required, and the originally confirmed cap still binds every retry.
- After any successful pay, call `poll_order_until` with the ticketed terminal statuses and treat its result as authoritative (I7).
- Update `RecoveryCase.status` to `recovered` or `failed`, and set `resolved_at`.
- Write an `AgentEvent` per attempt, including declines, and never let card data into any event payload (I4).

**Constraints:** I6 — exactly one human tap for the whole case. I4 — card data never enters an event, a log, or a cassette.

**Done when:** `CHAOS_PROFILE=none python -m packages.agents.smoke_execute <case_ref>` issues a ticket and returns `succeeded=True` with `len(attempts) == 1`; the same command with `CHAOS_PROFILE=decline` returns `succeeded=True` with `attempts[0].error_code == "604"` and `attempts[1].paid == True`, with zero additional confirmations recorded; and calling `execute` without a confirmation raises.

**Do not implement yet:** the chaos rewrite itself (Task 25 wires `apply_chaos`; here just honour the raised errors), the receipt, Caretaker.

---

## Phase 5 — API and SSE

### Task 20 — Webhook receiver and manual trigger

**Depends on:** Task 14
**Files to create/modify:** `apps/api/routes_webhook.py`, `apps/api/main.py`
**Files you must NOT touch:** `packages/agents/watcher.py`, `packages/guardian/`
**Read first:** `docs/SPEC.md` §2 (I7), `docs/INTERFACES.md` §5

**Do:**

- Add `POST /webhooks/atlas` accepting Atlas's schedule-change, cancellation, ticketing-complete, void, airline-status, email and incident events **[E]**, mapping each to a `DisruptionSignal` and delegating to `Watcher.ingest`.
- Return `200` with the `case_ref` for every accepted delivery, including duplicates — a duplicate is a success, not an error (I7).
- Never trust the webhook body for order facts; `Watcher` re-reads via `query_order_details`.
- Add `POST /cases/trigger` requiring the `OPERATOR_TOKEN` bearer, accepting `{"atlas_order_no": "..."}`, and producing `kind="manual_trigger"` — this is the demo's webhook fallback.
- Register both routers in `apps/api/main.py` and add a bearer-token dependency for operator routes.
- Log the raw delivery to `AgentEvent` with the payload redacted.

**Constraints:** Idempotency is enforced by `trigger_fingerprint`, not by the route. Unauthenticated access to `/cases/trigger` returns `401`.

**Done when:** posting the same fixture webhook body twice returns `200` with the same `case_ref` both times and leaves exactly one `RecoveryCase`; and `POST /cases/trigger` without a bearer token returns `401`, with a valid token returns a new `case_ref`.

**Do not implement yet:** SSE, the confirm endpoint, any template.

---

### Task 21 — Case endpoints and SSE trace stream

**Depends on:** Task 10, Task 19, Task 20
**Files to create/modify:** `apps/api/routes_cases.py`, `apps/api/sse.py`, `apps/api/main.py`
**Files you must NOT touch:** `packages/guardian/audit.py`, `packages/agents/`
**Read first:** `docs/SPEC.md` §2 (I5, I6, I8), `docs/INTERFACES.md` §6

**Do:**

- Write `apps/api/sse.py` with `TraceEvent`, `SandboxGridEvent`, `CandidatesEvent`, `ConfirmationEvent`, `ReceiptEvent` and `CaseStatusEvent` exactly as `INTERFACES.md` §6 defines them, plus a per-case in-process publisher.
- Add `GET /cases/{case_ref}/stream` emitting the six named event types with `id` set to the `AgentEvent.id`, honouring `Last-Event-ID` by replaying from `read_events(after_id=...)`, and sending a `heartbeat` every 15 seconds.
- Every event carries a full state snapshot for its concern, never a patch.
- Add `GET /cases/{case_ref}` returning the case, intent, candidates and receipt as JSON.
- Add `POST /cases/{case_ref}/confirm` accepting `{"candidate_id": N, "nonce": "..."}`, delegating to `ConfirmationGate.resolve`, persisting the decision, and then launching `ExecutorAgent.execute` as a background task (I6).
- Add `POST /cases/{case_ref}/run` (operator token) that drives interpret → plan → fan_out → score_and_verify and opens the confirmation gate, publishing events as it goes.
- Stream steps only — no token-level output anywhere (I5).

**Constraints:** I5, I6, I8. The stream must survive a client reconnect without losing or duplicating an event.

**Done when:** with a case in flight, `curl -N localhost:8000/cases/RC-0001/stream` prints named events with increasing ids; reconnecting with `-H "Last-Event-ID: <n>"` resumes at `n+1` with no gap and no repeat; `POST /confirm` with a stale nonce returns `4xx`; and a valid confirm transitions the case to `executing`.

**Do not implement yet:** any HTML template, the sandbox grid rendering, the receipt computation.

---

## Phase 6 — UI

### Task 22 — Three-pane shell and the Recovery Case pane

**Depends on:** Task 21
**Files to create/modify:** `apps/web/templates/base.html`, `apps/web/templates/case.html`, `apps/web/templates/_pane_case.html`, `apps/api/routes_web.py`
**Files you must NOT touch:** `apps/api/routes_cases.py`, `apps/api/sse.py`, `packages/`
**Read first:** `docs/SPEC.md` §6 (non-goals 6, 10), `hackathon-strategy.md` §5.2

**Do:**

- Write `base.html` loading Tailwind and HTMX from CDN — no build step, no bundler, no React (SPEC §6.6).
- Lay out three panes on one screen with no navigation: left Recovery Case, centre agent trace, right traveller view. Dark, calm, monospace timings.
- Implement the left pane: traveller, original itinerary rendered with a strikethrough, and a live status chip bound to the `status` SSE event.
- Add `GET /` in `routes_web.py` which, per the `SURFACE` setting, renders the operator console or redirects to the traveller view (I10), and `GET /cases/{case_ref}` rendering `case.html`.
- Everything must be legible at three metres — large type, high contrast — because Daytona allows only 2 minutes.
- Leave the centre and right panes as empty placeholder divs with stable ids.

**Constraints:** No build step, no SPA, no client-side router. Operator routes stay behind the bearer token.

**Done when:** loading `/cases/RC-0001` in a browser shows three panes, the left pane populated from the API with the original itinerary struck through, and the status chip changing without a page reload when the case status changes.

**Do not implement yet:** the trace, the sandbox grid, the option cards, the confirm button, the traveller pane.

---

### Task 23 — Centre pane: trace, sandbox grid, options, confirm

**Depends on:** Task 22
**Files to create/modify:** `apps/web/templates/_pane_trace.html`, `apps/web/static/trace.js`
**Files you must NOT touch:** `apps/web/templates/base.html`, `apps/api/sse.py`, `packages/`
**Read first:** `docs/SPEC.md` §2 (I5, I6), §7, `docs/INTERFACES.md` §6

**Do:**

- Subscribe to `GET /cases/{case_ref}/stream` with `EventSource` and render `trace` events as one line per step with the step name, summary and elapsed time (I5).
- Render `sandboxes` events as a grid of `DAYTONA_TARGET_SANDBOXES` tiles transitioning pending → amber (`starting`/`running`) → green (`done`) or red (`failed`), driven by the full-snapshot payload so a missed event cannot desynchronise the grid.
- Render `candidates` events as ranked cards showing price, arrival, delay versus original, and the visible score components; mark `verified` explicitly and show `rejected_reason` where set.
- Render `confirmation` events as exactly one **Confirm** button per recommended candidate, posting `candidate_id` and `nonce` to `/cases/{case_ref}/confirm`, disabled after the first click (I6).
- Show the Guardian policy pane inline: the effective cap, the redactions performed (`kinds_found`), and the pending confirmation.
- On reconnect, pass `Last-Event-ID` so the pane rebuilds without gaps.

**Constraints:** I5 — no token streaming. I6 — the button is the single human tap; there is no way to confirm twice.

**Done when:** running a case end to end shows the trace appending in real time, eight tiles going amber then green in parallel, three verified option cards with visible score components, and one Confirm click transitioning the case to `executing` with the button disabled; killing and reloading the page mid-run rebuilds the identical pane state.

**Do not implement yet:** the traveller pane, the receipt rendering.

---

### Task 24 — Right pane: traveller view and traveller surface

**Depends on:** Task 23
**Files to create/modify:** `apps/web/templates/_pane_traveller.html`, `apps/web/templates/traveller.html`, `apps/api/routes_web.py`
**Files you must NOT touch:** `apps/web/templates/_pane_trace.html`, `packages/`
**Read first:** `docs/SPEC.md` §6 (non-goals 3, 5, 10), `hackathon-strategy.md` §5.2

**Do:**

- Render the right pane as an actual phone frame containing the traveller's view: very large type, high contrast, minimal words, and a play button for the spoken plan.
- Implement `GET /t/{token}` serving `traveller.html` for a signed magic link — no account, no login, no password (SPEC §6.3).
- The traveller view must be usable by a 71-year-old under stress: one decision on screen at a time, tap targets no smaller than 44px, no icon without a text label.
- When `SURFACE=traveller`, `GET /` lands directly on the traveller view (I10).
- Show the delivered artifacts when present: the spoken plan audio, a link to the large-print PDF, and confirmation that the family message was sent.
- Reuse the same SSE stream; do not add a second transport.

**Constraints:** One language pair only (SPEC §6.5). No account system. No native app.

**Done when:** the right pane renders the traveller view inside a phone frame with the spoken-plan play button working; `SURFACE=traveller` makes `/` land on it; and a signed `/t/{token}` link opens the same view in a fresh browser session with no login.

**Do not implement yet:** TTS generation itself, PDF generation, Telegram.

---

## Phase 7 — Chaos and polish

### Task 25 — Chaos harness

**Depends on:** Task 19, Task 23
**Files to create/modify:** `packages/atlas/chaos.py`, `packages/atlas/client.py`, `apps/api/routes_chaos.py`
**Files you must NOT touch:** `packages/guardian/`, `packages/agents/executor_agent.py`
**Read first:** `docs/SPEC.md` §2 (I3, I10), §7, `hackathon-strategy.md` Appendix A, §6.1

**Do:**

- Implement `apply_chaos` per `INTERFACES.md` §1.5, rewriting `CardDetails.holder_given_name` to `"Reject"` for `DECLINE` and `"Three DS"` for `THREE_DS` — Atlas's own documented sandbox triggers producing `604` and `616` **[E]**.
- For `TIMEOUT`, have the transport delay past its configured timeout so `AtlasTimeoutError` is raised naturally.
- Wire `apply_chaos` into `AtlasClient.pay` so the active `CHAOS_PROFILE` is applied on every payment.
- Add `POST /chaos` (operator token) setting the active profile at runtime, plus `GET /chaos` returning it, and surface the active profile in the UI header — this is a product feature, not a hidden test hook.
- Chaos must never bypass Guardian: the cap and the confirmation gate apply identically under every profile (I3, I6).
- Write an `AgentEvent` whenever the profile changes and whenever an injected failure fires.

**Constraints:** Chaos alters only the cardholder first name and transport timing. It must never fabricate an Atlas response — the failure must come from Atlas.

**Done when:** `POST /chaos {"profile":"decline"}` then running a case shows a real Atlas `604` in the trace followed by automatic failover to option 2 and a successful ticket, with the UI header showing the active profile; `profile":"3ds"` produces `616`; and switching back to `none` restores the clean happy path.

**Do not implement yet:** the receipt, replay parity checking.

---

### Task 26 — Recovery Receipt, counterfactual, replay parity, `demo.sh`

**Depends on:** Task 24, Task 25
**Files to create/modify:** `packages/agents/caretaker.py`, `packages/agents/counterfactual.py`, `apps/web/templates/_receipt.html`, `ops/demo.sh`
**Files you must NOT touch:** `packages/guardian/`, `packages/atlas/`, `docs/SPEC.md`
**Read first:** `docs/SPEC.md` §4 (`RecoveryReceipt`), §7, `docs/INTERFACES.md` §5, `hackathon-strategy.md` §7.6, §9.1

**Do:**

- Implement `Caretaker` per `INTERFACES.md` §5: `deliver` producing the spoken plan in `intent.language`, the large-print one-page PDF, and the family Telegram message, with every flight fact interpolated from `OrderDetails` and never generated (I1).
- Implement `packages/agents/counterfactual.py` computing the DIY baseline deterministically — no model call — yielding `counterfactual_cost_delta_sgd` and `counterfactual_hours_delta`.
- Implement `build_receipt` assembling `RecoveryReceipt` from the case, the outcome and `read_events`, storing the ordered event ids so the receipt is replayable (I8).
- Render `_receipt.html` below the fold: elapsed seconds, `human_taps`, amount paid, both counterfactual deltas, and every attempt including declines.
- Write `ops/demo.sh` that resets SQLite, reseeds the three personas and three pre-created Atlas sandbox orders, warms the Atlas and model connections, and prints the wall-clock time of each demo stage.
- Add a parity check invoked by `demo.sh` that runs the rehearsed case in `live` and `replay` mode and asserts the emitted `step` sequence is identical (I9).

**Constraints:** I1 — receipt numbers are computed, never model-generated. I9 — replay must produce the same step sequence as live.

**Done when:** `ops/demo.sh` runs from a cold start and the rehearsed happy path completes in under 90 seconds three consecutive times; the receipt shows `human_taps == 1` with both counterfactual deltas populated; the parity check prints `PARITY OK` for `live` versus `replay`; and every checkbox in `SPEC.md` §7 is ticked.

**Do not implement yet:** nothing — this is the last task. After this, freeze features and spend remaining time on reliability and the pitch.

---

## How to use this file

1. **Open one task at a time.** Read it fully before starting.
2. **Paste only that task's block** into Cursor with Grok 4.5 selected. Do not paste two tasks into one session, and do not paste this whole file.
3. Let the agent read `docs/SPEC.md` and `docs/INTERFACES.md` itself — they are named in every task's **Read first**.
4. **Review the diff against "Files to create/modify."** A file outside that list was not authorised; revert it.
5. **Run the "Done when" check literally.** It is the only acceptance signal. If it does not pass, iterate inside the same session rather than moving on.
6. **Start a fresh session for the next task.** Context from the previous task is a liability, not an asset — the frozen docs carry everything needed.
7. If the agent argues an interface is wrong, it must append the objection to `docs/RISKS.md` and implement the interface as written. Only the human owner edits `SPEC.md` or `INTERFACES.md`; needing to edit them is a signal something upstream broke.
8. Append every architectural decision to `docs/QODER.md` as you go — it is submission evidence for the Atlas rubric's 20% "Use of Qoder" criterion **[E]**.
9. **Checkpoint at Tasks 7, 12, 19 and 24** — these map to the strategy's hour-4, hour-12, hour-24 and hour-40 review points. Tag the repo when green.
