---
name: release-pwa
description: Release a PWA version — bump the service-worker CACHE name (generation + date) so installed users auto-update. Use on every PR that changes a served file. Forgetting the bump silently strands installed clients on the old version.
argument-hint: "[what changed]"
---

# Release a PWA version

Release: **$ARGUMENTS**.

GitHub Pages is production — there is no staging. The service worker (`sw.js`)
caches the app, so a UX change only reaches **installed** users if the CACHE
version is bumped (triggers the layered auto-update). See CLAUDE.md
§ *Versioning and propagation* + § *Architecture traps to avoid*.

## Steps

1. **Bump the cache name — the ONE marker.** Format
   `plex-jqh-omv-v<generation>-<YYYY-MM-DD><letter>`:
   ```bash
   grep -n 'var CACHE' sw.js       # e.g. plex-jqh-omv-v8-2026-07-29a
   date +%F                         # today → the new date
   ```
   - **The date is what you bump**, every time: today's date, plus the next
     letter (`b`, `c`…) if a deploy already shipped today. Nothing to guess.
   - **The generation (`v8`) does NOT move** for a fix, a test or a UI tweak,
     however important — only for a deep rewrite backed by an ADR.
   - There is **no second marker**. The footer (`index.html`), the debug page
     and `fallback.html` derive their label at runtime through `version.js`.
     ⚠️ Never hardcode a version into a page: this skill used to say to edit the
     `index.html` footer by hand, which stopped being true long before anyone
     noticed (2026-07-29).

2. **Bump in the SAME PR as any change to a served file** (`app.js`,
   `version.js`, `index.html`, `sw.js`, `fallback.*`, `debug.*`, icons). A
   pure-doc, test-only or relay-only change needs no bump. When unsure, bump —
   a spurious bump costs one extra update cycle; a missed bump strands
   installed users silently (observed v2.25→v2.26, and again 2026-07-29 when
   PR #165 touched `app.js` without one).

3. **Don't undo the SW hardening** when editing `sw.js` (CLAUDE.md lists the
   layered detection + tolerant install learned the hard way):
   - `register('sw.js', { updateViaCache: 'none' })`, `reg.update()`, `focus`
     + `visibilitychange` listeners, 5-min interval safety net.
   - install: per-file `add().catch()` (not all-or-nothing `addAll`) +
     `new Request(url, { cache: 'reload' })` to avoid precaching a stale copy.

4. **PR** (English commit, Conventional Commits, no scope), `gh pr create` →
   `gh pr merge --merge --delete-branch` (no squash).

5. **Verify post-merge** on the live URL (no staging): hard-reload, confirm the
   footer reads `v<generation> · <today>` — a footer still showing an older date
   is the signal that a client has not taken the new code — and that an
   installed client updates within a foreground return / 5-min window. The
   footer itself is pinned by `tests/version-footer-e2e.py`. If the UX touched timing logic, run
   `/test-pwa` layer 2 (E2E) against the deployed version.

## Guard
No personal data / secrets in the diff (public repo) — use placeholders
(`AABBCCDDEEFF`, `myserver.example.com`). The pre-commit secret-scan hook
blocks full-shape secrets.
