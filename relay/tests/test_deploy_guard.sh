#!/usr/bin/env bash
# test_deploy_guard.sh — bench for the origin/main guard of relay/scripts/deploy.sh.
#
# deploy.sh pushes whatever the working tree holds. Run from an unmerged
# branch, a stale clone or an edited file, it used to ship unreviewed or
# outdated code to the VM without a word. Throwaway git repos + a stub ssh;
# the healthy states come first (a guard that refuses a sane clone gets
# bypassed for good).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../scripts/deploy.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0
ok()   { if [ "$1" = "$2" ]; then echo "  ok   $3"; else echo "  FAIL $3 (want=$1 got=$2)"; fail=1; fi }
has()  { if printf '%s' "$2" | grep -- "$1" >/dev/null; then echo "  ok   $3"; else echo "  FAIL $3"; fail=1; fi }
none() { if [ -s "$1" ]; then echo "  FAIL $2 (ssh calls: $(tr '\n' ' ' < "$1"))"; fail=1; else echo "  ok   $2"; fi }

G() { git -c user.name=bench -c user.email=bench@example.invalid -c init.defaultBranch=main "$@"; }

# Stub ssh: records the route, always succeeds (venvs in sync), answers the
# byte count deploy.sh checks. Reads stdin on push-* only: the other routes
# get none and would block on an open stdin.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/ssh" <<'EOF'
#!/usr/bin/env bash
shift  # drop the alias
echo "$*" >> "$SSH_CALLS"
case "$1" in
  push-app|push-caddyfile) echo "[$1] OK ($(wc -c | tr -d ' ') bytes)" ;;
  push-*) cat > /dev/null ;;
esac
exit 0
EOF
chmod +x "$TMP/bin/ssh"

# A fresh origin + clone holding the deploy.sh under test and the files it pushes.
mkrepo() {  # $1 = case dir → $1/origin.git, $1/clone
  mkdir -p "$1"
  G init -q --bare "$1/origin.git"
  G clone -q "$1/origin.git" "$1/clone" 2>/dev/null
  mkdir -p "$1/clone/relay/scripts" "$1/clone/other"
  cp "$SCRIPT" "$1/clone/relay/scripts/deploy.sh"
  : > "$1/clone/relay/scripts/requirements-wol-relay.lock"
  : > "$1/clone/relay/scripts/requirements-pock-sync.lock"
  echo "app v1" > "$1/clone/relay/app.py"
  echo "caddy v1" > "$1/clone/relay/Caddyfile"
  echo "x" > "$1/clone/other/file"
  G -C "$1/clone" add -A
  G -C "$1/clone" commit -q -m init
  G -C "$1/clone" push -q origin HEAD:main
}

run() {  # $1 = case dir; extra env via caller
  : > "$1/calls"
  OUT="$(SSH_CALLS="$1/calls" PATH="$TMP/bin:$PATH" bash "$1/clone/relay/scripts/deploy.sh" 2>&1)"; RC=$?
}

echo "case A — HEALTHY: clone at origin/main, clean"
mkrepo "$TMP/a"; run "$TMP/a"
ok 0 "$RC" "exit 0"
has "^apply$" "$(cat "$TMP/a/calls")" "apply reached"

echo "case B — HEALTHY: detached HEAD at origin/main (worktree)"
mkrepo "$TMP/b"; G -C "$TMP/b/clone" checkout -q --detach; run "$TMP/b"
ok 0 "$RC" "exit 0"

echo "case C — HEALTHY: edits outside relay/ + untracked file inside"
mkrepo "$TMP/c"; echo "y" >> "$TMP/c/clone/other/file"; echo "n" > "$TMP/c/clone/relay/notes.txt"; run "$TMP/c"
ok 0 "$RC" "exit 0"

echo "case D — unmerged commit on a branch"
mkrepo "$TMP/d"
G -C "$TMP/d/clone" checkout -q -b feat/x
echo "app v2" > "$TMP/d/clone/relay/app.py"; G -C "$TMP/d/clone" commit -q -am wip
run "$TMP/d"
ok 4 "$RC" "exit 4"
has "origin/main" "$OUT" "names origin/main"
none "$TMP/d/calls" "nothing sent to the VM"

echo "case E — clone behind origin/main"
mkrepo "$TMP/e"
G clone -q "$TMP/e/origin.git" "$TMP/e/other-clone"
echo "app v2" > "$TMP/e/other-clone/relay/app.py"
G -C "$TMP/e/other-clone" commit -q -am newer; G -C "$TMP/e/other-clone" push -q origin HEAD:main
run "$TMP/e"
ok 4 "$RC" "exit 4"
none "$TMP/e/calls" "nothing sent to the VM"

echo "case F — tracked file edited under relay/ (unstaged, then staged)"
mkrepo "$TMP/f"; echo "app hack" > "$TMP/f/clone/relay/app.py"; run "$TMP/f"
ok 4 "$RC" "exit 4 (unstaged)"
has "relay/app.py" "$OUT" "names the edited file"
none "$TMP/f/calls" "nothing sent to the VM"
G -C "$TMP/f/clone" add relay/app.py; run "$TMP/f"
ok 4 "$RC" "exit 4 (staged)"

echo "case G — fetch impossible: unmeasured is a refusal"
mkrepo "$TMP/g"; G -C "$TMP/g/clone" remote set-url origin "$TMP/g/gone.git"; run "$TMP/g"
ok 4 "$RC" "exit 4"
has "fetch" "$OUT" "says the fetch failed"
none "$TMP/g/calls" "nothing sent to the VM"

echo "case H — escape hatch: DEPLOY_ALLOW_UNMERGED=1 deploys and says so"
: > "$TMP/d/calls"
OUT="$(DEPLOY_ALLOW_UNMERGED=1 SSH_CALLS="$TMP/d/calls" PATH="$TMP/bin:$PATH" bash "$TMP/d/clone/relay/scripts/deploy.sh" 2>&1)"; RC=$?
ok 0 "$RC" "exit 0"
has "DEPLOY_ALLOW_UNMERGED" "$OUT" "warning printed"
has "^apply$" "$(cat "$TMP/d/calls")" "apply reached"

[ "$fail" -eq 0 ] && echo "ALL CASES PASS" || { echo "SOME CASES FAIL"; exit 1; }
