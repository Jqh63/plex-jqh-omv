#!/usr/bin/env bash
# test_bootstrap_pull.sh — the remote bootstrap rerun cannot be abused.
#
# bootstrap-pull.sh reruns the bootstrap as ROOT from a commit id sent by the
# home server. What must hold, each proven against a real local git repo:
#   A. input grammar: anything but 40 lowercase hex chars is refused.
#   B. the commit must be in the history of main, at or after the floor.
#   C. never backwards: a commit older than the installed one is refused.
#   D. preflight: a symlink in the tree, a script that does not parse or a
#      sudoers that visudo rejects installs NOTHING.
#   E. success runs the bootstrap of THAT commit with --keep-keys and records
#      the id; a failed bootstrap or health check does not record it.
#   F. replaying the installed id does nothing.
#   G. privilege boundary: `deploy` has no right on the script, relayboot has
#      one argument-less verb, dispatch.sh never calls it, and the relayboot
#      key line is restricted.
#
# Usage: bash relay/tests/test_bootstrap_pull.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
S="$HERE/../scripts"
PULL="$S/bootstrap-pull.sh"
pass=0; fail=0
ok()  { echo "  ok   $*"; pass=$((pass + 1)); }
ko()  { echo "  FAIL $*"; fail=$((fail + 1)); }
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

# --- fixture: an "origin" whose main has 3 commits + one side branch ---------
O="$T/origin"; mkdir -p "$O"
gi() { git -C "$O" -c user.name=t -c user.email=t@example.com -c commit.gpgsign=false "$@"; }
git init -q -b main "$O"
mk_commit() { # <label> : writes relay/ with a stub bootstrap that records the label
  mkdir -p "$O/relay/scripts"
  cat > "$O/relay/scripts/bootstrap-wol-relay.sh" <<STUB
#!/usr/bin/env bash
echo "stub-bootstrap $1 args=\$*" >> "\${BP_TEST_MARK:?}"
[ -z "\${BP_TEST_BOOT_FAIL:-}" ] || exit 7
STUB
  printf 'x ALL=(root) NOPASSWD: /bin/true\n' > "$O/relay/scripts/sudoers.deploy"
  gi add -A && gi commit -q -m "$1" && gi rev-parse HEAD
}
C1="$(mk_commit c1)"; C2="$(mk_commit c2)"; C3="$(mk_commit c3)"
gi checkout -q -b side "$C2"; echo side > "$O/relay/side"; gi add -A; gi commit -q -m side
SIDE="$(gi rev-parse HEAD)"; gi checkout -q main
# Bad trees, each as a further commit of main (C4 symlink, C5 syntax, C6 sudoers).
ln -s /etc/passwd "$O/relay/link"; gi add -A; gi commit -q -m symlink; C4="$(gi rev-parse HEAD)"
git -C "$O" rm -q relay/link; echo 'if then' > "$O/relay/scripts/broken.sh"; gi add -A; gi commit -q -m syntax; C5="$(gi rev-parse HEAD)"
git -C "$O" rm -q relay/scripts/broken.sh; echo 'BAD' > "$O/relay/scripts/sudoers.deploy"; gi add -A; gi commit -q -m sudoers; C6="$(gi rev-parse HEAD)"
C7="$(mk_commit c7)"

# visudo stub: rejects a file whose content is BAD (real visudo may be absent).
cat > "$T/visudo" <<'V'
#!/usr/bin/env bash
f="${*: -1}"; ! grep -qx 'BAD' "$f"
V
chmod +x "$T/visudo"

new_state() { ST="$T/st.$RANDOM$RANDOM"; mkdir -p "$ST"; MARK="$ST/mark"; : > "$MARK"; }
run() { # <stdin> [extra env…] → OUT, RC
  local in="$1"; shift
  OUT="$(printf '%s' "$in" | env BP_ALLOW_NONROOT=1 BP_FOREGROUND=1 BP_STATE="$ST/state" \
        BP_LOG_DIR="$ST/logs" BP_REPO_URL="$O" BP_FLOOR="$C2" BP_HEALTH_CMD=true BP_SSHD_T=true \
        BP_VISUDO="$T/visudo" BP_UPGRADE_LOCK="$ST/none" BP_TEST_MARK="$MARK" "$@" bash "$PULL" 2>&1)"; RC=$?
}
installed() { cat "$ST/state/installed-sha" 2>/dev/null || true; }
refused() { # <label> <needle>
  if [ "$RC" -eq 65 ] && [[ "$OUT" == *"$2"* ]] && [ ! -s "$MARK" ] && [ -z "$(installed)" ]; then ok "$1"
  else ko "$1 (rc=$RC out=$OUT mark=$(cat "$MARK"))"; fi
}

echo "case A — input grammar"
new_state
run "";                         refused "empty input refused" "40 lowercase hex"
run "${C3:0:39}"$'\n';          refused "39 chars refused" "40 lowercase hex"
run "$(printf '%s' "$C3" | tr a-f A-F)"$'\n'; refused "uppercase refused" "40 lowercase hex"
run "$C3; id"$'\n';             refused "trailing junk refused" "40 lowercase hex"
run "main"$'\n';                refused "ref name refused" "40 lowercase hex"

echo "case B — history of main, floor"
new_state
run "$SIDE"$'\n';               refused "commit of another branch refused" "not"
run "0123456789abcdef0123456789abcdef01234567"$'\n'; refused "unknown commit refused" "not a commit"
run "$C1"$'\n';                 refused "commit older than the floor refused" "older than the floor"
# The object is already in the VM's repo (left by anything): only the ancestry
# check stands between a non-main commit and root.
new_state; git init -q --bare "$ST/state/repo.git"
git --git-dir="$ST/state/repo.git" fetch -q "$O" side:refs/heads/elsewhere
run "$SIDE"$'\n';               refused "non-main commit already present locally refused" "not in the history of main"

echo "case E — success records the id, runs THAT commit's bootstrap with --keep-keys"
run "$C3"$'\n'
if [ "$RC" -eq 0 ] && [ "$(installed)" = "$C3" ] && grep -qx 'stub-bootstrap c3 args=--keep-keys' "$MARK"; then ok "c3 installed"
else ko "c3 install (rc=$RC out=$OUT mark=$(cat "$MARK"))"; fi
if grep -q '^VERDICT: DONE' "$ST"/logs/*.log; then ok "log carries VERDICT: DONE"; else ko "no DONE verdict in the log"; fi
[ -z "$(ls -d "$ST"/state/tree.* 2>/dev/null)" ] && ok "work tree removed" || ko "work tree left behind"

echo "case F — replaying the installed id does nothing"
: > "$MARK"; run "$C3"$'\n'
if [ "$RC" -eq 0 ] && [[ "$OUT" == *"ALREADY INSTALLED"* ]] && [ ! -s "$MARK" ]; then ok "replay is a no-op"
else ko "replay (rc=$RC out=$OUT)"; fi

echo "case C — never backwards"
run "$C2"$'\n'
if [ "$RC" -eq 65 ] && [[ "$OUT" == *"never backwards"* ]] && [ ! -s "$MARK" ] && [ "$(installed)" = "$C3" ]; then ok "older commit refused, installed id unchanged"
else ko "downgrade (rc=$RC out=$OUT)"; fi

echo "case D — preflight: nothing installed"
for pair in "$C4:non-regular" "$C5:syntax:broken.sh" "$C6:sudoers:sudoers.deploy"; do
  : > "$MARK"; run "${pair%%:*}"$'\n'
  if [ "$RC" -eq 65 ] && [[ "$OUT" == *"${pair#*:}"* ]] && [ ! -s "$MARK" ] && [ "$(installed)" = "$C3" ]; then ok "refused: ${pair#*:}"
  else ko "preflight ${pair#*:} (rc=$RC out=$OUT)"; fi
done

echo "case E' — a failed run does not record the id"
: > "$MARK"; run "$C7"$'\n' BP_TEST_BOOT_FAIL=1
if [ "$RC" -ne 0 ] && [ "$(installed)" = "$C3" ] && grep -q '^VERDICT: FAILED' "$(ls -1t "$ST"/logs/*.log | head -1)"; then ok "bootstrap rc≠0 ⇒ FAILED, id unchanged"
else ko "failed bootstrap (rc=$RC installed=$(installed))"; fi
: > "$MARK"; run "$C7"$'\n' BP_HEALTH_CMD=false
if [ "$RC" -ne 0 ] && [ "$(installed)" = "$C3" ]; then ok "health KO ⇒ id unchanged"; else ko "health KO (rc=$RC)"; fi
: > "$MARK"; run "$C7"$'\n' BP_SSHD_T=false
if [ "$RC" -ne 0 ] && [ "$(installed)" = "$C3" ]; then ok "sshd -t KO ⇒ id unchanged"; else ko "sshd KO (rc=$RC)"; fi
: > "$MARK"; run "$C7"$'\n'
if [ "$RC" -eq 0 ] && [ "$(installed)" = "$C7" ]; then ok "the same id replays after a failure, then lands"; else ko "replay after failure (rc=$RC)"; fi

echo "case H — arguments and concurrency"
OUT="$(env BP_ALLOW_NONROOT=1 bash "$PULL" --anything 2>&1 </dev/null)"; RC=$?
[ "$RC" -eq 64 ] && ok "a free argument is refused" || ko "free argument (rc=$RC out=$OUT)"
OUT="$(echo "$C7" | bash "$PULL" 2>&1)"; RC=$?
if [ "$(id -u)" -ne 0 ]; then [ "$RC" -eq 1 ] && ok "refuses to run unprivileged" || ko "non-root (rc=$RC)"; fi
new_state; mkdir -p "$ST/state"
( exec 9>"$ST/state/.lock"; flock 9; sleep 3 ) & sleep 0.5
run "$C3"$'\n'; wait
[ "$RC" -eq 75 ] && [ ! -s "$MARK" ] && ok "a second run is refused while one holds the lock" || ko "lock (rc=$RC out=$OUT)"

echo "case G — privilege boundary"
grep -q 'bootstrap-pull\|relayboot' "$S/sudoers.deploy" && ko "sudoers.deploy mentions bootstrap-pull/relayboot" || ok "sudoers.deploy grants nothing on bootstrap-pull"
rules="$(grep -vE '^\s*(#|$|Defaults)' "$S/sudoers.relayboot")"
[ "$rules" = 'relayboot ALL=(root) NOPASSWD: /opt/wol-relay/scripts/bootstrap-pull.sh ""' ] \
  && ok "sudoers.relayboot: one argument-less verb" || ko "sudoers.relayboot rules: $rules"
grep -qx 'Defaults:relayboot env_reset, !setenv' "$S/sudoers.relayboot" && ok "sudoers.relayboot resets the environment" || ko "env_reset missing"
if command -v visudo >/dev/null 2>&1; then
  visudo -c -f "$S/sudoers.relayboot" >/dev/null 2>&1 && ok "visudo accepts sudoers.relayboot" || ko "visudo rejects sudoers.relayboot"
else echo "  skip visudo absent"; fi
code="$(grep -vE '^\s*#' "$S/dispatch.sh")"
[[ "$code" == *bootstrap-pull.sh* || "$code" == *relayboot* ]] && ko "dispatch.sh reaches bootstrap-pull" || ok "dispatch.sh never calls bootstrap-pull"
line="$(BOOTSTRAP_LIB_ONLY=1 bash -c 'source "$1"; render_relayboot_auth "ssh-ed25519 AAAAjobs"' _ "$S/bootstrap-wol-relay.sh")"
[ "$line" = 'command="sudo -n /opt/wol-relay/scripts/bootstrap-pull.sh",restrict ssh-ed25519 AAAAjobs' ] \
  && ok "relayboot key line: forced command + restrict" || ko "relayboot line: $line"
AK="$T/ak"; printf '%s\n' 'command="/opt/wol-relay/scripts/dispatch.sh",no-pty ssh-ed25519 AAAAdeploy' \
  'command="/opt/wol-relay/scripts/dispatch.sh jobs",no-pty ssh-ed25519 AAAAjobs' > "$AK"
got="$(BOOTSTRAP_LIB_ONLY=1 bash -c 'source "$1"; existing_deploy_key "$2"' _ "$S/bootstrap-wol-relay.sh" "$AK")"
[ "$got" = "ssh-ed25519 AAAAdeploy" ] && ok "--keep-keys reads the deploy key, not the jobs key" || ko "existing_deploy_key: $got"

echo
echo "bootstrap-pull: $pass ok, $fail failed"
[ "$fail" -eq 0 ]
