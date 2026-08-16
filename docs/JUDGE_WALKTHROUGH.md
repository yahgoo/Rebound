# Rebound — Judge Walkthrough & Technology Stack

**Generated:** 15 Aug 2026  
**Verdict:** `TASK 26 NOT VERIFIED`  
**Method:** Code inspection + read-only local walkthrough (no DB reset, no new bookings)

---

# Part 1: Browser walkthrough — load to recovery receipt

## How to start

| Item | Detail |
|---|---|
| **Command** | From `rebound/`: `uv run uvicorn apps.api.main:app --host 127.0.0.1 --port 8000` (demo script uses the same via `ops/demo.sh`) |
| **URL** | `http://127.0.0.1:8000/` or `http://127.0.0.1:8000/cases/{case_ref}` |
| **Auth** | Operator HTML routes require `Authorization: Bearer $OPERATOR_TOKEN`. Demo sets `OPERATOR_TOKEN=rebound-demo-operator` if unset. `.env` does **not** define `OPERATOR_TOKEN` today. |
| **Health check** | `GET /healthz` → `{status, mode, executor, surface, chaos}` — no auth required |

**Important:** The browser is a **monitoring and confirmation console**, not the place cases are started. Case creation and the agent pipeline are triggered by API calls (`demo.sh` uses `curl`). The one human action in the browser is **Confirm** during `awaiting_confirmation`.

---

## Step-by-step journey

### 1. Start the web app

- **User:** Opens the operator URL (with bearer token — typically via a browser extension or by running the demo script first).
- **Screen:** Three-pane dark console: case summary (left), agent trace (center), traveller phone mock (right).
- **Frontend:** `base.html` → `case.html` → `_pane_case.html`, `_pane_trace.html`, `_pane_traveller.html`; `trace.js` for SSE.
- **Backend:** FastAPI in `apps/api/main.py`; Jinja via `apps/api/routes_web.py`.
- **DB/events:** None yet.
- **External:** None.
- **Observed:** Server started read-only in **replay** mode; `GET /healthz` returned `mode=replay`, `executor=local`. **Code inspection** for HTML auth requirement.

---

### 2. Initial page

- **User:** Lands on `/` or `/cases/RC-0001`.
- **Screen (left pane):** Case ref (`RC-0001`), status chip (`failed` in current DB), traveller name, struck-through disrupted itinerary (e.g. CGK → SUB).
- **Screen (center):** “Live agent trace”, Guardian policy, sandbox grid, recovery options, agent steps, **Recovery Receipt** section (“Below the fold”).
- **Screen (right):** Phone mock with traveller-friendly headline/detail; play/PDF/family buttons.
- **Frontend:** Server-rendered Jinja; status chip also listens to HTMX SSE on `_pane_case.html`.
- **Backend:** `landing_page()` / `case_page()` in `routes_web.py` loads latest case from SQLite.
- **DB:** Reads `RecoveryCase`, `Order`, `RecoveryIntent`, `AgentEvent`.
- **External:** None.
- **Observed:** HTML fetched with bearer token shows `RC-0001`, status `failed`, traveller “Tan Mei Lin”, receipt initially **Pending** until JS polls JSON. **Read-only walkthrough on existing DB.**

---

### 3. Create a disruption case

- **User:** Does **not** click anything in the browser. Demo operator (or Atlas) triggers the case.
- **Screen:** Case appears on refresh; status moves to `waiting` then pipeline states.
- **Frontend:** None for creation — browser is passive until SSE events arrive.
- **Backend routes:**
  - `POST /webhooks/atlas` → `atlas_webhook()` in `routes_webhook.py`
  - **Demo path:** `POST /cases/trigger` with `{"atlas_order_no":"..."}` → `manual_trigger()`
- **Python:** `Watcher.ingest()` in `packages/agents/watcher.py` — **no model**; loads order facts from Atlas `queryOrderDetails`.
- **DB:** New `RecoveryCase` (+ `Order` row if needed); `AgentEvent` `watcher.ingest`.
- **External:** Atlas `queryOrderDetails.do` (live records cassettes; replay plays cassettes).
- **Observed:** SSE event 1: `watcher.ingest` / `manual_trigger` on `TESTA20260815020605810`. **Observed via SSE replay from existing DB**, not a new trigger in this session.

---

### 4. Interpret the traveller’s request

- **User:** Still passive in browser.
- **Screen:** Trace fills with “interpreter.started / interpreter.succeeded”; status → `interpreting` then `planning`.
- **Frontend:** `trace.js` listens for SSE `trace` and `status` on `/cases/{case_ref}/stream`.
- **Backend:** `POST /cases/{case_ref}/run` → `run_case()` in `routes_cases.py`.
- **Python:** `Interpreter.interpret()` in `packages/agents/interpreter.py`.
- **Model:** Gemini via OpenRouter (`GEMINI_VIA=openrouter`, model `google/gemini-3.6-flash` in `openrouter_gemini.py`). Interprets **constraints only** (budget, deadline, language, mobility) — not flight numbers or prices.
- **DB:** `RecoveryIntent` row; events `interpreter.started`, `interpreter.succeeded`.
- **External:** Model API only (OpenRouter).
- **Demo input:** `RunBody.text` or auto-generated `_default_run_text()` — **text only** in the browser demo path (no voice/photo wired to `/run`).
- **Observed:** SSE shows confidence `0.95`, budget S$800, language `en`. **Observed in replay from DB.**

---

### 5. Search and display flight alternatives

- **User:** Watches center pane populate.
- **Screen:** “Sandbox fan-out” grid (8 slots); “Recovery options” list with prices, arrival times, verify status; trace shows strategist steps.
- **Frontend:** SSE events `sandboxes`, `candidates`; `trace.js` → `renderSandboxes()`, `renderCandidates()`.
- **Backend:** Same `run_case()` orchestration.
- **Python:** `Strategist.plan()` + `Strategist.fan_out()` in `packages/agents/strategist.py`; model picks **search strategies**, not itineraries.
- **DB:** Many `Candidate` rows; events `strategist.strategy_dispatched`, `strategist.search_returned`.
- **External:** Atlas `search.do` (multiple strategies: same_route_later, nearby_airport, one_stop_reroute, next_morning_hotel).
- **Observed:** 13 candidates returned (6+6+6+7 searches deduped). **Observed via SSE from existing case** (events 7–15 on `/stream`).

---

### 6. Verify candidates

- **User:** Still watching.
- **Screen:** Candidates gain scores; top options show `verified=true` and verified prices; over-cap options marked rejected.
- **Frontend:** Updated `candidates` SSE snapshots.
- **Backend:** `run_case()` continues inside `ExecutorAgent.score_and_verify()`.
- **Python:** `ExecutorAgent` in `packages/agents/executor_agent.py` — runs model-generated scoring code in **LocalExecutor** (default) or Daytona sandboxes; then Atlas `verify.do` on top 3.
- **DB:** Candidate `verified`, `verified_price`, `score`, `rejected_reason` updated; events `executor.scoring_*`, `executor.verified`, `executor.cap_rejected`.
- **External:** Atlas `verify.do`.
- **Observed:** 3 verified candidates in DB. **Observed in replay from DB.**

---

### 7. Human confirmation (“one tap”)

- **User:** Clicks **Confirm** on the recommended candidate (only interactive browser step).
- **Screen:** Guardian shows effective cap; button locks to “Confirmation sent”; status → `executing`.
- **Frontend:** `trace.js` → `confirmCandidate()` → `POST /cases/{case_ref}/confirm` with `{candidate_id, nonce}`; nonce stored in `sessionStorage` to prevent double-tap.
- **Backend:** `confirm_case()` in `routes_cases.py` → `ConfirmationGate.resolve()`.
- **DB:** `AgentEvent` `confirmation.resolved` with `human_taps: 1`; status `executing`.
- **External:** None.
- **Observed:** Receipt shows `human_taps=1`. Confirm happened in the prior live run that populated this DB; **not re-clicked** in this read-only session. **Observed via DB/receipt JSON.**

---

### 8. Order, payment, polling, failover

- **User:** Watches trace; no further clicks (failover is automatic).
- **Screen:** Attempt rows in trace; receipt section lists every attempt including errors.
- **Backend:** Background `_execute_confirmed()` → `ExecutorAgent.execute()`.
- **Python flow:** For up to 3 candidates: `verify` (re-verify on failover) → `order.do` → `pay.do` → `queryOrderDetails` poll until `ticketed`.
- **Failover:** On 604/616 (pay decline/3DS) or order failures like **318**, tries next candidate **without a second human tap** (I6).
- **DB:** Events `executor.attempt_started`, `executor.ordered`, `executor.paid` / `executor.pay_failed`, `executor.attempt_finished`, `executor.execute_finished`; case → `recovered` or `failed`.
- **External:** Atlas `order.do`, `pay.do`, `queryOrderDetails.do`.
- **Observed (current DB, failed run):** 3 attempts, all `error_code=318` at order stage; case `failed`. **Observed in replay.**
- **Observed (prior successful demo run):** attempts `318, 318, paid`; `amount_paid=75.81 USD`; status `recovered`. **Observed in live demo, not re-run here.**

---

### 9. Caretaker — spoken plan, PDF, family Telegram

- **User (traveller pane):** Would see Play / PDF / “Message sent to family” when delivery succeeds.
- **Screen:** Large-print friendly copy; optional audio; PDF link.
- **Python:** `Caretaker.deliver()` in `packages/agents/caretaker.py` — model writes **template copy only**; flight facts interpolated from Atlas `OrderDetails` (I1).
- **PDF:** Minimal PDF bytes written by `FileNotifier.render_pdf()` — values from `OrderDetails`, not the model.
- **Telegram:** `httpx` POST to Telegram API if `TELEGRAM_BOT_TOKEN` + `TELEGRAM_FAMILY_CHAT_ID` set.
- **TTS:** macOS `say` unless `DEMO_SKIP_TTS=1`.
- **DB:** `caretaker.delivered` event with artifact paths.
- **Observed:** Current failed case has **no** `caretaker.delivered` — deliver runs only when `outcome.succeeded`. Prior I1 proof showed verbatim flight/price in spoken/PDF/family. **Telegram not sent** (`telegram_sent=false` — credentials absent). PDF file exists on disk for `RC-0001` but deliver event absent (likely leftover test artifact).

---

### 10. Receipt assembly and rendering

- **User:** Scrolls center pane to “Below the fold — Recovery Receipt”.
- **Screen:** Elapsed seconds, human taps, amount paid, vs-DIY deltas, every attempt.
- **Frontend:** `_receipt.html` — server shows Pending placeholders; inline JS polls `GET /cases/{case_ref}` (JSON) every 2s and fills fields.
- **Backend:** `Caretaker.build_receipt()` after execute; JSON via `get_case()` in `routes_cases.py`.
- **Python:** `counterfactual.compute_from_candidates()` — **pure math**, no model.
- **DB:** `RecoveryReceipt` with `attempts_json`, `event_ids_json`, deltas.
- **Observed (current DB):** `human_taps=1`, `amount_paid≈0 SGD` (failed booking), 3 attempts with 318, deltas present. **Observed via JSON poll.**
- **Observed (successful run):** `75.81 USD`, `-29.29 SGD`, `3.25h`. **From prior live demo logs.**

---

### 11. SQLite and event log

| Store | What |
|---|---|
| **File** | `rebound/rebound.db` (WAL/SHM alongside during writes) |
| **Tables** | `Order`, `RecoveryCase`, `RecoveryIntent`, `Candidate`, `RecoveryReceipt`, `AgentEvent` |
| **Case** | Ref, status, trigger kind/fingerprint, order link |
| **Intent** | Parsed constraints from Interpreter |
| **Candidates** | Atlas offer IDs, segments, prices, verify state |
| **Receipt** | Timing, taps, attempts, paid amount, counterfactual deltas, **`event_ids_json`** |
| **Event log** | Append-only `AgentEvent` — every agent step with actor, step, payload |

**Receipt replay:** Ordered event IDs let `caretaker receipt-rebuild` reconstruct the receipt field-for-field (I8 — verified synthetically).

**Accumulation:** `demo.sh` **deletes** the DB each run (`reset_sqlite`). Without reset, cases accumulate; current DB has 1 case, 76 events, 1 receipt.

---

### 12. Live vs replay mode

| | **Live** (`REBOUND_MODE=live`) | **Replay** (`REBOUND_MODE=replay`) |
|---|---|---|
| Atlas transport | `LiveTransport` + `CassetteRecorder` | `ReplayTransport` + `CassettePlayer` |
| Behavior | Real sandbox API calls; writes cassettes | Plays saved JSON fixtures |
| UI/agents | Identical Python agents and same routes | Same |
| Demo default | `demo.sh` starts live for happy path | Parity phase restarts in replay |

**Observed:** Read-only walkthrough used **replay** on existing DB. Live-vs-replay **never printed `PARITY OK`** in demo runs (replay missed cassettes after live 318 failover).

---

### 13. What is a cassette?

**Analogy:** A cassette is a saved recording of an external API conversation. Live mode talks to Atlas and records request/response. Replay mode uses the recording instead of calling Atlas.

| Item | Detail |
|---|---|
| **Location** | `fixtures/cassettes/*.json` (~107 files) |
| **Recorded calls** | `search.do`, `verify.do`, `order.do`, `pay.do`, `queryOrderDetails.do` |
| **Matching** | SHA-256 key from endpoint + canonical request (volatile fields like `sessionId` stripped) |
| **Miss causes** | Different search results, new offer/session IDs, missing order/pay recording after failover, body drift |
| **Parity** | `caretaker parity-dump` + `parity-compare` on step sequences — synthetic test showed `PARITY OK`; **end-to-end live vs replay did not** |

---

### 14. Atlas errors 318, 604, 616

| Code | Stage | What Rebound does |
|---|---|---|
| **318** | Usually `order.do` | Recorded as `error_code=318`; **failover to next candidate** (same as other order failures). Frequent in sandbox when re-ordering same passenger. Can exhaust all 3 attempts → `failed`. |
| **604** | `pay.do` | Raised as `AtlasPaymentDeclinedError`; failover to next candidate. Chaos profile `decline` uses cardholder name `"Reject"` — **Atlas sandbox often ignores this** (Task 25 not verified). |
| **616** | `pay.do` | Raised as `AtlasThreeDSRequiredError`; failover. Chaos `"Three DS"` name trigger — **same Atlas sandbox limitation**. |

Rebound does **not** invent these codes — they come from Atlas responses (or cassettes in replay).

---

### 15. What requires live credentials

| Capability | Required |
|---|---|
| Atlas search/verify/order/pay | `ATLAS_BASE_URL`, `ATLAS_CLIENT_ID`, `ATLAS_CLIENT_SECRET` |
| Interpreter / Strategist / Caretaker copy | OpenRouter (`GEMINI_VIA=openrouter`, `OPENROUTER_API_KEY`) or direct `GEMINI_API_KEY` |
| Operator browser/API | `OPERATOR_TOKEN` (demo default: `rebound-demo-operator`) |
| Telegram delivery | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_FAMILY_CHAT_ID` — **not configured** |
| Daytona scoring sandboxes | `DAYTONA_API_KEY` + `EXECUTOR=daytona` — **demo uses `EXECUTOR=local`** |
| Replay-only demo | Atlas credentials still needed at boot; runtime Atlas calls served from cassettes |

---

# Part 2: Technology stack (verified from code)

## Browser and web application

**Plain English:** The browser is the control panel. It sends requests to the Python application, and the Python application sends back updated case information.

| Layer | Actual implementation |
|---|---|
| **Server** | FastAPI + Uvicorn (`pyproject.toml`) |
| **Templates** | Jinja2 — `apps/web/templates/` |
| **CSS** | Tailwind via CDN in `base.html` |
| **Live updates** | HTMX SSE extension + native `EventSource` in `trace.js` on `/cases/{ref}/stream` |
| **Static JS** | `apps/web/static/trace.js` — trace, sandboxes, candidates, confirm button, guardian panel |
| **Receipt polling** | Inline JS in `_receipt.html` — JSON poll every 2s |
| **API** | JSON at `GET /cases/{ref}`; operator POSTs at `/cases/trigger`, `/cases/{ref}/run`, `/cases/{ref}/confirm` |
| **Webhooks** | `POST /webhooks/atlas` |

State reaches the browser through **SSE** (primary) and **JSON polling** (receipt only).

---

## Python agents

| Agent | Role | File / entry |
|---|---|---|
| **Watcher** | Ingest disruption; load order from Atlas; open case | `packages/agents/watcher.py` — `Watcher.ingest()` |
| **Interpreter** | Text/voice/photo → travel **constraints** | `packages/agents/interpreter.py` — `Interpreter.interpret()` |
| **Strategist** | Plan searches; fan-out; scoring code; rank offers | `packages/agents/strategist.py` — `Strategist.plan()`, `.fan_out()`, `.write_scoring_code()` |
| **ExecutorAgent** | Score, verify, cap check, **order/pay/poll**, failover | `packages/agents/executor_agent.py` — `.score_and_verify()`, `.execute()` |
| **Caretaker** | Spoken plan, PDF, Telegram, receipt | `packages/agents/caretaker.py` — `.deliver()`, `.build_receipt()` |
| **Counterfactual** | DIY baseline math for receipt | `packages/agents/counterfactual.py` — `compute_from_candidates()` |
| **Orchestrator** | Wires the pipeline | `apps/api/routes_cases.py` — `run_case()`, `confirm_case()`, `_execute_confirmed()` |
| **Guardian** | Spend cap, confirmation gate, redaction, audit | `packages/guardian/` |

There is no separate “router agent” — `packages/router/` is the **model router**, not travel routing.

---

## Gemini / model layer

| Question | Answer |
|---|---|
| **Used in browser demo?** | **Yes** — Interpreter, Strategist (plan + scoring code), Caretaker (copy templates) all call the model during `/run`. |
| **Provider** | OpenRouter → `google/gemini-3.6-flash` when `GEMINI_VIA=openrouter` (current `.env`) |
| **Factory** | `get_router()` in `packages/router/__init__.py` |
| **Model may decide** | Intent constraints, search strategy choices, scoring code, prose templates |
| **Model must not decide** | Flight numbers, prices, payment results, receipt figures — enforced by schema stripping (Interpreter), offer-ID-only selection (Strategist), and `OrderDetails` interpolation (Caretaker) |
| **Deterministic** | Guardian caps, verify results, order/pay outcomes, counterfactual deltas, receipt numbers |
| **If model unavailable** | `get_router()` raises; `/run` fails |
| **Warm step** | `demo.sh` `warm_model` sends real `ping` → expects model response — **actually verifies connectivity** (~2s in prior runs) |

**Distinction:** The model interprets or summarizes intent. The model does not invent booking facts. The receipt numbers are computed by code.

---

## Atlas

Rebound does **not** own airline inventory. Atlas supplies search results, verify prices, order IDs, payment results, and authoritative ticket status.

Endpoints used: `search.do`, `verify.do`, `order.do`, `pay.do`, `queryOrderDetails.do`, plus inbound webhooks.

Sandbox URL in `.env`: `https://sandbox.atriptech.com`.

Atlas errors (especially **318** on repeat orders) can stop an otherwise logically correct Rebound flow from reaching `recovered`.

---

## Daytona

| Question | Answer |
|---|---|
| **In repo?** | Yes — `daytona` dependency, `packages/executors/daytona.py` |
| **Purpose** | Run untrusted Strategist scoring code in isolated sandboxes (Zone B) |
| **Env** | `EXECUTOR=daytona`, `DAYTONA_API_KEY` |
| **Demo** | **`EXECUTOR=local`** — scoring runs in-process |
| **Practical difference** | Daytona would isolate scoring; local is faster and simpler for hackathon demo |
| **Task 26 verified with Daytona?** | **No** — not exercised in demo verification |

Daytona is an **available** execution path, not the active demo path.

---

## SQLite, cassettes, Telegram, PDF

- **SQLite:** `rebound.db` — see Part 1 §11.
- **Cassettes:** `fixtures/cassettes/` — see Part 1 §13.
- **PDF:** Minimal PDF bytes in `caretaker.py` (`_render_pdf`); facts from `OrderDetails`.
- **Telegram:** Implemented via `httpx`; requires `TELEGRAM_BOT_TOKEN` + `TELEGRAM_FAMILY_CHAT_ID` — **not configured in demo**.

---

## `ops/demo.sh` timing (from prior runs)

| Stage | Typical duration | Outcome |
|---|---|---|
| reset | 0s | OK |
| start_live | 2s | OK |
| reseed | 2s | OK (3 personas, Atlas order details) |
| warm_atlas | 1s | OK |
| warm_model | 2s | OK |
| trigger | 1s | OK |
| run | ~28s | OK → `awaiting_confirmation` |
| confirm | ~69s | OK when Atlas cooperates; **timeout possible** |
| receipt | 2s | OK |
| **happy_path total** | **92–100s** | **Failed <90s target** |
| **cold_start total** | **~108s** | Includes reset + warm |
| parity | — | **Failed** — no `PARITY OK` |

Set `SKIP_PARITY=1` to skip the replay comparison phase.

---

# Part 3: Safe read-only walkthrough (this session)

**Constraints honored:** No file edits to source, no DB reset, no reseed, no new `/run` or `/confirm`.

**Method:** Server in `REBOUND_MODE=replay` on existing `RC-0001` (failed case from prior live demo). HTML via authenticated `curl`; live state via JSON + SSE.

### SSE replay (events 1–15)

Connecting to `/cases/RC-0001/stream` with `Last-Event-ID: 0` replayed stored audit events without new Atlas calls:

1. `watcher.ingest` — manual trigger on `TESTA20260815020605810`
2. Status → `interpreting`
3. `interpreter.started` / `interpreter.succeeded` — confidence 0.95, budget S$800, English
4. Status → `planning`
5. `strategist.plan_completed` — 4 strategies
6. Four `strategist.strategy_dispatched` events
7. Four `strategist.search_returned` events (6+6+6+7 offers)
8. `candidates` snapshot — **13 options** (USD prices, CGK→SUB)

Capture truncated before confirmation, execution, 318 failures, and receipt (events 16+).

### Screens observed

1. **Initial case page:** `RC-0001`, status **failed**, traveller **Tan Mei Lin**, disrupted CGK→SUB itinerary, opened 14 Aug.
2. **Search/candidates:** SSE replayed 13 candidates with USD prices.
3. **Verification:** 3 candidates verified in DB.
4. **Confirmation:** Receipt proves **`human_taps=1`** from prior run.
5. **Execution/failover:** 3 attempts, all **`318`** at order stage; status **failed**.
6. **Receipt (below fold):** Initial HTML **Pending**; JSON poll fills **`human_taps=1`**, **`amount_paid≈0 SGD`**, cost delta **S$73.05**, hours delta **717.34h**, **3 attempts** listed.

**Browser note:** Operator pages require a **Bearer token** — a normal browser address bar alone returns 401. Demo uses `rebound-demo-operator`.

---

# Part 4: Explain the stack as a story

## A. 30-second judge explanation

When a flight is cancelled, Rebound opens a recovery case, reads what the traveller needs, searches real flights through Atlas, scores and verifies options, and asks a human for **one tap** to approve. It then books and pays automatically, trying backup flights if something fails. Finally it gives the traveller a spoken plan and a receipt that proves exactly what happened — including how much better Rebound was than doing it yourself.

## B. 2-minute technical-but-accessible explanation

You open a three-pane operator console in the browser. Behind it, FastAPI runs a team of Python agents: Watcher opens the case from an Atlas disruption, Interpreter (Gemini via OpenRouter) turns the traveller’s message into constraints, Strategist searches Atlas with multiple strategies, and Executor verifies prices and handles booking. The browser shows live progress over SSE; the operator clicks **Confirm once**. Executor then orders, pays, and polls Atlas, failing over to the next verified option if needed. Caretaker produces traveller-friendly output using real Atlas ticket facts, while a deterministic counterfactual calculator fills the receipt. Everything is stored in SQLite with an append-only event log. Live demos call Atlas sandbox; replay mode replays saved “cassettes” instead. Daytona exists for isolated scoring but the demo runs locally.

## C. Developer trace (one request)

1. `POST /cases/trigger` → Watcher → Atlas `queryOrderDetails` → `RecoveryCase RC-0001`.
2. `POST /cases/RC-0001/run` → Interpreter (model) → `RecoveryIntent` → Strategist (model + Atlas `search.do` × 4) → 13 `Candidate` rows.
3. Executor scores (local sandbox) → Atlas `verify.do` × 3 → confirmation gate opens → SSE `confirmation`.
4. Browser `POST /confirm` → `confirmation.resolved` (`human_taps=1`) → background `Executor.execute`.
5. Loop: verify → `order.do` → (`pay.do` if ordered) → poll `queryOrderDetails` until ticketed or fail.
6. On success: Caretaker `deliver` (model copy + Atlas facts) + `build_receipt` (counterfactual math + event IDs).
7. Browser receipt JS polls JSON; SSE streams all steps for audit.

---

# Part 5: Verification status

| Feature | Implemented | Observed working | Replay verified | Evidence / limitation |
|---|---:|---:|---:|---|
| Browser case creation | ✓ | — | — | No browser UI; API/webhook only |
| Intent interpretation | ✓ | ✓ | ✓ | SSE + DB; text-only on `/run` |
| Atlas search | ✓ | ✓ | ✓ | 13 candidates in RC-0001 |
| Candidate verification | ✓ | ✓ | ✓ | 3 verified in DB |
| Human tap | ✓ | ✓ | ✓ | `human_taps=1` in receipt |
| Order creation | ✓ | ✓ | partial | Success in demo run 2; 318 failures common |
| Payment | ✓ | ✓ | partial | Paid on 3rd attempt in best run |
| Failover | ✓ | ✓ | partial | 318→318→paid observed once; all-318 fail also observed |
| Caretaker spoken plan | ✓ | partial | — | I1 proof passed; not on failed RC-0001 |
| PDF | ✓ | partial | — | Generated in I1 proof; stale file on disk for RC-0001 |
| Telegram message | ✓ | — | — | No credentials; `telegram_sent=false` |
| Counterfactual cost delta | ✓ | ✓ | ✓ | In receipt JSON |
| Counterfactual hours delta | ✓ | ✓ | ✓ | In receipt JSON |
| Receipt event IDs | ✓ | ✓ | ✓ | 43 IDs stored |
| Live cassette recording | ✓ | ✓ | — | 107 cassette files |
| Replay | ✓ | ✓ | partial | Replay mode runs; parity mismatch after failover |
| Live/replay parity | ✓ | — | — | **`PARITY OK` never observed end-to-end** |
| Daytona executor | ✓ | — | — | Demo uses `EXECUTOR=local` |
| Gemini/model warm-up | ✓ | ✓ | — | ~2s; real ping/pong |
| Voice/photo input | ✓ | — | — | Interpreter supports; `/run` is text-only |
| Decline/3DS chaos profiles | ✓ | — | — | Atlas sandbox ignores name triggers (Task 25 NOT VERIFIED) |

### Explicit reports

| Check | Result |
|---|---|
| `human_taps == 1` | **Yes** on receipts (including failed RC-0001) |
| Receipt has amount paid + both counterfactual deltas | **Yes** (failed run: ~0 SGD; success run: 75.81 USD, -29.29 SGD, 3.25h) |
| All attempts including declines appear | **Yes** for 318 attempts; 604/616 not observed live |
| Literal `PARITY OK` observed | **No** (only synthetic comparator test) |
| Telegram actually delivered | **No** — implemented only |
| Browser path text vs voice/photo | **Text only** on demo path |
| Happy path <90s × 3 consecutive | **No** — best 92s/100s; one run timed out |

---

# Part 6: Presentation materials

## 1. Browser demo script (read aloud)

> “I’m opening the Rebound operator console — three panels: the case, the agent trace, and what the traveller sees on her phone.
>
> Behind the scenes, our demo script tells Atlas that Mrs. Tan’s flight was disrupted and starts the recovery engine. You’ll see the Interpreter figure out her constraints, the Strategist search real flights, and the Executor verify prices — all live in the center panel.
>
> Rebound finds several options and ranks them. I don’t re-book blindly: I give **one tap** — Confirm — and Guardian enforces the spend cap.
>
> Rebound then orders and pays through Atlas. If something fails, it automatically tries the next verified option — still with that single human tap.
>
> When it succeeds, the traveller gets a spoken plan and large-print PDF with **real ticket facts from Atlas**, not invented by AI. Scroll down and you’ll see the Recovery Receipt: elapsed time, exactly one human tap, what we paid, how we beat the do-it-yourself option, and every booking attempt including failures.
>
> That receipt is signed with event IDs so you can replay the whole case offline.”

## 2–5. One-sentence definitions

- **Atlas:** The external travel-booking API that supplies real flight search, verification, orders, payments, and ticket status — Rebound orchestrates around it but does not own inventory.
- **Gemini/model:** A language layer that interprets traveller intent and writes explanatory copy; it never invents flight numbers, prices, or receipt figures.
- **Daytona:** An optional isolated sandbox for running Strategist scoring code; the demo uses local execution instead.
- **Cassette:** A saved JSON recording of one Atlas request/response pair, used in replay mode instead of calling the live API.

## 6. Troubleshooting checklist

1. Is the server up? `curl localhost:8000/healthz`
2. Is `OPERATOR_TOKEN` set? Demo uses `rebound-demo-operator`
3. Are Atlas sandbox credentials in `.env`?
4. Is OpenRouter/Gemini reachable? Run warm_model or check `/run` errors
5. Did `order.do` return **318**? Re-run `demo.sh` (resets DB) or wait — failover may still fail all three
6. For replay parity, do cassettes exist for **every** verify/order/pay in the failover path?
7. Telegram/PDF missing? Expected without Telegram env; Caretaker deliver skipped on `failed`
8. Browser shows 401? Add Bearer token to requests

## 7. Three biggest pre-demo risks

1. **Atlas 318** on repeat `order.do` — can blow the 90-second target or fail entirely.
2. **Live/replay parity** — cassettes drift after failover; no demonstrated `PARITY OK`.
3. **Operator auth** — browser needs bearer token; easy to forget during live presentation.

## 8. Final statement

# **`TASK 26 NOT VERIFIED`**

Implementation is in place (receipt, counterfactual, caretaker, `demo.sh`, cassette recording), and several sub-proofs passed (I1 interpolation, counterfactual determinism, I8 rebuild, synthetic I9 comparator). The full verification bar — happy path under 90 seconds three times and end-to-end live/replay **`PARITY OK`** — was **not** achieved in observed runs.

---

# Part 7: A6 — pre-announced full-replay fallback demo (16 Aug 2026)

## What `REBOUND_MODE=replay` is

`ops/demo.sh` has an existing replay transport (`CassettePlayer` in `packages/atlas/`): instead of calling the Atlas sandbox, the server serves `search/verify/order/pay/queryOrderDetails` from saved **cassette** JSON files. A full replay happy-path run is scripted inside `parity_check()` (reset → `REBOUND_MODE=replay` → reseed → run happy path).

**Sanctioned fallback demo (A6):** when the live Atlas identity is exhausted or a live run is unsafe, the presenter-facing fallback is to **pre-announce and run the whole demo in replay mode**:

```bash
REBOUND_MODE=replay bash ops/demo.sh
```

Two important precisions:

1. **The main flow hardcodes `REBOUND_MODE=live`** in the `start_live` stage, so the env var must be preset on the CLI as above — it is *not* toggled mid-script.
2. **Replay demonstrates ONLY the default (non-unique) identity.** It does **not** validate the `DEMO_UNIQUE_PAX` path (parity is skipped under that config by design, and unique-pax cassette keys are per-case_ref synthetic).

## Presenter narration — announce BEFORE starting, never during

Read this to the judges **before** the demo starts (it is the only sanctioned way to use replay):

> “This run is a **replay demo**. Instead of calling the live Atlas sandbox again — which is exhausted after today's rehearsals and would just refuse to re-book the same passenger — the engine will replay a **pre-recorded real session** from saved cassettes. Every step you see — search, verify, the confirmation tap, order, payment, ticket polling — is the same agent code path, but served from the recorded transcript of an actual Atlas booking made earlier. This is the offline fallback we use when the sandbox is spent; it is announced up front, never switched mid-case.”

## Why the mid-case automatic live→replay fallback was NOT built

The earlier option — auto-fall back from live to replay *during* a case when Atlas fails — was **considered and rejected**:

1. **Hybrid transport inside `packages/atlas/`** — it would require a live/replay hybrid transport layer inside the Atlas client, mixing real and replayed HTTP for the same session, which is exactly the seam that causes cassette-key drift.
2. **Session IDs may not match cassette keys** — a mid-case switch hands the replayed transport a session ID issued by the live server; cassette keys are derived from session-scoped payloads, so replay would miss (this is the same structural failure documented in §14).
3. **Executed at the moment of peak stress** — the fallback would trigger precisely when the demo is failing, making the switch look like a cheat and hiding the live failure from the judge.

## Known-good cassette backup

Backed up before the 16 Aug runs (A6 verification):

```bash
cp -r fixtures/cassettes /tmp/cassettes-known-good && ls /tmp/cassettes-known-good | wc -l   # 218
```

- **Coverage:** the TAN default identity (Tan Mei Lin) happy path — the seeded order `TESTA20260815020605810` (CGK→SUB); `order.do` cassette key `3347c510…` returns `status=0`.
- **Identity precision:** this backup demonstrates only the **default non-unique** identity. It does **not** validate the `DEMO_UNIQUE_PAX` path.
- **Limitation:** even this known-good set cannot complete a replay booking today — see the cassette-key defect below.

## Cassette-key defect (why replay cannot complete a booking)

I4 redaction (`_SENSITIVE_KEYS`: `cardnum`, `birthday`, `dob`, `passport_number`, `date_of_birth` → `[REDACTED]`) is applied to stored responses. Replay reseeds from the replayed `queryOrderDetails.do`, so the derived passenger carries redacted identity, and the `order.do` cassette key (which includes birthday + cardNum) differs from the live-recorded key. Result: replay `verify.do` matches (routingIdentifier-only key), but `order.do` always raises `cassette_miss` → **PARITY OK is structurally impossible in default mode** until the order.do key uses a stable passenger identifier. Full evidence: `docs/DUPLICATE_BOOKING_TASKS.md` §14.

## A6 verification outcome (16 Aug)

| Check | Result |
|---|---|
| Backup captured (`/tmp/cassettes-known-good`, 218 files) | ✅ |
| Literal `REBOUND_MODE=replay bash ops/demo.sh` — TAN identity | ❌ live phase: 3 attempts, all 318 (Tan exhausted: duplicates of seed + prior bookings) → replay never ran |
| Replay run via BIZ order (`DEMO_TAN_ORDER=TESTA20260815002321968`) | ❌ live RECOVERED ($42.73, 117s happy path) but replay phase failed — `cassette_miss` ×2 at `order.do` → case failed |
| Replay reaching `recovered` with `human_taps=1` + complete receipt | ❌ never observed |

# **`TASK A6 NOT VERIFIED`**

Documentation deliverables are complete (this Part 7 + `ops/demo.sh` header); the verification bar fails on the structural cassette-key defect above — a real defect, not a documentation gap.

## Task 26 re-verify (Part 2, 16 Aug, with A1–A4 landed)

Re-run of the original Task 26 protocol on the BIZ order (default non-unique mode, `DEMO_UNIQUE_PAX=0`):

| Run | Happy path | <90s? | human_taps | amount_paid | Deltas (cost SGD / hours) | Attempts used | Parity |
|---|---|---|---|---|---|---|---|
| 1 (04:55Z) | 117s (run 23s + confirm 93s + receipt 1s; case elapsed 108s) | ❌ | 1 | 42.73 USD | +38.27 / −24.0h | 1 (clean pay) | replay `cassette_miss` ×2 |
| 2 (05:03Z) | 74s (run 24s + confirm 48s + receipt 2s; case elapsed 64s) | ✅ | 1 | 57.19 USD | +18.75 / −17.25h | 2 (318 → paid) | replay `cassette_miss` ×2 |
| 3 (05:06Z) | failed (run 30s + confirm; case failed, receipt null) | ❌ | — | — | — | 2 (318 on seed, then `order.do` OK → `AssertionError` in pay path, exact line unconfirmed) | not reached |

- **Happy-path reliability: not verified** — 1 of 3 runs under 90s; run 3 failed with a new pay-path `AssertionError` defect (fired 1.25 ms after the `ordered` event; traceback not retained — see §15).
- **Parity: not verified, defect found** — replay cannot complete `order.do` in default mode (cassette-key defect above).
- Attempts never exceeded 2 despite `DEMO_MAX_ATTEMPTS=5`: the I2 verify-top-3 gate bounds attempts to the verified set (≤3).
