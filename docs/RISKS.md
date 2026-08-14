# Rebound — RISKS (append-only)

## Task 4 — `sessionId` missing from live `search.do`

INTERFACES.md §1.1 assumes search.do returns a sessionId, but the live Atlas sandbox response does not appear to contain one, and Task 5's verify() cannot echo a session id that was never issued by search.

Evidence (live cassette `fixtures/cassettes/5a21d03b91c79c08eac4f9d5d449c0ffca30375c0759ca99b79af2d3f5e1f8b4.json`, path `search.do`):

- Top-level keys only: `status`, `msg`, `requestId`, `clientRequestId`, `routings`
- `requestId` = `null`, `clientRequestId` = `null`
- No `sessionId` / `session_id` / `searchId` anywhere in the body
- Published search.md OpenAPI response schema lists `status`, `msg`, `routings` — zero mentions of `sessionId`
- Published verify.md: verify **request** requires `routingIdentifier` from search; verify **response** returns `sessionId` for `order.do`

## Task 6 — Verify-block `CardDetails` example vs frozen INTERFACES

Task 6's Verify block in `docs/TASKS.md` constructed
`CardDetails(holder_family_name=..., number=..., cvv=...)` without
`expiry_month` / `expiry_year`. That does not match the frozen
`docs/INTERFACES.md` §1.1 signature, which requires `holder_surname` plus
required `expiry_month` and `expiry_year`.

This is a docs-only inconsistency, not an API contract issue. Task 6
implementation followed INTERFACES.md; no package code was affected. The
Verify snippet in TASKS.md was corrected to match INTERFACES.md so a later
re-run does not fail the same way.

## Task 7 — Verify-block `poll_order_until` call shape vs frozen INTERFACES

Task 7's Verify block in `docs/TASKS.md` called
`AtlasClient()` with no transport and
`c.poll_order_until('NONEXISTENT-ORDER', ...)` as a sync positional call.

Frozen `docs/INTERFACES.md` §1.2 requires a transport argument and keyword-only
`order_no` on an `async` method. Implementation followed INTERFACES.md.
Sandbox observation: unknown order numbers still return HTTP/Atlas `status=0`
with null `orderNo`/`orderStatus`, so polling them correctly times out with
`AtlasTimeoutError` rather than raising a not-found error.

## Task 9 — Confirmation `expires_at` cites the wrong Atlas clock

Task 9 and frozen `INTERFACES.md` §2 say `ConfirmationGate.open` /
`ConfirmationRequest.expires_at` must align to Atlas's **5-minute ticketing
window** [E].

Task 5 corrected the Atlas session lifecycle:

- `verify.do` issues a **new** `sessionId` valid **~2 hours** for later
  `order.do` (INTERFACES §1.2, TASKS Task 5).
- The **5-minute** figure is `OrderResult.ticketing_deadline` /
  `tktLimitTime` — a **post-order** ticketing constraint, not a pre-order
  confirmation TTL.

Confirmation sits between verify and order, so the Atlas clock that actually
bounds “we can still call `order.do`” is the ~2h verify session, not the
5-minute post-order ticketing window. Task 9 / INTERFACES conflate those
two clocks.

**Implementation choice (Task 9):** keep enforcing `expires_at ≤ now + 5
minutes` as written in frozen INTERFACES / Task 9 (stricter; still within
the ~2h session). Do **not** silently widen the gate to 2h against the
frozen interface. A later INTERFACES amendment may raise the confirmation
TTL up to the verify session lifetime if product wants that headroom.

## Task 13 — `ModelRequest.timeout_seconds` default too low for Gemini 3.6

Frozen `INTERFACES.md` §4 sets `ModelRequest.timeout_seconds: float = 20.0`.

Pre-Task-13 gate against `gemini-3.6-flash` measured audio at ~22.4s wall time
and 84–307 thoughts tokens on every modality. The frozen 20s default will
false-positive `ModelTimeoutError` on real multimodal Interpreter / Caretaker
calls.

**Implementation choice (Task 13):** keep the ModelRequest field default at
`20.0` as written. Document the operational override (`60` text/image, `90`
audio) in `docs/QODER.md`. Do not silently widen the frozen default in
`base.py`. Prefer an INTERFACES amendment later if product wants the safer
default baked in.
