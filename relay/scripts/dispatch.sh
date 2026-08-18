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
#   ssh wol-relay-deploy push-window       # stdin (1 line HH:MM-HH:MM) → staging
#   ssh wol-relay-deploy apply-window      # install /opt/wol-relay/window (hot, no restart)
#   ssh wol-relay-deploy status            # systemctl is-active wol-relay caddy
#   ssh wol-relay-deploy health            # curl http://127.0.0.1:8000/health
#   ssh wol-relay-deploy logs-wol-relay [500|3000]  # journalctl tail, read-only
#                                          #   (defaut 100 lignes ~= 5 jours)
#   ssh wol-relay-deploy logs-caddy        # journalctl -u caddy -n 100 (read-only)
#   ssh wol-relay-deploy log-footprint     # journald size + log dirs + df (read-only)
#
# home-watch (external homelab monitor, content pushed in from the private
# knowledge-base repo — never stored here):
#   ssh wol-relay-deploy push-home-watch{,-service,-timer}  # stdin → staging
#   ssh wol-relay-deploy apply-home-watch  # install + enable home-watch.timer
#   ssh wol-relay-deploy home-watch-status # timer active + next run (read-only)
#   ssh wol-relay-deploy logs-home-watch   # journalctl -u home-watch -n 100 (read-only)
#
# pock-sync (per-app JSON blob store, code in the public repo Jqh63/pock
# under sync/ — deployed by that repo's sync/deploy.sh):
#   ssh wol-relay-deploy push-pock-sync-app      # stdin → staging app.py
#   ssh wol-relay-deploy push-pock-sync-service  # stdin → staging unit
#   ssh wol-relay-deploy apply-pock-sync         # install + restart pock-sync
#   ssh wol-relay-deploy pock-sync-status        # is-active + /pock/health (read-only)
#   ssh wol-relay-deploy logs-pock-sync          # journalctl -u pock-sync -n 100 (read-only)
#   ssh wol-relay-deploy pock-dump               # tar of /var/lib/pock-sync → stdout (read-only,
#                                                #   pulled daily by the home server for backup)
#
# pat-offsite (encrypted patrimoine backup pushed by the home server —
# knowledge-base ADR 2026-06-12, blobs opaque to this VM by construction):
#   ssh wol-relay-deploy pat-receive daily       # stdin (age blob) → ~deploy/pat-offsite, keep 7
#   ssh wol-relay-deploy pat-receive weekly      # idem, keep 4
#   ssh wol-relay-deploy pat-list                # list stored blobs (read-only)
#   ssh wol-relay-deploy pat-dump-latest         # newest blob → stdout (restore path, read-only)
#
# reverse-SSH out-of-band fallback (knowledge-base ADR 2026-06-05 — the
# endpoint the admin uses when the home server's WireGuard container is down):
#   ssh wol-relay-deploy tunnel-status           # listener + omvtunnel sessions (read-only)
#   ssh wol-relay-deploy tunnel-reap             # drop stale sessions holding the listener
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

# This VM runs on UTC; the home server it watches — and the admin reading
# these logs — live in Europe/Paris. journalctl stamps lines in the VM's
# zone, and its arg vector is pinned in sudoers, so the timestamps cannot
# be re-zoned here. Print the live offset instead, ahead of every tail.
# Not cosmetic: reading a UTC tail as local time once inverted a whole
# diagnosis (2026-07-26 — a clean auto-shutdown read as a 2 h overshoot,
# because the relay's "home DOWN" line sat 2 h off the server's own log).
journal_banner() { # <unit> [line count, default 100]
  printf '=== journalctl -u %s (last %s) — VM clock %s | Europe/Paris %s ===\n' \
    "$1" "${2:-100}" "$(date '+%H:%M %Z')" "$(TZ=Europe/Paris date '+%H:%M %Z')"
}

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
  push-window)
    # Scheduled-uptime window, single line on stdin ("HH:MM-HH:MM" or
    # "HHhMM-HHhMM"). Strictly validated BEFORE staging: the only free-form
    # input this route accepts is a value matching the time-window shape,
    # so the static-enum property of the whitelist is preserved in spirit.
    IFS= read -r line || true
    if ! [[ "$line" =~ ^([01]?[0-9]|2[0-3])[h:][0-5][0-9]-([01]?[0-9]|2[0-3])[h:][0-5][0-9]$ ]]; then
      echo "[push-window] FAIL — want HH:MM-HH:MM or HHhMM-HHhMM, got: '$line'" >&2
      exit 65
    fi
    printf '%s\n' "$line" > "$STAGING_DIR/window"
    echo "[push-window] OK ($line)"
    ;;
  apply-window)
    # Installs the staged window for app.py's current_window() — picked up
    # on the next /status poll (mtime re-read), no service restart needed.
    if [[ ! -s "$STAGING_DIR/window" ]]; then
      echo "[apply-window] FAIL — $STAGING_DIR/window missing or empty. Run push-window first." >&2
      exit 1
    fi
    sudo /usr/bin/install -o wol -g wol -m 0644 "$STAGING_DIR/window" /opt/wol-relay/window
    echo "[apply-window] OK — live on next /status poll ($(cat "$STAGING_DIR/window"))"
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
  logs-wol-relay|"logs-wol-relay 500"|"logs-wol-relay 3000")
    # Read-only journal tail. journalctl needs sudo because the `deploy`
    # user isn't in the systemd-journal group; the sudoers entry pins
    # the exact arg vector (no user-controlled flags, fixed -n 100).
    # Optional wider window. Deliberately NOT a parsed argument: each depth is
    # a LITERAL case pattern, like `pat-receive daily|weekly`, so the static-enum
    # property holds (no free args reaching sudo) and every resulting argv is
    # pinned verbatim in sudoers — no glob, per this file's isolation model.
    #
    # No filtering here on purpose. A `grep device=` view was proposed (PR #96,
    # closed): measured on the real journal, it removed 58 lines out of 100 —
    # including the `home declares UP/DOWN` heartbeats and the wake campaigns,
    # i.e. exactly the lines needed to reconstruct an auto-shutdown timeline
    # (2026-07-26). THIS unit's journal carries no access-log noise to filter
    # out — the access log added in 2026-08 lives on the `caddy` unit and skips
    # /heartbeat, which is what keeps both journals' retention intact.
    n="${SSH_ORIGINAL_COMMAND#logs-wol-relay}"; n="${n# }"; n="${n:-100}"
    journal_banner wol-relay "$n"
    sudo /usr/bin/journalctl -u wol-relay -n "$n" --no-pager
    ;;
  logs-caddy)
    journal_banner caddy
    sudo /usr/bin/journalctl -u caddy -n 100 --no-pager
    ;;
  log-footprint)
    # Janitorial measurement (read-only): journald size + pinned log dirs +
    # disk headroom. Decides whether the e2-micro needs a journald cap
    # (knowledge-base ADR 2026-07-07-housekeeping-janitorial §6).
    echo "=== JOURNALD ==="
    sudo /usr/bin/journalctl --disk-usage
    echo "=== LOG DIRS (du -shx) ==="
    sudo /usr/bin/du -shx /var/log /var/lib/caddy
    echo "=== DISK ==="
    /bin/df -h /
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
    journal_banner home-watch
    sudo /usr/bin/journalctl -u home-watch -n 100 --no-pager
    ;;
  push-pock-sync-app)
    cat > "$STAGING_DIR/pock-sync-app.py"
    echo "[push-pock-sync-app] OK ($(wc -c < "$STAGING_DIR/pock-sync-app.py") bytes)"
    ;;
  push-pock-sync-service)
    cat > "$STAGING_DIR/pock-sync.service"
    echo "[push-pock-sync-service] OK ($(wc -c < "$STAGING_DIR/pock-sync.service") bytes)"
    ;;
  apply-pock-sync)
    # pock-sync = per-app JSON blob store (code from the public Jqh63/pock
    # repo, pushed via stdin). Pre-condition: the 2 staged files exist.
    for f in pock-sync-app.py pock-sync.service; do
      if [[ ! -s "$STAGING_DIR/$f" ]]; then
        echo "[apply-pock-sync] FAIL — $STAGING_DIR/$f missing or empty. Run push-pock-sync-* first." >&2
        exit 1
      fi
    done
    sudo /usr/bin/install -o pock -g pock -m 0644 "$STAGING_DIR/pock-sync-app.py" /opt/pock-sync/app.py
    sudo /usr/bin/install -m 0644 "$STAGING_DIR/pock-sync.service" /etc/systemd/system/pock-sync.service
    sudo /bin/systemctl daemon-reload
    sudo /bin/systemctl restart pock-sync
    echo "[apply-pock-sync] OK — pock-sync restarted"
    ;;
  pock-sync-status)
    /bin/systemctl is-active pock-sync
    /usr/bin/curl -fsS http://127.0.0.1:8001/pock/health
    ;;
  logs-pock-sync)
    journal_banner pock-sync
    sudo /usr/bin/journalctl -u pock-sync -n 100 --no-pager
    ;;
  pock-dump)
    # Read-only dump of the blob dir (700 pock:pock, hence sudo with a
    # pinned arg vector). Tar to stdout — the home server pulls this daily
    # and feeds it to its regular backup. Never prints the token.
    sudo /usr/bin/tar -C /var/lib/pock-sync -cf - .
    ;;
  "pat-receive daily"|"pat-receive weekly")
    # Off-site patrimoine backup: the home server PUSHES an age-encrypted
    # blob on stdin. Public-key encryption — the private key never leaves
    # home, so this VM stores ciphertext it cannot read. Two literal case
    # patterns: the static-enum property is preserved (no free args).
    # Stored under ~deploy (no sudo involved), rotation per class.
    class="${SSH_ORIGINAL_COMMAND#pat-receive }"
    dir="$HOME/pat-offsite"
    mkdir -p "$dir" && chmod 700 "$dir"
    f="$dir/pat-$class-$(date -u +%Y%m%dT%H%M%SZ).age"
    cat > "$f.tmp"
    sz=$(wc -c < "$f.tmp")
    # A bare age header is ~200 bytes — anything at or below is a broken pipe.
    if [ "$sz" -le 200 ]; then
      rm -f "$f.tmp"
      echo "ERR payload too small ($sz bytes) — refusing to store" >&2
      exit 65
    fi
    mv "$f.tmp" "$f"
    keep=7; [ "$class" = "weekly" ] && keep=4
    ls -1t "$dir"/pat-"$class"-*.age 2>/dev/null | tail -n +$((keep + 1)) | xargs -r rm -f
    echo "OK $sz bytes -> $(basename "$f")"
    ;;
  pat-list)
    ls -lh "$HOME/pat-offsite" 2>/dev/null || echo "(no backups yet)"
    ;;
  pat-dump-latest)
    # Restore path: newest blob (any class) to stdout. Decryption happens
    # at home with the age private key — the VM never sees cleartext.
    f=$(ls -1t "$HOME/pat-offsite"/pat-*.age 2>/dev/null | head -1 || true)
    [ -n "$f" ] || { echo "ERR no backup stored" >&2; exit 66; }
    cat "$f"
    ;;
  tunnel-status)
    # Reverse-SSH out-of-band fallback endpoint (knowledge-base ADR
    # 2026-06-05): is the home server's tunnel actually terminated here?
    # Read-only, no sudo — the loopback listener shows up in `ss -lnt` and
    # the session processes in `pgrep -u omvtunnel`. BOTH views on purpose:
    # a listener whose session is gone is precisely the stale state that
    # makes every reconnect fail with "remote port forwarding failed".
    # `ss`/`pgrep` resolved through PATH, not pinned: no sudo is involved
    # here, so nothing depends on the literal path (unlike the sudoers-pinned
    # pkill of tunnel-reap), and a hardcoded /usr/bin would silently turn this
    # read-only view into "(unavailable)" on a differently laid-out image.
    echo "=== loopback listener (want 127.0.0.1:2222) ==="
    ss -lnt 'src 127.0.0.1:2222' || echo "(ss unavailable)"
    echo "=== omvtunnel sessions (sshd) ==="
    pgrep -a -u omvtunnel -f sshd || echo "(none)"
    ;;
  tunnel-reap)
    # Drop every sshd session owned by omvtunnel, freeing the loopback
    # listener. Bounded and recoverable by construction: that user can hold
    # nothing but this tunnel (nologin + permitlisten), and the home server's
    # reverse-ssh-tunnel.service reconnects within its RestartSec.
    #
    # Why this exists: after an abrupt reboot of the home server, its old
    # session survives here until the TCP keepalive expires (~2 h), holding
    # the listener — so the fallback channel, whose only job is to exist
    # during an outage, stays down exactly when it is needed. The keepalive
    # drop-in installed by bootstrap-wol-relay.sh makes that self-healing in
    # ~90 s; this verb is the manual override when it must be immediate.
    # Compter les sshd, PAS tous les process du user : la sortie réelle du
    # 2026-08-18 montre un `systemd --user` + `(sd-pam)` qui survivent à la
    # session. Sans le -f, un reap sans session à tuer verrait before=2,
    # after=2 et rapporterait FAILED alors qu'il n'y avait rien à faire.
    before=$(pgrep -c -u omvtunnel -f sshd || true)
    # Path pinned HERE on purpose: this argv must match sudoers verbatim.
    sudo /usr/bin/pkill -u omvtunnel -f sshd || true
    sleep 2
    after=$(pgrep -c -u omvtunnel -f sshd || true)
    echo "[tunnel-reap] omvtunnel processes: ${before:-0} -> ${after:-0}"
    # Report the RESULT, never a reassuring constant. `pkill` exits 1 both when
    # nothing matched and when sudo refused it, so the process count is the only
    # honest witness: a verb that prints "done" while a missing sudoers entry
    # silently blocked it would be worse than no verb at all.
    if [ "${before:-0}" -eq 0 ]; then
      echo "[tunnel-reap] nothing to reap — no omvtunnel session was registered here"
    elif [ "${after:-0}" -lt "${before:-0}" ]; then
      echo "[tunnel-reap] listener released — the home server reconnects on its own restart timer"
    else
      echo "[tunnel-reap] FAILED: sessions still present. sudoers entry missing or pkill blocked?" >&2
      exit 1
    fi
    ;;
  *)
    echo "dispatch.sh: unknown command '${SSH_ORIGINAL_COMMAND:-}'" >&2
    echo "Expected: push-app, push-caddyfile, push-service, apply, push-window, apply-window, status, health, logs-wol-relay [500|3000], logs-caddy, log-footprint," >&2
    echo "          push-home-watch{,-service,-timer}, apply-home-watch, home-watch-status, logs-home-watch," >&2
    echo "          push-pock-sync-{app,service}, apply-pock-sync, pock-sync-status, logs-pock-sync, pock-dump," >&2
    echo "          pat-receive {daily,weekly}, pat-list, pat-dump-latest," >&2
    echo "          tunnel-status, tunnel-reap." >&2
    exit 64
    ;;
esac
