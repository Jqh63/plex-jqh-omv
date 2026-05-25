# relay — self-hosted HTTP→UDP Wake-on-LAN backend

> Status: stable
> Last update: 2026-05-25

## Purpose

This folder ships a small **HTTP→UDP relay** that the PWA `POST`s to in
order to wake a machine on your LAN. Browsers cannot send raw UDP, so
the magic packet must be dispatched by a server-side process. The
relay is intentionally minimal (~80 lines of Python) and self-hosted
on a free-tier VM (e.g. GCP `e2-micro` Always Free).

PWA ↔ relay contract is defined in the [root README](../README.md#api-contract).

## Files in this folder

| File | Runtime destination | Role |
|---|---|---|
| `app.py` | `/opt/wol-relay/app.py` (owner `wol:wol`) | FastAPI relay. Validates token + MAC allowlist, sends 3 magic packets spaced 500 ms apart |
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
  `ALLOWED_MAC`, `WOL_TOKEN`, `TARGET_HOST`, `TARGET_PORT`.
- `/etc/caddy/wol-relay.env` (mode `0640 root:caddy`) — Caddy
  variables referenced in the Caddyfile as `{$VAR}`: `LE_EMAIL`,
  `RELAY_DOMAIN`, `CORS_ORIGIN`.

Templates with placeholders live in this folder (`*.env.example`). The
`bootstrap-wol-relay.sh` script seeds them on the VM but never
overwrites existing files — you must edit the real values manually.

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
ssh wol-relay-deploy status       # systemctl is-active wol-relay caddy
ssh wol-relay-deploy health       # curl http://127.0.0.1:8000/health
ssh wol-relay-deploy push-app < relay/app.py             # stage only
ssh wol-relay-deploy push-caddyfile < relay/Caddyfile    # stage only
ssh wol-relay-deploy push-service < relay/wol-relay.service
ssh wol-relay-deploy apply        # install + restart (run push-* first)
```

Security by construction: forced-command `dispatch.sh` on the VM
(static enum whitelist, no free-form parsing), minimal sudoers
(3 installs + 3 systemctl verbs, exact paths), fixed staging
directory `/tmp/wol-relay-staging/`. No GitHub PAT or secret embedded
on the VM — files flow over stdin SSH, no `git pull` server-side.

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

Restrict SSH to your admin IP. Open 80/tcp + 443/tcp to the world
(Caddy needs 80 for the LE HTTP-01 challenge).

### 4. Base packages (Debian 12)

```bash
sudo apt update && sudo apt full-upgrade -y && sudo apt install -y \
  debian-keyring debian-archive-keyring apt-transport-https curl gnupg \
  python3-venv python3-pip ufw vim

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
sudo -u wol /opt/wol-relay/venv/bin/pip install fastapi 'uvicorn[standard]'

# Optional UFW (defense in depth; cloud firewall is primary)
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
sudo ufw --force enable
```

### 5. Continue with the GitOps bootstrap

From this point, follow *GitOps deploy channel → One-shot bootstrap*
above. The bootstrap script installs the drop-in, seeds the env
templates and posts the dispatcher.

## Hardening notes

| Measure | Why |
|---|---|
| Cloud firewall SSH IP-restricted | Reduces SSH public surface to your admin IP only |
| UFW redundant (deny incoming + allow 22/80/443) | Defense in depth if the cloud firewall is misconfigured |
| Caddy auto-HTTPS Let's Encrypt | TLS without manual config; the token transits in an encrypted header |
| Caddy CORS on 502 | Error responses don't break browser-side diagnostics |
| uvicorn `--no-access-log` | The token never ends up in a log |
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
