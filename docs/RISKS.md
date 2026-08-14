# Rebound — RISKS (append-only)

## Task 4 — `sessionId` missing from live `search.do`

INTERFACES.md §1.1 assumes search.do returns a sessionId, but the live Atlas sandbox response does not appear to contain one, and Task 5's verify() cannot echo a session id that was never issued by search.

Evidence (live cassette `fixtures/cassettes/5a21d03b91c79c08eac4f9d5d449c0ffca30375c0759ca99b79af2d3f5e1f8b4.json`, path `search.do`):

- Top-level keys only: `status`, `msg`, `requestId`, `clientRequestId`, `routings`
- `requestId` = `null`, `clientRequestId` = `null`
- No `sessionId` / `session_id` / `searchId` anywhere in the body
- Published search.md OpenAPI response schema lists `status`, `msg`, `routings` — zero mentions of `sessionId`
- Published verify.md: verify **request** requires `routingIdentifier` from search; verify **response** returns `sessionId` for `order.do`
