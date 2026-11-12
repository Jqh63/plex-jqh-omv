#!/usr/bin/env bash
# test_staging_integrity.sh — what sits in the staging dir is what gets installed.
#
# Three holes closed here (knowledge-base script review, 2026-11-12):
#   A. apply-* left the staged files behind, so a later bare `apply` installed
#      a leftover — possibly truncated — from an older deploy. Staging is now
#      emptied whatever the outcome.
#   B. apply installed the Caddyfile, and a refused reload left the bad file in
#      /etc/caddy for the next restart. The previous file is now put back.
#   C. deploy.sh never compared the "OK (N bytes)" answer to what it sent.
#   D. scripts-manifest: sha256 of the root-installed scripts, the only way to
#      tell whether a merged dispatch.sh has been bootstrapped.
# dispatch.sh runs for real against a stub sudo; deploy.sh against a stub ssh.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DISPATCH="$HERE/../scripts/dispatch.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0
ok()  { if [ "$1" = "$2" ]; then echo "  ok   $3"; else echo "  FAIL $3 (want=$1 got=$2)"; fail=1; fi }
has() { if printf '%s' "$2" | grep -- "$1" >/dev/null; then echo "  ok   $3"; else echo "  FAIL $3"; fail=1; fi }

mkdir -p "$TMP/bin" "$TMP/staging" "$TMP/live" "$TMP/scripts"
# Stub sudo: logs argv; on `install … SRC DST` also logs SRC's content hash, so
# the bench can tell WHICH Caddyfile was installed. Fails the reload on demand.
cat > "$TMP/bin/sudo" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$TMP/sudo.log"
case "\$*" in
  /usr/bin/install*) src="\${*: -2:1}"; echo "installed \$(basename "\$src") \$(sha256sum < "\$src" | cut -c1-12)" >> "$TMP/sudo.log" ;;
  *"reload caddy") [ -z "\${FAIL_RELOAD:-}" ] || exit 1 ;;
esac
exit 0
EOF
chmod +x "$TMP/bin/sudo"
sed -e "s#^STAGING_DIR=.*#STAGING_DIR=\"$TMP/staging\"#" \
    -e "s#^CADDYFILE_LIVE=.*#CADDYFILE_LIVE=\"$TMP/live/Caddyfile\"#" \
    -e "s#^SCRIPTS_DIR=.*#SCRIPTS_DIR=\"$TMP/scripts\"#" "$DISPATCH" > "$TMP/dispatch.sh"
d() { SSH_ORIGINAL_COMMAND="$1" PATH="$TMP/bin:$PATH" bash "$TMP/dispatch.sh" ""; }
h() { sha256sum | cut -c1-12; }

echo "case A — HEALTHY deploy, then staging is empty and a bare apply refuses"
echo "old caddy" > "$TMP/live/Caddyfile"; : > "$TMP/sudo.log"
echo "app v2" | d push-app >/dev/null; echo "new caddy" | d push-caddyfile >/dev/null
OUT="$(d apply 2>&1)"; RC=$?
ok 0 "$RC" "apply exit 0"
has "installed Caddyfile $(echo "new caddy" | h)" "$(cat "$TMP/sudo.log")" "the pushed Caddyfile was installed"
ok "" "$(ls "$TMP/staging")" "staging empty after apply"
OUT="$(d apply 2>&1)"; RC=$?
ok 1 "$RC" "second apply without a push refuses"

echo "case A' — apply-home-watch and apply-pock-sync empty their staged file too"
echo "hw" | d push-home-watch >/dev/null; d apply-home-watch >/dev/null 2>&1; ok 0 "$?" "apply-home-watch exit 0"
echo "ps" | d push-pock-sync-app >/dev/null; d apply-pock-sync >/dev/null 2>&1; ok 0 "$?" "apply-pock-sync exit 0"
ok "" "$(ls "$TMP/staging")" "staging empty"

echo "case B — caddy refuses the reload: previous Caddyfile put back, loud failure"
echo "old caddy" > "$TMP/live/Caddyfile"; : > "$TMP/sudo.log"
echo "app v3" | d push-app >/dev/null; echo "broken caddy" | d push-caddyfile >/dev/null
OUT="$(FAIL_RELOAD=1 d apply 2>&1)"; RC=$?
ok 1 "$RC" "apply exit 1"
has "rolled back" "$OUT" "says it rolled back"
ok "installed Caddyfile $(echo "old caddy" | h)" "$(grep '^installed Caddyfile' "$TMP/sudo.log" | tail -1)" "last Caddyfile installed = the previous one"
ok "" "$(ls "$TMP/staging")" "staging empty after a failed apply"

echo "case D — scripts-manifest lists the installed scripts with their sha256"
echo "one" > "$TMP/scripts/dispatch.sh"; echo "two" > "$TMP/scripts/venv-run.sh"
OUT="$(d scripts-manifest 2>&1)"; RC=$?
ok 0 "$RC" "exit 0"
has "^$(sha256sum < "$TMP/scripts/dispatch.sh" | cut -d' ' -f1)  dispatch.sh$" "$OUT" "dispatch.sh hashed"
ok 2 "$(printf '%s\n' "$OUT" | grep -c '\.sh$')" "2 scripts listed"

echo "case C — deploy.sh refuses a push whose byte count differs from what it sent"
mkdir -p "$TMP/c/relay/scripts"
cp "$HERE/../scripts/deploy.sh" "$TMP/c/relay/scripts/"
: > "$TMP/c/relay/scripts/requirements-wol-relay.lock"; : > "$TMP/c/relay/scripts/requirements-pock-sync.lock"
echo "app" > "$TMP/c/relay/app.py"; echo "caddy" > "$TMP/c/relay/Caddyfile"
cat > "$TMP/bin/ssh" <<'EOF'
#!/usr/bin/env bash
shift; echo "$*" >> "$SSH_CALLS"
case "$1" in
  push-app|push-caddyfile) n="$(wc -c)"; [ "$1" = "${SHORT:-}" ] && n=$((n - 1)); echo "[$1] OK ($n bytes)" ;;
  push-*) cat > /dev/null ;;
esac
exit 0
EOF
chmod +x "$TMP/bin/ssh"
dep() { : > "$TMP/calls"; OUT="$(DEPLOY_ALLOW_UNMERGED=1 SSH_CALLS="$TMP/calls" PATH="$TMP/bin:$PATH" bash "$TMP/c/relay/scripts/deploy.sh" 2>&1 </dev/null)"; RC=$?; }
dep
ok 0 "$RC" "HEALTHY: matching byte counts deploy"
has "^apply$" "$(cat "$TMP/calls")" "apply reached"
SHORT=push-caddyfile dep
ok 1 "$RC" "short Caddyfile: exit 1"
has "push-caddyfile" "$OUT" "names the route"
if grep -x apply "$TMP/calls" >/dev/null; then echo "  FAIL apply must not run"; fail=1; else echo "  ok   apply not run"; fi

echo "case E — scripts-drift.sh: in sync, drift, and an old dispatch.sh is 'unmeasured'"
G() { git -c user.name=bench -c user.email=bench@example.invalid -c init.defaultBranch=main "$@"; }
mkdir -p "$TMP/e/relay/scripts"; cp "$HERE/../scripts/scripts-drift.sh" "$TMP/e/relay/scripts/"
echo "v1" > "$TMP/e/relay/scripts/dispatch.sh"
G -C "$TMP/e" init -q; G -C "$TMP/e" add -A; G -C "$TMP/e" commit -q -m init
cat > "$TMP/bin/ssh" <<'EOF'
#!/usr/bin/env bash
[ -n "${VM_OLD:-}" ] && { echo "dispatch.sh: unknown command 'scripts-manifest'" >&2; exit 64; }
echo "$(printf '%s\n' "$VM_CONTENT" | sha256sum | cut -d' ' -f1)  dispatch.sh"
EOF
sd() { OUT="$(SCRIPTS_DRIFT_REF=HEAD PATH="$TMP/bin:$PATH" bash "$TMP/e/relay/scripts/scripts-drift.sh" 2>&1)"; RC=$?; }
VM_CONTENT=v1 sd; ok 0 "$RC" "HEALTHY: same content => 0"
VM_CONTENT=v0 sd; ok 2 "$RC" "different content => 2"; has "DRIFT dispatch.sh" "$OUT" "names the script"
VM_OLD=1 VM_CONTENT=v1 sd; ok 3 "$RC" "route unknown => 3, not a green"

[ "$fail" -eq 0 ] && echo "ALL CASES PASS" || { echo "SOME CASES FAIL"; exit 1; }
