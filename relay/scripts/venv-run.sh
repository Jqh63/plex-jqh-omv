#!/usr/bin/env bash
# venv-run.sh — Python venvs of the relay VM: compare them to their lock, or
# sync them to it. Routed by `ssh wol-relay-deploy venv-versions` (report) and
# `venv-upgrade` (sync, detached; read with `venv-upgrade-log`).
#
# Model (knowledge-base ADR 2026-11-01): the versions live in a hashed lock
# file per venv, versioned in relay/scripts/requirements-<venv>.lock and
# compiled by relay/scripts/lock.sh. The VM never resolves anything: it
# installs exactly the lock, wheels only, every file checked against its hash.
# Updating = a reviewed PR that recompiles the lock, then deploy.sh.
#
# PRIVILEGE: runs as root through two argvs pinned in sudoers.deploy
# (`venv-run.sh report`, `venv-run.sh upgrade`). Root only chowns the venvs to
# their service user, restarts the two services and writes its own logs. pip
# ALWAYS runs as the service user that already executes that venv (`wol`,
# `pock`). The locks reach the VM through the deploy key (push-lock-*), so they
# are untrusted input here: each one must pass lint_lock (pins + sha256 hashes
# + comments, nothing else — no pip option, no index, no include) before pip
# sees it, and pip only ever reads it as that service user. A hostile lock
# therefore gains what push-app already grants: code running as wol/pock.
#
# upgrade: freeze → install the lock (--require-hashes --no-deps) → uninstall
# what the lock does not list → restart → health check; health KO ⇒ reinstall
# the freeze, restart again, verdict ROLLED BACK.
# Exit: report 0 in sync / 2 drift from the lock / 3 could not measure;
# upgrade 0 DONE / 1 FAILED or ROLLED BACK; lint 0 valid / 1 invalid.

set -uo pipefail

# Test seams — real runs take every default.
PREFIX="${VR_PREFIX:-}"
LOCKS="${VR_LOCKS:-/opt/wol-relay/locks}"
LOG_DIR="${VR_LOG_DIR:-/var/log/venv-run}"
STATE_DIR="${VR_STATE_DIR:-/var/lib/venv-run}"
AS_USER="${VR_AS_USER:-runuser -u}"
CHOWN="${VR_CHOWN:-chown -hR}"  # -h: a symlink (bin/python) is never followed
SYSTEMCTL="${VR_SYSTEMCTL:-/bin/systemctl}"
HEALTH="${VR_HEALTH:-curl -fsS --max-time 5 -o /dev/null}"
HEALTH_TRIES="${VR_HEALTH_TRIES:-30}"
HEALTH_SLEEP="${VR_HEALTH_SLEEP:-2}"
KEEP=10
VENVS="wol-relay pock-sync"

venv_dir() { echo "$PREFIX/opt/$1/venv"; }
lock_of()  { echo "$LOCKS/requirements-$1.lock"; }
owner()    { case "$1" in wol-relay) echo wol ;; pock-sync) echo pock ;; esac; }
health_url() {
  case "$1" in
    wol-relay) echo http://127.0.0.1:8000/health ;;
    pock-sync) echo http://127.0.0.1:8001/pock/health ;;
  esac
}
# pip as the service user, with no cache (service homes are not writable)
# and no inherited environment (nothing of the caller reaches pip).
pip_as() {
  local v="$1"; shift
  $AS_USER "$(owner "$v")" -- env -i PATH=/usr/bin:/bin HOME=/nonexistent \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    "$(venv_dir "$v")/bin/pip" "$@"
}
healthy() {
  local i
  for i in $(seq 1 "$HEALTH_TRIES"); do
    $HEALTH "$(health_url "$1")" >/dev/null 2>&1 && return 0
    sleep "$HEALTH_SLEEP"
  done
  return 1
}
ts() { date '+%F %T %Z'; }

# The lock grammar, and nothing more: `name==version \`, then its
# `    --hash=sha256:<64 hex>` lines (the last one without `\`), comments and
# blank lines. Every pin carries at least one hash. Prints the offending lines.
lint_lock() {  # <file>
  awk '
    function bad(why) { printf "  line %d: %s: %s\n", NR, why, $0; err = 1 }
    /^[[:space:]]*$/ || /^[[:space:]]*#/ { if (cont) bad("pin without hash"); cont = 0; next }
    /^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9.+!_-]+ \\$/ {
      if (cont) bad("pin without hash")
      cont = 1; n++; next }
    # No {64}: the default awk of Debian is mawk (no interval expressions).
    /^    --hash=sha256:/ {
      h = substr($0, 19); more = sub(/ \\$/, "", h)
      if (h !~ /^[0-9a-f]+$/ || length(h) != 64) { bad("malformed hash"); next }
      if (!cont) bad("hash outside a pin")
      cont = more; next }
    { bad("not a pin, hash or comment") }
    END {
      if (cont) { print "  end of file: pin or hash left open"; err = 1 }
      if (n == 0) { print "  no pin at all"; err = 1 }
      exit err
    }' "$1"
}

# `name==version` per line, names normalised (PEP 503), sorted.
norm() { awk -F'==' 'NF == 2 { n = tolower($1); gsub(/[-_.]+/, "-", n); print n "==" $2 }' | sort; }
lock_pins() { grep -E '^[A-Za-z0-9][A-Za-z0-9._-]*==' "$1" | sed 's/ \\$//' | norm; }
installed()  { pip_as "$1" list --format=freeze 2>/dev/null | norm; }

# Lines "  <name>: installed X, lock Y" for every difference. Empty = in sync.
drift() {  # <installed file> <lock pins file>
  awk -F'==' '
    NR == FNR { inst[$1] = $2; next }
    { lock[$1] = $2 }
    END {
      for (n in lock) if (!(n in inst)) print "  " n ": missing, lock " lock[n]
                      else if (inst[n] != lock[n]) print "  " n ": installed " inst[n] ", lock " lock[n]
      for (n in inst) if (!(n in lock)) print "  " n ": installed " inst[n] ", not in the lock"
    }' "$1" "$2" | sort
}

report() {
  local v rc=0 d tmp; tmp="$(mktemp -d)"
  echo "=== RELAY VM VENVS — $(ts) ==="
  for v in $VENVS; do
    echo; echo "--- $v ($(venv_dir "$v"), user $(owner "$v"), lock $(lock_of "$v"))"
    if [ ! -x "$(venv_dir "$v")/bin/pip" ]; then echo "MISSING: no pip in this venv"; rc=3; continue; fi
    if [ ! -f "$(lock_of "$v")" ]; then echo "MISSING: no lock"; rc=3; continue; fi
    if ! lint_lock "$(lock_of "$v")" > "$tmp/lint"; then echo "INVALID LOCK:"; cat "$tmp/lint"; rc=3; continue; fi
    $AS_USER "$(owner "$v")" -- "$(venv_dir "$v")/bin/python" --version 2>&1
    installed "$v" > "$tmp/inst"
    if [ ! -s "$tmp/inst" ]; then echo "UNMEASURED: pip list returned nothing"; rc=3; continue; fi
    lock_pins "$(lock_of "$v")" > "$tmp/lock"
    echo "installed: $(wc -l < "$tmp/inst") packages, lock: $(wc -l < "$tmp/lock") pins"
    d="$(drift "$tmp/inst" "$tmp/lock")"
    if [ -z "$d" ]; then echo "drift: 0"
    else echo "drift: $(printf '%s\n' "$d" | wc -l)"; printf '%s\n' "$d"; [ "$rc" -eq 0 ] && rc=2; fi
  done
  rm -rf "$tmp"
  echo
  case "$rc" in
    0) echo "VERDICT: in sync with the lock" ;;
    2) echo "VERDICT: drift from the lock — ssh wol-relay-deploy venv-upgrade" ;;
    *) echo "VERDICT: could not measure — never a green light" ;;
  esac
  return "$rc"
}

restore_freeze() {  # <venv> <freeze>
  pip_as "$1" install --no-deps --only-binary=:all: -r /dev/stdin < "$2"
}

upgrade_one() {  # prints its own lines; returns 0 DONE / 1 FAILED / 2 ROLLED BACK
  local v="$1" lock freeze="$STATE_DIR/$1.freeze" extra
  lock="$(lock_of "$v")"
  echo; echo "--- $v"
  [ -f "$lock" ] || { echo "FAILED: missing $lock"; return 1; }
  if ! lint_lock "$lock"; then echo "FAILED: invalid lock, nothing installed"; return 1; fi
  [ -x "$(venv_dir "$v")/bin/pip" ] || { echo "FAILED: no pip in $(venv_dir "$v")"; return 1; }
  # The venv belongs to the user that runs it — pip never runs as root.
  $CHOWN "$(owner "$v"):$(owner "$v")" "$(venv_dir "$v")" || { echo "FAILED: chown"; return 1; }
  # `list --format=freeze` and not `freeze`: it keeps pip/setuptools/wheel, so
  # a rollback restores the tooling too.
  pip_as "$v" list --format=freeze > "$freeze" || { echo "FAILED: freeze"; return 1; }
  echo "before: $(tr '\n' ' ' < "$freeze")"
  # --no-deps: the lock IS the full closure; pip resolves nothing.
  if ! pip_as "$v" install --require-hashes --no-deps --only-binary=:all: -r /dev/stdin < "$lock"; then
    echo "pip install failed — restoring the freeze"
    restore_freeze "$v" "$freeze" || echo "WARN: freeze reinstall failed too"
    $SYSTEMCTL restart "$v"; healthy "$v" && echo "health after restore: OK" || echo "health after restore: KO"
    return 1
  fi
  # What the lock does not list goes (old tooling, deps dropped upstream).
  # Names come from pip's own list, normalised; never from the caller.
  extra="$(comm -23 <(installed "$v" | cut -d= -f1) <(lock_pins "$lock" | cut -d= -f1) | tr '\n' ' ')"
  if [ -n "${extra// /}" ]; then
    echo "removing (not in the lock): $extra"
    # shellcheck disable=SC2086  # word splitting wanted: one name per word
    pip_as "$v" uninstall -y $extra || echo "WARN: uninstall failed — extras left in place"
  fi
  echo "after : $(pip_as "$v" list --format=freeze | tr '\n' ' ')"
  $SYSTEMCTL restart "$v"
  if healthy "$v"; then echo "health: OK"; return 0; fi
  echo "health: KO after upgrade — ROLLING BACK to the freeze"
  restore_freeze "$v" "$freeze" || echo "WARN: freeze reinstall failed"
  $SYSTEMCTL restart "$v"
  if healthy "$v"; then echo "health after rollback: OK"; else echo "health after rollback: KO — service DOWN, act now"; fi
  return 2
}

upgrade() {
  local v rc verdict=0 summary=""
  mkdir -p "$LOG_DIR" "$STATE_DIR"; chmod 0755 "$LOG_DIR"; chmod 0700 "$STATE_DIR"
  exec 9>"$STATE_DIR/.lock"
  flock -n 9 || { echo "venv-run: an upgrade is already running — see venv-upgrade-log" >&2; exit 75; }
  local log; log="$LOG_DIR/$(date +%Y%m%d-%H%M%S).log"
  exec >>"$log" 2>&1
  echo "=== RELAY VM VENV UPGRADE (sync to lock) — start $(ts) ==="
  renice -n 19 -p $$ >/dev/null 2>&1 || true   # e2-micro: Caddy/sshd first
  for v in $VENVS; do
    upgrade_one "$v"; rc=$?
    case "$rc" in 0) summary="$summary $v=DONE" ;; 1) summary="$summary $v=FAILED"; verdict=1 ;; *) summary="$summary $v=ROLLED-BACK"; verdict=1 ;; esac
  done
  echo; echo "=== SUMMARY $(ts) ==="
  echo "venvs:$summary"
  if [ "$verdict" -eq 0 ]; then echo "VERDICT: DONE"; else echo "VERDICT: FAILED — read the lines above"; fi
  ls -1t "$LOG_DIR"/*.log 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
  return "$verdict"
}

case "${1:-}" in
  report)  report ;;
  upgrade) upgrade ;;
  # Not reachable as root (sudoers pins report/upgrade only): the bench and
  # lock.sh use it, so the grammar the VM enforces is the one tested.
  lint)    [ -f "${2:-}" ] || { echo "usage: venv-run.sh lint <lock>" >&2; exit 64; }
           lint_lock "$2" ;;
  *) echo "usage: venv-run.sh report|upgrade|lint <lock>" >&2; exit 64 ;;
esac
