# relay — self-hosted HTTP→UDP Wake-on-LAN backend + status oracle

> Status: stable
> Last update: 2026-05-27

## Purpose

This folder ships a small **HTTP→UDP relay** that the PWA `POST`s to in
order to wake a machine on your LAN. Browsers cannot send raw UDP, so
the magic packet must be dispatched by a server-side process. The
relay is intentionally minimal (~80 lines of Python) and self-hosted
on a free-tier VM (e.g. GCP `e2-micro` Always Free).

PWA ↔ relay contract is defined in the [root README](../README.md#api-contract).

Since v7.0 (May 2026), the relay also exposes a `GET /status` endpoint:
the PWA fetches it once on open and gets back `{up, stale, age_s}`,
removing the need for a parallel direct-to-home probe (cf. ADR
`2026-05-27-pwa-plex-jqh-omv-relay-as-oracle` in the operator's private
knowledge base). The relay polls the home via HEAD on a configurable
`STATUS_TARGET_URL`, with a 5 s fresh / 60 s stale in-memory cache.

**Shared wake-state** (since 2026-06): after a `POST /wol`, `/status` also
returns `waking: true` + `wake_age_s` for `WAKE_SIGNAL_TTL_S` (default 150 s)
while the home is still down. This lets *any* open PWA — not just the device
that fired the wake — show the boot countdown, and deduplicates a wake across
devices. The signal clears implicitly once the home answers (`up` wins).

**Push heartbeat, oracle inversion** (since 2026-07-17): when `HEARTBEAT_TOKEN`
is set, the home server declares its own state via `POST /heartbeat` (~15 s
POSTs, dedicated token, burst-tolerant rate limit). A fresh beat (age <
`HEARTBEAT_TTL_S`, measured on the VM's clock — no shared-clock assumption) is
the **primary** `/status` verdict (`source: "heartbeat"` in the response); the
HEAD pull described above is only consulted when the beat is stale/absent, so
a broken push channel degrades to exactly the pull-only behaviour. A last-gasp
`{"up": false}` at clean shutdown flips the verdict down instantly, and the
first post-WoL beat ends the wake campaign and records a to-the-second boot
ETA sample. DOWN detection stays pull-based (a crashed home cannot post) — see
the stale-beat demotion below for how that pull is now *started* rather than
waited for.

**Closing the false-green window** (2026-07-30): a clean stop is covered by the
last-gasp, but a stop that says nothing — hard power cut, kernel panic, sender
killed — left the last `up` beat standing, and `/status` answered green for the
rest of `HEARTBEAT_TTL_S` (up to 45 s of "allumé" on a machine that is off,
which is the wrong way for this verdict to be wrong: the family taps Plex
instead of the wake button). Silence still cannot be read as "down" — that is
what the TTL is for — but it is now a reason to go and **measure**: past one
missed beat (`HEARTBEAT_MISS_PROBE_S`, 20 s, nominal interval being 15 s) the
relay starts a background pull, and a pull that comes back down **and**
post-dates the beat replaces the verdict (`source: "pull"`, `confirmed: true`).
That down has already survived `STATUS_DOWN_CONFIRM_POLLS`, so `confirmed` tells
the PWA it may commit red without its own re-check detour — re-litigating it
client-side would move the window rather than close it. A healthy home never
pays a poll for this, and a dropped beat on a home that answers stays green.
Pinned by `test_missed_beat_triggers_a_pull_that_can_demote_the_stale_green`
plus its two controls (`test_a_fresh_beat_is_never_second_guessed`,
`test_a_missed_beat_on_a_live_home_stays_green` — the one that forbids trading a
false green for a false red).

**The last-gasp outlives the TTL** (2026-07-29): a declaration is not a
measurement whose freshness decays — "I am shutting down" stays true until
something contradicts it, and a powered-off home cannot un-say it. Previously
the TTL erased it after 45 s and every later `/status` **blocked on a full
relay→home pull** (up to `STATUS_POLL_FIRST + RETRY` = 7 s) of a machine known
to be off: per family open, all night, on an e2-micro — and the body lost
`source: "heartbeat"`, so the PWA added its own re-check detour on top of the
wait. The declaration is now sticky, cleared by either channel that can
actually contradict it: any beat (the home is back, and it beats ~4×/min once
up), or a pull that answers. That pull is what covers "the home is up but its
heartbeat sender is not" — kept on a slow clock (`DECLARED_REVALIDATE_S`, 60 s
default) and fired in the background, never in a reader's critical path.
Pinned by `tests/test_heartbeat.py::test_declared_down_survives_the_beat_ttl`
(counts the polls) and `…_is_revalidated_on_the_slow_clock` (the escape hatch).

**Shared wake-FAILED signal** (2026-07-29): when a campaign runs its full
course — every burst, plus a grace period up to `WAKE_FAIL_GRACE_S` (default
`WAKE_SIGNAL_TTL_S`, so the instant the relay stops advertising "it's coming
up" is the instant it may say "it didn't") — without the home ever answering,
`/status` serves `wake_failed: true`. Two things no client could compute: the
device that tapped learns at ~150 s instead of its own 5-min timeout, and every
OTHER open PWA learns at all (before this, two phones in the same room showed
one red and one blue after the same failed wake). Retracted by a new `/wol` and
by the home coming up; bounded by `WAKE_FAIL_SIGNAL_TTL_S` (600 s), past which
"the last wake failed" is no longer news about now. Served in preference to
`waking` — testing `waking` first silently coupled the signal's visibility to
two constants agreeing, which the compressed-grace test broke.

**Server-side wake campaign** (since 2026-07-17): a `POST /wol` also arms a
relay-side task that re-sends the magic packets at +15/30/60/90 s
(`WOL_CAMPAIGN_DELAYS_S`) until the home is seen up. The retry bursts used to
be `setTimeout`s in the PWA page, which Android freezes as soon as the phone
is pocketed — precisely the nominal gesture. The campaign stops at the first
fresh `up` verdict, when the uptime window closes (if it was armed inside it —
no re-wake after a scheduled shutdown), or after the last burst. One campaign
at a time: concurrent `/wol` posts (several devices/scripts within a minute)
attach to it, and a `/wol` against an up home arms nothing.

`/status` also serves `eta_s`: the relay measures each boot's wall-clock (from
`/wol` to the first **non-degraded** up signal — services actually serving, not
the first HTTP answer, which lands ~20 s earlier while the apps are still
starting; heartbeat and pull paths both gate on it), keeps a small in-memory
ring, and serves its median. Every open PWA seeds its wake countdown from this single value, so the
timer is identical across devices instead of each running its own local
boot-history median. Ephemeral (resets on relay restart → `ETA_FALLBACK_S`,
default 80 s, until a few wakes reconverge).

**Device / usage telemetry** (log-only, no persistence): the PWA sends an opaque
`X-Client-Id` (a random UUID it persists locally — not a secret, no PII) on
`/status` and `/wol`. The relay derives a coarse device class from the
`User-Agent` and logs `wol ip=… device=… cid=… status=200` per wake, plus a
deduped `open ip=… device=… cid=…` per client at most once every
`USAGE_LOG_DEDUPE_S` (default 600 s) on `/status` — so "who woke it / when is the
PWA open, on what kind of device" is visible via `journalctl -u wol-relay`
without flooding. The client-id is charset/length-constrained before logging
(anti log-injection). No MAC or token is ever logged.

## Files in this folder

| File | Runtime destination | Role |
|---|---|---|
| `app.py` | `/opt/wol-relay/app.py` (owner `wol:wol`) | FastAPI relay. Rate-limits per source IP, validates token + MAC allowlist, audit-logs every attempt, sends 3 magic packets spaced 500 ms apart |
| `Caddyfile` | `/etc/caddy/Caddyfile` | Reverse proxy + automatic HTTPS via Let's Encrypt + CORS handling on 502 |
| `wol-relay.service` | `/etc/systemd/system/wol-relay.service` | systemd unit for uvicorn, sandboxed (NoNewPrivileges, ProtectSystem=strict) |
| `wol-relay.env.example` | (template) | FastAPI env file template. Copy to `/etc/wol-relay.env` (mode `0640 root:wol`), fill in real values |
| `caddy.env.example` | (template) | Caddy env file template. Copy to `/etc/caddy/wol-relay.env` (mode `0640 root:caddy`), fill in real values |
| `systemd/caddy.service.d/wol-relay.conf` | `/etc/systemd/system/caddy.service.d/wol-relay.conf` | Drop-in that wires `EnvironmentFile=/etc/caddy/wol-relay.env` into the Caddy unit |
| `scripts/dispatch.sh` | `/opt/wol-relay/scripts/dispatch.sh` (owner `root`, mode 0755) | Forced-command in `~deploy/.ssh/authorized_keys`, routes the SSH GitOps subcommands |
| `scripts/sudoers.deploy` | `/etc/sudoers.d/deploy` (mode 0440) | Minimal sudoers for the `deploy` user: 3 installs + 3 systemctl verbs, exact paths |
| `scripts/bootstrap-wol-relay.sh` | (run one-shot) | Installs the `deploy` user, sudoers, dispatch.sh, drop-in, env templates, authorized_keys with forced-command |
| `scripts/deploy.sh` | (run on the deploying host) | Pipes app.py + Caddyfile + wol-relay.service to the VM and triggers apply + health |

## Configuration model

Deployment-specific values **never live in the repo**. The Caddyfile
and FastAPI process read them at runtime from two env files on the VM,
each owned by the relevant service user:

- `/etc/wol-relay.env` (mode `0640 root:wol`) — FastAPI variables:
  `ALLOWED_MAC`, `WOL_TOKEN`, `TARGET_HOST`, `TARGET_PORT`. Optional:
  `TARGET_IP` (static-IP fallback, see below),
  `STATUS_TARGET_URL` (enables `/status`),
  `STATUS_POLL_FIRST_TIMEOUT_S`/`STATUS_POLL_RETRY_TIMEOUT_S`/`STATUS_CACHE_FRESH_S`/`STATUS_CACHE_STALE_S`
  (tuning), `WAKE_SIGNAL_TTL_S` (how long `/status` advertises `waking` after a
  wake, default 150 s), `WOL_CAMPAIGN_DELAYS_S` (comma-separated offsets in
  seconds of the server-side wake campaign bursts, default `15,30,60,90`),
  `USAGE_LOG_DEDUPE_S` (min interval between `open` log
  lines per client, default 600 s), `UPTIME_WINDOW` (e.g. `13h50-00h10` or `13:50-00:10` —
  echoed as a `window` field in `/status`; the PWA adopts it
  automatically, so every user gets the scheduled-uptime "En veille"
  display without a new URL. Validated at startup — a malformed value
  refuses to boot. Since the deployable window file this env is only a
  fallback: `/opt/wol-relay/window` — pushed via `ssh wol-relay-deploy
  push-window` + `apply-window`, sourced from the home server's
  versioned autoshutdown config — wins when present, and is re-read on
  change without a restart).
- `/etc/caddy/wol-relay.env` (mode `0640 root:caddy`) — Caddy
  variables referenced in the Caddyfile as `{$VAR}`: `LE_EMAIL`,
  `RELAY_DOMAIN`, `CORS_ORIGIN`.

Templates with placeholders live in this folder (`*.env.example`). The
`bootstrap-wol-relay.sh` script seeds them on the VM but never
overwrites existing files — you must edit the real values manually.

### Static-IP fallback (`TARGET_IP`)

`TARGET_HOST` is resolved server-side on every burst, which is what stops a
client from redirecting magic packets at an arbitrary address. The cost is a
hard dependency on the dynamic-DNS provider: when it goes down, `/wol` answers
`502 dns_resolution_failed` and **nobody can wake the home** — while the home
itself is fine and the packet would have been delivered.

Set `TARGET_IP` to the public address and that failure mode disappears: the
resolver falls back to it, and only when DNS has already failed.

```
TARGET_IP=203.0.113.10
```

- **Only set it if the address is genuinely stable** (fixed/dedicated ISP
  address, or a static reservation). On a rotating address a stale value fans
  magic packets at whoever holds that IP now. Harmless in content — a magic
  packet is inert to anything but the matching NIC — but it is someone else's
  network, and the wake silently stops working.
- It is **never** client-supplied, so the redirect property above is preserved.
- Exercising it logs a `WARNING` ("dns resolution of the target failed"). That
  line is the only signal the provider is down, since wakes keep working.
- `GET /health/deep` reports `dns: fallback_ip` (still `503` overall — this is
  a degraded state worth seeing) instead of `dns: fail`. The PWA's "Tester le
  relais" surfaces it inline.

## Runtime architecture

```
[PWA on https://<your-name>.github.io]
        │ POST /wol  {mac: "AA:BB:..."}  Header X-Token
        ▼  HTTPS 443 (Caddy auto-LE)
[Caddy reverse_proxy :443]
        │ CORS Allow-Origin set if Origin matches {$CORS_ORIGIN}
        │ OPTIONS preflight handled at the Caddy level (204)
        ▼  HTTP localhost:8000
[uvicorn — user `wol`, non-priv, systemd sandboxed]
        │ Pydantic regex validates the MAC
        │ Token compare (X-Token header vs WOL_TOKEN env)
        │ MAC allowlist (ALLOWED_MAC env)
        │ DNS resolve TARGET_HOST → public IP of the LAN
        │ socket UDP SO_BROADCAST → 3 packets spaced 500 ms
        ▼
[your home router NAT, UDP/9 → LAN broadcast]
        ▼
[target machine wakes up]
```

## GitOps deploy channel

A small SSH-based channel (`wol-relay-deploy`) lets the deploying host
push code/config changes to the VM without manual `scp`+`sudo`. The
forced-command on the VM only accepts a static set of subcommands.

### Standard workflow (post-merge from main)

From the host that holds your SSH key:

```bash
bash relay/scripts/deploy.sh
```

The script pipes the 3 files (`app.py`, `Caddyfile`,
`wol-relay.service`) over stdin to the VM-side `dispatch.sh`, then
triggers `apply` (install + `systemctl daemon-reload` + `restart
wol-relay` + `reload caddy`) and a final `health`. Typical duration:
~5 s.

### Individual subcommands

```bash
ssh wol-relay-deploy status         # systemctl is-active wol-relay caddy
ssh wol-relay-deploy health         # curl http://127.0.0.1:8000/health
ssh wol-relay-deploy logs-wol-relay        # journalctl tail, last 100 lines (~5 days)
ssh wol-relay-deploy logs-wol-relay 500    # wider window
ssh wol-relay-deploy logs-wol-relay 3000   # months of history (journald holds ~174 MB)
ssh wol-relay-deploy logs-caddy     # journalctl -u caddy -n 100 --no-pager
ssh wol-relay-deploy logs-caddy 3000   # ~35 h — the access log is dense
ssh wol-relay-deploy log-footprint  # journald size + log dirs + df (read-only)
ssh wol-relay-deploy tunnel-status  # reverse-SSH fallback: listener + sessions (read-only)
ssh wol-relay-deploy tunnel-reap    # free a listener held by a stale session
ssh wol-relay-deploy push-app < relay/app.py             # stage only
ssh wol-relay-deploy push-caddyfile < relay/Caddyfile    # stage only
ssh wol-relay-deploy push-service < relay/wol-relay.service
ssh wol-relay-deploy apply          # install + restart (run push-* first)
ssh wol-relay-deploy push-window    # stage uptime window (stdin, one line)
ssh wol-relay-deploy apply-window   # install /opt/wol-relay/window (hot-reload)
```

> ⚠️ **`dispatch.sh` and `sudoers.deploy` are NOT deployed by `deploy.sh`** —
> they are installed by `bootstrap-wol-relay.sh`, so a new subcommand or sudoers
> entry (like the `500`/`3000` depths above) needs the bootstrap re-run on the
> VM, or the file reinstalled by hand (admin, Cloud Shell — idempotent). App /
> Caddyfile / service changes go through `deploy.sh` as usual. Merging alone
> changes nothing on the VM.

Security by construction: forced-command `dispatch.sh` on the VM
(static enum whitelist, no free-form parsing), minimal sudoers
(3 installs + 3 systemctl verbs, exact paths), fixed staging
directory `/tmp/wol-relay-staging/`. No GitHub PAT or secret embedded
on the VM — files flow over stdin SSH, no `git pull` server-side.

### Second service on this channel: `home-watch`

The same channel also carries **`home-watch`**, an external homelab
monitor (systemd timer that probes the home's public surface from this
VM and emails on outage). Its code is **private** (it enumerates the
monitored home surface) and lives in the author's `knowledge-base`
repo — it is pushed in over stdin and **never stored here**. The
channel just exposes generic handlers:

```bash
ssh wol-relay-deploy push-home-watch{,-service,-timer}  # stage (stdin)
ssh wol-relay-deploy apply-home-watch                   # install + enable timer
ssh wol-relay-deploy home-watch-status                  # timer active + next run
ssh wol-relay-deploy logs-home-watch                    # journalctl -n 100
```

Prereqs (provisioned by `bootstrap-wol-relay.sh` step 9): `homewatch`
user, `/opt/home-watch` + `/var/lib/home-watch`, and `msmtp` for mail
egress. The two secret files (`/etc/msmtprc` with a dedicated Gmail
app-password, `/etc/home-watch.env`) are posted manually (0600).
Deploy is driven from the private repo's `deploy-home-watch.sh`.

### Third service on this channel: `pock-sync`

The channel also carries **`pock-sync`**, a per-app JSON blob store for
the [Pock PWAs](https://github.com/Jqh63/pock) (code public in that
repo under `sync/`, exposed behind this Caddy on the `/pock/*` path —
see that repo's `sync/README.md` for the service itself and its
one-shot bootstrap):

```bash
ssh wol-relay-deploy push-pock-sync-{app,service}  # stage (stdin)
ssh wol-relay-deploy apply-pock-sync               # install + restart
ssh wol-relay-deploy pock-sync-status              # is-active + /pock/health
ssh wol-relay-deploy logs-pock-sync                # journalctl -n 100
ssh wol-relay-deploy pock-dump                     # tar of the blobs → stdout
```

`pock-dump` is read-only and pulled daily by the home server, which
feeds the blobs to its regular backup — the VM is never the sole holder
of the data. Deploy is driven from the Pock repo's `sync/deploy.sh`.

### One-shot bootstrap (DR or first install)

Run UNCE to activate the channel. If the VM already exists but
without `wol-relay-deploy`, this is the procedure.

**1. Generate the dedicated SSH key on your deploying host**

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_wol_relay_deploy -N "" \
  -C "wol-relay-deploy-$(hostname)"
cat ~/.ssh/id_ed25519_wol_relay_deploy.pub
```

Add to `~/.ssh/config`:

```
Host wol-relay-deploy
  HostName <VM_STATIC_IP>
  User deploy
  IdentityFile ~/.ssh/id_ed25519_wol_relay_deploy
  IdentitiesOnly yes
```

**2. Drop the bootstrap files on the VM**

From a host with admin SSH access to the VM (the `deploy` channel
doesn't exist yet — use your regular admin user):

```bash
scp -r relay/ admin-vm:/tmp/relay-bootstrap/
echo '<paste id_ed25519_wol_relay_deploy.pub here>' > /tmp/wol-relay-deploy.pub
scp /tmp/wol-relay-deploy.pub admin-vm:/tmp/
```

**3. Run the bootstrap on the VM**

```bash
ssh admin-vm
sudo bash /tmp/relay-bootstrap/scripts/bootstrap-wol-relay.sh \
     /tmp/wol-relay-deploy.pub
```

Effects: `deploy` user created, `/etc/sudoers.d/deploy` validated by
visudo, `/opt/wol-relay/scripts/dispatch.sh` installed, Caddy drop-in
posted, env templates seeded (NOT real values),
`~deploy/.ssh/authorized_keys` written with forced-command + hardened
flags (no-pty, no-X11-forwarding, no-agent-forwarding,
no-port-forwarding).

### Re-running the bootstrap on a live VM

Once provisioned, the VM needs **nothing from a workstation** to pick up a new
`dispatch.sh`, `sudoers.deploy` or hardening drop-in: this repo is public, so it
fetches its own sources, and both public keys are re-derivable from the
`authorized_keys` already installed. The script is idempotent and restarts
neither Caddy nor `wol-relay` (it only reloads sshd, so an admin session
survives). Run it from an admin shell on the VM:

```bash
( set -e
  [ "$(hostname -s)" = "wol-relay" ] || { echo "WRONG MACHINE — STOP"; exit 1; }
  SRC=/tmp/relay-bootstrap
  rm -rf "$SRC" && mkdir -p "$SRC"
  URL=https://github.com/Jqh63/plex-jqh-omv/archive/refs/heads/main.tar.gz
  curl -fsSL "$URL" | tar xz -C "$SRC"
  BS="$SRC/plex-jqh-omv-main/relay/scripts/bootstrap-wol-relay.sh"
  [ -f "$BS" ] || { echo "bootstrap not found — STOP"; exit 1; }
  sudo grep -o 'ssh-ed25519 [A-Za-z0-9+/=]*' /home/deploy/.ssh/authorized_keys > /tmp/d.pub
  sudo grep -o 'ssh-ed25519 [A-Za-z0-9+/=]*' /home/omvtunnel/.ssh/authorized_keys > /tmp/t.pub
  grep -q '^ssh-ed25519 AAAA' /tmp/d.pub || { echo "deploy key not extracted — STOP"; exit 1; }
  grep -q '^ssh-ed25519 AAAA' /tmp/t.pub || { echo "tunnel key not extracted — STOP"; exit 1; }
  sudo bash "$BS" /tmp/d.pub /tmp/t.pub
)
```

⚠️ The two `grep -q` guards are **not** decoration. An empty extraction still
satisfies the script's `[[ -f ]]` precondition, and the bootstrap would then
write an `authorized_keys` carrying options but **no key** — locking out the
`deploy` channel and the reverse tunnel at once, on the machine whose whole
job is to be reachable when the home server is not. Never drop them.

Omit the second argument only if this VM has no reverse-SSH endpoint.

**4. Fill in the real env values on the VM**

```bash
sudo vi /etc/wol-relay.env          # ALLOWED_MAC, WOL_TOKEN, TARGET_HOST, TARGET_PORT
sudo vi /etc/caddy/wol-relay.env    # LE_EMAIL, RELAY_DOMAIN, CORS_ORIGIN
sudo systemctl restart caddy wol-relay
```

**5. End-to-end smoke test from the deploying host**

```bash
ssh wol-relay-deploy status        # → active active
ssh wol-relay-deploy health        # → {"status":"ok"}
bash relay/scripts/deploy.sh       # → DONE
```

If any of these fail: check `journalctl -u sshd` on the VM
(forced-command denial), `sudo -l -U deploy` (expected sudoers),
`cat ~deploy/.ssh/authorized_keys` (forced-command present).

### Reverse-SSH fallback endpoint (optional, operator-specific)

This VM can double as an **out-of-band SSH fallback** to a home server
whose only remote access path is a VPN that might itself break. The
design and threat model live in the operator's knowledge-base ADR
`2026-06-05-fallback-ssh-out-of-band-reverse-autossh`; this section only
covers the VM-side endpoint.

**Recovery chain** (2 hops, 2 auths): `admin → VM (IAP: Google + 2FA) →
127.0.0.1:2222 tunnel socket → home sshd (password + Fail2ban)`. The home
server keeps a permanent **outbound** reverse tunnel open during its uptime
(`ssh -N -R 127.0.0.1:2222:127.0.0.1:2222 omvtunnel@<vm>`), so the path
exists *before* the VPN breaks — you cannot ask a locked-out host to open
it after the fact.

To provision the endpoint:

```bash
# On the HOME server: generate a dedicated key (private stays there).
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_omvtunnel -N "" -C "omvtunnel"
cat ~/.ssh/id_ed25519_omvtunnel.pub          # → copy this

# Drop the .pub on the VM, then re-run the bootstrap WITH the 2nd arg:
sudo bash /tmp/relay-bootstrap/scripts/bootstrap-wol-relay.sh \
     /tmp/wol-relay-deploy.pub /tmp/id_ed25519_omvtunnel.pub
```

The bootstrap then creates the `omvtunnel` user and an `authorized_keys`
restricted to the single loopback listener (`command=nologin` + `no-pty` +
`no-agent/x11/user-rc` + `permitlisten="127.0.0.1:2222"`). Note: **not**
`restrict` — under OpenSSH 9.2 it disables port-forwarding and `permitlisten`
does *not* re-enable it, so the `-R` fails (`remote port forwarding failed`);
the explicit `no-*` set leaves `-R` allowed while `command=nologin` still
guarantees zero command capability. The home-side tunnel unit (systemd
`ssh -N -R`) is versioned separately in the operator's homelab repo. Validate
**from 4G/VPN-off** with the home VPN deliberately cut server-side — a LAN test proves nothing
here.

**Stale listener after an abrupt reboot of the home server.** Its old session
stays registered on the VM, sshd keeps `127.0.0.1:2222` bound, and every
reconnect fails with `remote port forwarding failed for listen port 2222` —
the fallback channel is down for as long as the kernel TCP keepalive takes to
expire (~2 h), i.e. exactly when an outage is in progress. Two mitigations,
both installed by the bootstrap:

- `ClientAliveInterval 30` / `ClientAliveCountMax 3` in
  `/etc/ssh/sshd_config.d/10-tunnel-keepalive.conf` — sshd notices the dead
  peer in ~90 s and releases the listener by itself. Global on purpose: Debian
  includes that drop-in dir at the *top* of `sshd_config`, so a
  `Match User omvtunnel` block there would capture every global directive
  that follows in the main file.
- `ssh wol-relay-deploy tunnel-status` (read-only: listener + live sessions —
  a listener with no session is the stale state) and
  `ssh wol-relay-deploy tunnel-reap` (drop them now). The reap is bounded by
  construction: `omvtunnel` is a nologin user that can hold nothing but this
  tunnel, and the home server reconnects on its own restart timer.

## Initial VM provisioning (recovery from zero)

This section covers building a fresh VM from scratch. Skip it if you
already have a Linux VM with public HTTPS reachability — go straight
to *Bootstrap* above.

### 1. Cloud provider

Any small VM with UDP egress and a public HTTPS endpoint works. Free
options as of 2026: **GCP Compute Engine e2-micro**
(us-west1/central1/east1), **Oracle Cloud Always Free**. Avoid
serverless platforms that can't open raw UDP sockets (Cloudflare
Workers, Vercel Edge, Deno Deploy).

### 2. DNS

Point an A record (`relay.example.com`) at the VM's public IP. A
static IP at the cloud provider level is strongly recommended — Caddy
will request a Let's Encrypt cert for this name on first start, and
LE rate-limits per name.

### 3. Firewall

Open 80/tcp + 443/tcp to the world (Caddy needs 80 for the LE HTTP-01
challenge). For SSH, pick one of:

- **Base relay only** — restrict SSH (tcp:22) to your admin IP. Simplest,
  smallest public surface.
- **VM also used as an out-of-band fallback** (the optional `omvtunnel`
  reverse-SSH endpoint, see *Hardening notes*) — do **not** pin SSH to a
  fixed source IP: the recovery scenario (admin away from home, home VPN
  down) implies an *arbitrary* source IP, so pinning would lock you out at
  the exact moment the fallback is needed. Instead expose **no public
  tcp:22 at all** and reach SSH through a strong-auth broker that works
  from any network. On GCP that is **IAP** (Identity-Aware Proxy): allow
  only the IAP range `35.235.240.0/20` to tcp:22, drop any `0.0.0.0/0`
  tcp:22 rule, and grant your account the *IAP-secured Tunnel User* role —
  auth becomes Google account + 2FA from anywhere, no port open to the
  world.

### 4. Base packages (Debian 12)

```bash
sudo apt update && sudo apt full-upgrade -y && sudo apt install -y \
  debian-keyring debian-archive-keyring apt-transport-https curl gnupg \
  python3-venv python3-pip ufw vim fail2ban

# Caddy from the official repo
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
  sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | \
  sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
sudo systemctl stop caddy && sudo systemctl disable caddy

# Non-priv user for uvicorn
sudo useradd -r -s /usr/sbin/nologin -d /opt/wol-relay -m wol || true
sudo chown -R wol:wol /opt/wol-relay
sudo -u wol python3 -m venv /opt/wol-relay/venv
sudo -u wol /opt/wol-relay/venv/bin/pip install --upgrade pip wheel
sudo -u wol /opt/wol-relay/venv/bin/pip install fastapi 'uvicorn[standard]' 'httpx[http2]'

# Optional UFW (defense in depth; cloud firewall is primary)
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
sudo ufw --force enable

# SSH key-only (GCP images already default to this — verify and enforce).
# Required if this VM is also the out-of-band fallback endpoint: a password
# prompt reachable through the chain would be a brute-force surface.
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh

# Fail2ban: enable the sshd jail (backend systemd reads journald).
sudo tee /etc/fail2ban/jail.local >/dev/null <<'JAIL'
[sshd]
enabled  = true
backend  = systemd
maxretry = 4
bantime  = 1h
findtime = 10m
JAIL
sudo systemctl enable --now fail2ban
```

### 5. Continue with the GitOps bootstrap

From this point, follow *GitOps deploy channel → One-shot bootstrap*
above. The bootstrap script installs the drop-in, seeds the env
templates and posts the dispatcher.

## Status oracle (`GET /status`)

`/status` is the PWA's single source of truth for "is the home server
up?". The PWA does one `fetch` to the relay and gets a decisive answer
in <1 s in nominal conditions.

Since v8.17 the endpoint requires the same shared `X-Token` header as
`/wol` — an unauthenticated caller gets a `401` and learns nothing (not
even whether a status target is configured). A PWA without a configured
token treats the 401 as a degraded oracle and falls back to its direct
home probe.

### Response shape

```json
{"up": true, "stale": false, "age_s": 3}
```

- `up`: last verified state of the target (`true` if a recent HEAD got
  `<500`, `false` otherwise or if no successful probe in `>60 s`).
- `stale`: `true` if the verdict comes from a successful probe between
  5 s and 60 s old (PWA may want to show a subtle "Vérification…"
  indicator while the relay re-polls in background on the next call).
- `age_s`: seconds since the last successful probe (`null` if expired).

### Polling model

- Per-request, on-demand: no background timer, no per-tenant state.
- Cache fresh window (`STATUS_CACHE_FRESH_S=5`): cached verdict reused
  without hitting the home.
- Cache stale window (`5..STATUS_CACHE_STALE_S=60`): cached verdict
  returned to the client AND a fresh poll fires (under a single
  `asyncio.Lock` to dedupe concurrent callers).
- Expired (`>STATUS_CACHE_STALE_S`): verdict expires, response degrades
  to `{up: false, stale: false, age_s: null}` until a successful poll.

### Target requirements

`STATUS_TARGET_URL` must point at a URL that returns an HTTP response
(any `<500`) when the home is reachable. The DuckDNS wildcard cert
covers `*.example.duckdns.org` but **not** the apex — so the bare
`https://example.duckdns.org` triggers a TLS SAN mismatch and isn't a valid
target. Use an existing subdomain instead (see
[`wol-relay.env.example`](wol-relay.env.example) for working examples).

### Disabled mode (env var unset)

If `STATUS_TARGET_URL` is unset, `/status` returns `503 "status target
not configured"`. The PWA treats this exactly like a network failure
and falls back to a direct HEAD against the home — same UX as a GCP
outage. This means deploying the v7.0 backend before configuring the
env var is safe (no `/wol` regression).

## Hardening notes

| Measure | Why |
|---|---|
| SSH key-only (`PasswordAuthentication no`) | No password to brute-force; mandatory when the VM is an out-of-band fallback hop |
| Cloud firewall: no `0.0.0.0/0` on tcp:22 — IAP range for admin, + the reverse-tunnel source IP | Out-of-band admin reach → IAP (Google account + 2FA, any source) instead of an IP pin that would lock you out mid-incident. **But** the reverse tunnel dials the VM:22 directly from the home server's WAN IP (not via IAP), so that one static IP must stay allowed — it's transport, not admin recovery. Base-relay-only deployments may simply IP-restrict to the admin IP |
| Fail2ban `sshd` jail (systemd backend) | Caps auth-failure velocity on any connection that reaches sshd (incl. IAP-tunneled) |
| UFW redundant (deny incoming + allow 22/80/443) | Defense in depth if the cloud firewall is misconfigured |
| `omvtunnel` user: `command=nologin` + `no-pty` + `no-agent/x11/user-rc` + `permitlisten="127.0.0.1:2222"` | Reverse-SSH fallback endpoint can ONLY terminate the one loopback-bound listener — no shell, no PTY. Zero command capability, so no forced-command needed. (Not `restrict`: it kills the `-R` and `permitlisten` doesn't re-enable it under OpenSSH 9.2.) |
| Reverse-tunnel listener bound to VM loopback (not `0.0.0.0`) | The tunnelled OMV sshd is **not** reachable from the VM public IP — one must first be logged ON the VM (via IAP) to reach it. Closes the "VM becomes a free public SSH-to-home" risk |
| Caddy auto-HTTPS Let's Encrypt | TLS without manual config; the token transits in an encrypted header |
| Caddy CORS on 502 | Error responses don't break browser-side diagnostics |
| uvicorn `--no-access-log` | The token never ends up in a log |
| Sliding-window rate limit on `/wol` (10 req/min/IP, in-memory) | Caps scan / brute force velocity before any other check; refuses with 429 |
| Audit log on `/wol` (`journalctl -u wol-relay`) | Every attempt is logged with source IP + status (200/401/403/429/502). Token and MAC are **never** logged. Lets you spot unauthorized scans |
| `/status` requires the shared `X-Token` (since v8.17), returns no MAC/token, fixed shape | Closes the anonymous up/down info disclosure; the token check runs before any config-state branch, so a 401 reveals nothing |
| systemd `NoNewPrivileges` + `ProtectSystem=strict` + `PrivateTmp` | Limits the blast radius of a hypothetical RCE in FastAPI |
| user `wol` (non-priv, no shell) | uvicorn doesn't run as root |
| `EnvironmentFile` mode `0640 root:<service-user>` | Tokens readable only by root and the service user |
| MAC allowlist (`ALLOWED_MAC` env) | A leaked token can only wake the listed MAC, no other machines |
| `TARGET_HOST` resolved server-side | Clients cannot redirect packets to an arbitrary IP |
| 3 magic packets spaced 500 ms | Compensates for transient UDP drops (excellent gain/cost ratio) |

## References

- PWA (consumer of this relay): the root of this repo
- Reference operator's deployment notes (private homelab context): see
  the operator's private knowledge base — not needed for fork/use
