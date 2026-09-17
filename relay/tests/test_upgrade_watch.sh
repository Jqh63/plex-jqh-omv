#!/usr/bin/env bash
# test_upgrade_watch.sh — bench for relay/scripts/upgrade-watch.sh.
#
# HEALTHY states come first on purpose. This detector will spend its life on a
# VM where nothing is pending, and that is where a wrong verdict slips in: a
# detector is almost never disqualified by missing the outage, it is
# disqualified by shouting about a healthy machine.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../scripts/upgrade-watch.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0
ok()  { if [ "$1" = "$2" ]; then echo "  ok   $3"; else echo "  FAIL $3 (want=$1 got=$2)"; fail=1; fi }
has() { if printf '%s' "$2" | grep -q -- "$1"; then echo "  ok   $3"; else echo "  FAIL $3"; fail=1; fi }
no()  { if printf '%s' "$2" | grep -q -- "$1"; then echo "  FAIL $3"; fail=1; else echo "  ok   $3"; fi }

NOW=1789000000

# Fixture shape taken from a real `apt-get -s dist-upgrade`: the origin lives
# inside the parentheses of each Inst line. Confronted against the live VM
# right after deployment — a fixture only ever proves my model of the format.
sim_none() { printf 'Reading package lists...\n0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n'; }
sim_sec()  { printf 'Inst libc6 [2.41-12] (2.41-13 Debian-Security:13/stable-security [amd64])\nInst curl [8.14] (8.15 Debian-Security:13/stable-security [amd64])\n2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n'; }
sim_mixed(){ printf 'Inst libc6 [2.41-12] (2.41-13 Debian-Security:13/stable-security [amd64])\nInst vim [9.1] (9.2 Debian:13/stable [amd64])\n2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n'; }
sim_noorig(){ printf 'Inst weirdpkg [1.0] \n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n'; }

export -f sim_none sim_sec sim_mixed sim_noorig

armed_dir()   { mkdir -p "$1"; printf 'APT::Periodic::Unattended-Upgrade "1";\n' > "$1/20auto-upgrades";
                printf '"origin=Debian,codename=${distro_codename}-security";\n' > "$1/50unattended-upgrades"; }
unarmed_dir() { mkdir -p "$1"; printf 'APT::Periodic::Update-Package-Lists "1";\n' > "$1/20auto-upgrades"; }

run() { OUT="$(UW_NOW="$NOW" UW_APT_CONF_DIR="$1" UW_LISTS_DIR="$2" UW_SIM_CMD="$3" bash "$SCRIPT" 2>&1)"; RC=$?; }

mkdir -p "$TMP/lists"; touch -d "@$((NOW - 3600))" "$TMP/lists"
armed_dir "$TMP/armed"; unarmed_dir "$TMP/unarmed"

echo "case A — HEALTHY: armed, fresh lists, nothing pending"
run "$TMP/armed" "$TMP/lists" "sim_none"
ok 0 "$RC" "exit 0"
has "unattended-upgrades: yes" "$OUT" "the automation is reported as armed"
has "compliant (nothing to install)" "$OUT" "verdict says the measurement was MADE"
no "COULD NOT MEASURE" "$OUT" "does not confuse 'nothing' with 'unmeasured'"
no "⚠️" "$OUT" "no alarm on a healthy VM"

echo "case B — HEALTHY: pending, but all of it security-covered"
run "$TMP/armed" "$TMP/lists" "sim_sec"
ok 0 "$RC" "exit 0 — covered updates are not a gesture"
has "covered by unattended-upgrades: 2" "$OUT" "both classified as automatic"
has "NEEDS A MANUAL GESTURE:         0" "$OUT" "nothing asked of the admin"

echo "case C — a manual gesture IS pending"
run "$TMP/armed" "$TMP/lists" "sim_mixed"
ok 2 "$RC" "exit 2"
has "NEEDS A MANUAL GESTURE:         1 — vim" "$OUT" "only the non-security one"
has "covered by unattended-upgrades: 1" "$OUT" "the security one is not charged to the admin"
has "sudo apt-get update" "$OUT" "the gesture is spelled out"

echo "case D — nothing installs security updates on its own"
run "$TMP/unarmed" "$TMP/lists" "sim_sec"
has "unattended-upgrades: no" "$OUT" "the absence is NAMED"
has "Nothing installs security updates on its own" "$OUT" "and its consequence stated"
ok 2 "$RC" "exit 2 — an unarmed VM with pending security is a gesture"

echo "case E — stale package lists make every count optimistic"
mkdir -p "$TMP/old"; touch -d "@$((NOW - 10*86400))" "$TMP/old"
run "$TMP/armed" "$TMP/old" "sim_none"
has "10 day(s) ago" "$OUT" "the age is stated"
has "may understate reality" "$OUT" "and its effect on the verdict"

echo "case F — the measurement itself fails ⇒ sentinel, never a green light"
run "$TMP/armed" "$TMP/lists" "true"
ok 3 "$RC" "exit 3 — distinct from both 0 and 2"
has "COULD NOT MEASURE" "$OUT" "says so in words"
no "compliant" "$OUT" "never reports compliance on an unmeasured VM"

echo "case G — origins unparsable ⇒ refuse to classify (unanimity is the tell)"
run "$TMP/armed" "$TMP/lists" "sim_noorig"
ok 3 "$RC" "exit 3"
has "COULD NOT CLASSIFY" "$OUT" "does not silently call everything manual"
no "NEEDS A MANUAL GESTURE" "$OUT" "no fabricated split"

echo "case H — REGRESSION: commented example origins must NOT be reported as active"
# Shape taken from the REAL /etc/apt/apt.conf.d/50unattended-upgrades Debian
# ships: a dozen example origins, commented out with `//`. Reading them as
# active made the live VM report `stable` and `backports` as covered — the
# route overstating the very thing a reader consults it for. This case FAILS
# against the code before the fix.
mkdir -p "$TMP/commented"
printf 'APT::Periodic::Unattended-Upgrade "1";\n' > "$TMP/commented/20auto-upgrades"
cat > "$TMP/commented/50unattended-upgrades" <<'CONF'
Unattended-Upgrade::Origins-Pattern {
//      "o=Debian,a=stable";
//      "o=Debian,a=proposed-updates";
//      "origin=Debian,codename=${distro_codename}-backports";
        "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
};
CONF
run "$TMP/commented" "$TMP/lists" "sim_none"
has "codename=\${distro_codename}-security" "$OUT" "the ACTIVE origin is reported"
no "a=stable" "$OUT" "a commented example is NOT reported as active"
no "backports" "$OUT" "nor is a commented backports line"

[ "$fail" -eq 0 ] && { echo "ALL CASES OK"; exit 0; } || { echo "FAILURES"; exit 1; }
