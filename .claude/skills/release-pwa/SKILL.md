---
name: release-pwa
description: Release a PWA version — bump the visible version marker and the service-worker CACHE so installed users auto-update. Use on every release that changes the UX. Forgetting the CACHE bump silently strands installed clients on the old version.
argument-hint: "[X.Y - what changed]"
---

# Release a PWA version

Release: **$ARGUMENTS**.

GitHub Pages is production — there is no staging. The service worker (`sw.js`)
caches the app, so a UX change only reaches **installed** users if the CACHE
version is bumped (triggers the layered auto-update). See CLAUDE.md
§ *Versioning and propagation* + § *Architecture traps to avoid*.

## Steps

1. **Bump both version markers** (keep them in sync):
   - `index.html` footer → `vX.Y` (visual marker)
   - `sw.js` → `var CACHE = 'plex-jqh-omv-vX.Y'`
   ```bash
   grep -n 'plex-jqh-omv-v' sw.js
   grep -nE 'v[0-9]+\.[0-9]+' index.html | head
   ```

2. **Bump on every UX-changing release.** A pure-doc or relay-only change
   doesn't need a CACHE bump (no app asset changed). When unsure, bump — a
   spurious bump only costs one extra update cycle; a missed bump strands
   installed users silently (observed v2.25→v2.26).

3. **Don't undo the SW hardening** when editing `sw.js` (CLAUDE.md lists the
   layered detection + tolerant install learned the hard way):
   - `register('sw.js', { updateViaCache: 'none' })`, `reg.update()`, `focus`
     + `visibilitychange` listeners, 5-min interval safety net.
   - install: per-file `add().catch()` (not all-or-nothing `addAll`) +
     `new Request(url, { cache: 'reload' })` to avoid precaching a stale copy.

4. **PR** (English commit, Conventional Commits, no scope), `gh pr create` →
   `gh pr merge --merge --delete-branch` (no squash).

5. **Verify post-merge** on the live URL (no staging): hard-reload, confirm
   the new footer version, and that an installed client updates within a
   foreground return / 5-min window. If the UX touched timing logic, run
   `/test-pwa` layer 2 (E2E) against the deployed version.

## Guard
No personal data / secrets in the diff (public repo) — use placeholders
(`AABBCCDDEEFF`, `myserver.example.com`). The pre-commit secret-scan hook
blocks full-shape secrets.
