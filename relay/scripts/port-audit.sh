#!/usr/bin/env bash
# port-audit.sh — which TCP ports of the home server answer from the Internet,
# seen from this VM (the one observation point OUTSIDE the home NAT).
#
# Invoked by dispatch.sh `port-audit <host>`; the host name is validated there
# and never stored in this public repo. Plain connect() probes through bash's
# /dev/tcp — no nmap, no raw sockets, no sudo: zero privileged surface, and
# nothing to install on the VM.
#
# States per port: OPEN (handshake done), closed (refused), filtered (no answer
# within PROBE_TIMEOUT). A connect() scan cannot see UDP (WireGuard 51820, WoL 9
# never answer a probe): the output says so rather than staying silent.
#
# Deliberately gentle: the target runs Fail2ban. A bare TCP handshake sends no
# HTTP request, so SWAG's nginx jails (which read the access log) see nothing.
#
# Exit: 0 measured · 3 could not measure (resolution failed, private target).
set -uo pipefail

host="${1:-}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-2}"
PARALLEL="${PARALLEL:-24}"

# Well-known range + the high ports a homelab tends to expose by mistake
# (admin UIs, databases, remote desktops, the home server's own LAN-only ports).
default_ports() {
  seq 1 1024
  printf '%s\n' 1433 1521 1723 1883 2049 2082 2083 2222 2375 2376 3000 3001 3080 \
    3306 3389 4443 5000 5001 5055 5432 5601 5900 5984 6379 6443 7070 7878 8000 \
    8008 8080 8081 8088 8090 8096 8181 8443 8800 8888 8920 8989 9000 9090 9091 \
    9117 9200 9443 9696 10000 11211 27017 32400 32401 32402 32469 51413
}
PORTS="${PORT_AUDIT_PORTS:-$(default_ports | tr '\n' ' ')}"

unmeasured() { echo "PORT-AUDIT UNMEASURED — $*"; exit 3; }

[ -n "$host" ] || unmeasured "no target host given"
ip="$(getent ahostsv4 "$host" 2>/dev/null | awk 'NR==1 { print $1 }')"
[ -n "$ip" ] || unmeasured "cannot resolve $host from the VM"

# The point is the view from the Internet: a private or loopback answer means
# the resolver is lying (split DNS, /etc/hosts) and the scan would audit the
# wrong machine. PORT_AUDIT_ALLOW_PRIVATE exists for the local test bench only
# — sshd does not forward client environment to the forced command.
if [ "${PORT_AUDIT_ALLOW_PRIVATE:-0}" != 1 ]; then
  case "$ip" in
    10.*|127.*|192.168.*|169.254.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*)
      unmeasured "$host resolves to a non-public address — refusing to scan" ;;
  esac
fi

# Never print the full public IP: this output gets pasted into notes.
masked="${ip%.*}.x"
total=$(wc -w <<<"$PORTS")
echo "=== port-audit $host -> $masked — $(date -u '+%Y-%m-%d %H:%M UTC') — $total TCP ports, connect(), timeout ${PROBE_TIMEOUT}s"

# One probe per port, in parallel. `timeout` exit 124 = no answer = filtered.
# shellcheck disable=SC2016
results="$(tr ' ' '\n' <<<"$PORTS" | awk 'NF' | xargs -P "$PARALLEL" -I{} \
  bash -c 'timeout "$2" bash -c "exec 3<>/dev/tcp/$1/$0" 2>/dev/null; rc=$?
           case $rc in 0) echo "open $0" ;; 124) echo "filtered $0" ;; *) echo "closed $0" ;; esac' \
  {} "$ip" "$PROBE_TIMEOUT")"

awk '$1 == "open" { print $2 }' <<<"$results" | sort -n | sed 's/^/OPEN /;s/$/\/tcp/'
n_open=$(awk '$1 == "open"' <<<"$results" | wc -l)
n_closed=$(awk '$1 == "closed"' <<<"$results" | wc -l)
n_filt=$(awk '$1 == "filtered"' <<<"$results" | wc -l)
echo "SUMMARY open=$n_open closed=$n_closed filtered=$n_filt total=$((n_open + n_closed + n_filt))/$total"
echo "NOTE UDP (WireGuard 51820, WoL 9) is not measurable by a connect() scan — absent by design, not closed."
exit 0
