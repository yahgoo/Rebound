#!/usr/bin/env bash
# A2c: mint a fresh Atlas sandbox seeded order (rotation pool member).
#
# The sandbox mints TESTA... order numbers itself via order.do, so a new
# seeded identity is created by running the real booking chain
# (search.do -> verify.do -> order.do -> pay.do) with a fresh synthetic
# passenger on a fresh route/date. Recorder is disabled (recorder=None),
# so minting never pollutes the cassette store (I9) and never confuses
# the A3 preflight's past-318 detection.
#
# Usage:
#   ops/mint_order.sh <ORIGIN> <DESTINATION> <YYYY-MM-DD> [LABEL]
#     LABEL defaults to $DEMO_MINT_LABEL or "fresh".
#
# On success prints the new order_no and appends an entry to
# fixtures/seeded_orders.json (the A2c rotation pool). demo.sh selects
# from that pool via DEMO_ORDER / DEMO_ORDER_INDEX / auto (first fresh).
#
# Refuses to run the money path against any non-sandbox Atlas base URL.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Source .env for keys NOT already in the environment (same as demo.sh), so a
# pre-set ATLAS_BASE_URL survives sourcing.
if [[ -f .env ]]; then
  while IFS='=' read -r _env_key _env_value; do
    if [[ -n "$_env_key" && "$_env_key" != \#* && -z "${!_env_key:-}" ]]; then
      export "$_env_key=$_env_value"
    fi
  done < .env
fi

export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

ORIGIN="${1:-}"
DESTINATION="${2:-}"
DATE="${3:-}"
LABEL="${4:-${DEMO_MINT_LABEL:-fresh}}"
if [[ -z "$ORIGIN" || -z "$DESTINATION" || -z "$DATE" ]]; then
  echo "usage: ops/mint_order.sh <ORIGIN> <DESTINATION> <YYYY-MM-DD> [LABEL]" >&2
  echo "  mints a fresh ticketed order in the Atlas sandbox on a fresh" >&2
  echo "  route/date/passenger combination and appends it to" >&2
  echo "  fixtures/seeded_orders.json; prints the new order_no." >&2
  exit 2
fi

if command -v uv >/dev/null 2>&1; then
  PY=(uv run python)
else
  PY=(python3)
fi

"${PY[@]}" - "$ORIGIN" "$DESTINATION" "$DATE" "$LABEL" <<'PY'
"""Mint a fresh seeded order. See ops/mint_order.sh header for rationale."""
import asyncio
import hashlib
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

from apps.api.settings import get_settings
from packages.atlas.client import AtlasClient
from packages.atlas.models import CardDetails, Passenger, SearchRequest
from packages.atlas.transport import LiveTransport

_SANDBOX_HOST_NEEDLE = "sandbox"
_SANDBOX_VISA = "4532015112830366"  # documented Atlas sandbox test card [E]
_ORDER_CONTACT_EMAIL = "rebound.operator@example.com"
_ORDER_CONTACT_PHONE = "0065-91234567"
_REGISTRY = Path("fixtures/seeded_orders.json")

# Mirror routes_cases._unique_surname syllable table (deterministic derivation
# there; a random pick here keeps the mint identity distinct from every
# case_ref-derived recovery identity).
_SYLLABLES = [
    "Tan", "Lee", "Ong", "Lim", "Ng", "Wong", "Chua", "Goh",
    "Teo", "Yap", "Sim", "Loh", "Tay", "Ho", "Au", "Soh",
]


def _fresh_passenger() -> Passenger:
    seed = secrets.token_hex(8)
    digest = hashlib.sha256(f"rebound-mint:{seed}".encode()).hexdigest()
    surname = _SYLLABLES[int(digest, 16) % len(_SYLLABLES)]
    # RR{hex16} avoids Guardian's ICAO-ish passport regex (same trick as
    # DEMO_UNIQUE_PAX identities) so agent events never false-positive PII.
    passport = f"RR{digest[:16]}"
    return Passenger(
        given_name="Test",
        surname=surname,
        date_of_birth=datetime(1990, 1, 15, tzinfo=UTC),
        passport_number=passport,
        nationality="SG",
    )


async def main() -> int:
    origin, destination, date, label = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    settings = get_settings()
    url = str(settings.atlas_base_url or "").strip().lower()
    if _SANDBOX_HOST_NEEDLE not in url:
        print(f"MINT REFUSED base_url={url!r} is not an Atlas sandbox host", file=sys.stderr)
        return 3

    atlas = AtlasClient(
        LiveTransport(
            settings.atlas_base_url,
            settings.atlas_client_id,
            settings.atlas_client_secret,
            recorder=None,  # mint must not pollute the cassette store (I9)
        )
    )
    pax = _fresh_passenger()
    depart = datetime.fromisoformat(f"{date}T00:00:00+00:00")

    result = await atlas.search(
        SearchRequest(
            origin=origin,
            destination=destination,
            departure_date=depart,
            adults=1,
        )
    )
    offers = sorted(result.offers, key=lambda offer: offer.price)
    if not offers:
        print(
            f"MINT FAIL search returned no offers for {origin}-{destination} {date}",
            file=sys.stderr,
        )
        return 4
    offer = offers[0]

    verified = await atlas.verify(routing_identifier=offer.routing_identifier)
    ordered = await atlas.order(
        session_id=verified.session_id,
        offer_id=verified.offer_id,
        passengers=[pax],
        contact_email=_ORDER_CONTACT_EMAIL,
        contact_phone=_ORDER_CONTACT_PHONE,
    )
    card = CardDetails(
        holder_given_name="Test",
        holder_surname="User",
        number=_SANDBOX_VISA,
        expiry_month=12,
        expiry_year=2030,
        cvv="123",
    )
    paid = await atlas.pay(order_no=ordered.order_no, card=card)

    entry = {
        "order_no": ordered.order_no,
        "label": label,
        "route": f"{origin}-{destination}",
        "date": date,
        "passenger": f"{pax.surname}/{pax.given_name}",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "fresh",
        "paid": paid.paid,
        "amount": str(ordered.total_amount),
        "currency": ordered.currency,
    }
    orders: list[dict] = []
    if _REGISTRY.is_file():
        try:
            orders = (json.loads(_REGISTRY.read_text(encoding="utf-8")) or {}).get("orders") or []
        except Exception:
            orders = []
    orders = [o for o in orders if o.get("order_no") != entry["order_no"]]
    orders.append(entry)
    _REGISTRY.write_text(json.dumps({"orders": orders}, indent=2) + "\n", encoding="utf-8")

    print(
        f"MINT OK order_no={ordered.order_no} route={entry['route']} date={date} "
        f"passenger={entry['passenger']} amount={entry['amount']} {entry['currency']} "
        f"paid={paid.paid} status={ordered.status}"
    )
    print(f"  registry={_REGISTRY} status=fresh")
    return 0


raise SystemExit(asyncio.run(main()))
PY
