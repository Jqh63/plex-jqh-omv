---
name: test-pwa
description: Run the PWA's two-layer test suite (deterministic Python state-machine sim + Playwright browser E2E) when changing the status/probe/cold-radio timing logic in app.js. Use to validate any UX timing fix before claiming it works.
argument-hint: "[what timing logic changed]"
---

# Test the PWA (two layers)

Change: **$ARGUMENTS**.

The two layers cover the same timing bugs from different angles (see
`tests/README.md`). **Both must pass before claiming a UX timing fix works.**

## Layer 1 — deterministic state-machine sim (fast, ~50 ms)

Iterate the fix design here FIRST, before touching JS — it preserves
BuggyApp (pre-fix) vs FixedApp side by side across scenarios:

```bash
python3 tests/state-machine-sim.py
# expect: FixedApp (v4.3+): all scenarios PASS
```

No setup, no network, just Python 3.12+. If a scenario fails, fix the timing
logic in the sim until green, THEN port the change to `app.js`.

## Layer 2 — real-browser E2E (~2 min)

Once the JS change is committed and deployed (GitHub Pages is prod), drive
the actual JS on the live URL with Chromium (Playwright pre-installed in the
sandbox):

```bash
python3 tests/cold-radio-e2e.py
# expect: 3/3 PASS — cold-radio fail-then-OK / server down / relay down
```

E2E is the source of truth — it catches what the sim can't (real
`visibilitychange` semantics, real `fetch` rejection paths, CSS/DOM).

## Discipline (why both)
Skipping layer 1 loses fast iteration; skipping layer 2 risks shipping a fix
that doesn't hold IRL. Note the E2E hits the **live** GitHub Pages URL — so it
validates *after* deploy, not local edits. For a pre-deploy gate, rely on the
sim, then re-run E2E post-merge.

## Related
A UX release usually pairs with a cache bump → see `/release-pwa`.
