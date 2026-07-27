#!/usr/bin/env bash
# deploy.sh — deploy the relay's code/config to the VM.
#
# Pipes the 3 files (app.py, Caddyfile, wol-relay.service) over stdin
# to the VM-side dispatch.sh, then triggers apply + health.
#
# Prerequisites (one-shot, see relay/README.md § *GitOps deploy channel → One-shot bootstrap*):
#   - SSH key `id_ed25519_wol_relay_deploy` present on the deploying host
#   - Alias `wol-relay-deploy` in your SSH config:
#       Host wol-relay-deploy
#         HostName <VM_STATIC_IP>
#         User deploy
#         IdentityFile ~/.ssh/id_ed25519_wol_relay_deploy
#         IdentitiesOnly yes
#   - dispatch.sh + sudoers deployed on the VM via bootstrap-wol-relay.sh
#
# Usage:
#   bash relay/scripts/deploy.sh
#
# Override the SSH alias if needed:
#   WOL_RELAY_ALIAS=other-alias bash relay/scripts/deploy.sh
#
# Exit codes:
#   0 — apply + health OK
#   1 — a push or apply failed
#   2 — health KO post-restart (investigate manually)

set -euo pipefail

ALIAS="${WOL_RELAY_ALIAS:-wol-relay-deploy}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[deploy] push app.py ..."
ssh "$ALIAS" push-app < "$REPO_DIR/app.py"

echo "[deploy] push Caddyfile ..."
ssh "$ALIAS" push-caddyfile < "$REPO_DIR/Caddyfile"

echo "[deploy] push wol-relay.service ..."
ssh "$ALIAS" push-service < "$REPO_DIR/wol-relay.service"

echo "[deploy] apply ..."
ssh "$ALIAS" apply

echo "[deploy] health ..."
# Retry, don't single-shot. `apply` returns as soon as systemd has restarted the
# unit, but uvicorn takes ~1-3 s more to bind :8000 — so a single probe fired
# right behind it hits "connection refused" on a deploy that went fine and
# reports a WARN nobody should act on (observed 2026-07-27: WARN at 19:40:22,
# "Uvicorn running" at 19:40:24). A false alarm on the deploy path is worse than
# useless: it teaches the operator to ignore the one line that would matter.
health_ok=0
for attempt in 1 2 3 4 5 6; do
  if ssh "$ALIAS" health; then health_ok=1; break; fi
  [ "$attempt" -lt 6 ] && { echo "[deploy]   not up yet (attempt $attempt/6) — retrying in 2 s"; sleep 2; }
done
if [ "$health_ok" -eq 1 ]; then
  echo "[deploy] DONE — wol-relay restarted, /health OK"
else
  echo "[deploy] WARN — /health still KO after ~10 s, investigate VM side (journalctl -u wol-relay)" >&2
  exit 2
fi
