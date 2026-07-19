# CLAUDE.md — plex-jqh-omv

> Read first by any AI tool working in this repo. Kept short by design —
> the scope is small (~600 lines, PWA with extracted JS modules).

## Context

Public-facing PWA for Wake-on-LAN and status monitoring of a media
server. Pure HTML / JS / CSS, served by GitHub Pages. The repo is
**public**.

The project backs a personal use case (the author's home Plex / OMV
setup) but the code itself is **generic**: MAC, host, port and apps
are passed via URL parameters or the settings UI, never hardcoded.
Anyone can fork and configure their own instance.

The end-user UI is in **French** (the author's family is French — see
`<html lang="fr">`, button labels, toasts). Keep user-facing strings
in French; everything else (commits, PRs, docs, code comments) in
English.

The **roadmap, decisions and broader homelab context** for this PWA
live in the author's **private `knowledge-base` repo**. Don't duplicate
roadmap items here — link or summarize when needed, but the source of
truth is private.

The PWA used to depend on `depicus.com` (3rd-party WoL relay, served
via iframe). Since v3.0 (2026-05-24) the dependency is replaced by a
self-hosted relay (FastAPI + Caddy on a small always-free VM). The
relay's source lives **alongside the PWA in this repo** under
[`relay/`](relay/) — see *Scope* below. The PWA does a `fetch POST`
to the relay's `/wol` endpoint with proper success/failure feedback
(the previous `no-cors` iframe was a black box).

## Non-negotiable rules

1. **No personal data in code or commits.** No real MAC address, no
   real host, no LAN IP, no real DuckDNS domain. Always use
   placeholders like `AABBCCDDEEFF` / `myserver.example.com`. Real
   values live only in the shared URL and the user's localStorage.
2. **No force-push.** Ever.
3. **Noreply git identity** configured locally:
   `Jqh63 <12471916+Jqh63@users.noreply.github.com>`. Never expose a
   personal email in a commit.
4. **No secrets** (tokens, API keys, passwords) in code — by design
   the project doesn't need any. If a feature would require one, that
   feature is out of scope (see *Scope* below).
5. **Don't modify `CLAUDE.md` without an explicit request.** Same for
   any future `decisions/` or `BACKLOG.md` if they appear. Improving
   project doc on your own initiative is out of scope — propose, then
   wait for go.

## Workflow

- Every change goes through a **short-lived branch + PR** — no direct
  pushes to `main`.
- Branch format: `<type>/<short-subject>` — types match commit types:
  `feat`, `fix`, `docs`, `chore`, `refactor`, `security`.
- Commits are **Conventional Commits in English, imperative mood**:
  `type: short description` (no scope — the repo is single-project).
  End-user-facing strings inside the code stay in French; this rule
  is about commit messages, PR titles/bodies and docs.
- Open PRs with `gh pr create -R Jqh63/plex-jqh-omv`. Merge via the
  GitHub UI or `gh pr merge <num> -R Jqh63/plex-jqh-omv --merge
  --delete-branch` (preserve commit history, do not squash).
- **`-R Jqh63/<repo>` on EVERY `gh pr` / `gh issue` command, no
  exception** — including right after a `cd`. The agent sandbox shares
  a working directory across four checkouts and the cwd of a shell call
  does not reliably persist, so without `-R` the repo is resolved from
  whatever directory is current. PR numbers overlap across the repos,
  which makes the mistake silent and destructive: on 2026-07-18 a
  `gh pr merge 137` meant for this repo targeted `knowledge-base#137`,
  an unrelated PR.

## Before committing

Quick local checks before every commit:

- `git status` — confirm only the intended files are staged
- `git diff --cached` — read the actual staged changes
- `grep -E '(sk-|ghp_|Bearer |password\s*=)' <changed-files>` — last
  line of defense against secrets in this public repo
- Verify the commit message follows Conventional Commits (English,
  imperative, no trailing period)

If something looks off (unexpected file, suspicious-looking string,
unclear scope), stop and surface it before pushing.

## Editing discipline

- **Atomicity** — 1 commit = 1 logical change. Don't bundle a fix
  with an unrelated refactor. Open separate PRs.
- **Targeted edits** — prefer `Edit` on the precise location over
  full-file rewrites. Don't re-read files \"to understand the
  context\" beyond what the task actually needs.
- **Don't auto-fix unrelated issues.** If you spot a problem outside
  the current task (a typo in a comment, a missing aria-label, an
  outdated link), don't silently fix it on the side. Surface it,
  propose an action (issue, follow-up PR, leave it), and let the
  author decide. Keeps PRs focused and reviewable.
- **No speculative features.** Don't add error handling, validation
  or abstractions for cases that can't happen. The code is small
  enough that explicit beats clever.

## Versioning and propagation

The service worker (`sw.js`) caches the app. **Bump the `CACHE`
version on every release that changes the UX** to trigger the PWA
auto-update for installed users:

- `sw.js`: `var CACHE = 'plex-jqh-omv-vX.Y'` — the **only** marker to bump.
  The visible footer (`index.html`), the debug page and `fallback.html` all
  derive their `vX.Y` from this cache name at runtime, so there's no second
  marker to keep in sync.

No staging environment — `main` is production via GitHub Pages. Test
on the public URL after merge.

The e2e suites test the **working tree** by default (`file://` on this
checkout). To gate the live deploy after a merge: `PWA_BASE=deployed
python3 tests/cold-radio-e2e.py` (or any explicit URL). The default used
to be the deployed site — a deterministic v8.49 regression read as a
"pre-existing flaky" for exactly that reason (2026-07-19), hence the
flip.

## Architecture traps to avoid

Two constraints were learned the hard way and the fix lives in the
code. Don't undo them without re-bisecting:

- **Robust PWA auto-update needs a layered defence — no single trigger
  is reliable on Android standalone.** Required pieces:
  1. `register('sw.js', { updateViaCache: 'none' })` — without it the
     browser may serve a stale `sw.js` from its HTTP cache for up to
     24h, blocking the whole update chain.
  2. `reg.update()` after `register()` — explicit check at page load.
  3. `window 'focus'` listener — Chrome desktop, sometimes Android.
  4. `document 'visibilitychange'` listener — most reliable trigger
     on Android PWA standalone (foreground return from app switcher
     often doesn't fire `focus` but always fires `visibilitychange`).
  5. 5-minute `setInterval` safety net — catches PWAs left open for
     hours without any event firing.

  Plus the usual `skipWaiting` + `clients.claim` + `controllerchange
  → reload` on the consumer side. Those only fire *after* the browser
  has actually detected a new SW — they don't trigger the detection.

- **SW install has two non-obvious requirements stacked:**
  1. `cache.addAll(FILES)` is all-or-nothing — a 404 or network blip on
     any single asset kills the whole install and the previous SW stays
     in control. Use `Promise.all(FILES.map(f => c.add(...).catch(...)))`
     to degrade gracefully per file.
  2. Plain `c.add(url)` routes the asset fetch through the browser HTTP
     cache, which can hand back a stale older copy and silently precache
     it into the brand-new CACHE — defeating the whole point of the
     bump (observed v2.25 → v2.26: SW activated as v2.26 but served
     v2.25 indefinitely). Wrap each URL in `new Request(url, { cache:
     'reload' })` to force a fresh network read at precache time.

  Note: any client running a SW version that lacks the layered
  detection or the tolerant install will still need one manual
  refresh to cross over. The fixes are forward-acting only.

## Intentionally limited scope

- **No JS framework, no build step, no `package.json`.** Single HTML
  file = maximum portability and easy visual audit.
- **No tracking, no cookies.**
- **One co-located backend, by exception: the WoL relay** (under
  [`relay/`](relay/)). It is the only server-side component the PWA
  strictly depends on, and the wire contract between the two is
  small and stable enough that they evolve together. **No applicative
  coupling beyond the HTTP contract** — the runtime contract is
  `POST /wol` + `GET /status` (since v7.0 the status path is the relay
  oracle; `GET /health` / `/health/deep` are only hit by the settings
  "Tester le relais" button). A relay that answers `/status` with a
  degraded verdict (e.g. 503 when `STATUS_TARGET_URL` is unset) must not
  cost the PWA its wake button: app.js treats an *answered* `/status`
  failure as "relay alive, oracle degraded" and keeps WoL enabled, vs. a
  *transport* failure which marks the relay unreachable. The relay must
  keep working without assuming anything about the caller. **Any other
  future backend stays in its own repo** — this exception is not a
  precedent.

## Claude Code tooling (.claude/)

Repo-specific Claude Code skills live under `.claude/skills/`:

- **deploy-relay** — deploy the WoL relay (`relay/`) to the always-free
  VM via the GitOps channel; use after changing anything under `relay/`.
- **release-pwa** — bump the visible version marker + service-worker
  `CACHE` so installed users auto-update; use on every UX release.
- **test-pwa** — run the two-layer test suite (Python state-machine sim
  + Playwright E2E); use when changing status/probe timing in `app.js`.

## When in doubt

Ask the author rather than guessing. The repo is small and context
is rarely ambiguous — but when it is, one question beats a commit
that has to be reverted.
