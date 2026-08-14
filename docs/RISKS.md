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
