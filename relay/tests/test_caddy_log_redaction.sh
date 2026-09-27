#!/bin/bash
# F8 (claude-security 2026-09-27): no Caddy log line may carry X-Token.
# Runs the real Caddyfile with NO upstream, so every proxied request fails
# (the relay-restart case) and goes through the error logger — the path the
# site-level `log` filter did not cover. Needs a caddy binary (CADDY=/path or
# PATH); without one it SKIPS loudly: a skip proves nothing.
# Verified failing (2 leaks) on the pre-fix Caddyfile.
set -u
CADDY=${CADDY:-$(command -v caddy)}
[ -x "$CADDY" ] || { echo "SKIP: no caddy binary (set CADDY=)"; exit 0; }
CF=$(cd "$(dirname "$0")/.." && pwd)/Caddyfile
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
export LE_EMAIL=a@example.com RELAY_DOMAIN=http://127.0.0.1:18443 \
  CORS_ORIGIN=https://pwa.example.com RESCUE_HOST=h.example.com RESCUE_OOB=x \
  RESCUE_PATH=rescue-test RESCUE_SSH=x HOME=$D XDG_DATA_HOME=$D XDG_CONFIG_HOME=$D
"$CADDY" run --config "$CF" --adapter caddyfile > "$D/log" 2>&1 &
P=$!; sleep 2
for path in /status /wol /pock/x; do
  curl -s -o /dev/null -H 'X-Token: FAKE-SECRET-123' "http://127.0.0.1:18443$path"
done
sleep 1; kill "$P"; wait "$P" 2>/dev/null
errors=$(grep -c 'http.log.error' "$D/log")
leaks=$(grep -c 'FAKE-SECRET-123' "$D/log")
# Positive control: the failure must have been LOGGED, or "0 leaks" is vacuous.
[ "$errors" -ge 1 ] || { echo "FAIL: no error-log line — test observed nothing"; exit 1; }
[ "$leaks" -eq 0 ] || { echo "FAIL: $leaks log line(s) carry X-Token"; exit 1; }
echo "PASS: $errors error line(s), 0 token leak"
