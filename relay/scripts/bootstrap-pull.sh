#!/usr/bin/env bash
# bootstrap-pull.sh — rerun bootstrap-wol-relay.sh from an APPROVED commit of main.
#
# Root-installed scripts on this VM (dispatch.sh, sudoers, units…) only change
# when the bootstrap is rerun. This is the remote way to do it: the home
# server's ROOT sends one commit id, this script fetches that commit from the
# public repo itself and reruns the bootstrap from it, keeping installed keys.
#
# PRIVILEGE — who can reach this, and what they can make it do:
#   - Only the `relayboot` account (forced command, its own sudoers file, no
#     argument). The `deploy` user has NO sudo right on this script: the deploy
#     key, which also lives in a less trusted sandbox, cannot get here.
#   - Input is ONE line on stdin: 40 lowercase hex chars. Nothing received from
#     the caller is executed; the code comes from GitHub and git checks it
#     against the id.
#   - The commit must be in the history of main, must descend from FLOOR, and
#     must descend from the commit installed last: NEVER BACKWARDS. An older
#     commit would reinstall an older sudoers — a downgrade to a known hole.
#     To undo a bad commit, merge a fix; do not point back.
#   - Nothing is installed unless the whole tree passes the preflight.
#
# Detached once verified (an SSH drop must not leave half a bootstrap); the
# outcome is read with `ssh wol-relay-deploy bootstrap-log`. No automatic
# rollback: a bootstrap that breaks the relay is repaired by hand (IAP).

set -uo pipefail

# Oldest commit this script accepts (the main it was written against).
FLOOR_DEFAULT="39f3d218e577b1aa2128a41b80e2f94d56d5dc89"

# Test seams. sudo resets the environment (sudoers.relayboot), so none of
# these can be set through the relayboot account.
STATE="${BP_STATE:-/var/lib/relay-bootstrap}"
LOG_DIR="${BP_LOG_DIR:-/var/log/relay-bootstrap}"
REPO_URL="${BP_REPO_URL:-https://github.com/Jqh63/plex-jqh-omv.git}"
FLOOR="${BP_FLOOR:-$FLOOR_DEFAULT}"
HEALTH_CMD="${BP_HEALTH_CMD:-curl -fsS --max-time 10 http://127.0.0.1:8000/health}"
SSHD_T="${BP_SSHD_T:-/usr/sbin/sshd -t}"
VISUDO="${BP_VISUDO:-visudo}"
UPGRADE_LOCK="${BP_UPGRADE_LOCK:-/home/deploy/upgrade-logs/.lock}"
KEEP=10

GIT_DIR_BARE="$STATE/repo.git"
INSTALLED="$STATE/installed-sha"
# https only against the real repo; the test bench points at a local path.
PROTO=(-c protocol.allow=never -c protocol.https.allow=always)
[ -z "${BP_REPO_URL:-}" ] || PROTO=(-c protocol.allow=never -c protocol.file.allow=always)
g() { GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 \
        git "${PROTO[@]}" -c core.hooksPath=/dev/null -c fetch.fsckObjects=true \
        --git-dir="$GIT_DIR_BARE" "$@"; }
refuse() { echo "[bootstrap-pull] REFUSED — $*" >&2; exit 65; }
ts() { date '+%F %T %Z'; }

if [ "$(id -u)" -ne 0 ] && [ -z "${BP_ALLOW_NONROOT:-}" ]; then
  echo "[bootstrap-pull] must run as root" >&2; exit 1
fi

# --- run phase (detached child; never reachable through sudoers: it pins the
# argument-less form) ---------------------------------------------------------
run_phase() { # <sha> <workdir> <logfile>
  local sha="$1" work="$2" log="$3" rc=0 health="KO" sshd="KO" verdict=1
  exec >>"$log" 2>&1
  echo "=== RELAY BOOTSTRAP — $sha — start $(ts) ==="
  bash "$work/relay/scripts/bootstrap-wol-relay.sh" --keep-keys || rc=$?
  echo "--- bootstrap rc=$rc"
  $HEALTH_CMD >/dev/null 2>&1 && health="OK"
  $SSHD_T >/dev/null 2>&1 && sshd="OK"
  echo
  echo "=== SUMMARY $(ts) ==="
  echo "commit       : $sha"
  echo "bootstrap rc : $rc"
  echo "relay health : $health"
  echo "sshd -t      : $sshd"
  if [ "$rc" -eq 0 ] && [ "$health" = "OK" ] && [ "$sshd" = "OK" ]; then
    # Written LAST and only on full success: a run cut short leaves the
    # previous id, so the next call replays instead of answering "installed".
    printf '%s\n' "$sha" > "$INSTALLED.tmp" && chmod 0644 "$INSTALLED.tmp" \
      && mv "$INSTALLED.tmp" "$INSTALLED" && verdict=0
  fi
  if [ "$verdict" -eq 0 ]; then echo "VERDICT: DONE"
  else echo "VERDICT: FAILED — read the output above; installed id left unchanged"; fi
  rm -rf "$work"
  ls -1t "$LOG_DIR"/*.log 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
  exit "$verdict"
}
if [ "${1:-}" = "--run" ]; then run_phase "$2" "$3" "$4"; fi
[ $# -eq 0 ] || { echo "[bootstrap-pull] takes no argument" >&2; exit 64; }

# --- verify phase (synchronous: the caller gets the refusal and its reason) --
IFS= read -r SHA || true
[[ "${SHA:-}" =~ ^[0-9a-f]{40}$ ]] || refuse "expected one line of 40 lowercase hex chars on stdin"

install -d -m 0755 "$STATE" && install -d -m 0755 "$LOG_DIR" \
  || { echo "[bootstrap-pull] cannot create $STATE / $LOG_DIR" >&2; exit 1; }
exec 9>"$STATE/.lock"
flock -n 9 || { echo "[bootstrap-pull] another bootstrap is running — see bootstrap-log" >&2; exit 75; }
# Not while apt holds the VM (route `upgrade`): the bootstrap may apt-get too.
if [ -e "$UPGRADE_LOCK" ] && ! flock -n "$UPGRADE_LOCK" true 2>/dev/null; then
  echo "[bootstrap-pull] an OS upgrade is running — retry after upgrade-log says finished" >&2; exit 75
fi

cur=""; [ -r "$INSTALLED" ] && cur="$(head -c 64 "$INSTALLED" | tr -d '[:space:]')"
if [ "$cur" = "$SHA" ]; then
  echo "[bootstrap-pull] ALREADY INSTALLED — $SHA (nothing done)"; exit 0
fi

[ -d "$GIT_DIR_BARE" ] || git init -q --bare "$GIT_DIR_BARE" \
  || { echo "[bootstrap-pull] cannot create $GIT_DIR_BARE" >&2; exit 1; }
g fetch --quiet --no-tags "$REPO_URL" '+refs/heads/main:refs/heads/main' \
  || { echo "[bootstrap-pull] UNMEASURED — git fetch of main failed" >&2; exit 69; }

g cat-file -e "$SHA^{commit}" 2>/dev/null   || refuse "$SHA is not a commit of main"
g merge-base --is-ancestor "$SHA" main      || refuse "$SHA is not in the history of main"
g merge-base --is-ancestor "$FLOOR" "$SHA"  || refuse "$SHA is older than the floor ${FLOOR:0:7}"
if [[ "$cur" =~ ^[0-9a-f]{40}$ ]]; then
  g merge-base --is-ancestor "$cur" "$SHA" \
    || refuse "$SHA does not descend from the installed ${cur:0:7} (never backwards)"
fi

# Regular files only: a symlink in the tree would make `install` copy whatever
# it points at on this VM.
odd="$(g ls-tree -r "$SHA" -- relay | awk '$1 != "100644" && $1 != "100755"' | head -3)"
[ -z "$odd" ] || refuse "non-regular entry under relay/: $(printf '%s' "$odd" | tr '\n\t' '; ')"

WORK="$(mktemp -d "$STATE/tree.XXXXXX")" || { echo "[bootstrap-pull] mktemp failed" >&2; exit 1; }
if ! g archive "$SHA" relay | tar -x -C "$WORK"; then rm -rf "$WORK"; refuse "extraction of relay/ failed"; fi
preflight=""
for f in "$WORK"/relay/scripts/*.sh; do
  bash -n "$f" 2>/dev/null || preflight="$preflight syntax:$(basename "$f")"
done
for f in "$WORK"/relay/scripts/sudoers.*; do
  $VISUDO -c -f "$f" >/dev/null 2>&1 || preflight="$preflight sudoers:$(basename "$f")"
done
[ -f "$WORK/relay/scripts/bootstrap-wol-relay.sh" ] || preflight="$preflight missing:bootstrap-wol-relay.sh"
if [ -n "$preflight" ]; then rm -rf "$WORK"; refuse "preflight failed, nothing installed:$preflight"; fi

LOG="$LOG_DIR/$(date +%Y%m%d-%H%M%S).log"
: > "$LOG" && chmod 0644 "$LOG"
if [ -n "${BP_FOREGROUND:-}" ]; then
  ( run_phase "$SHA" "$WORK" "$LOG" ); exit $?
fi
# The child inherits fd 9: the lock lives as long as the bootstrap does.
setsid nohup "$0" --run "$SHA" "$WORK" "$LOG" </dev/null >/dev/null 2>&1 &
echo "[bootstrap-pull] ACCEPTED — $SHA (was ${cur:-unknown}); started detached."
echo "[bootstrap-pull] Read the outcome: ssh wol-relay-deploy bootstrap-log"
