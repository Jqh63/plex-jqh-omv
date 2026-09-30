#!/usr/bin/env bash
# test_port_audit.sh — port-audit.sh against REAL sockets (no stub of the probe):
# two listeners on loopback must come out OPEN, a free port closed, and the
# count must add up. Plus the refusals: private target, unresolvable host.
# Usage: bash relay/tests/test_port_audit.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PA="$HERE/../scripts/port-audit.sh"
DISPATCH="$HERE/../scripts/dispatch.sh"
pass=0; fail=0
ok() { echo "  ok   $*"; pass=$((pass + 1)); }
ko() { echo "  FAIL $*"; fail=$((fail + 1)); }

free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }
p1=$(free_port); p2=$(free_port); p3=$(free_port)
python3 - "$p1" "$p2" <<'EOF' &
import socket, sys, time
socks = []
for p in sys.argv[1:]:
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", int(p))); s.listen(8); socks.append(s)
time.sleep(30)
EOF
lpid=$!; trap 'kill $lpid 2>/dev/null' EXIT
sleep 0.5

echo "A. real probes on loopback"
out="$(PORT_AUDIT_ALLOW_PRIVATE=1 PORT_AUDIT_PORTS="$p1 $p2 $p3" bash "$PA" 127.0.0.1)"; rc=$?
[ "$rc" -eq 0 ] && ok "exit 0" || ko "exit $rc"
grep -qx "OPEN $p1/tcp" <<<"$out" && grep -qx "OPEN $p2/tcp" <<<"$out" && ok "both listeners OPEN" || ko "listeners not OPEN: $out"
grep -q "OPEN $p3/" <<<"$out" && ko "free port reported OPEN" || ok "free port not OPEN"
grep -qx "SUMMARY open=2 closed=1 filtered=0 total=3/3" <<<"$out" && ok "summary adds up (2/1/0 of 3)" || ko "summary: $(grep SUMMARY <<<"$out")"
grep -q -- '-> 127.0.0.x ' <<<"$out" && ! tail -n +2 <<<"$out" | grep '127.0.0.1' >/dev/null && ok "target IP masked" || ko "IP not masked"
grep -q '^NOTE UDP' <<<"$out" && ok "UDP blind spot stated" || ko "UDP note missing"

# Filtered = no answer at all. Needs an address that blackholes here; if this
# machine gets a fast refusal instead, the precondition is absent → SKIP, not a
# red (the bench proves the logic, never this host's routing).
if timeout 3 bash -c 'exec 3<>/dev/tcp/10.255.255.1/443' 2>/dev/null; [ $? -eq 124 ]; then
  out="$(PORT_AUDIT_ALLOW_PRIVATE=1 PORT_AUDIT_PORTS="443 444" PROBE_TIMEOUT=1 bash "$PA" 10.255.255.1)"
  grep -qx "SUMMARY open=0 closed=0 filtered=2 total=2/2" <<<"$out" && ok "no answer = filtered (2/2)" || ko "filtered: $(grep SUMMARY <<<"$out")"
else
  echo "  SKIP filtered case — 10.255.255.1 does not blackhole on this machine"
fi

echo "B. refusals (never a green light)"
out="$(PORT_AUDIT_PORTS="$p1" bash "$PA" 127.0.0.1)"; rc=$?
[ "$rc" -eq 3 ] && grep -q 'non-public' <<<"$out" && ok "private target refused (exit 3)" || ko "private target: rc=$rc $out"
out="$(PORT_AUDIT_PORTS="$p1" bash "$PA" no-such-host.invalid)"; rc=$?
[ "$rc" -eq 3 ] && ok "unresolvable host = UNMEASURED (exit 3)" || ko "unresolvable: rc=$rc $out"
out="$(bash "$PA")"; rc=$?
[ "$rc" -eq 3 ] && ok "no host = UNMEASURED (exit 3)" || ko "no host: rc=$rc"

echo "C. dispatch.sh host validation"
tmp="$(mktemp -d)"; trap 'kill $lpid 2>/dev/null; rm -rf "$tmp"' EXIT
sed -e "s#^STAGING_DIR=.*#STAGING_DIR=\"$tmp\"#" -e "s#/opt/wol-relay/scripts/port-audit.sh#$tmp/pa-stub.sh#" "$DISPATCH" > "$tmp/d.sh"
printf '#!/usr/bin/env bash\necho "STUB $*"\n' > "$tmp/pa-stub.sh"; chmod +x "$tmp/pa-stub.sh"
out="$(SSH_ORIGINAL_COMMAND="port-audit home.example.duckdns.org" bash "$tmp/d.sh" 2>&1)"
[ "$out" = "STUB home.example.duckdns.org" ] && ok "valid duckdns host passed through" || ko "valid host: $out"
for bad in 'port-audit 127.0.0.1' 'port-audit x.duckdns.org;id' 'port-audit $(id).duckdns.org' 'port-audit -oN.duckdns.org' 'port-audit evil.com' 'port-audit'; do
  out="$(SSH_ORIGINAL_COMMAND="$bad" bash "$tmp/d.sh" 2>&1)"; rc=$?
  if [ "$rc" -ne 0 ] && ! grep -q STUB <<<"$out"; then ok "rejected: '$bad'"; else ko "accepted: '$bad' → $out"; fi
done

echo "== $pass ok, $fail fail"
[ "$fail" -eq 0 ]
