# Rebound — SPEC (frozen product contract)

**Status:** frozen. Re-read before every task. Never modify.
If you believe something here is wrong, write the objection to `docs/RISKS.md` and build to this spec anyway.

Derived from `hackathon-strategy.md`. Evidence tags: **[E]** vendor-documented · **[O]** other reliable source · **[I]** inference · **[?]** unknown.

---

## 1. Product summary

Rebound is an autonomous recovery agent for cancelled and re-timed low-cost-carrier flights. Low-cost carriers carry no interline obligation: when an LCC cancels or re-times, the passenger is not rebooked — they are refunded and abandoned, mid-journey, often in a country whose language they don't speak, while the seller's agent re-shops manually across dozens of carrier quirks. Existing disruption tooling is built for GDS and legacy carriers, so it is structurally blind to exactly the segment where the failure is worst. Rebound watches live orders, detects disruption, accepts whatever the traveller can actually produce under stress — a voice note in their own language, a photo of the departure board — and autonomously assembles, verifies, prices and executes a replacement itinerary across 140+ LCCs through the Atlas API. It asks for exactly one human confirmation before spending money, and returns a spoken plan, a large-print one-page itinerary, and a message to the traveller's family. When something fails mid-execution, it recovers to the next-best option instead of stopping.

**Primary user:** operations and support teams at OTAs, TMCs and travel agencies selling LCC content.
**Secondary, demo-facing user:** the stranded traveller, with the elderly solo traveller as the sharpest case.
**Differentiating insight:** the value is not in finding the flight, it is in surviving the execution.

---

## 2. Non-negotiable invariants

These hold for every task, in every module, forever. A change that violates one is wrong even if it passes its own test.

| # | Invariant |
|---|---|
| **I1** | **Models never author itineraries.** A model may only *select* among offer IDs returned by `search.do`. Any flight number, price, time or carrier that did not come from an Atlas response is invalid and must be dropped, never repaired. |
| **I2** | **`verify.do` must succeed before `order.do`.** No order is created against an unverified offer. A candidate that fails verification is dropped, not fixed. |
| **I3** | **Guardian's spend cap is never model-controlled.** Guardian contains no model call and reads no model output as policy input. The cap comes from `GUARDIAN_MAX_SPEND_SGD` and the `RecoveryIntent.budget_ceiling_sgd`, whichever is lower. |
| **I4** | **Card data never enters a Daytona sandbox or a model prompt.** No PAN, CVV, expiry or cardholder name crosses into Zone B or Zone C. PAN is never persisted anywhere, including logs. |
| **I5** | **Stream steps, not tokens.** The SSE trace emits one discrete, named step event per agent action. Token-by-token model streaming is never surfaced to the UI. |
| **I6** | **Exactly one human confirmation before any spend.** `order.do` and `pay.do` are unreachable without a recorded `ConfirmationDecision`. No auto-approve path, no config flag that disables it. |
| **I7** | **Webhooks are best-effort [E]; polling is the safety net.** Every order whose state matters is also polled via `queryOrderDetails.do`. Order state is never inferred from a webhook alone. |
| **I8** | **The audit log is append-only.** `agent_events` rows are inserted, never updated or deleted. The audit log *is* the Recovery Receipt. |
| **I9** | **`REBOUND_MODE=replay` runs the identical code path** as `live`, differing only in the transport layer. Same agents, same UI, same ranking logic, same event sequence. |
| **I10** | **Every fallback is one environment variable plus a restart.** `REBOUND_MODE`, `EXECUTOR`, `CHAOS_PROFILE`, `SURFACE`. No code edit is ever required to reach a fallback. |

---

## 3. Security zones

Three zones. Anything not listed as permitted to cross a boundary is forbidden to cross it.

### Zone A — Host (FastAPI, SQLite, credentials)

Holds: Atlas master client ID and secret, card data, unredacted passenger PII, the SQLite database, the audit log, Guardian.

### Zone B — Daytona sandbox (model-generated code)

**May enter Zone B:** candidate itineraries as structured data (offer ID, carrier, times, price, stop count, transfer minutes, airport codes), the model-generated scoring script, a scoped single-use token.
**May never enter Zone B:** the Atlas master client secret, any card data, passport numbers, full passenger names, dates of birth, the SQLite file, any long-lived credential.
**May leave Zone B:** a ranked list of offer IDs with numeric scores and score components. Nothing else is trusted — a sandbox result is data, never code or instructions.

### Zone C — Model providers (Gemini / Gemma / Kimi / Qwen)

**May enter Zone C:** text and images that Guardian has redacted; tokenised placeholders in place of PII; Atlas-returned candidate summaries.
**May never enter Zone C:** passport numbers, full passenger names, dates of birth, card data, Atlas credentials, the operator token.
**Re-hydration is local only.** Guardian maps token → real value inside Zone A after the model returns. A model never sees the mapping.

**Prompt-injection stance.** A malicious airline email or a hostile sandbox result may reach a model prompt. This is assumed, not prevented. It is survivable *only* because Guardian is deterministic (I3) and confirmation is mandatory (I6). No model output is ever treated as a policy decision.

---

## 4. Data model

SQLModel over SQLite with WAL. One file, trivially seeded and reset.

### `Order` — a booking as Atlas knows it

| Field | Type | Notes |
|---|---|---|
| `id` | `int` PK | local |
| `atlas_order_no` | `str` unique | Atlas `orderNo` **[E]** |
| `pnr` | `str \| None` | may be absent pre-ticketing |
| `status` | `str` | last authoritative value from `queryOrderDetails.do` |
| `passengers_json` | `str` | unredacted, Zone A only |
| `itinerary_json` | `str` | original segments as booked |
| `total_amount` | `Decimal` | |
| `currency` | `str` | sandbox requires explicit `"USD"` **[E]** |
| `created_at` / `updated_at` | `datetime` | |

### `RecoveryCase` — one disruption being worked

| Field | Type | Notes |
|---|---|---|
| `id` | `int` PK | |
| `case_ref` | `str` unique | human-visible, e.g. `RC-0007` |
| `order_id` | `int` FK → `Order` | |
| `trigger_kind` | `str` | `webhook_schedule_change` \| `webhook_cancellation` \| `manual_trigger` |
| `trigger_fingerprint` | `str` unique | idempotency key; duplicate webhook must not open a second case (I7) |
| `status` | `str` | `open` \| `interpreting` \| `searching` \| `scoring` \| `awaiting_confirmation` \| `executing` \| `recovered` \| `failed` |
| `opened_at` | `datetime` | receipt clock starts here |
| `resolved_at` | `datetime \| None` | |
| `surface` | `str` | `operator` \| `traveller` |

### `RecoveryIntent` — what the traveller actually needs

Produced by Interpreter. One per case. Model-authored *constraints* only, never itineraries (I1).

| Field | Type | Notes |
|---|---|---|
| `id` | `int` PK | |
| `case_id` | `int` FK → `RecoveryCase` | |
| `passenger_count` | `int` | |
| `must_arrive_by` | `datetime \| None` | hard constraint |
| `budget_ceiling_sgd` | `Decimal` | Guardian takes `min(this, GUARDIAN_MAX_SPEND_SGD)` (I3) |
| `origin_candidates` | `list[str]` | IATA codes, incl. nearby-airport substitutions |
| `destination_candidates` | `list[str]` | IATA codes |
| `mobility_notes` | `str \| None` | e.g. walks with a cane → transfer-time and walking-distance penalties |
| `language` | `str` | BCP-47, drives Caretaker output |
| `confidence` | `float` | 0–1; below threshold triggers a clarification request, never a guess |
| `raw_input_kinds` | `list[str]` | `text` \| `voice` \| `photo` |

### `Candidate` — one replacement option

Every field originates from an Atlas response (I1).

| Field | Type | Notes |
|---|---|---|
| `id` | `int` PK | |
| `case_id` | `int` FK → `RecoveryCase` | |
| `offer_id` | `str` | from `search.do`; the only handle a model may select |
| `strategy` | `str` | `same_route_later` \| `nearby_airport` \| `one_stop_reroute` \| `next_morning_hotel` |
| `segments_json` | `str` | verbatim from Atlas |
| `price` / `currency` | `Decimal` / `str` | |
| `arrival_delay_minutes` | `int` | vs. original scheduled arrival |
| `stop_count` | `int` | |
| `min_transfer_minutes` | `int \| None` | |
| `self_transfer_risk` | `float` | 0–1, computed in Zone B |
| `mobility_fit` | `float` | 0–1, computed in Zone B |
| `score` | `float \| None` | from the executor; ranking key |
| `score_components_json` | `str \| None` | must be explainable in the UI |
| `verified` | `bool` | `verify.do` succeeded (I2) |
| `verified_price` | `Decimal \| None` | may differ from `price`; the verified value is authoritative |
| `rejected_reason` | `str \| None` | e.g. `over_cap`, `verify_failed`, `price_moved` |

### `RecoveryReceipt` — the signed, replayable outcome

| Field | Type | Notes |
|---|---|---|
| `id` | `int` PK | |
| `case_id` | `int` FK → `RecoveryCase` | |
| `elapsed_seconds` | `int` | `resolved_at − opened_at` |
| `human_taps` | `int` | must be exactly 1 on the happy path (I6) |
| `attempts_json` | `str` | every execution attempt including declines, in order |
| `final_offer_id` | `str \| None` | |
| `amount_paid` / `currency` | `Decimal` / `str` | |
| `counterfactual_cost_delta_sgd` | `Decimal` | DIY-path cost minus actual |
| `counterfactual_hours_delta` | `float` | DIY-path arrival minus actual |
<!-- AMBIGUOUS: the strategy quotes the counterfactual as "S$412 and 9 hours better
     than doing it alone" (§5.1, §9.1) but never defines the do-it-yourself baseline.
     It must be deterministic and defensible on stage, because a judge will ask.
     Define it once in packages/agents/counterfactual.py before Task 26 and state the
     definition in the receipt UI. Recommended baseline: the cheapest same-day offer
     from the unfiltered search set, plus the next-morning option when no same-day
     offer exists — never a model estimate (I1). -->

| `event_ids_json` | `str` | ordered `AgentEvent` ids — makes the receipt replayable |

### `AgentEvent` — append-only audit (I8)

| Field | Type | Notes |
|---|---|---|
| `id` | `int` PK | monotonic; also the SSE sequence number |
| `case_id` | `int` FK | |
| `at` | `datetime` | |
| `actor` | `str` | `watcher` \| `interpreter` \| `strategist` \| `executor` \| `caretaker` \| `guardian` \| `human` |
| `step` | `str` | stable machine name; this is what SSE streams (I5) |
| `summary` | `str` | one human-readable line |
| `payload_json` | `str` | redacted; never card data (I4) |
| `elapsed_ms` | `int` | since `case.opened_at` |

---

## 5. Environment variables

| Variable | Purpose |
|---|---|
| `ATLAS_BASE_URL` | Atlas API root; `https://sandbox.atriptech.com` for the hackathon **[E]** |
| `ATLAS_CLIENT_ID` | Sent as `x-atlas-client-id` header **[E]** |
| `ATLAS_CLIENT_SECRET` | Sent as `x-atlas-client-secret` header; Zone A only **[E]** |
| `GEMINI_API_KEY` | Gemini backend for the router (multimodal Interpreter, Caretaker) |
| `GEMMA_ENDPOINT` | Self-hosted Gemma URL for sovereign mode; optional |
| `KIMI_API_KEY` | Kimi backend for the router; optional |
| `MODEL_ROUTER_DEFAULT` | Which backend the router picks absent an override: `gemini` \| `gemma` \| `kimi` \| `qwen` |
| `DAYTONA_API_KEY` | Daytona SDK auth; required only when `EXECUTOR=daytona` |
| `DAYTONA_TARGET_SANDBOXES` | Fan-out width for parallel scoring; default `8` |
| `EXECUTOR` | `daytona` \| `local` — swaps the scoring backend (I10) |
| `REBOUND_MODE` | `live` \| `replay` — swaps the Atlas transport for the cassette player (I9, I10) |
| `CHAOS_PROFILE` | `none` \| `decline` \| `timeout` \| `3ds` — injects Atlas's documented failure paths **[E]** |
| `GUARDIAN_MAX_SPEND_SGD` | Hard spend ceiling; never model-writable (I3) |
<!-- AMBIGUOUS: the strategy names the cap in SGD (GUARDIAN_MAX_SPEND_SGD, §7.2) but
     requires Atlas sandbox requests to carry "currency":"USD" explicitly (Appendix A).
     The SGD↔USD conversion source is unspecified. Decide once, before Task 9, and
     record it in docs/QODER.md. Do not let a model supply the rate (I3). Recommended:
     a single hard-coded constant in packages/guardian/policy.py, documented as such. -->

| `TELEGRAM_BOT_TOKEN` | Family-notification bot |
| `TELEGRAM_FAMILY_CHAT_ID` | Demo recipient chat |
| `OPERATOR_TOKEN` | Single bearer token for the operator console; no user accounts |
| `PUBLIC_BASE_URL` | Public origin for Atlas webhook registration and traveller magic links |
| `SURFACE` | `operator` \| `traveller` — chooses the landing surface (I10) |
| `NOSANA_API_KEY` | Optional sovereign-inference GPU path; off the critical path |

---

## 6. Non-goals

Do not build these. Do not add them helpfully. Each was cut deliberately.

1. **Portal automation / Playwright scraping of airline websites.** Legally grey, brittle, unsafe to demo live. Recording only, if at all.
2. **Real payment rails.** Atlas sandbox only. No Stripe, no real card processing.
3. **User accounts, signup, password reset, roles.** One operator bearer token plus a signed magic link. Nothing more.
4. **Rail, bus or any non-air ground leg.** Air only.
5. **More than one fully-supported language pair.** English plus one (Mandarin for the rehearsed demo). No i18n framework.
6. **A React SPA, any build step, any bundler.** Jinja2 + HTMX + Tailwind via CDN.
7. **Any database server.** SQLite with WAL. No Postgres, no Redis, no queue broker.
8. **Nosana on the critical path.** Optional toggle, demonstrated once, never required.
9. **Model fine-tuning, embeddings, vector stores, RAG.** None of it.
10. **Native mobile apps.** The traveller view is a phone-framed web page.
11. **`route/export.do` and `smartSearch.do`.** Production-only and TMC-only respectively **[E]**. Unreachable.
12. **Token-by-token streaming to the UI** (I5).
13. **Tests, under any task in this ledger.** `tests/` is owned by the adversarial reviewer. Smoke scripts that a task's "Done when" names explicitly are not tests.
14. **Multi-tenancy, billing, analytics dashboards, admin CRUD.**

---

## 7. Definition of done — the whole project

The project is done when this exact path runs end to end, from a cold start, three consecutive times, in under 90 seconds per run. This is the §9.1 demo script as a checklist.

**Setup**
- [ ] `ops/demo.sh` resets SQLite, reseeds three personas and three pre-created Atlas sandbox orders on real future-dated routes, and warms Atlas and model connections.
- [ ] `docker compose up -d` brings the stack up behind Caddy with TLS.

**Trigger — 0:35–0:55**
- [ ] An Atlas webhook (or the manual trigger button) reports a schedule change on a real sandbox order, and Watcher opens exactly one `RecoveryCase` (duplicate delivery opens none).
- [ ] Mrs. Tan's 9-second Mandarin voice note and departure-board photo are accepted.
- [ ] Interpreter emits a `RecoveryIntent` with `must_arrive_by`, `budget_ceiling_sgd`, mobility notes and `language`, rendered as structured JSON on screen.

**Agent action — 0:55–1:25**
- [ ] Strategist fans out four named strategies to `search.do`.
- [ ] `DAYTONA_TARGET_SANDBOXES` sandboxes appear in the UI grid and turn amber→green in parallel while scoring runs.
- [ ] The top 3 candidates pass `verify.do` and are shown as cards with visible score components (I2).

**Execution — 1:25–1:45**
- [ ] Guardian's policy pane shows the cap, the redactions performed, and the pending confirmation.
- [ ] One tap confirms. `order.do` then `pay.do` succeed; ticket issued; `queryOrderDetails.do` polling confirms state independently of any webhook (I7).
- [ ] Caretaker delivers a spoken Mandarin plan, a large-print PDF, and a Telegram message to the daughter's chat.

**Deliberate failure — 1:45–2:20**
- [ ] `CHAOS_PROFILE=decline` reruns the same case; Atlas returns `604 Payment declined` via its documented cardholder-first-name `Reject` trigger **[E]**.
- [ ] The agent automatically re-verifies and executes option 2 without human intervention, and still completes.
- [ ] Every attempt, including the decline, appears in the audit log and the receipt.

**Impact — 2:20–2:40**
- [ ] The Recovery Receipt renders elapsed seconds, `human_taps == 1`, amount paid, and both counterfactual deltas.

**Fallbacks — each rehearsed at least once**
- [ ] `REBOUND_MODE=replay` completes the identical path with identical UI (I9).
- [ ] `EXECUTOR=local` produces rankings identical to `daytona`.
- [ ] `SURFACE=traveller` lands on the traveller view.
- [ ] `CHAOS_PROFILE=3ds` exercises the `616` path **[E]**.
- [ ] The entire stack runs on localhost in replay mode with no network.
