# Qoder / implementing-agent decision log (append-only)

## Task 4 — Atlas `search.do` field mapping

Source read (2026-08-14):
[https://resources.atriptech.com/api-document/api-reference/booking-apis/search.md](https://resources.atriptech.com/api-document/api-reference/booking-apis/search.md)
and verify.md for the `sessionId` lifecycle.

### Request body fields used (`POST /search.do`)

From the Search OpenAPI schema (required + currency):

| Our `SearchRequest` | Atlas wire field | Notes |
|---|---|---|
| (fixed) | `tripType` | `"1"` = oneway |
| `adults` | `adultNum` | |
| `children` | `childNum` | |
| `infants` | `infantNum` | |
| `origin` | `fromCity` | IATA, uppercased |
| `destination` | `toCity` | IATA, uppercased |
| `departure_date` | `fromDate` | `YYYYMMDD` |
| `currency` | `currency` | always sent as `"USD"` in sandbox [E] |

Not sent: `retDate`, `airlines`, `fromFlightNumbers`, `requestSource`, `cid` (auth is header-based).

### Response fields used

| Our model field | Atlas wire field | Notes |
|---|---|---|
| `SearchResult.raw` | full body | `status`, `msg`, `routings`, … |
| `SearchResult.session_id` | `sessionId` | **Not present on search.do.** verify.md: verify returns `sessionId` for `order.do`. We preserve when present; otherwise `""` (never invent). |
| `SearchResult.offers[]` | `routings[]` | empty/`null` → `AtlasNoResultsError` |
| `Offer.routing_identifier` | `routingIdentifier` | required on Routing; echoed to verify.do |
| `Offer.offer_id` | `fid` if non-empty, else `routingIdentifier` | `fid` appears on live sandbox routings; omitted from published Routing OpenAPI schema |
| `Offer.price` | `adultPrice` + `adultTax` + `transactionFeePerPax` | documented single-adult purchase total |
| `Offer.currency` | `currency` | settlement currency on the routing |
| `Offer.stop_count` | derived from `fromSegments` / `retSegments` lengths | `max(0, len-1)` per direction; not a separate Atlas field |
| `Offer.min_transfer_minutes` | — | no Atlas field; left `None` |
| `Offer.baggage_included` | `rule.hasBaggage` | `1` → true, `0` → false |
| `Offer.raw` | the routing object | verbatim |
| `Segment.carrier` | `fromSegments[].carrier` / `retSegments[].carrier` | |
| `Segment.flight_number` | `flightNumber` | e.g. `QG716` |
| `Segment.origin` | `depAirport` | |
| `Segment.destination` | `arrAirport` | |
| `Segment.departure_at` | `depTime` | `YYYYMMDDHHMM` local wall clock; stored with UTC tzinfo, unshifted |
| `Segment.arrival_at` | `arrTime` | same |
| `Segment.cabin` | `cabin` | RBD; empty → `None` |

### Deliberate non-inventions

- Do not map invented names like `origin`/`destination` onto the wire request.
- Do not fabricate `sessionId` from `requestId` / `clientRequestId` (both null on our sandbox cassette).
- `OfferId` (Fulfilment / `getOfferPrice.do`) is out of scope for Task 4.

## Task 13 — Model router timeout default (Gemini 3.6 thinking)

**Chosen operational default for callers:** `timeout_seconds=60.0` for text/image
structured calls; `timeout_seconds=90.0` when audio is attached.

**Frozen INTERFACES.md §4** still defines `ModelRequest.timeout_seconds: float = 20.0`.
We implement that field default as written. Gate evidence (pre-Task-13 live calls
against `gemini-3.6-flash`) showed:

| Call | Wall latency | Thoughts tokens | Total tokens |
|---|---|---|---|
| Text | ~1.5s | 84 | 92 |
| Image | ~4.8s | 307 | 1418 |
| Audio | ~22.4s | 174 | 210 |

Audio alone already exceeds the frozen 20s default, and thinking tokens add
variable latency on every call. Leaving the ModelRequest default at 20.0 would
falsely kill real agent multimodal calls. Until INTERFACES is amended, agents
and smokes MUST pass an explicit timeout (≥60 text/image, ≥90 audio). The
Task 13 smoke happy-path uses `60.0`.

Objection also appended to `docs/RISKS.md`.
