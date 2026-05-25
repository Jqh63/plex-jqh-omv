# CLAUDE.md — plex-jqh-omv

> Read first by any AI tool working in this repo. Kept short by design —
> the scope is small (~600 lines, single-file PWA).

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
- Open PRs with `gh pr create`. Merge via the GitHub UI or
  `gh pr merge <num> --merge --delete-branch` (preserve commit
  history, do not squash).

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

- `index.html` footer: `vX.Y` (visual marker)
- `sw.js`: `var CACHE = 'plex-jqh-omv-vX.Y'`

No staging environment — `main` is production via GitHub Pages. Test
on the public URL after merge.

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
  coupling beyond the HTTP contract** — the PWA must keep working
  against any backend that honours `POST /wol` + `GET /health`, and
  the relay must keep working without assuming anything about the
  caller. **Any other future backend stays in its own repo** — this
  exception is not a precedent.

## When in doubt

Ask the author rather than guessing. The repo is small and context
is rarely ambiguous — but when it is, one question beats a commit
that has to be reverted.
