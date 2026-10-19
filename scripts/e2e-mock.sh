#!/usr/bin/env bash
# End-to-end check with the mock provider: installs agentgateway into a throwaway folder,
# performs the UI steps through the UI's API, runs every script, and checks the outputs.
# No API keys needed.   ./scripts/e2e-mock.sh
# Overrides: E2E_HOME (scratch folder), INSTALL_URL, IPV6_ENABLED=false (hosts without IPv6).
# E2E_SKIP_INSTALL=1 reuses an agentgateway already on PATH (no network). Port 4000 must be free.
set -uo pipefail
cd "$(dirname "$0")/.."
export DEMO_HOME="${E2E_HOME:-/tmp/agw-demo-e2e}" DEMO_MOCK=1 NO_COLOR=1
. ./demo.env >/dev/null
fail=0
check() {  # check <text the output must contain> <command...>
  local want="$1"; shift
  local out; out="$("$@" 2>&1)"
  if grep -qF -- "$want" <<<"$out"; then echo "  ✓ $*"; else echo "  ✗ $*   (expected '$want')"; sed 's/^/      /' <<<"$out"; fail=1; fi
}
cleanup() { kill ${AGW_PID:-} ${MOCK_PID:-} 2>/dev/null; ./scripts/reset.sh -y >/dev/null; }
trap cleanup EXIT
./scripts/reset.sh -y >/dev/null

echo "install"
if [ "${E2E_SKIP_INSTALL:-}" = "1" ] && command -v agentgateway >/dev/null; then
  echo "  ✓ $(command -v agentgateway) (install skipped)"
elif curl -sL "${INSTALL_URL:-https://agentgateway.dev/install}" | bash >/tmp/agw-e2e-install.log 2>&1 && command -v agentgateway >/dev/null; then
  echo "  ✓ $(command -v agentgateway)"
else
  echo "  ✗ install failed, see /tmp/agw-e2e-install.log"; exit 1
fi
python3 scripts/mock-llm.py >/tmp/agw-e2e-mock.log 2>&1 & MOCK_PID=$!
agentgateway >/tmp/agw-e2e-gateway.log 2>&1 & AGW_PID=$!
ready=0
last=""
for _ in $(seq 1 50); do
  last=$(curl -s -o /dev/null -w "%{http_code}" --max-time 1 "$AGW_URL/api/config" || true)
  if [ "$last" = "200" ]; then ready=1; break; fi
  sleep 0.2
done
if [ "$ready" != 1 ]; then
  echo "  ✗ $AGW_URL/api/config never returned 200 (last: ${last:-none})"
  echo "    Port 4000 has to be free. ./scripts/reset.sh does not stop a gateway installed somewhere else."
  sed 's/^/    /' /tmp/agw-e2e-gateway.log
  exit 1
fi
if grep -q "serving UI at http://localhost:4000/ui" /tmp/agw-e2e-gateway.log; then
  echo "  ✓ UI at http://localhost:4000/ui"
else
  echo "  ✗ startup log did not advertise http://localhost:4000/ui"; sed 's/^/    /' /tmp/agw-e2e-gateway.log; fail=1
fi

echo "live UI steps, through the UI's API"
H='content-type: application/json'
put() {  # put <path> <json>
  local path="$1" body="$2" code
  code=$(curl -s -o /tmp/agw-e2e-put.json -w "%{http_code}" -X PUT "$AGW_URL$path" -H "$H" -d "$body" || true)
  if [ "$code" = "200" ]; then echo "  ✓ PUT $path"; else echo "  ✗ PUT $path ($code)"; cat /tmp/agw-e2e-put.json; echo; fail=1; fi
}
put /api/config/resources/llm.provider \
  '{"resources":[{"value":{"name":"anthropic","provider":"anthropic","params":{"apiKey":"$ANTHROPIC_API_KEY"}}}]}'
put /api/config/resources/llm.model \
  "{\"resources\":[{\"value\":{\"name\":\"$CLAUDE_MODEL\",\"provider\":{\"reference\":\"anthropic\"},\"params\":{\"model\":\"$CLAUDE_MODEL\"}}}]}"
put /api/config/resources/llm.apiKey \
  '{"resources":[{"value":{"key":"agw_sk_e2e_alice_from_the_ui","metadata":{"name":"alice"}}}]}'
python3 scripts/agw.py point-at-mock >/dev/null
check "200 OK" ./ask.sh alice "$CLAUDE_MODEL"

echo "fast-forward"
check "across the last 7 days" ./fast-forward-a-week.sh
python3 - << 'PY'
import json, os, urllib.request
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
body = {
    "timeRange": {
        "from": (now - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    },
    "groupBy": [{"field": "attributes", "key": "agentgateway.user"}],
    "bucketSeconds": 86400,
}
req = urllib.request.Request(
    os.environ["AGW_URL"] + "/api/logs/analytics/summary",
    data=json.dumps(body).encode(),
    headers={"content-type": "application/json"},
)
with urllib.request.urlopen(req, timeout=15) as response:
    data = json.load(response)
starts = sorted({bucket.get("start", "")[:10] for bucket in data.get("buckets") or [] if bucket.get("start")})
users = {(group.get("group") or {}).get("agentgateway.user") for group in data.get("groups") or []}
need = {"alice", "bob", "support-team", "nightly-agent", "intern"}
print("  days %s" % ", ".join(starts))
print("  users %s" % ", ".join(sorted(u for u in users if u)))
if len(starts) < 4 or not need <= users:
    raise SystemExit("history did not cover a week for every key")
PY
if [ "$?" -eq 0 ]; then echo "  ✓ analytics covers the past week"; else echo "  ✗ analytics history"; fail=1; fi
ui=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "$AGW_URL/ui" || true)
if [ "$ui" = "200" ]; then echo "  ✓ UI still open after strict mode"; else echo "  ✗ UI returned $ui after strict mode"; fail=1; fi
check "served by openai" ./ask.sh alice "$OPENAI_MODEL"
top="$(python3 scripts/agw.py report | sed -n 2p)"
case "$top" in *nightly-agent*) echo "  ✓ nightly-agent is the top spender" ;; *) echo "  ✗ top spender: $top"; fail=1 ;; esac

echo "enforce"
check "no budgets yet" ./ask.sh nightly-agent "$CLAUDE_MODEL" --until-blocked
check "intern: allowed models" ./scripts/apply-policies.sh 0.02 1
sleep 1.5
blocked_out="$(./ask.sh nightly-agent "$CLAUDE_MODEL" --until-blocked 2>&1)" || true
sed 's/^/    /' <<<"$blocked_out"
if grep -qF "budget_exceeded" <<<"$blocked_out"; then echo "  ✓ until-blocked hits budget_exceeded"; else echo "  ✗ until-blocked (expected budget_exceeded)"; fail=1; fi
check "model_not_allowed" ./ask.sh intern "$CLAUDE_MODEL"
check "200 OK" ./ask.sh intern "$OPENAI_MODEL"
check "no virtual key named" ./ask.sh nobody "$OPENAI_MODEL"

[ "$fail" = 0 ] && echo "all checks passed" || { echo "FAILED"; exit 1; }
