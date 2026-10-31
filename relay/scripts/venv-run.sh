#!/usr/bin/env bash
# venv-run.sh — Python venvs of the relay VM: report versions, or upgrade them.
# Routed by `ssh wol-relay-deploy venv-versions` (report) and `venv-upgrade`
# (upgrade, detached; read with `venv-upgrade-log`).
#
# Why it exists: /opt/wol-relay/venv and /opt/pock-sync/venv were installed
# once, unpinned, and never looked at again — on the most exposed machine of
# the ecosystem. OS updates had a route (upgrade-watch/upgrade); these did not.
#
# PRIVILEGE: runs as root through two argvs pinned in sudoers.deploy
# (`venv-run.sh report`, `venv-run.sh upgrade`), no other argument accepted.
# Root only chowns the two venvs to their service user, restarts the two
# services and writes its own logs. pip ALWAYS runs as the service user that
# already executes that venv (`wol`, `pock`): a poisoned PyPI package gains
# nothing that service did not already have. Wheels only (--only-binary), so
# installing never runs a setup.py. Inputs are root-owned files only.
#
# upgrade: freeze → install the requirements file → restart → health check;
# health KO ⇒ reinstall the freeze, restart again, verdict ROLLED BACK.
# Exit: report 0 up to date / 2 updates available / 3 could not measure;
# upgrade 0 DONE / 1 FAILED or ROLLED BACK.

set -uo pipefail

# Test seams — real runs take every default.
PREFIX="${VR_PREFIX:-}"
SCRIPTS="${VR_SCRIPTS:-/opt/wol-relay/scripts}"
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

report() {
  local v rc=0 out n
  echo "=== RELAY VM VENVS — $(ts) ==="
  for v in $VENVS; do
    echo; echo "--- $v ($(venv_dir "$v"), user $(owner "$v"))"
    if [ ! -x "$(venv_dir "$v")/bin/pip" ]; then
      echo "MISSING: no pip in this venv"; rc=3; continue
    fi
    $AS_USER "$(owner "$v")" -- "$(venv_dir "$v")/bin/python" --version 2>&1
    echo "installed:"
    pip_as "$v" list --format=freeze 2>&1 | sed 's/^/  /'
    # `--outdated` refuses `--format=freeze` (pip error, seen live 2026-10-31):
    # columns, and only rows "<pkg> <version> <latest> ..." count — pip
    # warnings share the captured stream.
    if ! out="$(pip_as "$v" list --outdated --format=columns 2>&1)"; then
      echo "OUTDATED: pip list --outdated failed"; printf '%s\n' "$out" | tail -3 | sed 's/^/  /'
      rc=3; continue
    fi
    out="$(printf '%s\n' "$out" | awk 'NF >= 3 && $2 ~ /^[0-9]/ && $3 ~ /^[0-9]/')"
    n=$(printf '%s\n' "$out" | awk 'NF' | wc -l)
    echo "outdated: $n"
    [ "$n" -gt 0 ] && printf '%s\n' "$out" | sed 's/^/  /'
    [ "$n" -gt 0 ] && [ "$rc" -eq 0 ] && rc=2
  done
  echo
  case "$rc" in
    0) echo "VERDICT: up to date" ;;
    2) echo "VERDICT: updates available — ssh wol-relay-deploy venv-upgrade" ;;
    *) echo "VERDICT: could not measure — never a green light" ;;
  esac
  return "$rc"
}

upgrade_one() {  # prints its own lines; returns 0 DONE / 1 FAILED / 2 ROLLED BACK
  local v="$1" req="$SCRIPTS/requirements-$1.txt" freeze="$STATE_DIR/$1.freeze"
  echo; echo "--- $v"
  [ -f "$req" ] || { echo "FAILED: missing $req"; return 1; }
  [ -x "$(venv_dir "$v")/bin/pip" ] || { echo "FAILED: no pip in $(venv_dir "$v")"; return 1; }
  # The venv belongs to the user that runs it — pip never runs as root.
  $CHOWN "$(owner "$v"):$(owner "$v")" "$(venv_dir "$v")" || { echo "FAILED: chown"; return 1; }
  pip_as "$v" freeze > "$freeze" || { echo "FAILED: freeze"; return 1; }
  echo "before: $(tr '\n' ' ' < "$freeze")"
  if ! pip_as "$v" install --upgrade --only-binary=:all: -r /dev/stdin < "$req"; then
    echo "pip install failed — restoring the freeze"
    pip_as "$v" install --only-binary=:all: -r /dev/stdin < "$freeze" \
      || echo "WARN: freeze reinstall failed too"
    $SYSTEMCTL restart "$v"; healthy "$v" && echo "health after restore: OK" || echo "health after restore: KO"
    return 1
  fi
  echo "after : $(pip_as "$v" freeze | tr '\n' ' ')"
  $SYSTEMCTL restart "$v"
  if healthy "$v"; then echo "health: OK"; return 0; fi
  echo "health: KO after upgrade — ROLLING BACK to the freeze"
  pip_as "$v" install --only-binary=:all: -r /dev/stdin < "$freeze" \
    || echo "WARN: freeze reinstall failed"
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
  echo "=== RELAY VM VENV UPGRADE — start $(ts) ==="
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
  *) echo "usage: venv-run.sh report|upgrade" >&2; exit 64 ;;
esac
