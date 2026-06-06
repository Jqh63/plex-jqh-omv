# plex-jqh-omv tests

Two-layer test suite for the PWA's v8 status / probe / cold-radio resume
state machine.

## Layout

| File | What | Speed |
|---|---|---|
| `state-machine-sim.py` | Deterministic Python sim of the app.js v8 timer/fetch logic. `OldCascade` (v7 baseline) vs `V8App` on the status scenarios + a contrast check. Also models the v8.3 power-button honesty (`BuggyButtonApp` baseline vs the fixed button) — the confident green "Serveur allumé" only on a FRESH verdict, never off a cache pre-paint or a relay `stale=true`. | ~50 ms |
| `cold-radio-e2e.py` | Playwright headless drives Chromium against the PWA with mocked network + spoofed visibilitychange. 12 scenarios. | ~30 s |
| `screenshots/` | E2E output, gitignored. | — |

## The v8 model (what's under test)

v4→v7 accumulated a *ladder* of cold-radio defences (retry chain, two
fail-streaks, all-timeout HOLD, adaptive tick) all fighting one root cause: a
5 s status timeout was too tight against a cold mobile radio (~3 s to warm) +
TLS handshake, so the fetch timed out and the code cascaded — up to ~33 s of
orange "Vérification…/reconnexion…" on reopen (the IRL "PWA en background,
réouverture → check orange 30 s ou plus").

v8 deletes the pile. `checkStatus()` fires `probe()`, which resolves EXACTLY
ONCE to `{up, relayReachable}` and never rejects: one relay `/status` fetch
(`PROBE_TIMEOUT_MS`, generous so the radio warms inside the attempt) and, on
its failure, one direct-home fallback (`HOME_FALLBACK_TIMEOUT_MS`). No retry,
no hold, no streak. A `probeGen` counter drops a stale in-flight probe that
resolves after a resume (the Android suspend-mid-fetch race). Worst case =
PROBE + HOME ≈ 13 s and only on a genuine relay+home outage; the common reopen
settles in <3 s.

## When to use which

- **State-machine sim** — change app.js timing logic, run in <1 s, get a
  verdict on every scenario. It also asserts the headline property: the orange
  card is never held longer than `max_orange_s` (one PROBE+HOME). The
  `contrast` check confirms `OldCascade` does measurably worse on the
  cold-radio scenarios, so they genuinely exercise the fix. This is where the
  v8 design was iterated before touching any JS.

- **Real-browser E2E** — drives the actual `app.js` through real fetch +
  timer + visibilitychange paths in Chromium. Catches anything the sim misses
  (real fetch rejection, CSS rendering, DOM mounting, real `visibilitychange`
  semantics). The E2E is the source of truth; the sim is a fast first line.

Both should pass before claiming a UX timing fix works. Neither models a real
mobile radio — validate on a real Android device over 4G/WG before closing a
cold-radio change.

## Run

State machine sim — no setup, no network, just Python 3.12+:

```bash
python3 tests/state-machine-sim.py
# expect: V8App: all scenarios PASS  /  Contrast: confirmed
```

Real-browser E2E — needs Playwright + Chromium:

```bash
python3 -m pip install --user playwright
python3 -m playwright install chromium
# Validate the WORKING TREE before merge (flat HTML/JS → file:// works):
PWA_BASE="file:///config/workspace/plex-jqh-omv/index.html" python3 tests/cold-radio-e2e.py
# Or the live deploy (post-merge gate): leave PWA_BASE unset.
# expect: ALL PASS (9 scenarios)
```

## What the E2E actually does

For each scenario:

1. Opens the PWA at `…/?host=test.example.com&relay=https://r.example.com&…`
2. Installs a `page.route()` handler that intercepts requests by parsed URL
   host (`urlparse(url).netloc`, NOT substring — see Gotchas) and plays a
   scripted relay `/status` + direct-home outcome sequence.
3. For resume scenarios, fakes background→foreground via
   `Object.defineProperty(document, 'hidden', …)` + an optional event.
4. Polls DOM state at fixed offsets, captures the transition timeline, and
   checks the expected paints (green / red / warn) and the WoL-button state.

## Gotchas (learned the hard way)

- **Don't substring-match URLs in `page.route()` handlers.** The PWA config
  URL contains the test host as a query param (`?host=test.example.com`), so
  `'test.example.com' in url` also matches the navigation URL itself. Use
  `urlparse(url).netloc` for host equality + path matching.

- **Vite SPA needs HTTP not file://** for `/assets/*` resolution. This PWA is
  flat HTML/JS and works fine via `file://`; dash-pat (Vite) needs a loopback
  http.server.

- **`route.abort()` rejects the fetch INSTANTLY**, while the real PWA timeout
  is `PROBE_TIMEOUT_MS` (8 s, relay) / `HOME_FALLBACK_TIMEOUT_MS` (5 s, home).
  For failure paths this is fine — app.js's fallback runs identically whether
  the fetch was rejected or timed out. The *timing bounds* (orange ≤ 13 s) are
  the sim's job; the E2E checks the transitions.

- **visibilitychange spoofing** has a stable cross-browser pattern: override
  `document.hidden` AND `document.visibilityState` via
  `Object.defineProperty(…, configurable: true)`, then dispatch the event.

## Adding scenarios

- **state-machine sim** — append a `Scenario(...)` to `SCENARIOS`. Specify
  `relay_outcomes` and `home_outcomes` as lists of `FetchOutcome(latency, ok,
  up, answered)` in call order; the tape repeats its LAST entry once exhausted
  (so a "relay down" tape stays down for both apps regardless of how many
  fetches each makes). `latency=None` = timeout. Set `max_orange_s` to bound
  the orange card, `is_contrast=True` to assert `OldCascade` does worse.

- **E2E** — add a `run_scenario(...)` / `run_resume_scenario(...)` call in
  `main()` with `relay_plan` / `home_plan` lambdas over the 1-indexed call
  number returning `'up'|'down'|'degraded'|'fail'` (relay) or `'ok'|'fail'`
  (home), plus the `sample_delays_s` capture offsets and a verdict tuple.
