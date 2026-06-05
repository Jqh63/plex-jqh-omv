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
#   ssh wol-relay-deploy logs-wol-relay    # journalctl -u wol-relay -n 100 (read-only)
#   ssh wol-relay-deploy logs-caddy        # journalctl -u caddy -n 100 (read-only)
#
# home-watch (external homelab monitor, content pushed in from the private
# knowledge-base repo — never stored here):
#   ssh wol-relay-deploy push-home-watch{,-service,-timer}  # stdin → staging
#   ssh wol-relay-deploy apply-home-watch  # install + enable home-watch.timer
#   ssh wol-relay-deploy home-watch-status # timer active + next run (read-only)
#   ssh wol-relay-deploy logs-home-watch   # journalctl -u home-watch -n 100 (read-only)
#
# Standard usage pattern: `relay/scripts/deploy.sh` on the deploying host
# pipes the 3 push commands from the local repo, then triggers apply.
# home-watch is deployed analogously by knowledge-base's deploy-home-watch.sh.
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
  push-home-watch)
    cat > "$STAGING_DIR/home-watch.sh"
    echo "[push-home-watch] OK ($(wc -c < "$STAGING_DIR/home-watch.sh") bytes)"
    ;;
  push-home-watch-service)
    cat > "$STAGING_DIR/home-watch.service"
    echo "[push-home-watch-service] OK ($(wc -c < "$STAGING_DIR/home-watch.service") bytes)"
    ;;
  push-home-watch-timer)
    cat > "$STAGING_DIR/home-watch.timer"
    echo "[push-home-watch-timer] OK ($(wc -c < "$STAGING_DIR/home-watch.timer") bytes)"
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
  logs-wol-relay)
    # Read-only journal tail. journalctl needs sudo because the `deploy`
    # user isn't in the systemd-journal group; the sudoers entry pins
    # the exact arg vector (no user-controlled flags, fixed -n 100).
    sudo /usr/bin/journalctl -u wol-relay -n 100 --no-pager
    ;;
  logs-caddy)
    sudo /usr/bin/journalctl -u caddy -n 100 --no-pager
    ;;
  apply-home-watch)
    # home-watch = external homelab monitor (private content pushed via stdin
    # from the knowledge-base repo). Pre-condition: the 3 staged files exist.
    for f in home-watch.sh home-watch.service home-watch.timer; do
      if [[ ! -s "$STAGING_DIR/$f" ]]; then
        echo "[apply-home-watch] FAIL — $STAGING_DIR/$f missing or empty. Run push-home-watch* first." >&2
        exit 1
      fi
    done
    sudo /usr/bin/install -o homewatch -g homewatch -m 0755 "$STAGING_DIR/home-watch.sh" /opt/home-watch/home-watch.sh
    sudo /usr/bin/install -m 0644 "$STAGING_DIR/home-watch.service" /etc/systemd/system/home-watch.service
    sudo /usr/bin/install -m 0644 "$STAGING_DIR/home-watch.timer" /etc/systemd/system/home-watch.timer
    sudo /bin/systemctl daemon-reload
    sudo /bin/systemctl enable --now home-watch.timer
    echo "[apply-home-watch] OK — home-watch.timer enabled"
    ;;
  home-watch-status)
    /bin/systemctl is-active home-watch.timer
    /bin/systemctl list-timers home-watch.timer --no-pager
    ;;
  logs-home-watch)
    sudo /usr/bin/journalctl -u home-watch -n 100 --no-pager
    ;;
  *)
    echo "dispatch.sh: unknown command '${SSH_ORIGINAL_COMMAND:-}'" >&2
    echo "Expected: push-app, push-caddyfile, push-service, apply, status, health, logs-wol-relay, logs-caddy," >&2
    echo "          push-home-watch{,-service,-timer}, apply-home-watch, home-watch-status, logs-home-watch." >&2
    exit 64
    ;;
esac
