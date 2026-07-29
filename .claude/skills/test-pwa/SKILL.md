---
name: test-pwa
description: Run the PWA's test suites (deterministic Python state-machine sim + Playwright browser E2E on Chromium and WebKit) when changing status/probe/timing logic, the render, or anything served to the family. Use to validate a fix before claiming it works.
---

# Test the PWA

`tests/README.md` is the authority: it lists every suite, what each one exists to
catch, and the traps met writing them. Read it rather than trusting this file for
an inventory — that is exactly how this skill went stale (it still claimed "3/3
PASS" and a `FixedApp v4.3+` baseline long after neither existed).

## Layer 1 — deterministic sim (~50 ms)

```bash
python3 tests/state-machine-sim.py       # expect: exit 0, every scenario PASS
```

No setup, no network. Iterate here first: fix the logic in the sim until green,
then port to `app.js`.

## Layer 2 — real browser (~3 min both engines)

```bash
# iteration — 80 % of the family is on Android, so Chromium is the one that matters daily
PWA_BASE="file:///config/workspace/plex-jqh-omv/index.html" PWA_ENGINES=chromium \
  python3 tests/cold-radio-e2e.py
# merge gate — both engines (the run prints a PARTIAL banner when you forget)
PWA_BASE="file:///config/workspace/plex-jqh-omv/index.html" python3 tests/cold-radio-e2e.py
```

- **`PWA_BASE` defaults to the working tree**, not the live site — so this is a
  *pre*-merge gate, not only a post-deploy check. ⚠️ `cold-radio` wants a **page**
  URL, `fallback` and `version-footer` want the **directory**; mixing them runs
  the suite against the wrong page and fails in a way that looks like an app bug.
- Touching the **render** (tile, layout, copy, footer) means the render pins too:
  `layout-stability`, `text-overflow`, `tile-crossfade`, `screen-fade`, `a11y`,
  `version-footer`. An assertion about styles is not an assertion about what the
  family sees.
- Touching the **relay**: `cd relay && python3 -m pytest -q`.

## Discipline

- A regression test must be seen **FAILING against the unfixed code** before it
  is worth anything. Every negative assertion ("never green", "no red flash")
  needs a positive control, or it passes because nothing was armed.
- Skipping layer 1 loses fast iteration; skipping layer 2 ships a fix that
  doesn't hold IRL — the render pins have caught four defects that every
  style assertion missed.
