# Git hooks

`pre-push` refuses to push a branch that changes a served file without bumping
`CACHE` in `sw.js` (CLAUDE.md § *Versioning and propagation*). Judged on the
whole branch, so a bump made in its own commit is fine.

```bash
git config core.hooksPath .githooks   # per clone — redo after a fresh clone
bash .githooks/test-pre-push.sh       # bench (throwaway repo, no network)
```

Replayed on the last 34 PRs touching served files (2026-09-25): 1 blocked —
#165, a test-only `?poll=` knob added to `app.js` without a bump. That is the
kind of case `git push --no-verify` exists for: deliberate, and said so in the PR.
