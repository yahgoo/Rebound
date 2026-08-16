#!/usr/bin/env bash
# Reset, reseed, warm, rehearse the happy path, then live-vs-replay parity (I9).
# warm_atlas doubles as the A3 pre-flight gate (read-only Atlas probes).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Source .env for keys NOT already in the environment, so a pre-set
# ATLAS_BASE_URL (e.g. the A3 degraded-Atlas check) survives sourcing.
if [[ -f .env ]]; then
  while IFS='=' read -r _env_key _env_value; do
    if [[ -n "$_env_key" && "$_env_key" != \#* && -z "${!_env_key:-}" ]]; then
      export "$_env_key=$_env_value"
    fi
  done < .env
fi

export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
export CHAOS_PROFILE="${CHAOS_PROFILE:-none}"
export EXECUTOR="${EXECUTOR:-local}"
export SURFACE="${SURFACE:-operator}"
export DB_PATH="${DB_PATH:-$ROOT/rebound.db}"
export DEMO_HOST="${DEMO_HOST:-127.0.0.1}"
export DEMO_PORT="${DEMO_PORT:-8000}"
export OPERATOR_TOKEN="${OPERATOR_TOKEN:-rebound-demo-operator}"
export DEMO_SKIP_TTS="${DEMO_SKIP_TTS:-1}"
export DEMO_UNIQUE_PAX="${DEMO_UNIQUE_PAX:-0}"
BASE="http://${DEMO_HOST}:${DEMO_PORT}"

# Pre-created Atlas sandbox orders (Mrs. Tan is the rehearsed path).
TAN_ORDER="${DEMO_TAN_ORDER:-TESTA20260815020605810}"
BIZ_ORDER="${DEMO_BIZ_ORDER:-TESTA20260815002321968}"
FAMILY_ORDER="${DEMO_FAMILY_ORDER:-TESTA20260815002134580}"

if command -v uv >/dev/null 2>&1; then
  PY=(uv run python)
  UVICORN=(uv run uvicorn)
else
  PY=(python3)
  UVICORN=(python3 -m uvicorn)
fi

STAGE_NAME=""
STAGE_START=0
declare -a STAGE_LINES=()

stage_begin() {
  STAGE_NAME="$1"
  STAGE_START="$(date +%s)"
  printf 'STAGE %-18s START %s\n' "$STAGE_NAME" "$(date -u +%H:%M:%S)Z"
}

stage_end() {
  local now elapsed
  now="$(date +%s)"
  elapsed="$((now - STAGE_START))"
  printf 'STAGE %-18s %ss\n' "$STAGE_NAME" "$elapsed"
  STAGE_LINES+=("${STAGE_NAME} ${elapsed}s")
}

json_get() {
  local url="$1"
  curl -sS "$url" -H "Accept: application/json" \
    -H "Authorization: Bearer ${OPERATOR_TOKEN}"
}

json_post() {
  local url="$1"
  local body="${2:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X POST "$url" \
      -H "Accept: application/json" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer ${OPERATOR_TOKEN}" \
      --max-time 180 \
      -d "$body"
  else
    curl -sS -X POST "$url" \
      -H "Accept: application/json" \
      -H "Authorization: Bearer ${OPERATOR_TOKEN}" \
      --max-time 180
  fi
}

stop_server() {
  local pids
  pids="$(lsof -tiTCP:"$DEMO_PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    kill $pids 2>/dev/null || true
    sleep 0.4
    pids="$(lsof -tiTCP:"$DEMO_PORT" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
      kill -9 $pids 2>/dev/null || true
    fi
  fi
}

start_server() {
  local mode="$1"
  export REBOUND_MODE="$mode"
  export CHAOS_PROFILE=none
  "${UVICORN[@]}" apps.api.main:app --host "$DEMO_HOST" --port "$DEMO_PORT" \
    >/tmp/rebound-demo-uvicorn.log 2>&1 &
  local i
  for i in $(seq 1 40); do
    if curl -sf "$BASE/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  echo "server failed to start; log:" >&2
  tail -n 40 /tmp/rebound-demo-uvicorn.log >&2 || true
  return 1
}

reset_sqlite() {
  stop_server
  rm -f "$DB_PATH" "${DB_PATH}-wal" "${DB_PATH}-shm"
}

reseed() {
  "${PY[@]}" - <<'PY'
import asyncio, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

from apps.api.settings import ReboundMode, get_settings
from packages.atlas.cassette import CassettePlayer, CassetteRecorder
from packages.atlas.client import AtlasClient
from packages.atlas.transport import LiveTransport, ReplayTransport

settings = get_settings()
cassette_dir = Path("fixtures/cassettes")
if settings.rebound_mode is ReboundMode.REPLAY:
    atlas = AtlasClient(ReplayTransport(CassettePlayer(cassette_dir)))
else:
    atlas = AtlasClient(
        LiveTransport(
            settings.atlas_base_url,
            settings.atlas_client_id,
            settings.atlas_client_secret,
            recorder=CassetteRecorder(cassette_dir),
        )
    )

personas = [
    ("tan", os.environ["TAN_ORDER"], "Mrs. Tan / Mandarin / mobility"),
    ("biz", os.environ["BIZ_ORDER"], "Business traveller / English"),
    ("family", os.environ["FAMILY_ORDER"], "Family of four"),
]
fixture_dir = Path("fixtures/personas")

async def main() -> int:
    print("RESEED personas + Atlas sandbox orders")
    for slug, order_no, label in personas:
        voice = fixture_dir / f"{slug}_voice.m4a"
        photo = next(fixture_dir.glob(f"{slug}_board.*"), None)
        print(f"  persona={slug} order={order_no} {label}")
        print(f"    voice={'yes' if voice.is_file() else 'missing'} photo={'yes' if photo else 'missing'}")
        details = await atlas.query_order_details(order_no=order_no)
        print(
            f"    atlas_status={details.status} "
            f"segments={len(details.segments)} "
            f"amount={details.total_amount} {details.currency}"
        )
    print("RESEED OK")
    return 0

raise SystemExit(asyncio.run(main()))
PY
}

warm_atlas() {
  "${PY[@]}" - <<'PY'
"""A3 pre-flight gate - read-only Atlas probes, no cassette writes.

Duplicates the executor's identity derivation (routes_cases._passengers
with the same case_ref and DEMO_UNIQUE_PAX semantics) so the order.do
cassette key computed here is byte-identical to the executor's, and an
existing cassette for it is identity-level duplicate evidence.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

from sqlmodel import select

from apps.api.routes_cases import _passengers
from apps.api.settings import get_settings
from packages.agents.watcher import _passengers_json
from packages.atlas.cassette import CassetteRecorder
from packages.atlas.client import (
    _passenger_to_wire,
    _passenger_wire_name,
    AtlasClient,
)
from packages.atlas.errors import AtlasNoResultsError
from packages.atlas.models import SearchRequest
from packages.atlas.transport import LiveTransport
from packages.domain.db import session_factory
from packages.domain.models import RecoveryCase

_ORDER_CONTACT_EMAIL = "rebound.operator@example.com"
_ORDER_CONTACT_PHONE = "0065-91234567"

settings = get_settings()
cassette_dir = Path("fixtures/cassettes")
atlas = AtlasClient(
    LiveTransport(
        settings.atlas_base_url,
        settings.atlas_client_id,
        settings.atlas_client_secret,
        recorder=None,  # probe must not pollute the cassette store (I9)
    )
)


def next_case_ref() -> str:
    """Mirror watcher._next_case_ref; a fresh/absent DB yields RC-0001."""
    max_n = 0
    refs: list[str] = []
    try:
        factory = session_factory(os.environ["DB_PATH"])
        with factory() as session:
            refs = [str(r) for r in session.exec(select(RecoveryCase.case_ref)).all()]
    except Exception:
        refs = []
    for ref in refs:
        match = re.match(r"^RC-(\d+)$", ref)
        if match:
            max_n = max(max_n, int(match.group(1)))
    return f"RC-{max_n + 1:04d}"


def flight_key(segment) -> tuple[str, str, str]:
    return (
        segment.carrier,
        segment.flight_number,
        segment.departure_at.strftime("%Y%m%d"),
    )


async def main() -> int:
    try:
        details = await atlas.query_order_details(order_no=os.environ["TAN_ORDER"])
        print(
            f"WARM atlas order={details.order_no} status={details.status} "
            f"segments={len(details.segments)}"
        )
    except Exception as exc:
        print(f"PREFLIGHT DEGRADED reason=atlas_unreachable detail={type(exc).__name__}")
        return 1

    if not details.segments:
        print("PREFLIGHT DEGRADED reason=order_segments_missing")
        return 1
    booked = {flight_key(seg) for seg in details.segments}
    first = details.segments[0]

    case_ref = next_case_ref()
    passengers = _passengers(_passengers_json(details), case_ref=case_ref)
    if not passengers:
        print("PREFLIGHT DEGRADED reason=passenger_identity_missing")
        return 1
    pax = passengers[0]
    print(
        f"PREFLIGHT identity case_ref={case_ref} "
        f"unique_pax={os.environ.get('DEMO_UNIQUE_PAX', '0')} "
        f"passenger={pax.surname}/{pax.given_name}"
    )

    order_payload = {
        "sessionId": "a3-preflight-probe",  # volatile; stripped from the key
        "passengers": [_passenger_to_wire(pax)],
        "contact": {
            "name": _passenger_wire_name(pax),
            "email": _ORDER_CONTACT_EMAIL,
            "mobile": _ORDER_CONTACT_PHONE,
        },
    }
    order_key = CassetteRecorder.key_for("order.do", order_payload)
    cassette = cassette_dir / f"{order_key}.json"
    past_318 = False
    held = set(booked)
    if cassette.exists():
        try:
            stored = json.loads(cassette.read_text())
            response = stored.get("response", {}) or {}
            past_318 = str(response.get("status")) == "318"
        except Exception:
            response = {}
        # Identity-level held flights: duplicateOrders from a past 318 and the
        # orderNo from a past success both point at flights this identity has
        # already booked - a booking there would 318 again.
        for dup_order in response.get("duplicateOrders") or []:
            try:
                dup_details = await atlas.query_order_details(order_no=dup_order)
                held.update(flight_key(seg) for seg in dup_details.segments)
            except Exception:
                pass
        order_no = response.get("orderNo")
        if order_no:
            try:
                order_details = await atlas.query_order_details(order_no=order_no)
                held.update(flight_key(seg) for seg in order_details.segments)
            except Exception:
                pass
    print(
        f"PREFLIGHT order_do_key={order_key[:16]} "
        f"cassette={'present' if cassette.exists() else 'absent'} "
        f"past_318={past_318} held_flights={len(held)}"
    )

    try:
        search = await atlas.search(
            SearchRequest(
                origin=first.origin,
                destination=first.destination,
                departure_date=first.departure_at,
                adults=1,
            )
        )
    except AtlasNoResultsError as exc:
        print(f"PREFLIGHT DEGRADED reason=search_empty detail={exc.code}")
        return 1
    except Exception as exc:
        print(f"PREFLIGHT DEGRADED reason=atlas_unreachable detail={type(exc).__name__}")
        return 1

    top = sorted(search.offers, key=lambda offer: offer.price)[:3]
    risky = [
        offer
        for offer in top
        if any(flight_key(seg) in held for seg in offer.segments)
    ]
    n_risk = len(risky)
    print(
        f"PREFLIGHT probe search offers={len(search.offers)} "
        f"route={first.origin}-{first.destination} "
        f"date={first.departure_at.strftime('%Y%m%d')} "
        f"top3_duplicate_risk={n_risk}"
    )

    reasons: list[str] = []
    if past_318:
        reasons.append(f"cassette_318_evidence:{order_key[:12]}")
    if cassette.exists():
        if n_risk >= 3:
            reasons.append("identity_duplicate_exhaustion:all_top3_resell_disrupted_flight")
        elif n_risk >= 2:
            reasons.append(f"duplicate_risk:{n_risk}")

    if reasons:
        print(f"PREFLIGHT DEGRADED reason={','.join(reasons)}")
        return 1
    print("PREFLIGHT OK")
    return 0


raise SystemExit(asyncio.run(main()))
PY
}

warm_model() {
  "${PY[@]}" - <<'PY'
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))
from packages.router import get_router
from packages.router.base import ModelRequest

async def main() -> None:
    router = get_router()
    response = await router.generate(
        ModelRequest(
            system="You reply with the single word pong.",
            prompt="ping",
            temperature=0.0,
            max_output_tokens=8,
            timeout_seconds=30.0,
        )
    )
    print(f"WARM model backend={response.backend.value} latency_ms={response.latency_ms}")

asyncio.run(main())
PY
}

wait_case_field() {
  local case_ref="$1"
  local field="$2"
  local want="$3"
  local tries="${4:-90}"
  local i raw
  for i in $(seq 1 "$tries"); do
    raw="$(json_get "$BASE/cases/$case_ref")"
    if "${PY[@]}" - "$raw" "$field" "$want" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
field, want = sys.argv[2], sys.argv[3]
if field == "status":
    value = (payload.get("case") or {}).get("status")
    sys.exit(0 if str(value) == want else 1)
if field == "receipt":
    sys.exit(0 if payload.get("receipt") else 1)
sys.exit(1)
PY
    then
      return 0
    fi
    sleep 0.25
  done
  echo "timeout waiting for $case_ref $field=$want" >&2
  echo "$raw" >&2
  return 1
}

run_happy_path() {
  local case_ref=""
  stage_begin trigger
  local trigger_body
  trigger_body="$(printf '{"atlas_order_no":"%s"}' "$TAN_ORDER")"
  local trigger_resp
  trigger_resp="$(json_post "$BASE/cases/trigger" "$trigger_body")"
  echo "$trigger_resp"
  case_ref="$("${PY[@]}" -c 'import json,sys; print(json.loads(sys.stdin.read())["case_ref"])' <<<"$trigger_resp")"
  echo "CASE_REF $case_ref"
  stage_end

  stage_begin run
  local run_resp
  run_resp="$(json_post "$BASE/cases/${case_ref}/run" '{}')"
  echo "$run_resp"
  stage_end

  if ! "${PY[@]}" -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1]).get("status")=="awaiting_confirmation" else 1)' "$run_resp"; then
    echo "RUN did not reach awaiting_confirmation" >&2
    return 1
  fi

  stage_begin confirm
  local nonce cand
  nonce="$("${PY[@]}" -c 'import json,sys; print(json.loads(sys.stdin.read())["confirmation"]["nonce"])' <<<"$run_resp")"
  cand="$("${PY[@]}" -c 'import json,sys; print(json.loads(sys.stdin.read())["confirmation"]["recommended_candidate_id"])' <<<"$run_resp")"
  local confirm_body
  confirm_body="$(printf '{"candidate_id":%s,"nonce":"%s"}' "$cand" "$nonce")"
  json_post "$BASE/cases/${case_ref}/confirm" "$confirm_body"
  echo
  wait_case_field "$case_ref" status recovered 360
  wait_case_field "$case_ref" receipt present 160
  stage_end

  stage_begin receipt
  echo "RECEIPT"
  "${PY[@]}" -m packages.agents.caretaker receipt "$case_ref"
  stage_end

  printf '%s\n' "$case_ref" > /tmp/rebound-demo-case-ref.txt
}

dump_counts() {
  "${PY[@]}" - <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))
from sqlmodel import select
from packages.domain.db import session_factory
from packages.domain.models import AgentEvent, RecoveryCase, RecoveryReceipt

factory = session_factory(os.environ["DB_PATH"])
with factory() as session:
    cases = len(list(session.exec(select(RecoveryCase)).all()))
    events = len(list(session.exec(select(AgentEvent)).all()))
    receipts = len(list(session.exec(select(RecoveryReceipt)).all()))
print(f"DEMO_COUNTS cases={cases} events={events} receipts={receipts}")
PY
}

parity_check() {
  local live_ref replay_ref
  live_ref="$(cat /tmp/rebound-demo-case-ref.txt)"
  "${PY[@]}" -m packages.agents.caretaker parity-dump "$live_ref" > /tmp/rebound-live-steps.txt

  echo "PARITY restarting in replay mode"
  reset_sqlite
  export REBOUND_MODE=replay
  start_server replay
  reseed
  run_happy_path
  replay_ref="$(cat /tmp/rebound-demo-case-ref.txt)"
  "${PY[@]}" -m packages.agents.caretaker parity-dump "$replay_ref" > /tmp/rebound-replay-steps.txt
  "${PY[@]}" -m packages.agents.caretaker parity-compare \
    /tmp/rebound-live-steps.txt /tmp/rebound-replay-steps.txt
}

export TAN_ORDER BIZ_ORDER FAMILY_ORDER

# A3: standalone pre-flight gate - no server, no DB reset, read-only Atlas.
if [[ "${PREFLIGHT_ONLY:-0}" == "1" ]]; then
  warm_atlas
  exit $?
fi

COLD_START="$(date +%s)"
stage_begin reset
reset_sqlite
stage_end

stage_begin start_live
export REBOUND_MODE=live
start_server live
curl -sS "$BASE/healthz"; echo
json_post "$BASE/chaos" '{"profile":"none"}'; echo
stage_end

# A3 pre-flight gate runs before reseed so a dead Atlas still reports
# PREFLIGHT DEGRADED (reseed would abort the script first otherwise).
stage_begin warm_atlas
if warm_atlas; then
  PREFLIGHT_RC=0
else
  PREFLIGHT_RC=$?
fi
stage_end
if [[ "${PREFLIGHT_STRICT:-0}" == "1" && "$PREFLIGHT_RC" != "0" ]]; then
  echo "PREFLIGHT STRICT abort rc=$PREFLIGHT_RC" >&2
  exit 1
fi

stage_begin reseed
reseed
stage_end

stage_begin warm_model
warm_model
stage_end

HAPPY_START="$(date +%s)"
run_happy_path
HAPPY_END="$(date +%s)"
dump_counts

if [[ "${SKIP_PARITY:-0}" != "1" && "${DEMO_UNIQUE_PAX:-0}" != "1" ]]; then
  parity_check
fi

stop_server
COLD_END="$(date +%s)"

echo
echo "TIMING SUMMARY"
for line in "${STAGE_LINES[@]}"; do
  echo "  $line"
done
echo "  happy_path $((HAPPY_END - HAPPY_START))s"
echo "  cold_start $((COLD_END - COLD_START))s"
