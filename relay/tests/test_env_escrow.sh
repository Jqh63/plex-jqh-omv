#!/usr/bin/env bash
# test_env_escrow.sh — env-escrow.sh must list without leaking and dump only
# ciphertext.
#
# A. manifest: present/absent per escrowed file, never a VALUE (control: the
#    value is in the fixture), and a non-world-readable file nobody listed
#    shows up — the reason the manifest exists.
# B. dump preconditions: no age, no recipients, recipients that are not age
#    public keys, nothing to dump ⇒ refusal with EMPTY stdout (a half dump
#    stored as a backup is worse than none).
# C. dump: the tar holds exactly the present files; a failing age is a failure.
# D. real age round-trip (CI installs age): decrypts to the fixture, and the
#    plaintext value is absent from the ciphertext.
#
# Usage: bash relay/tests/test_env_escrow.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/env-escrow.sh"
pass=0; fail=0
ok()  { echo "  ok   $*"; pass=$((pass + 1)); }
ko()  { echo "  FAIL $*"; fail=$((fail + 1)); }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
P="$tmp/root"; SECRET='s3cr3t-VALUE-must-not-leak'
mkdir -p "$P/etc/caddy" "$P/home/omvtunnel/.ssh" "$tmp/bin"
printf 'WOL_TOKEN=%s\n' "$SECRET" > "$P/etc/wol-relay.env";   chmod 640 "$P/etc/wol-relay.env"
printf 'password %s\n' "$SECRET"  > "$P/etc/msmtprc";         chmod 600 "$P/etc/msmtprc"
printf 'ssh-ed25519 AAAA tunnel\n' > "$P/home/omvtunnel/.ssh/authorized_keys"; chmod 600 "$P/home/omvtunnel/.ssh/authorized_keys"
printf 'X=%s\n' "$SECRET"         > "$P/etc/forgotten.env";   chmod 600 "$P/etc/forgotten.env"
printf '127.0.0.1 localhost\n'    > "$P/etc/hosts";           chmod 644 "$P/etc/hosts"
RCP='age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq'

# Stub age: records its argv, emits a marker then its stdin (so the tar can
# be read back). STUB_RC makes it fail.
cat > "$tmp/bin/age" <<EOF2
#!/usr/bin/env bash
printf '%s\n' "\$*" > "$tmp/age.args"
cat > "$tmp/age.stdin"
echo STUB-CIPHERTEXT
exit "\${STUB_RC:-0}"
EOF2
chmod +x "$tmp/bin/age"
run() { EE_PREFIX="$P" EE_AGE="${AGE_BIN-$tmp/bin/age}" bash "$SCRIPT" "$@" >"$tmp/out" 2>"$tmp/err"; }

echo "A. manifest"
run manifest; rc=$?
[ "$rc" -eq 0 ] && ok "manifest exits 0" || ko "manifest rc=$rc: $(cat "$tmp/err")"
grep -q '^escrowed: 3/6 present$' "$tmp/out" && ok "3 of 6 escrowed files seen present" || ko "count line: $(grep '^escrowed:' "$tmp/out")"
grep -qE '^  /etc/wol-relay\.env  size=[0-9]+ mtime=20[0-9-]+T[0-9:]+Z$' "$tmp/out" && ok "present file carries size + UTC mtime" || ko "present line malformed"
grep -qx '  /etc/pock-sync.env  ABSENT' "$tmp/out" && ok "absent file is named ABSENT" || ko "absent file not reported"
grep -q "$SECRET" "$P/etc/wol-relay.env" && ! grep -q "$SECRET" "$tmp/out" "$tmp/err" \
  && ok "no value in the manifest (control: it is in the fixture)" || ko "VALUE LEAKED into the manifest"
grep -qx '  /etc/forgotten.env' "$tmp/out" && ok "unlisted non-world-readable file shows up" || ko "forgotten.env invisible"
grep -qx 'others: 1' "$tmp/out" && ok "others counts it alone (escrowed and world-readable files excluded)" || ko "others: $(grep '^others' "$tmp/out")"
grep -q 'recipients: NOT USABLE' "$tmp/out" && ok "missing recipients file is said, not fatal" || ko "recipients state missing"

echo "B. dump preconditions"
refused() { # <label> <expected rc>
  local rc=$3
  if [ "$rc" -eq "$2" ] && [ ! -s "$tmp/out" ]; then ok "$1 ⇒ rc=$2, empty stdout"; else ko "$1: rc=$rc stdout=$(wc -c < "$tmp/out") bytes"; fi
}
run dump; refused "no recipients file" 3 $?
printf 'ssh-ed25519 AAAAC3Nza attacker\n' > "$P/etc/env-escrow.recipients"
run dump; refused "recipients = an ssh key" 3 $?
printf '%s\n-i /etc/shadow\n' "$RCP" > "$P/etc/env-escrow.recipients"
run dump; refused "recipients with a stray option line" 3 $?
printf '# comment only\n' > "$P/etc/env-escrow.recipients"
run dump; refused "recipients without any key" 3 $?
printf '# escrow key\n%s\n' "$RCP" > "$P/etc/env-escrow.recipients"
AGE_BIN="$tmp/bin/absent" run dump; refused "age not installed" 3 $?
mkdir "$tmp/empty" && mkdir -p "$tmp/empty/etc" && cp "$P/etc/env-escrow.recipients" "$tmp/empty/etc/"
EE_PREFIX="$tmp/empty" EE_AGE="$tmp/bin/age" bash "$SCRIPT" dump >"$tmp/out" 2>"$tmp/err"; refused "no escrowed file at all" 1 $?
run bogus; refused "unknown verb" 64 $?

echo "C. dump through a stub age"
run dump; rc=$?
[ "$rc" -eq 0 ] && grep -qx STUB-CIPHERTEXT "$tmp/out" && ok "dump exits 0 and emits what age wrote" || ko "dump rc=$rc: $(cat "$tmp/err")"
grep -qxF -- "-R $P/etc/env-escrow.recipients" "$tmp/age.args" && ok "age called with the pinned recipients file only" || ko "age argv: $(cat "$tmp/age.args")"
got="$(tar -tf "$tmp/age.stdin" | sort | tr '\n' ' ')"
want="etc/msmtprc etc/wol-relay.env home/omvtunnel/.ssh/authorized_keys "
[ "$got" = "$want" ] && ok "tar holds exactly the 3 present escrowed files" || ko "tar holds: $got"
! grep -q "$SECRET" "$tmp/out" && ok "stdout carries no plaintext (only age's output)" || ko "plaintext on stdout"
grep -q '3/6 file(s) encrypted to 1 recipient' "$tmp/err" && ok "stderr states N/T files and recipients" || ko "summary: $(cat "$tmp/err")"
STUB_RC=1 run dump; rc=$?
[ "$rc" -eq 1 ] && ok "a failing age is a failed dump (rc=1)" || ko "failing age read as success (rc=$rc)"

echo "D. real age round-trip"
if command -v age >/dev/null && command -v age-keygen >/dev/null; then
  age-keygen -o "$tmp/key.txt" 2>/dev/null
  age-keygen -y "$tmp/key.txt" > "$P/etc/env-escrow.recipients"
  AGE_BIN="$(command -v age)" run dump; rc=$?
  [ "$rc" -eq 0 ] && ok "dump with real age exits 0" || ko "real age dump rc=$rc: $(cat "$tmp/err")"
  ! grep -qa "$SECRET" "$tmp/out" && ok "ciphertext does not contain the value" || ko "value readable in the dump"
  mkdir "$tmp/restore"
  if age -d -i "$tmp/key.txt" "$tmp/out" | tar -C "$tmp/restore" -xf - \
     && cmp -s "$tmp/restore/etc/wol-relay.env" "$P/etc/wol-relay.env" \
     && cmp -s "$tmp/restore/etc/msmtprc" "$P/etc/msmtprc"; then
    ok "decrypts with the private key back to the original files"
  else ko "round-trip does not restore the files"; fi
else
  echo "SKIP real age round-trip — age not installed here (CI installs it)"
fi

echo
echo "test_env_escrow: $pass ok, $fail failed"
[ "$fail" -eq 0 ]
