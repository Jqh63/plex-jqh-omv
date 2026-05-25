#!/usr/bin/env bash
# dispatch.sh — router for SSH GitOps commands on the relay VM.
#
# Installed as a forced-command in ~deploy/.ssh/authorized_keys, reads
# $SSH_ORIGINAL_COMMAND (set by sshd) and routes it to a STATIC
# whitelist of subcommands. No free-form parsing, no user-supplied
# arguments.
#
# Usage from the deploying host (via SSH alias `wol-relay-deploy`):
#   ssh wol-relay-deploy push-app          # stdin → /tmp/wol-relay-staging/app.py
#   ssh wol-relay-deploy push-caddyfile    # stdin → /tmp/wol-relay-staging/Caddyfile
#   ssh wol-relay-deploy push-service      # stdin → /tmp/wol-relay-staging/wol-relay.service
#   ssh wol-relay-deploy apply             # install the 3 files + restart services
#   ssh wol-relay-deploy status            # systemctl is-active wol-relay caddy
#   ssh wol-relay-deploy health            # curl http://127.0.0.1:8000/health
#
# Standard usage pattern: `relay/scripts/deploy.sh` on the deploying host
# pipes the 3 push commands from the local repo, then triggers apply.
#
# Security by construction:
#   - Static enum whitelist (no regex, no glob, no free args).
#   - To extend, edit this file in a reviewed PR.
#   - Push commands accept stdin but write to /tmp/wol-relay-staging/
#     (fixed path, outside any sensitive directory).
#   - apply delegates to sudo with a minimal sudoers file — exact verbs
#     for the 3 installs + 3 systemctl invocations, nothing else
#     (see sudoers.deploy).

set -euo pipefail

STAGING_DIR="/tmp/wol-relay-staging"
mkdir -p "$STAGING_DIR"

case "${SSH_ORIGINAL_COMMAND:-}" in
  push-app)
    cat > "$STAGING_DIR/app.py"
    echo "[push-app] OK ($(wc -c < "$STAGING_DIR/app.py") bytes)"
    ;;
  push-caddyfile)
    cat > "$STAGING_DIR/Caddyfile"
    echo "[push-caddyfile] OK ($(wc -c < "$STAGING_DIR/Caddyfile") bytes)"
    ;;
  push-service)
    cat > "$STAGING_DIR/wol-relay.service"
    echo "[push-service] OK ($(wc -c < "$STAGING_DIR/wol-relay.service") bytes)"
    ;;
  apply)
    # Pre-condition: the 3 staged files must exist.
    for f in app.py Caddyfile wol-relay.service; do
      if [[ ! -s "$STAGING_DIR/$f" ]]; then
        echo "[apply] FAIL — $STAGING_DIR/$f missing or empty. Run push-* first." >&2
        exit 1
      fi
    done
    sudo /usr/bin/install -o wol -g wol -m 0644 "$STAGING_DIR/app.py" /opt/wol-relay/app.py
    sudo /usr/bin/install -m 0644 "$STAGING_DIR/Caddyfile" /etc/caddy/Caddyfile
    sudo /usr/bin/install -m 0644 "$STAGING_DIR/wol-relay.service" /etc/systemd/system/wol-relay.service
    sudo /bin/systemctl daemon-reload
    sudo /bin/systemctl restart wol-relay
    sudo /bin/systemctl reload caddy
    echo "[apply] OK — wol-relay restarted, caddy reloaded"
    ;;
  status)
    /bin/systemctl is-active wol-relay caddy
    ;;
  health)
    /usr/bin/curl -fsS http://127.0.0.1:8000/health
    ;;
  *)
    echo "dispatch.sh: unknown command '${SSH_ORIGINAL_COMMAND:-}'" >&2
    echo "Expected: push-app, push-caddyfile, push-service, apply, status, health." >&2
    exit 64
    ;;
esac
