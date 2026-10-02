#!/usr/bin/env bash
# test_jobs_key_role.sh — the home server's root backup jobs get their own key.
#
# Finding F7 (knowledge-base scan, 2026-10-02): the root jobs and the
# code-server sandbox shared the deploy key, so the sandbox could push fake
# blobs through pat-receive/secrets-receive and rotate the real backups away.
# Step 1 of the fix (additive): a 2nd authorized_keys line whose forced command
# is `dispatch.sh jobs`, admitting only the three verbs the jobs use.
#
# A. dispatch.sh role gate, run for real against a stub sudo:
#    jobs role admits pock-dump / pat-receive / secrets-receive, refuses
#    everything else BEFORE any side effect; deploy role unchanged; an unknown
#    role is refused.
# B. bootstrap rendering (functions sourced with BOOTSTRAP_LIB_ONLY=1):
#    deploy line unchanged, jobs line carries the role and the same
#    restrictions, an installed jobs key survives a re-run without the 3rd
#    argument, and key files holding 0 or 2 keys are rejected.
#
# Usage: bash relay/tests/test_jobs_key_role.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISPATCH="$HERE/../scripts/dispatch.sh"
BOOTSTRAP="$HERE/../scripts/bootstrap-wol-relay.sh"
pass=0; fail=0
ok()  { echo "  ok   $*"; pass=$((pass + 1)); }
ko()  { echo "  FAIL $*"; fail=$((fail + 1)); }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/staging" "$tmp/home"
cat > "$tmp/bin/sudo" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$tmp/sudo.log"
EOF
chmod +x "$tmp/bin/sudo"
sed "s#^STAGING_DIR=.*#STAGING_DIR=\"$tmp/staging\"#" "$DISPATCH" > "$tmp/dispatch.sh"
# run <role> <command> — stdin forwarded; side effects land in $tmp.
run() {
  : > "$tmp/sudo.log"; rm -rf "$tmp/staging"/* "$tmp/home"/*
  SSH_ORIGINAL_COMMAND="$2" HOME="$tmp/home" PATH="$tmp/bin:$PATH" \
    bash "$tmp/dispatch.sh" ${1:+"$1"} >"$tmp/out" 2>&1
}
blob() { head -c 900 /dev/zero; }   # above both receive floors (200 / 500 bytes)
side_effects() { cat "$tmp/sudo.log"; find "$tmp/staging" "$tmp/home" -mindepth 1; }

echo "A. dispatch.sh role gate"
if blob | run jobs "pat-receive daily" && ls "$tmp/home/pat-offsite"/pat-daily-*.age >/dev/null 2>&1; then
  ok "jobs: pat-receive daily stores a blob"
else ko "jobs: pat-receive daily refused: $(cat "$tmp/out")"; fi
if blob | run jobs "secrets-receive weekly" && ls "$tmp/home/secrets-offsite"/secrets-weekly-*.age >/dev/null 2>&1; then
  ok "jobs: secrets-receive weekly stores a blob"
else ko "jobs: secrets-receive weekly refused: $(cat "$tmp/out")"; fi
if run jobs pock-dump </dev/null && grep -qxF -- '/usr/bin/tar -C /var/lib/pock-sync -cf - .' "$tmp/sudo.log"; then
  ok "jobs: pock-dump runs the pinned tar"
else ko "jobs: pock-dump refused: $(cat "$tmp/out")"; fi

for c in apply push-app health status secrets-dump-latest pat-dump-latest secrets-list \
         "pat-receive monthly" "pat-receive daily;id" "pock-dump " upgrade tunnel-reap ""; do
  blob | run jobs "$c"; rc=$?
  if [ "$rc" -eq 64 ] && [ -z "$(side_effects)" ]; then
    ok "jobs: '$c' refused (64), no side effect"
  else
    ko "jobs: '$c' rc=$rc side effects: $(side_effects | tr '\n' ' ')"
  fi
done

# Positive control: the deploy key (no role) keeps every route of today.
echo 'print(1)' > "$tmp/app.py"
if run "" push-app < "$tmp/app.py" && grep -q 'push-app\] OK' "$tmp/out"; then
  ok "deploy: push-app still accepted"
else ko "deploy: push-app refused: $(cat "$tmp/out")"; fi
if blob | run "" "pat-receive weekly" && ls "$tmp/home/pat-offsite"/pat-weekly-*.age >/dev/null 2>&1; then
  ok "deploy: pat-receive still accepted (removed only at step 3)"
else ko "deploy: pat-receive refused: $(cat "$tmp/out")"; fi

run admin status </dev/null; rc=$?
if [ "$rc" -eq 64 ] && [ -z "$(side_effects)" ]; then ok "unknown role refused (64)"; else ko "unknown role: rc=$rc"; fi

echo "B. bootstrap authorized_keys rendering"
# shellcheck disable=SC1090
BOOTSTRAP_LIB_ONLY=1 source "$BOOTSTRAP" || { ko "bootstrap cannot be sourced in lib mode"; echo "== $pass ok, $fail fail"; exit 1; }
KD='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDeployDeployDeployDeployDeployDeployDeploy00'
KJ='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJobsJobsJobsJobsJobsJobsJobsJobsJobsJobs00'

want_deploy='command="/opt/wol-relay/scripts/dispatch.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty '"$KD"
out="$(render_deploy_auth "$KD" "")"
if [ "$out" = "$want_deploy" ]; then ok "no jobs key: deploy line byte-identical to the pre-F7 line"; else ko "deploy line changed: $out"; fi

out="$(render_deploy_auth "$KD" "$KJ")"
l2="$(sed -n 2p <<<"$out")"
if [ "$(wc -l <<<"$out")" -eq 2 ] && [ "$(sed -n 1p <<<"$out")" = "$want_deploy" ] \
   && [ "$l2" = 'command="/opt/wol-relay/scripts/dispatch.sh jobs",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty '"$KJ" ]; then
  ok "jobs key: 2nd line with role 'jobs' and the same restrictions"
else ko "jobs rendering: $out"; fi

if render_deploy_auth "$KD" "$KD" >/dev/null 2>&1; then ko "identical deploy/jobs keys accepted"; else ok "identical deploy/jobs keys refused"; fi

printf '%s\n' "$out" > "$tmp/auth"
if [ "$(existing_jobs_key "$tmp/auth")" = "$KJ" ]; then ok "installed jobs key re-read (kept on a re-run without 3rd arg)"; else ko "jobs key not re-read: '$(existing_jobs_key "$tmp/auth")'"; fi
printf '%s\n' "$want_deploy" > "$tmp/auth1"
if [ -z "$(existing_jobs_key "$tmp/auth1")" ]; then ok "control: no jobs line ⇒ nothing re-read"; else ko "control: phantom jobs key"; fi

printf '%s user@host\n' "$KD" > "$tmp/one.pub"
printf '' > "$tmp/zero.pub"
printf '%s\n' "$out" | grep -oE 'ssh-ed25519 [A-Za-z0-9+/=]+' > "$tmp/two.pub"   # the README's naive grep
if [ "$(read_one_pubkey "$tmp/one.pub")" = "$KD" ]; then ok "read_one_pubkey: one key (comment stripped)"; else ko "read_one_pubkey one"; fi
if read_one_pubkey "$tmp/zero.pub" >/dev/null 2>&1; then ko "empty key file accepted (lockout)"; else ok "empty key file refused"; fi
if read_one_pubkey "$tmp/two.pub" >/dev/null 2>&1; then ko "2-key file accepted (glued keys)"; else ok "2-key file refused (naive grep over a 2-line authorized_keys)"; fi

echo "== $pass ok, $fail fail"
[ "$fail" -eq 0 ]
