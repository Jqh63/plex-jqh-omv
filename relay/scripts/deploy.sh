#!/usr/bin/env bash
# deploy.sh — deploy the relay's code/config to the VM.
#
# Pushes the two venv locks first and, only if a venv drifts from its lock,
# syncs it (venv-upgrade, waited for). Then pipes app.py + Caddyfile over stdin
# to the VM-side dispatch.sh and triggers apply + health. Venvs before the app:
# a new app.py may need the new deps, never the reverse.
# wol-relay.service is NOT deployed here: a unit is root-equivalent, so only
# bootstrap-wol-relay.sh installs it (finding F8).
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
#   0 — venvs in sync, apply + health OK
#   1 — a push or apply failed
#   2 — health KO post-restart (investigate manually)
#   3 — venv sync FAILED / rolled back / unmeasured: app NOT deployed

set -euo pipefail

ALIAS="${WOL_RELAY_ALIAS:-wol-relay-deploy}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[deploy] push venv locks ..."
ssh "$ALIAS" push-lock-wol-relay < "$REPO_DIR/scripts/requirements-wol-relay.lock"
ssh "$ALIAS" push-lock-pock-sync < "$REPO_DIR/scripts/requirements-pock-sync.lock"
ssh "$ALIAS" apply-locks

echo "[deploy] venvs vs locks ..."
vrc=0; ssh "$ALIAS" venv-versions > /dev/null || vrc=$?
case "$vrc" in
  0) echo "[deploy]   venvs in sync with their lock" ;;
  2)
    before="$(ssh "$ALIAS" venv-upgrade-log | head -1)"
    ssh "$ALIAS" venv-upgrade
    # e2-micro: a full sync takes minutes. Wait for a NEW log that is finished.
    for _ in $(seq 1 90); do
      sleep 10
      log="$(ssh "$ALIAS" venv-upgrade-log)"
      if [ "$(head -1 <<<"$log")" != "$before" ] && grep -q 'state: finished' <<<"$log"; then break; fi
      log=""
    done
    if [ -z "$log" ]; then echo "[deploy] FAIL — venv sync not finished after 15 min: ssh $ALIAS venv-upgrade-log" >&2; exit 3; fi
    grep -E '^venvs:|^VERDICT:' <<<"$log" | sed 's/^/[deploy]   /'
    grep -q '^VERDICT: DONE' <<<"$log" || { echo "[deploy] FAIL — venv sync did not complete, app NOT deployed" >&2; exit 3; }
    ;;
  *) echo "[deploy] FAIL — venv-versions could not measure (rc=$vrc), app NOT deployed: ssh $ALIAS venv-versions" >&2; exit 3 ;;
esac

echo "[deploy] push app.py ..."
ssh "$ALIAS" push-app < "$REPO_DIR/app.py"

echo "[deploy] push Caddyfile ..."
ssh "$ALIAS" push-caddyfile < "$REPO_DIR/Caddyfile"

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
