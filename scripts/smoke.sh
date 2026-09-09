#!/usr/bin/env bash
# End-to-end smoke test against the running service and the real upstreams.
# Usage: ./scripts/smoke.sh [MC_NUMBER]
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
MC="${1:-1515}"

# shellcheck disable=SC1091
set -a && . "$(dirname "$0")/../.env" && set +a
AUTH="Authorization: Bearer ${API_AUTH_TOKEN}"
JSON="Content-Type: application/json"

say() { printf '\n\033[1m── %s\033[0m\n' "$1"; }
call() { curl -s -m 30 -H "$AUTH" -H "$JSON" "$@"; }

say "health"
call "$BASE/health" | python3 -m json.tool

say "start call"
CALL_ID=$(call -X POST "$BASE/v1/calls/start" -d '{}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["call_id"])')
echo "call_id=$CALL_ID"

say "gate check — load search before verification must be refused"
call -o /dev/null -w 'HTTP %{http_code} (expect 403)\n' -X POST "$BASE/v1/loads/search" \
  -d "{\"call_id\":\"$CALL_ID\",\"origin_state\":\"IL\"}"

say "FMCSA authority check for MC $MC"
call -X POST "$BASE/v1/carriers/verify" \
  -d "{\"call_id\":\"$CALL_ID\",\"mc_number\":\"$MC\"}" | python3 -m json.tool

say "send OTP"
call -X POST "$BASE/v1/identity/otp/send" \
  -d "{\"call_id\":\"$CALL_ID\",\"destination\":\"dispatch@example.com\"}" | python3 -m json.tool

say "OTP bypass attempt — must still be refused"
call -o /dev/null -w 'HTTP %{http_code} (expect 403)\n' -X POST "$BASE/v1/loads/search" \
  -d "{\"call_id\":\"$CALL_ID\",\"origin_state\":\"IL\"}"

CODE=$(docker compose logs app 2>/dev/null | grep -o "\"call_id\": \"$CALL_ID\".*\"code\": \"[0-9]*\"" | tail -1 | grep -o '"code": "[0-9]*"' | grep -o '[0-9]*')
say "verify OTP (code read from the audit stream: $CODE)"
call -X POST "$BASE/v1/identity/otp/verify" \
  -d "{\"call_id\":\"$CALL_ID\",\"code\":\"$CODE\"}" | python3 -m json.tool

say "search live loads out of IL"
SEARCH=$(call -X POST "$BASE/v1/loads/search" \
  -d "{\"call_id\":\"$CALL_ID\",\"origin_state\":\"IL\",\"limit\":3}")
echo "$SEARCH" | python3 -m json.tool
LOAD_ID=$(echo "$SEARCH" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["loads"][0]["load_id"] if d["loads"] else "")')
POSTED=$(echo "$SEARCH" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["loads"][0]["loadboard_rate"] if d["loads"] else 0)')
echo "selected load=$LOAD_ID posted=$POSTED"

say "load detail (note: no max_rate anywhere in the payload)"
call "$BASE/v1/loads/$LOAD_ID?call_id=$CALL_ID" | python3 -m json.tool

say "negotiation — carrier pushes hard three times"
for MULT in 4 3 2; do
  OFFER=$(( POSTED * MULT ))
  echo "  carrier asks \$$OFFER:"
  call -X POST "$BASE/v1/negotiate" \
    -d "{\"call_id\":\"$CALL_ID\",\"load_id\":\"$LOAD_ID\",\"carrier_offer\":$OFFER}" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("    ->",d["decision"],"counter=",d["broker_counter"],"remaining=",d["rounds_remaining"],"may_transfer=",d["may_transfer"])'
done

say "handoff after a failed negotiation must be withheld"
call -X POST "$BASE/v1/handoff" -d "{\"call_id\":\"$CALL_ID\"}" | python3 -m json.tool

say "ops dashboard"
call "$BASE/v1/ops/dashboard" | python3 -m json.tool
