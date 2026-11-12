#!/usr/bin/env bash
# scripts-drift.sh — are the root-installed scripts on the VM those of origin/main?
#
# dispatch.sh, venv-run.sh, upgrade-*.sh… are installed by bootstrap-wol-relay.sh
# only (root-equivalent, never through the deploy channel): merging a change
# does nothing on the VM until the admin reruns the bootstrap. This compares
# `ssh wol-relay-deploy scripts-manifest` to origin/main, read-only.
# Not covered: sudoers.deploy (/etc/sudoers.d is root-only).
#
# Exit codes: 0 in sync · 2 drift (bootstrap to rerun) · 3 could not measure
# (fetch failed, or the VM's dispatch.sh predates the route — itself a drift).
set -uo pipefail
ALIAS="${WOL_RELAY_ALIAS:-wol-relay-deploy}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REF="${SCRIPTS_DRIFT_REF:-origin/main}"

if [ "$REF" = "origin/main" ]; then
  git -C "$DIR" fetch --quiet origin main 2>/dev/null \
    || { echo "[scripts-drift] UNMEASURED — git fetch origin main failed" >&2; exit 3; }
fi
manifest="$(ssh "$ALIAS" scripts-manifest 2>&1)" \
  || { echo "[scripts-drift] UNMEASURED — scripts-manifest refused (VM dispatch.sh older than the route ⇒ bootstrap pending?):" >&2
       printf '%s\n' "$manifest" | sed -n '1,2p' >&2; exit 3; }

drift=0; n=0
while read -r sum name; do
  [ -n "$name" ] || continue
  n=$((n + 1))
  want="$(git -C "$DIR" show "$REF:relay/scripts/$name" 2>/dev/null | sha256sum | cut -d' ' -f1)"
  if ! git -C "$DIR" cat-file -e "$REF:relay/scripts/$name" 2>/dev/null; then
    echo "  ?     $name — on the VM, not in $REF"; drift=1
  elif [ "$sum" = "$want" ]; then
    echo "  ok    $name"
  else
    echo "  DRIFT $name"; drift=1
  fi
done <<<"$manifest"
[ "$n" -gt 0 ] || { echo "[scripts-drift] UNMEASURED — empty manifest" >&2; exit 3; }
if [ "$drift" -eq 0 ]; then echo "[scripts-drift] IN SYNC — $n script(s) match $REF"; exit 0; fi
echo "[scripts-drift] DRIFT — rerun bootstrap-wol-relay.sh on the VM from an up-to-date clone"; exit 2
