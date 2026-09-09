#!/usr/bin/env bash
# Read back the one-time code during a live call.
#
# The agent sends a code, but delivery is mocked — the org has no SMS or email
# integration provisioned, so nothing arrives in an inbox. On a real web call a
# human therefore has no way to read the code back to the agent, which stalls
# the identity gate and with it the whole call.
#
# The code is recoverable from the authenticated call trail, and that is the only
# place it is ever readable: it is never in a carrier-facing response. This finds
# the newest call and prints its current code.
#
#   ./scripts/live_otp.sh                 # newest call
#   ./scripts/live_otp.sh call_abc123     # a specific call
#   BASE=https://your-tunnel ./scripts/live_otp.sh
set -euo pipefail

cd "$(dirname "$0")/.."
set -a && . ./.env && set +a
BASE="${BASE:-http://localhost:8000}"

api() {
  curl -sS -H "Authorization: Bearer $API_AUTH_TOKEN" \
       -H "ngrok-skip-browser-warning: true" "$@"
}

CID="${1:-}"
if [ -z "$CID" ]; then
  CID=$(api "$BASE/v1/ops/dashboard" | python3 -c '
import sys, json
calls = json.load(sys.stdin).get("recent_calls") or []
calls.sort(key=lambda c: c.get("started_at") or "", reverse=True)
print(calls[0]["call_id"] if calls else "")')
fi

if [ -z "$CID" ]; then
  echo "No calls on record yet. Start the web call first, then run this again." >&2
  exit 1
fi

api "$BASE/v1/calls/$CID" | python3 -c '
import sys, json
d = json.load(sys.stdin)
codes = [e["code"] for e in d["events"]
         if e["event"] == "otp_sent" and e.get("code")]
print("call   " + d["call_id"])
print("stage  " + d["stage"])
print()
if codes:
    print("  CODE:  " + codes[-1])
    print()
    print("  Read those six digits back to the agent.")
else:
    print("  No code sent yet - let the agent ask for your MC number first.")'
