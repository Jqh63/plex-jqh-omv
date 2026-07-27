# plex-jqh-omv tests

Two-layer test suite for the PWA's v8 status / probe / cold-radio resume
state machine.

## Layout

| File | What | Speed |
|---|---|---|
| `state-machine-sim.py` | Deterministic Python sim of the app.js v8 timer/fetch logic. `OldCascade` (v7 baseline) vs `V8App` on the status scenarios + a contrast check. Models the v8.4 power-button honesty (`BuggyButtonApp` baseline) AND the v8.5 status-card honesty (`BuggyCardApp` baseline) — the confident green ("Serveur allumé" / "En ligne") lights once a live probe settles, never off a cache pre-paint; a relay `stale=true` up is still trusted as up (a healthy home is almost always served stale → gating green on `!stale` stuck the indicator orange, so honesty keys on "a live probe settled this session", not the stale flag). v8.5 also shortens the self-healing poll (15 s → 8 s) so a just-stopped home corrects to red in ~8 s, asserted via `expect_red_by`. | ~50 ms |
| `cold-radio-e2e.py` | Playwright headless drives the PWA on Chromium **and WebKit/Safari** (cross-browser, see § Engines) with mocked network + spoofed visibilitychange. 15 scenarios × engine. Covers the **status** machine — it never fires a wake. | ~30 s/engine |
| `wake-e2e.py` | Playwright headless on the **wake** paths, which `cold-radio-e2e.py` does not touch — and where the 2026-07-14 bug lived. Pins v8.45: a wake must not survive a background freeze and repaint its countdown when the app is reopened — both for a wake this device TAPPED (`wolSent`) and, crucially, for one it merely ADOPTED from the relay (`remoteWaking`, the AM5 logon task's wake — the variant actually hit). Uses Playwright's **clock API** to reproduce the Android freeze/thaw. Two traps it exists to avoid, both of which produced a green-but-worthless test on the first pass: for the **reap** scenarios assert on the countdown (`powerProgress`), not the status card; and jump time with `set_system_time`, **not** `fast_forward` (the latter also fires the thawed poll timer, which reaps the wake on its own — the test would pass even without the fix). ⚠️ v8.53 — that first trap was half a misreading: the card being "repainted to Vérification… while the countdown keeps ticking" was not only a fixture artefact, it was a **real defect** on adopted wakes (`setRechecking` guarded `wolSent` but not `remoteWaking`). `remote-wake-outlives-the-relay-waking-signal` now asserts on the card ON PURPOSE. It also has to **sample** across the ~2.5 s (`DOWN_RECHECK_MS`) contradiction window rather than snapshot once — a single late snapshot passes against the bug. | ~30 s |
| `fallback-e2e.py` | Playwright on **`fallback.html`**, the manual-wake page — which had zero coverage until 2026-07-27 while becoming the family's real degraded path (v8.53 promotes it to a full-size call to action on ANY failed wake). Static page, so no network mocking. Pins: parameters rendered from the URL, the copyable PowerShell / `wakeonlan` commands filled with the user's OWN values, `?ip=` winning over the domain in those commands (its whole reason to exist is a DNS outage) with a malformed-IP control, click-to-copy actually reaching the clipboard, and its **failure** state being visible rather than silent. ⚠️ Two traps met while writing it: the Linux/macOS command sits in a collapsed `<details>`, so `inner_text` returns `""` and reads as an app bug — assert on `text_content`; and the test MAC must differ from the page's own placeholder, or "filled with the real value" cannot be told apart from "fell back to the example". Clipboard cases need a secure context (`http://127.0.0.1`), and self-skip on `file://`. | ~10 s |
| `screenshots/` | E2E output, gitignored. | — |

> ⚠️ **WebKit and route interception (2026-07-27).** Playwright's WebKit drops
> route interception for *some* requests: the first `/status` is served by the
> handler, a later one escapes to the real network and dies on DNS. The app then
> gets a genuine transport failure and correctly commits red — so the scenario
> goes red while the code behaved perfectly. Three scenarios had been failing
> for that reason alone (`relay-fail-fallback-home-up`,
> `relay-single-miss-debounced-no-warn`, `transient-relay-false-down-no-red`),
> long enough that WebKit's whole verdict had become background noise. The
> danger is not the noise but what it hides: on those runs WebKit is not testing
> what the scenario claims, and the family does use iOS. `_watch_interception`
> now detects a mock host reaching the real network and reports those scenarios
> as **`SKIP-ENV`**, so the remaining FAILs stay meaningful. `ctx.route` instead
> of `page.route` was tried and does **not** fix it; a real fix needs a local
> HTTPS mock server (the PWA's `validRelay` refuses plain http), which is worth
> doing the day WebKit coverage of those three paths actually matters.

> **The wake paths went untested in a browser until 2026-07-14** — which is exactly
> why two bugs shipped there. If you touch `sendWol()`, the countdown, `setOffline()`
> or `setRechecking()`, `wake-e2e.py` is the layer that has to stay green.

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

> ⚠️ **Both E2E suites default to `PWA_BASE=https://jqh63.github.io/plex-jqh-omv/`
> — the DEPLOYED app, not your working tree.** A green run proves nothing about
> uncommitted changes (bit us 2026-07-18: new scenarios "passed" against the live
> v8.47). To validate local edits:
> `python3 -m http.server 8123 &` then `PWA_BASE=http://127.0.0.1:8123/ python3 tests/…`

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
#         Button honesty: confirmed  /  Card honesty: confirmed
```

Real-browser E2E — needs Playwright + a browser:

```bash
python3 -m pip install --user playwright
python3 -m playwright install chromium
# Validate the WORKING TREE before merge (flat HTML/JS → file:// works):
PWA_BASE="file:///config/workspace/plex-jqh-omv/index.html" python3 tests/cold-radio-e2e.py
# Or the live deploy (post-merge gate): leave PWA_BASE unset.
# expect: [chromium] ALL PASS (15 scenarios)  /  ALL ENGINES PASS
```

### Engines (cross-browser — Chromium + WebKit/iOS)

The suite runs every scenario on each engine in `PWA_ENGINES`
(default `chromium,webkit`):

- **chromium** — the Blink baseline = Chrome desktop / Android Chrome.
- **webkit** — Playwright's WebKit is the same WebCore/JSCore engine Safari
  ships, so it's the **best headless approximation of iOS Safari** short of a
  real iPhone (catches `:has()`, `100dvh`, `env(safe-area-inset-*)`, WebKit CSS
  quirks). It is **not** a real device — a physical iPhone over 4G/WG stays the
  gold standard, this is the fast first line.

An engine whose browser can't launch (binary or system libs missing) is
**SKIPPED with a note**, never a hard failure — so the Chromium gate still
works on a host without the WebKit deps. WebKit needs a heavy lib stack
(`libgtk-4`, `libgstreamer`, `libwoff2dec`, `libenchant`, …) that requires root:

```bash
# On a root-capable host (NOT the code-server sandbox, which has no sudo):
python3 -m playwright install --with-deps webkit
# Then both engines run from a plain invocation. To run one only:
PWA_ENGINES=chromium python3 tests/cold-radio-e2e.py
PWA_ENGINES=webkit   python3 tests/cold-radio-e2e.py
```

> The code-server sandbox gets the WebKit deps **on demand** (same flow as
> Chromium): if the lane SKIPs after a container recreate, run
> `ssh omv-deploy setup-codeserver-browser` once — it installs the Chromium +
> WebKit system libs via a pinned root exec (route documented in the private
> knowledge-base repo, `code-server/README.md` § Playwright). Both engines run
> there since 2026-07-19; a missing install only degrades WebKit to SKIP —
> Chromium is never affected.
> Engine coverage caveat: this exercises WebCore/JSC, **not** iOS PWA
> OS-level quirks (standalone cookie jar, popup activation, background
> freeze) — those still need a physical iPhone.

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
