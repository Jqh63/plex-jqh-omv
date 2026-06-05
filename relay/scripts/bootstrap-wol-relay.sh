#!/usr/bin/env bash
# bootstrap-wol-relay.sh — one-shot initialisation of the GitOps deploy
# channel on the relay VM.
#
# Run ONCE on the VM after the initial system setup (cf. relay/README.md
# § *Initial deployment*) — installs the `deploy` user, its
# authorized_keys with forced-command, the minimal sudoers, dispatch.sh,
# the Caddy systemd drop-in, and (optionally) seeds the two env file
# templates. Idempotent: re-runnable without breaking existing state.
#
# Optional reverse-SSH fallback endpoint: pass a SECOND argument (path to
# the OMV tunnel public key) to also provision the `omvtunnel` user. That
# user can ONLY terminate a reverse-listener bound to the VM loopback
# (127.0.0.1:2222) — no shell, no PTY, no other forward (restrict +
# permitlisten). It is the VM-side endpoint of the out-of-band SSH fallback
# (admin → VM via IAP → tunnel → OMV sshd) decided in knowledge-base ADR
# 2026-06-05-fallback-ssh-out-of-band-reverse-autossh. Omit the 2nd arg to
# skip it entirely (backward compatible with the deploy-only bootstrap).
#
# Prerequisites: this script and the following files must live in the
# SAME directory when invoked (the helper resolves them relative to
# itself):
#   - dispatch.sh
#   - sudoers.deploy
#   - ../systemd/caddy.service.d/wol-relay.conf  (Caddy drop-in)
#   - ../caddy.env.example                       (Caddy vars template)
#   - ../wol-relay.env.example                   (FastAPI vars template)
#
# A typical bootstrap sequence is: scp the whole relay/ subtree to
# /tmp/relay-bootstrap/ on the VM, then
#   sudo bash /tmp/relay-bootstrap/scripts/bootstrap-wol-relay.sh \
#        /tmp/wol-relay-deploy.pub
#
# Effects:
#   - Creates user `deploy` (login shell /bin/bash, home /home/deploy)
#   - Installs /etc/sudoers.d/deploy (mode 0440, validated by visudo)
#   - Installs /opt/wol-relay/scripts/dispatch.sh (mode 0755, owner root)
#   - Installs ~deploy/.ssh/authorized_keys with forced-command pointing
#     to dispatch.sh + hardening flags (no-pty, no-X11-forwarding,
#     no-agent-forwarding, no-port-forwarding)
#   - Installs /etc/systemd/system/caddy.service.d/wol-relay.conf
#     (drop-in for the Caddy unit's EnvironmentFile)
#   - Seeds /etc/caddy/wol-relay.env.example and /etc/wol-relay.env.example
#     IF the runtime files don't exist yet (does NOT overwrite)
#   - Creates /tmp/wol-relay-staging/ (also created by dispatch.sh on
#     each push, but pre-created here for clarity)
#   - (IF a 2nd arg is given) Creates user `omvtunnel` (nologin shell, no
#     sudo) and ~omvtunnel/.ssh/authorized_keys restricted to the single
#     reverse-listener 127.0.0.1:2222 (restrict,permitlisten,nologin)
#
# Reload note: this script does `systemctl daemon-reload` so the Caddy
# drop-in takes effect. It does NOT restart caddy itself — the operator
# must edit /etc/caddy/wol-relay.env with real values, then
# `systemctl restart caddy`, BEFORE the first deploy.sh run.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERR: this script must run as root (sudo bash ...)." >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "Usage: sudo bash $0 <path-to-deploy-public-key> [<path-to-omvtunnel-public-key>]" >&2
  exit 64
fi

PUBKEY_PATH="$1"
# Optional 2nd arg: enables the reverse-SSH fallback endpoint (omvtunnel user).
OMVTUNNEL_PUBKEY_PATH="${2:-}"

# Reverse-tunnel listener exposed on the VM loopback ONLY (jamais 0.0.0.0).
# Must match the OMV-side `ssh -R 127.0.0.1:2222:127.0.0.1:2222` and the OMV
# sshd port (2222). The bind-localhost is the central guard rail of the ADR:
# the OMV sshd is NOT reachable from the VM public IP — one must first be
# logged ON the VM (via IAP) to reach the tunnel socket.
TUNNEL_LISTEN="127.0.0.1:2222"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELAY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DISPATCH_SRC="$SCRIPT_DIR/dispatch.sh"
SUDOERS_SRC="$SCRIPT_DIR/sudoers.deploy"
CADDY_DROPIN_SRC="$RELAY_DIR/systemd/caddy.service.d/wol-relay.conf"
CADDY_ENV_SRC="$RELAY_DIR/caddy.env.example"
WOL_ENV_SRC="$RELAY_DIR/wol-relay.env.example"

for f in "$PUBKEY_PATH" "$DISPATCH_SRC" "$SUDOERS_SRC" "$CADDY_DROPIN_SRC" "$CADDY_ENV_SRC" "$WOL_ENV_SRC"; do
  [[ -f "$f" ]] || { echo "ERR: required file missing: $f" >&2; exit 1; }
done

# The omvtunnel pubkey is optional, but if a path is given it must exist.
if [[ -n "$OMVTUNNEL_PUBKEY_PATH" && ! -f "$OMVTUNNEL_PUBKEY_PATH" ]]; then
  echo "ERR: omvtunnel pubkey path given but not found: $OMVTUNNEL_PUBKEY_PATH" >&2
  exit 1
fi

# --- 1. deploy user --------------------------------------------------------
if ! id -u deploy >/dev/null 2>&1; then
  useradd -m -s /bin/bash deploy
  echo "[bootstrap] user 'deploy' created"
else
  echo "[bootstrap] user 'deploy' already present (skip)"
fi

# --- 2. sudoers ------------------------------------------------------------
install -m 0440 -o root -g root "$SUDOERS_SRC" /etc/sudoers.d/deploy.tmp
if visudo -c -f /etc/sudoers.d/deploy.tmp >/dev/null; then
  mv /etc/sudoers.d/deploy.tmp /etc/sudoers.d/deploy
  echo "[bootstrap] /etc/sudoers.d/deploy installed (visudo OK)"
else
  rm -f /etc/sudoers.d/deploy.tmp
  echo "ERR: visudo rejected the sudoers file, aborting." >&2
  exit 1
fi

# --- 3. dispatch.sh --------------------------------------------------------
install -d -m 0755 -o root -g root /opt/wol-relay/scripts
install -m 0755 -o root -g root "$DISPATCH_SRC" /opt/wol-relay/scripts/dispatch.sh
echo "[bootstrap] /opt/wol-relay/scripts/dispatch.sh installed"

# --- 4. ~deploy/.ssh/authorized_keys --------------------------------------
install -d -m 0700 -o deploy -g deploy /home/deploy/.ssh
PUBKEY=$(cat "$PUBKEY_PATH")
AUTH_LINE='command="/opt/wol-relay/scripts/dispatch.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty '"$PUBKEY"
AUTH_FILE=/home/deploy/.ssh/authorized_keys

# Idempotence: if the key is already present with the correct
# forced-command, skip.
if [[ -f "$AUTH_FILE" ]] && grep -qF "$PUBKEY" "$AUTH_FILE"; then
  echo "[bootstrap] authorized_keys already up to date (skip)"
else
  echo "$AUTH_LINE" > "$AUTH_FILE"
  chmod 0600 "$AUTH_FILE"
  chown deploy:deploy "$AUTH_FILE"
  echo "[bootstrap] authorized_keys installed with forced-command"
fi

# --- 5. Caddy systemd drop-in ---------------------------------------------
install -d -m 0755 -o root -g root /etc/systemd/system/caddy.service.d
install -m 0644 -o root -g root "$CADDY_DROPIN_SRC" /etc/systemd/system/caddy.service.d/wol-relay.conf
systemctl daemon-reload
echo "[bootstrap] caddy.service.d/wol-relay.conf installed + daemon-reload done"

# --- 6. Env file templates (seed only if runtime file is missing) ---------
if [[ ! -f /etc/caddy/wol-relay.env ]]; then
  install -d -m 0755 -o root -g root /etc/caddy
  install -m 0640 -o root -g caddy "$CADDY_ENV_SRC" /etc/caddy/wol-relay.env
  echo "[bootstrap] /etc/caddy/wol-relay.env seeded from template — EDIT IT before restarting caddy"
else
  echo "[bootstrap] /etc/caddy/wol-relay.env already present (skip)"
fi

if [[ ! -f /etc/wol-relay.env ]]; then
  install -m 0640 -o root -g wol "$WOL_ENV_SRC" /etc/wol-relay.env
  echo "[bootstrap] /etc/wol-relay.env seeded from template — EDIT IT before starting wol-relay"
else
  echo "[bootstrap] /etc/wol-relay.env already present (skip)"
fi

# --- 7. Staging dir --------------------------------------------------------
install -d -m 0755 -o deploy -g deploy /tmp/wol-relay-staging
echo "[bootstrap] /tmp/wol-relay-staging ready"

# --- 8. omvtunnel reverse-SSH fallback endpoint (optional) ----------------
# Provisioned only when a 2nd arg (omvtunnel pubkey path) is supplied.
# Cf. knowledge-base ADR 2026-06-05-fallback-ssh-out-of-band-reverse-autossh.
# This user CANNOT run anything: command=nologin neutralises any session, no-pty
# + no-agent/X11/user-rc strip the rest, and permitlisten restricts the -R
# forward to the single loopback listener. That is precisely why it needs no
# forced-command à la dispatch.sh — it has zero command capability to begin with.
#
# NOTE — pas de `restrict` : constaté au runtime 2026-06-05 (OpenSSH 9.2 /
# Debian 12), `restrict` désactive le port-forwarding ET `permitlisten` ne le
# ré-active PAS (contrairement au man) → `remote port forwarding failed for
# listen port 2222`. On liste donc les no-* explicitement SAUF no-port-
# forwarding, en gardant permitlisten pour borner le -R. Sécurité équivalente.
if [[ -n "$OMVTUNNEL_PUBKEY_PATH" ]]; then
  if ! id -u omvtunnel >/dev/null 2>&1; then
    useradd -m -s /usr/sbin/nologin omvtunnel
    echo "[bootstrap] user 'omvtunnel' created (nologin shell, no sudo)"
  else
    echo "[bootstrap] user 'omvtunnel' already present (skip)"
  fi

  install -d -m 0700 -o omvtunnel -g omvtunnel /home/omvtunnel/.ssh
  OMVTUNNEL_PUBKEY=$(cat "$OMVTUNNEL_PUBKEY_PATH")
  OMVTUNNEL_LINE='command="/usr/sbin/nologin",no-pty,no-agent-forwarding,no-x11-forwarding,no-user-rc,permitlisten="'"$TUNNEL_LISTEN"'" '"$OMVTUNNEL_PUBKEY"
  OMVTUNNEL_AUTH=/home/omvtunnel/.ssh/authorized_keys

  if [[ -f "$OMVTUNNEL_AUTH" ]] && grep -qF "$OMVTUNNEL_PUBKEY" "$OMVTUNNEL_AUTH"; then
    echo "[bootstrap] omvtunnel authorized_keys already up to date (skip)"
  else
    echo "$OMVTUNNEL_LINE" > "$OMVTUNNEL_AUTH"
    chmod 0600 "$OMVTUNNEL_AUTH"
    chown omvtunnel:omvtunnel "$OMVTUNNEL_AUTH"
    echo "[bootstrap] omvtunnel authorized_keys installed (restrict + permitlisten=$TUNNEL_LISTEN)"
  fi
else
  echo "[bootstrap] no omvtunnel pubkey (2nd arg) — reverse-SSH endpoint skipped"
fi

cat <<EOF

[bootstrap] DONE.

Next steps (manual, ONE-SHOT):
  1. Edit /etc/caddy/wol-relay.env with real LE_EMAIL / RELAY_DOMAIN / CORS_ORIGIN
  2. Edit /etc/wol-relay.env with real ALLOWED_MAC / WOL_TOKEN / TARGET_HOST / TARGET_PORT
  3. sudo systemctl restart caddy wol-relay
  4. Test from your deploy host:
       ssh wol-relay-deploy status
       ssh wol-relay-deploy health
EOF

if [[ -n "$OMVTUNNEL_PUBKEY_PATH" ]]; then
  cat <<EOF
  5. Reverse-SSH fallback endpoint provisioned (omvtunnel). Remaining,
     ONE-SHOT, host-side (NOT done by this script):
       - VM SSH must be key-only (PasswordAuthentication no) + reachable
         via IAP only (no 0.0.0.0/0 on tcp:22) — cf. relay/README.md
         § Hardening notes.
       - Start the OMV-side tunnel unit (knowledge-base bootstrap-host.sh).
       - Validate from 4G/WG with WireGuard cut server-side:
           gcloud compute ssh <vm> --tunnel-through-iap
           ssh -p 2222 <omv-user>@127.0.0.1     # → OMV sshd through the tunnel
EOF
fi
