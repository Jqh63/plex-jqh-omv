# plex-jqh-omv tests

Two-layer test suite for the PWA's status / probe / cold-radio resume
state machine. Both layers earned their keep validating the v4.3 fix
([PR #21](https://github.com/Jqh63/plex-jqh-omv/pull/21)).

## Layout

| File | What | Speed |
|---|---|---|
| `state-machine-sim.py` | Deterministic Python sim of the app.js timer/fetch logic. BuggyApp (pre-v4.3) vs FixedApp (v4.3+) on 7 scenarios. | ~50 ms |
| `cold-radio-e2e.py` | Playwright headless drives Chromium against the LIVE PWA at jqh63.github.io with mocked network + spoofed visibilitychange. | ~2 min |
| `screenshots/` | E2E output, gitignored. | — |

## When to use which

The two layers cover the same bug from different angles:

- **State-machine sim** — write or change app.js timing logic, run sim
  in <1 s, get verdict on 7 scenarios. Catches regressions without
  spinning up a browser. The simulator was where the v4.3 fix design
  was iterated before any JS was touched.

- **Real-browser E2E** — once the JS change is committed and deployed,
  drives the actual JS on the actual GitHub Pages URL with Chromium.
  Catches anything the sim misses (real `visibilitychange` semantics,
  real `fetch` rejection paths, CSS rendering, DOM mounting). The
  E2E is the source of truth — the sim is a fast first line.

Both should pass before claiming a UX timing fix works.

## Run

State machine sim — no setup, no network, just Python 3.12+:

```bash
python3 tests/state-machine-sim.py
# expect: FixedApp (v4.3+): all scenarios PASS
```

Real-browser E2E — needs Playwright + Chromium:

```bash
python3 -m pip install --user playwright
python3 -m playwright install chromium
python3 tests/cold-radio-e2e.py
# expect: 3/3 PASS — cold-radio fail-then-OK / server down / relay down
```

## What the E2E actually does

For each of the 3 scenarios:

1. Opens the live PWA at `jqh63.github.io/plex-jqh-omv/?host=test.example.com&...`
2. Installs a `page.route()` handler that intercepts requests by parsed
   URL host (`urlparse(url).netloc`, NOT substring — see Gotchas)
3. Plays out a scripted sequence of fetch outcomes (ok / abort)
4. Fakes background→foreground via `Object.defineProperty(document, 'hidden', ...)`
   + `dispatchEvent(new Event('visibilitychange'))`
5. Polls DOM state at fixed offsets after foreground, captures
   transition timeline
6. Verifies no forbidden paint (RED/WARN) for the success scenarios
   and presence of paint for the real-failure scenarios

## Gotchas (learned the hard way)

- **Don't substring-match URLs in `page.route()` handlers.** The PWA
  config URL contains the test host as a query param
  (`?host=test.example.com`), so `'test.example.com' in url` matches
  the GitHub Pages navigation URL itself — and our `route.fulfill()`
  replaces the HTML with an empty body, breaking the whole page load.
  Use `urlparse(url).netloc` for host equality + path prefix matching.

- **Vite SPA needs HTTP not file://** for relative `/assets/*` path
  resolution. Pock and plex-jqh-omv are flat HTML/JS and work fine
  via `file://`; dash-pat (Vite SPA) needs a loopback http.server.

- **visibilitychange spoofing via JS** has a stable pattern across
  browsers: override `document.hidden` AND `document.visibilityState`
  via `Object.defineProperty(..., configurable: true)`, then
  `dispatchEvent(new Event('visibilitychange'))`. The app's listener
  reads `document.hidden`, so the spoof is honored.

- **`route.abort()` is instant**, while the real PWA timeout is 2 s
  (status, v5.3) / 2.5 s (probe). For testing failure paths this is fine —
  the PWA's catch handler runs identically regardless of WHY the fetch
  failed. Just be aware sample delays don't have to wait the full
  timeout when modelling failures.

## Adding scenarios

- **state-machine sim** — append a `Scenario(...)` to the `SCENARIOS`
  list. Specify `status_outcomes` and `probe_outcomes` as lists of
  `FetchOutcome(latency, ok)` in call order. `latency=None` simulates
  timeout. The `forbid_red_flash` / `forbid_warn_flash` flags let you
  invert the expectation for "real failure" scenarios.

- **E2E** — add a `run_scenario(...)` call in `main()` with a
  `route_plan` lambda over `(kind, n_call)` returning `'ok'` or
  `'fail'`. The `sample_delays_s` list says when to capture state
  (relative to the foreground event).
