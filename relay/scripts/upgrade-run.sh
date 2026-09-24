#!/usr/bin/env bash
# upgrade-run.sh — install pending OS updates on the relay VM, then verify.
# Routed DETACHED by `ssh wol-relay-deploy upgrade`; read with `upgrade-log`.
#
# Why it exists: unattended-upgrades only takes Debian-Security (deliberate —
# a full auto-upgrade could restart Caddy or bump Python under the relay at
# 06:00 with nobody watching). Everything else (e.g. Google's guest agent and
# CLI) piled up waiting for an IAP SSH session. This route is the "somebody
# is watching" path: triggered by hand, verified by a health check.
#
# PRIVILEGE: two sudo verbs pinned WORD FOR WORD in sudoers.deploy (update +
# dist-upgrade with fixed options). No free arguments, packages come only from
# the sources already configured on the VM. Never reboots, never restarts a
# service: a reboot-required flag and the health verdict are REPORTED.
#
# Detached on purpose: an SSH drop mid-dpkg would leave the VM half-upgraded.

set -uo pipefail

# Test seams — real runs take every default.
LOG_DIR="${UR_LOG_DIR:-/home/deploy/upgrade-logs}"
SUDO="${UR_SUDO:-sudo -n}"
HEALTH_CMD="${UR_HEALTH_CMD:-curl -fsS --max-time 10 http://127.0.0.1:8000/health}"
WATCH_CMD="${UR_WATCH_CMD:-/opt/wol-relay/scripts/upgrade-watch.sh}"
REBOOT_FLAG="${UR_REBOOT_FLAG:-/var/run/reboot-required}"
KEEP=10

mkdir -p "$LOG_DIR"
exec 9>"$LOG_DIR/.lock"
if ! flock -n 9; then
  echo "upgrade-run: another upgrade is already running — see upgrade-log" >&2
  exit 75
fi

LOG="$LOG_DIR/$(date +%Y%m%d-%H%M%S).log"
exec >>"$LOG" 2>&1
ts() { date '+%F %T %Z'; }
echo "=== RELAY VM UPGRADE — start $(ts) ==="

rc_update=0; rc_upgrade=0
$SUDO /usr/bin/apt-get update || rc_update=$?
echo "--- apt-get update rc=$rc_update"
if [ "$rc_update" -eq 0 ]; then
  $SUDO /usr/bin/env DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get -y \
    -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confdef \
    -o Dpkg::Options::=--force-confold dist-upgrade || rc_upgrade=$?
  echo "--- apt-get dist-upgrade rc=$rc_upgrade"
fi

health="KO"; $HEALTH_CMD >/dev/null 2>&1 && health="OK"
reboot="no"; [ -e "$REBOOT_FLAG" ] && reboot="YES"

echo
echo "--- post-upgrade watch"
$WATCH_CMD 2>&1 | grep -E '^(pending|VERDICT|  NEEDS|  covered)' || true

echo
echo "=== SUMMARY $(ts) ==="
echo "update rc      : $rc_update"
echo "dist-upgrade rc: $rc_upgrade$([ "$rc_update" -ne 0 ] && echo ' (skipped: update failed)')"
echo "relay health   : $health"
echo "reboot required: $reboot (never done here — admin decision)"
echo "services       : NOT restarted — patched libraries load at the next restart/reboot"
if [ "$rc_update" -eq 0 ] && [ "$rc_upgrade" -eq 0 ] && [ "$health" = "OK" ]; then
  echo "VERDICT: DONE"; verdict=0
else
  echo "VERDICT: FAILED — read the apt output above"; verdict=1
fi

# Bounded: keep the last $KEEP runs.
ls -1t "$LOG_DIR"/*.log 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
exit "$verdict"
