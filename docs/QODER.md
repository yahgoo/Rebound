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
