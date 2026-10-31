#!/usr/bin/env bash
# test_venv_run.sh — bench for relay/scripts/venv-run.sh (pip/systemctl/runuser stubbed).
#
# The healthy upgrade comes first: it is what the route will meet on the VM,
# and a false FAILED there would teach us to ignore the verdict. Then the two
# failure shapes (pip refuses, service unhealthy after upgrade ⇒ rollback),
# the report verdicts, and the privilege invariant: pip never runs as root.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../scripts/venv-run.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0
ok()  { if [ "$1" = "$2" ]; then echo "  ok   $3"; else echo "  FAIL $3 (want=$1 got=$2)"; fail=1; fi }
has() { if printf '%s' "$2" | grep -- "$1" >/dev/null; then echo "  ok   $3"; else echo "  FAIL $3"; fail=1; fi }
hasnt() { if printf '%s' "$2" | grep -- "$1" >/dev/null; then echo "  FAIL $3"; fail=1; else echo "  ok   $3"; fi }

# One case = a fake root with two venvs whose pip is a stub. The stub runs
# under `env -i`, so its state path is baked into it, not passed by env.
setup() {  # $1 = case dir
  local c="$1" v
  mkdir -p "$c/scripts" "$c/state"
  printf 'fastapi\nuvicorn[standard]\n' > "$c/scripts/requirements-wol-relay.txt"
  printf 'fastapi\nuvicorn[standard]\n' > "$c/scripts/requirements-pock-sync.txt"
  for v in wol-relay pock-sync; do
    mkdir -p "$c/opt/$v/venv/bin" "$c/st/$v"
    printf 'fastapi==0.110.0\nuvicorn==0.29.0\n' > "$c/st/$v/installed"
    printf 'fastapi==0.139.2\nuvicorn==0.38.0\n' > "$c/st/$v/new"
    printf 'fastapi==0.110.0\nuvicorn==0.29.0\n' > "$c/st/$v/outdated"
    printf '#!/bin/sh\necho "Python 3.13.5"\n' > "$c/opt/$v/venv/bin/python"
    cat > "$c/opt/$v/venv/bin/pip" <<EOF
#!/usr/bin/env bash
S="$c/st/$v"
echo "\$*" >> "$c/calls"
case "\$*" in
  "list --format=freeze"|freeze) cat "\$S/installed" ;;
  "list --outdated --format=freeze") [ -e "\$S/pypi-down" ] && { echo "ConnectionError" >&2; exit 1; }; cat "\$S/outdated" ;;
  "install --upgrade --only-binary=:all: -r /dev/stdin")
    cat > "\$S/asked"; [ -e "\$S/pip-fails" ] && exit 1; cp "\$S/new" "\$S/installed" ;;
  "install --only-binary=:all: -r /dev/stdin") cat > "\$S/installed" ;;
  *) echo "unexpected pip argv: \$*" >&2; exit 99 ;;
esac
EOF
    chmod +x "$c/opt/$v/venv/bin/pip" "$c/opt/$v/venv/bin/python"
  done
  # runuser stub: records the user, refuses root, runs the rest.
  cat > "$c/as" <<EOF
#!/usr/bin/env bash
echo "as \$1" >> "$c/calls"
[ "\$1" = root ] && exit 98
shift; [ "\$1" = -- ] && shift; exec "\$@"
EOF
  # health: KO while the venv holds a version marked BROKEN.
  cat > "$c/health" <<EOF
#!/usr/bin/env bash
case "\$*" in *8000*) v=wol-relay ;; *) v=pock-sync ;; esac
! grep -q BROKEN "$c/st/\$v/installed"
EOF
  printf '#!/bin/sh\necho "systemctl $*" >> "%s/calls"\n' "$c" > "$c/systemctl"
  printf '#!/bin/sh\necho "chown $*" >> "%s/calls"\n' "$c" > "$c/chown"
  chmod +x "$c/as" "$c/health" "$c/systemctl" "$c/chown"
  : > "$c/calls"
}

run() {  # $1 = case dir, $2 = mode
  local c="$1"
  VR_PREFIX="$c" VR_SCRIPTS="$c/scripts" VR_LOG_DIR="$c/logs" VR_STATE_DIR="$c/state" \
    VR_AS_USER="$c/as" VR_CHOWN="$c/chown" VR_SYSTEMCTL="$c/systemctl" \
    VR_HEALTH="$c/health" VR_HEALTH_TRIES=2 VR_HEALTH_SLEEP=0 \
    bash "$SCRIPT" "$2" > "$c/stdout" 2>&1; RC=$?
  if [ "$2" = upgrade ]; then OUT="$(cat "$(ls -1t "$c"/logs/*.log | head -1)")"; else OUT="$(cat "$c/stdout")"; fi
  CALLS="$(cat "$c/calls")"
}

echo "case A — HEALTHY upgrade: both venvs DONE"
setup "$TMP/a"; run "$TMP/a" upgrade
ok 0 "$RC" "exit 0"
has "VERDICT: DONE" "$OUT" "verdict DONE"
has "venvs: wol-relay=DONE pock-sync=DONE" "$OUT" "both venvs DONE"
has "before: fastapi==0.110.0" "$OUT" "records versions before"
has "after : fastapi==0.139.2" "$OUT" "records versions after"
has "systemctl restart wol-relay" "$CALLS" "restarts wol-relay"
has "systemctl restart pock-sync" "$CALLS" "restarts pock-sync"
has "chown pock:pock $TMP/a/opt/pock-sync/venv" "$CALLS" "pock venv handed to pock"
ok "$(cat "$TMP/a/scripts/requirements-wol-relay.txt")" "$(cat "$TMP/a/st/wol-relay/asked")" "pip fed the root-owned requirements file"
has "^as wol$" "$CALLS" "pip runs as wol"
has "^as pock$" "$CALLS" "pip runs as pock"
hasnt "^as root$" "$CALLS" "pip never runs as root"
hasnt "unexpected pip argv" "$(cat "$TMP/a/stdout")" "every pip argv is an expected one"

echo "case B — pip install fails: freeze restored, FAILED, other venv still upgraded"
setup "$TMP/b"; touch "$TMP/b/st/wol-relay/pip-fails"; run "$TMP/b" upgrade
ok 1 "$RC" "exit 1"
has "venvs: wol-relay=FAILED pock-sync=DONE" "$OUT" "per-venv verdicts"
has "restoring the freeze" "$OUT" "says it restores"
ok "fastapi==0.110.0" "$(head -1 "$TMP/b/st/wol-relay/installed")" "wol-relay back on its old versions"

echo "case C — unhealthy after upgrade: ROLLED BACK, health OK again"
setup "$TMP/c"; printf 'fastapi==9.9.9-BROKEN\n' > "$TMP/c/st/pock-sync/new"; run "$TMP/c" upgrade
ok 1 "$RC" "exit 1"
has "pock-sync=ROLLED-BACK" "$OUT" "pock-sync rolled back"
has "health after rollback: OK" "$OUT" "service healthy after rollback"
ok "fastapi==0.110.0" "$(head -1 "$TMP/c/st/pock-sync/installed")" "pock-sync on its old versions"
ok 2 "$(grep -c 'systemctl restart pock-sync' "$TMP/c/calls")" "restarted twice (upgrade + rollback)"

echo "case D — report: updates available ⇒ exit 2"
setup "$TMP/d"; run "$TMP/d" report
ok 2 "$RC" "exit 2"
has "outdated: 2" "$OUT" "counts outdated packages"
has "Python 3.13.5" "$OUT" "shows the python version"
has "VERDICT: updates available" "$OUT" "verdict"

echo "case E — report: up to date ⇒ exit 0"
setup "$TMP/e"; : > "$TMP/e/st/wol-relay/outdated"; : > "$TMP/e/st/pock-sync/outdated"; run "$TMP/e" report
ok 0 "$RC" "exit 0"
has "VERDICT: up to date" "$OUT" "verdict"

echo "case F — report: PyPI unreachable ⇒ exit 3, never green"
setup "$TMP/f"; touch "$TMP/f/st/wol-relay/pypi-down"; : > "$TMP/f/st/pock-sync/outdated"; run "$TMP/f" report
ok 3 "$RC" "exit 3"
has "could not measure" "$OUT" "verdict unmeasured"

echo "case G — unknown mode refused"
setup "$TMP/g"; run "$TMP/g" "upgrade --index-url x"
ok 64 "$RC" "exit 64"

[ "$fail" -eq 0 ] && echo "PASS" || { echo "FAILED"; exit 1; }
