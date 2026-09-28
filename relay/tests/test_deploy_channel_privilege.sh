#!/usr/bin/env bash
# test_deploy_channel_privilege.sh — the `deploy` key must not be root-equivalent.
#
# Scan finding F8 (2026-09-27): sudoers let `deploy` install systemd units it
# supplied itself on stdin, then daemon-reload + restart — i.e. an arbitrary
# ExecStart running as root. Units are now installed by the bootstrap scripts
# only (reviewed copy, admin gesture); the deploy channel carries content that
# runs as an unprivileged service user.
#
# A. sudoers: every `install` destination is on an allowlist of files run or
#    read by a NON-root user; nothing under /etc/systemd; no daemon-reload.
#    Adding a destination means editing this allowlist — a visible decision.
# B. every literal `sudo …` argv in dispatch.sh exists verbatim in sudoers
#    (a verb missing from sudoers fails silently on the VM, after the merge).
# C. dispatch.sh, run for real against a stub sudo: the apply-* routes work
#    WITHOUT any staged unit, never touch /etc/systemd, and the legacy
#    push-*-service routes drain stdin and stage nothing.
#
# Usage: bash relay/tests/test_deploy_channel_privilege.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUDOERS="$HERE/../scripts/sudoers.deploy"
DISPATCH="$HERE/../scripts/dispatch.sh"
pass=0; fail=0
ok()  { echo "  ok   $*"; pass=$((pass + 1)); }
ko()  { echo "  FAIL $*"; fail=$((fail + 1)); }

# Destination → why a non-root user is the one executing/reading it.
ALLOWED_DEST='
/opt/wol-relay/app.py
/opt/wol-relay/window
/etc/caddy/Caddyfile
/opt/home-watch/home-watch.sh
/opt/pock-sync/app.py
'
# app.py/window: read by uvicorn as `wol`. Caddyfile: interpreted by caddy
# running as `caddy` (no exec directive in the stock build). home-watch.sh: run
# as `homewatch` (unit User=). pock-sync app.py: run as `pock`.

# Prints every install destination found in a sudoers file, one per line.
install_dests() {
  awk '/\/usr\/bin\/install/ { sub(/,[[:space:]]*\\?[[:space:]]*$/, ""); print $NF }' "$1"
}

# Echoes the offending lines of a sudoers file (empty = compliant).
sudoers_violations() {
  local f="$1" d
  while read -r d; do
    [ -z "$d" ] && continue
    case "$d" in /etc/systemd/*) echo "unit install: $d"; continue ;; esac
    grep -qxF "$d" <<<"$ALLOWED_DEST" || echo "destination not allowlisted: $d"
  done < <(install_dests "$f")
  awk '!/^[[:space:]]*#/ && /daemon-reload/ { print "daemon-reload granted: " $0 }' "$f"
}

echo "A. sudoers install surface"
dests="$(install_dests "$SUDOERS")"
n=$(printf '%s\n' "$dests" | awk 'NF' | wc -l)
if [ "$n" -ge 5 ]; then ok "parser sees $n install destinations"; else ko "parser sees only $n destinations — broken parser would pass vacuously"; fi
v="$(sudoers_violations "$SUDOERS")"
if [ -z "$v" ]; then ok "no root-executed destination, no daemon-reload"; else ko "violations:"; printf '       %s\n' "$v"; fi
# Positive control: the predicate must fire on the pre-F8 shape.
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/bad" <<'EOF'
Cmnd_Alias X = \
  /usr/bin/install -m 0644 /tmp/wol-relay-staging/x.service /etc/systemd/system/x.service, \
  /usr/bin/install -m 0755 /tmp/wol-relay-staging/y /usr/local/bin/y
Cmnd_Alias Y = /bin/systemctl daemon-reload
EOF
v="$(sudoers_violations "$tmp/bad")"
if [ "$(printf '%s\n' "$v" | awk 'NF' | wc -l)" -eq 3 ]; then ok "control: pre-F8 shape flagged (3/3)"; else ko "control: expected 3 violations, got: $v"; fi

echo "B. dispatch.sh sudo verbs ⊂ sudoers"
checked=0
while IFS= read -r line; do
  argv="$(sed -E 's/^[[:space:]]*sudo //; s/"\$STAGING_DIR/\/tmp\/wol-relay-staging/g; s/"//g; s/[[:space:]]*(\|\|.*)?$//' <<<"$line")"
  case "$argv" in *'$'*) continue ;; esac   # argv built from a pinned case pattern
  checked=$((checked + 1))
  grep -qF -- "$argv" "$SUDOERS" || ko "not in sudoers: $argv"
done < <(grep -E '^[[:space:]]*sudo /' "$DISPATCH")
if [ "$checked" -ge 8 ]; then ok "$checked literal sudo argv checked"; else ko "only $checked sudo argv found — extraction broken"; fi

echo "C. dispatch.sh run against a stub sudo"
mkdir -p "$tmp/bin" "$tmp/staging"
cat > "$tmp/bin/sudo" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$tmp/sudo.log"
EOF
chmod +x "$tmp/bin/sudo"
sed "s#^STAGING_DIR=.*#STAGING_DIR=\"$tmp/staging\"#" "$DISPATCH" > "$tmp/dispatch.sh"
run() { : > "$tmp/sudo.log"; SSH_ORIGINAL_COMMAND="$1" PATH="$tmp/bin:$PATH" bash "$tmp/dispatch.sh" >"$tmp/out" 2>&1; }

rm -f "$tmp/staging"/*
echo 'print(1)' > "$tmp/staging/app.py"; echo ':443' > "$tmp/staging/Caddyfile"
if run apply; then ok "apply succeeds with app.py + Caddyfile only"; else ko "apply refused without a staged unit: $(cat "$tmp/out")"; fi
if grep -qE 'systemd/system|daemon-reload' "$tmp/sudo.log"; then ko "apply touched units: $(cat "$tmp/sudo.log")"; else ok "apply never touches /etc/systemd"; fi

rm -f "$tmp/staging"/*; echo 'echo hi' > "$tmp/staging/home-watch.sh"
if run apply-home-watch; then ok "apply-home-watch succeeds with the script only"; else ko "apply-home-watch refused: $(cat "$tmp/out")"; fi
if grep -qE 'systemd/system|daemon-reload' "$tmp/sudo.log"; then ko "apply-home-watch touched units"; else ok "apply-home-watch never touches /etc/systemd"; fi

rm -f "$tmp/staging"/*; echo 'x=1' > "$tmp/staging/pock-sync-app.py"
if run apply-pock-sync; then ok "apply-pock-sync succeeds with app.py only"; else ko "apply-pock-sync refused: $(cat "$tmp/out")"; fi
if grep -qE 'systemd/system|daemon-reload' "$tmp/sudo.log"; then ko "apply-pock-sync touched units"; else ok "apply-pock-sync never touches /etc/systemd"; fi

for r in push-service push-home-watch-service push-home-watch-timer push-pock-sync-service; do
  rm -f "$tmp/staging"/*
  if printf '[Service]\nExecStart=/bin/sh -c id\n' | run "$r" && [ -z "$(ls -A "$tmp/staging")" ]; then
    ok "$r: accepted for old clients, nothing staged"
  else
    ko "$r: still stages a unit ($(ls -A "$tmp/staging" | tr '\n' ' '))"
  fi
done

echo "== $pass ok, $fail fail"
[ "$fail" -eq 0 ]
