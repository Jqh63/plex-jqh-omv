#!/usr/bin/env bash
# upgrade-watch.sh — what OS updates are pending on the relay VM, and who will
# install them. Routed: `ssh wol-relay-deploy upgrade-watch`.
#
# This VM is the most exposed machine of the ecosystem (public IP, Caddy,
# FastAPI) and until 2026-09-17 it had no update visibility at all: the only
# way to know was to SSH in. The home server has had `host-upgrade-watch` for
# months; this is its counterpart.
#
# UNPRIVILEGED BY CONSTRUCTION: `apt-get -s dist-upgrade` simulates as a plain
# user, so no sudoers entry is added. Nothing here writes, installs or locks.
#
# Reads state only. It NEVER installs — reporting is the whole job.

set -uo pipefail

# Test seams. Real runs take every default; the bench injects fixtures.
APT_CONF_DIR="${UW_APT_CONF_DIR:-/etc/apt/apt.conf.d}"
LISTS_DIR="${UW_LISTS_DIR:-/var/lib/apt/lists}"
NOW="${UW_NOW:-$(date +%s)}"
SIM_CMD="${UW_SIM_CMD:-apt-get -s -o Debug::NoLocking=1 dist-upgrade}"
# Origins unattended-upgrades is allowed to act on. Anything outside them is a
# manual gesture, and saying so is the point of this route.
SECURITY_PATTERN="${UW_SECURITY_PATTERN:-Debian-Security|Debian.*security}"
STALE_LISTS_DAYS="${UW_STALE_LISTS_DAYS:-3}"

echo "=== RELAY VM UPDATE WATCH ($(date -d "@$NOW" '+%F %T %Z')) ==="
echo "machine: $(hostname -s 2>/dev/null || echo '?') (GCP wol-relay)"
echo

# ── Is anything even going to install security updates on its own? ──────────
# A VM with no unattended-upgrades looks exactly like a VM that is up to date:
# both report zero pending for a while. Name the difference.
armed="no"; auto_detail="no unattended-upgrades configuration found"
if [ -d "$APT_CONF_DIR" ]; then
  # ⚠️ Read the WHOLE directory, never a *periodic* glob: on Debian the
  # `APT::Periodic::*` directives live in a file named `20auto-upgrades`, which
  # that glob misses — the bench caught exactly this, reporting a correctly
  # armed VM as unarmed.
  conf="$(cat "$APT_CONF_DIR"/* 2>/dev/null)"
  if printf '%s' "$conf" | grep -qE 'APT::Periodic::Unattended-Upgrade[^0-9]*1'; then
    armed="yes"
    # Origins are read from apt's RESOLVED configuration, never grepped from
    # the files. Two lessons, both from the live VM: (1) 2026-09-17, a raw grep
    # reported Debian's commented example origins as active; (2) 2026-09-19,
    # skipping comments still reported every file's list side by side, while
    # apt MERGES lists across files and honours `#clear` — the only honest
    # answer is apt's own. `apt-config dump` with Dir::Etc::Parts pointed at
    # the directory under test resolves exactly what unattended-upgrade sees.
    origins=""
    if command -v apt-config >/dev/null 2>&1; then
      aptcfg="$(mktemp)"
      printf 'Dir::Etc::main "%s";\nDir::Etc::Parts "%s";\n' \
        "$APT_CONF_DIR/.none" "$APT_CONF_DIR" > "$aptcfg"
      origins="$(APT_CONFIG="$aptcfg" apt-config dump 2>/dev/null \
                 | grep -E '^Unattended-Upgrade::(Origins-Pattern|Allowed-Origins):: ' \
                 | sed -E 's/^[^"]*"//; s/";$//' | tr '\n' ' ')"
      rm -f "$aptcfg"
    else
      origins="(apt-config absent — origins not resolved)"
    fi
    auto_detail="enabled${origins:+ — origins: $origins}"
  fi
fi
echo "unattended-upgrades: $armed ($auto_detail)"
if [ "$armed" != "yes" ]; then
  echo "  ⚠️  Nothing installs security updates on its own on this VM."
  echo "     Fix: rerun relay/scripts/bootstrap-wol-relay.sh (section 'unattended')."
fi
echo

# ── Freshness of the package lists ─────────────────────────────────────────
# Stale lists make every verdict below optimistic without ever erroring out.
if [ -d "$LISTS_DIR" ]; then
  lists_epoch="$(stat -c %Y "$LISTS_DIR" 2>/dev/null || echo 0)"
  age_days=$(( (NOW - lists_epoch) / 86400 ))
  printf 'apt lists refreshed: %s (%d day(s) ago)\n' \
    "$(date -d "@$lists_epoch" '+%F %T' 2>/dev/null || echo '?')" "$age_days"
  [ "$age_days" -gt "$STALE_LISTS_DAYS" ] && \
    echo "  ⚠️  Older than ${STALE_LISTS_DAYS}d — the counts below may understate reality."
else
  echo "apt lists refreshed: UNKNOWN ($LISTS_DIR missing)"
fi
echo

# ── What is actually pending ───────────────────────────────────────────────
# A simulated dist-upgrade is the only view that accounts for held packages and
# dependency resolution. `apt list --upgradable` would over-report.
sim="$($SIM_CMD 2>/dev/null)"
if [ -z "$sim" ] || ! printf '%s' "$sim" | grep -q 'upgraded,'; then
  echo "COULD NOT MEASURE — '$SIM_CMD' returned nothing usable."
  echo "  → this is NOT 'nothing pending': the measurement itself failed."
  exit 3
fi

pending="$(printf '%s\n' "$sim" | awk '/^Inst /{print $2}' | sort -u)"
count="$(printf '%s' "$pending" | grep -c . || true)"

if [ "$count" -eq 0 ]; then
  echo "pending: 0 package(s) — measured, not assumed."
  echo
  echo "VERDICT: compliant (nothing to install)"
  exit 0
fi

# Split by who will install it. The whole value of this route is that split:
# "12 pending" is noise, "12 pending, 0 of which anyone will install" is a fact.
#
# ⚠️ The split reads the ORIGIN that apt prints inside each `Inst` line, e.g.
#   Inst libc6 [2.41-12] (2.41-13 Debian-Security:13/stable-security [amd64])
# If no pending line carries a parsable origin at all, the classification has
# FAILED — and silently calling everything "manual" would look like an alarming
# but plausible result. Unanimity is the tell, so it is checked for explicitly.
auto="" ; manual="" ; with_origin=0
while IFS= read -r pkg; do
  [ -z "$pkg" ] && continue
  line="$(printf '%s\n' "$sim" | grep -m1 "^Inst $pkg ")"
  printf '%s' "$line" | grep -q '(.*:.*)' && with_origin=$((with_origin + 1))
  if [ "$armed" = "yes" ] && printf '%s' "$line" | grep -qE "$SECURITY_PATTERN"; then
    auto="$auto $pkg"
  else
    manual="$manual $pkg"
  fi
done <<< "$pending"

if [ "$with_origin" -eq 0 ]; then
  echo "pending: $count package(s)"
  echo "COULD NOT CLASSIFY — not one pending line carried a parsable origin."
  echo "  → the split below would be an artefact of the parser, not a measurement."
  echo "  → read the raw simulation on the VM: apt-get -s dist-upgrade"
  exit 3
fi

n_auto="$(printf '%s' "$auto" | wc -w)"; n_manual="$(printf '%s' "$manual" | wc -w)"

echo "pending: $count package(s)"
echo "  covered by unattended-upgrades: $n_auto${auto:+ —$auto}"
echo "  NEEDS A MANUAL GESTURE:         $n_manual${manual:+ —$manual}"
echo

if [ "$n_manual" -gt 0 ]; then
  echo "Gesture (admin, from Cloud Shell or IAP SSH on the VM):"
  echo "  sudo apt-get update && sudo apt-get dist-upgrade"
  echo
  echo "VERDICT: $n_manual package(s) awaiting a manual gesture"
  exit 2
fi

echo "VERDICT: compliant (all pending updates are covered automatically)"
exit 0
