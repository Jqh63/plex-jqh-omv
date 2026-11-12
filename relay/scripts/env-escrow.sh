#!/usr/bin/env bash
# env-escrow.sh — the secret files placed BY HAND on the relay VM: list them,
# or hand them over encrypted. Routed by `ssh wol-relay-deploy env-manifest`
# and `env-dump` (jobs key only).
#
# Why: the home server escrows its secrets to this VM, but nothing carried the
# VM's own secrets home. Losing the VM meant regenerating WOL_TOKEN and the
# pock-sync token, i.e. re-entering them on every family device.
#
#   manifest  names, sizes and mtimes — NEVER a value. Also lists every other
#             file under /etc and /home that is not world-readable, so a secret
#             file nobody wrote down shows up instead of staying invisible.
#   dump      tar of the files below, piped straight into `age` for the
#             recipients in $RECIPIENTS, to stdout. Plaintext exists only in a
#             root pipe: never on disk, never on the SSH channel. The private
#             key lives off this VM, so the VM cannot read its own dump.
#
# PRIVILEGE: root through two argvs pinned in sudoers.deploy. No argument is
# free, no input is read. The recipients file is root-owned and linted (age
# public keys and comments only), so the dump cannot be re-addressed through
# the deploy channel.
#
# Exit: manifest 0 measured / 3 could not measure;
#       dump 0 written / 1 nothing to dump / 3 precondition missing (age,
#       recipients).
set -uo pipefail

# Test seams — real runs take every default (sudo resets the environment).
PREFIX="${EE_PREFIX:-}"
AGE="${EE_AGE:-/usr/bin/age}"
RECIPIENTS="$PREFIX/etc/env-escrow.recipients"

# The escrowed set. Adding a hand-placed secret file = adding it here.
FILES=(
  etc/wol-relay.env
  etc/caddy/wol-relay.env
  etc/pock-sync.env
  etc/msmtprc
  etc/home-watch.env
  home/omvtunnel/.ssh/authorized_keys
)

stat_line() { # <absolute path> → "size=N mtime=YYYY-MM-DDTHH:MM:SSZ"
  local s m
  s="$(stat -c %s "$1")" && m="$(date -u -d "@$(stat -c %Y "$1")" +%Y-%m-%dT%H:%M:%SZ)" || return 1
  echo "size=$s mtime=$m"
}

# Prints the number of recipients, or fails with the reason on stderr.
recipients_count() {
  [ -f "$RECIPIENTS" ] || { echo "recipients file absent: /etc/env-escrow.recipients" >&2; return 1; }
  [ -z "$PREFIX" ] && [ "$(stat -c %U "$RECIPIENTS")" != root ] && {
    echo "recipients file is not owned by root" >&2; return 1; }
  if grep -vE '^(#.*|age1[0-9a-z]{20,}|[[:space:]]*)$' "$RECIPIENTS" | grep -q .; then
    echo "recipients file holds something other than age public keys and comments" >&2; return 1
  fi
  local n; n="$(grep -cE '^age1' "$RECIPIENTS")"
  [ "$n" -ge 1 ] || { echo "recipients file holds no age public key" >&2; return 1; }
  echo "$n"
}

manifest() {
  local f present=0 line n
  echo "[env-manifest] $(date -u +%Y-%m-%dT%H:%M:%SZ) — names, sizes, mtimes; never a value"
  echo "escrowed set:"
  for f in "${FILES[@]}"; do
    if [ -f "$PREFIX/$f" ]; then
      line="$(stat_line "$PREFIX/$f")" || { echo "  /$f  UNREADABLE"; return 3; }
      echo "  /$f  $line"; present=$((present + 1))
    else
      echo "  /$f  ABSENT"
    fi
  done
  echo "escrowed: $present/${#FILES[@]} present"
  if n="$(recipients_count 2>&1)"; then echo "recipients: $n"; else echo "recipients: NOT USABLE — $n"; fi
  if [ -x "$AGE" ]; then echo "age: present"; else echo "age: ABSENT ($AGE)"; fi

  # Everything else that is not world-readable: the candidates nobody listed.
  local others
  others="$(find "$PREFIX/etc" "$PREFIX/home" -xdev -type f ! -perm -o=r 2>/dev/null | sort)" || return 3
  echo "other non-world-readable files (not escrowed):"
  n=0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    line="${line#"$PREFIX"}"
    case " ${FILES[*]/#//} " in *" $line "*) continue ;; esac
    echo "  $line"; n=$((n + 1))
  done <<<"$others"
  echo "others: $n"
}

dump() {
  local f n; local -a present=()
  [ -x "$AGE" ] || { echo "[env-dump] FAIL — age not installed ($AGE). Rerun bootstrap-wol-relay.sh." >&2; return 3; }
  n="$(recipients_count)" || { echo "[env-dump] FAIL — see above." >&2; return 3; }
  for f in "${FILES[@]}"; do [ -f "$PREFIX/$f" ] && present+=("$f"); done
  [ "${#present[@]}" -ge 1 ] || { echo "[env-dump] FAIL — none of the escrowed files exists." >&2; return 1; }
  set -o pipefail
  tar -C "$PREFIX/" -cf - -- "${present[@]}" | "$AGE" -R "$RECIPIENTS" \
    || { echo "[env-dump] FAIL — tar or age failed; the output is not a valid dump." >&2; return 1; }
  echo "[env-dump] ${#present[@]}/${#FILES[@]} file(s) encrypted to $n recipient(s)" >&2
}

case "${1:-}" in
  manifest) manifest ;;
  dump)     dump ;;
  *) echo "usage: env-escrow.sh {manifest|dump}" >&2; exit 64 ;;
esac
