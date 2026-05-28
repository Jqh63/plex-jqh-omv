---
name: deploy-relay
description: Deploy the WoL relay (app.py + Caddyfile + wol-relay.service) to the always-free VM via the wol-relay-deploy GitOps channel. Use after changing anything under relay/. Read-only checks available via status/health.
argument-hint: "[what changed in relay/]"
---

# Deploy the WoL relay

Change: **$ARGUMENTS**.

The relay (FastAPI + Caddy on a small always-free VM) is deployed through a
forced-command channel — the VM never pulls the repo; the 3 files are piped
over stdin. See `relay/README.md` § *Automation* and `relay/scripts/deploy.sh`.

## Prerequisite
SSH alias `wol-relay-deploy` + key `id_ed25519_wol_relay_deploy` present on
the deploying host (the code-server sandbox has them). The VM-side
`dispatch.sh` + sudoers were bootstrapped once via `bootstrap-wol-relay.sh`.

## Steps

1. **Land the change first**: edit `relay/app.py` / `relay/Caddyfile` /
   `relay/wol-relay.service`, open a PR, merge to `main` (the deploy reads the
   working tree, but keeping main as the source of truth avoids drift).

2. **Deploy** (pipes the 3 files via stdin, then apply + health):
   ```bash
   bash relay/scripts/deploy.sh
   # WOL_RELAY_ALIAS=other-alias bash relay/scripts/deploy.sh   # to override
   ```
   Exit 0 = apply + health OK ; exit 1 = a push or apply failed.

3. **Verify** (read-only, idempotent):
   ```bash
   ssh wol-relay-deploy status    # service state + last apply summary
   ssh wol-relay-deploy health    # GET /health through Caddy (auto-HTTPS)
   ```

4. **End-to-end sanity**: the PWA's WoL one-tap should hit `POST /wol` and
   get success feedback. The Homepage tile `WoL Relay (GCP)` (siteMonitor
   `/health`) should stay green.

## Notes
- The VM is structurally slow (e2-micro shared vCPU) — apt/pip ops take ~3-4×
  nominal. Don't over-interpret a slow apply.
- No secrets ship in the repo: the relay's env (token, GCP specifics) lives on
  the VM / in the private knowledge-base, never here. If a diff to `relay/`
  introduces a literal token, the pre-commit secret-scan hook will block it.
- Never `git push --force`. The relay channel has no rollback beyond
  re-deploying a previous commit's files.
