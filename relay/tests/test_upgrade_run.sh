#!/usr/bin/env bash
# test_upgrade_run.sh — bench for relay/scripts/upgrade-run.sh (sudo/apt stubbed).
#
# The healthy run comes first: this route will mostly run on a VM where apt
# succeeds, and a false FAILED there would teach us to ignore the verdict.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../scripts/upgrade-run.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0
ok()  { if [ "$1" = "$2" ]; then echo "  ok   $3"; else echo "  FAIL $3 (want=$1 got=$2)"; fail=1; fi }
has() { if printf '%s' "$2" | grep -q -- "$1"; then echo "  ok   $3"; else echo "  FAIL $3"; fail=1; fi }

# Fake sudo: records the exact argv, fails on demand per verb.
cat > "$TMP/fake-sudo" <<'EOF'
#!/usr/bin/env bash
shift  # drop -n
echo "$*" >> "$FAKE_CALLS"
case "$*" in
  *" update") [ -n "${FAIL_UPDATE:-}" ] && exit 100 ;;
  *dist-upgrade) [ -n "${FAIL_UPGRADE:-}" ] && exit 100 ;;
esac
exit 0
EOF
chmod +x "$TMP/fake-sudo"
printf '#!/bin/sh\necho "pending: 0 package(s)"\necho "VERDICT: compliant"\n' > "$TMP/watch"; chmod +x "$TMP/watch"

run() {  # $1 = case dir; extra env via caller
  mkdir -p "$1"; : > "$1/calls"
  FAKE_CALLS="$1/calls" UR_LOG_DIR="$1/logs" UR_SUDO="$TMP/fake-sudo -n" \
    UR_WATCH_CMD="$TMP/watch" UR_REBOOT_FLAG="$1/reboot-required" \
    UR_HEALTH_CMD="${HEALTH:-true}" bash "$SCRIPT"; RC=$?
  OUT="$(cat "$(ls -1t "$1"/logs/*.log | head -1)")"; CALLS="$(cat "$1/calls")"
}

echo "case A — HEALTHY: update + upgrade OK, relay healthy"
run "$TMP/a"
ok 0 "$RC" "exit 0"
has "VERDICT: DONE" "$OUT" "verdict DONE"
has "reboot required: no" "$OUT" "no reboot flag"
has "NOT restarted" "$OUT" "says services were not restarted"
# The argv must match the sudoers pin word for word, or sudo -n refuses live.
has "^/usr/bin/apt-get update$" "$CALLS" "update argv pinned"
has "^/usr/bin/env DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get -y -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dist-upgrade$" "$CALLS" "dist-upgrade argv pinned"

echo "case B — reboot flag present is REPORTED, never acted on"
mkdir -p "$TMP/b"; touch "$TMP/b/reboot-required"
run "$TMP/b"
ok 0 "$RC" "still exit 0"
has "reboot required: YES" "$OUT" "reboot flag reported"

echo "case C — update fails: upgrade skipped, FAILED"
FAIL_UPDATE=1 run "$TMP/c"
ok 1 "$RC" "exit 1"
has "skipped: update failed" "$OUT" "upgrade marked skipped"
ok 1 "$(printf '%s\n' "$CALLS" | grep -c .)" "only one apt call made"

echo "case D — dist-upgrade fails: FAILED"
FAIL_UPGRADE=1 run "$TMP/d"
ok 1 "$RC" "exit 1"
has "dist-upgrade rc: 100" "$OUT" "rc surfaced"

echo "case E — relay unhealthy after a clean upgrade: FAILED (positive control of the health gate)"
HEALTH=false run "$TMP/e"
ok 1 "$RC" "exit 1"
has "relay health   : KO" "$OUT" "health KO surfaced"

echo "case F — concurrent run refused by the lock"
mkdir -p "$TMP/f/logs"
( exec 9>"$TMP/f/logs/.lock"; flock 9; sleep 3 ) & sleep 0.5
UR_LOG_DIR="$TMP/f/logs" UR_SUDO="$TMP/fake-sudo -n" FAKE_CALLS="$TMP/f/calls" bash "$SCRIPT" 2>/dev/null; RC=$?
ok 75 "$RC" "exit 75 while locked"
wait

echo "case G — logs bounded to the last 10 runs"
mkdir -p "$TMP/g/logs"; for i in $(seq -w 1 12); do touch -d "2026-01-$i" "$TMP/g/logs/202601$i-000000.log"; done
run "$TMP/g"
ok 10 "$(ls "$TMP/g/logs"/*.log | wc -l | tr -d ' ')" "10 logs kept"

[ "$fail" -eq 0 ] && echo "ALL CASES PASS" || { echo "SOME CASES FAIL"; exit 1; }
