"""
Real-browser E2E validation of plex-jqh-omv v8.7 (single-probe status model
with v8.7 confirm-before-red: a "down" verdict shows orange and re-probes once
before committing red; "up" stays instant).

Drives the PWA with Playwright headless on every engine in PWA_ENGINES
(default chromium,webkit — Blink baseline + the WebKit/Safari engine for iOS;
see tests/README.md § Engines). The route handler intercepts the relay's
`/status` endpoint (the single PWA fetch) and the direct-home fallback. Paint
events are captured via DOM polling; a verdict is printed per engine. An engine
whose browser can't launch is skipped with a note, not a failure.

What this E2E covers vs. the offline sim (`state-machine-sim.py`):
- The sim verifies the v8 state-machine semantics + timing bounds (orange
  never held past one PROBE+HOME) on a synthetic clock — fast, deterministic.
- This E2E verifies that the actual `app.js` wires into those semantics
  through real fetch + timer + visibilitychange paths in a real browser
  (Chromium + WebKit). It's the gate before declaring a release usable.

Note (same as v7): `route.abort()` rejects the fetch INSTANTLY, whereas the
real PWA timeout is PROBE_TIMEOUT_MS (8 s) / HOME_FALLBACK_TIMEOUT_MS (5 s).
For the failure-path scenarios that's fine — app.js's fallback runs identically
regardless of WHY the relay fetch failed (reject vs. timeout). The *timing
bounds* (orange ≤ 13 s) are the sim's job; this E2E checks the transitions.

Run against the working tree BEFORE merge (the PWA is flat HTML/JS so file://
works):
  PWA_BASE="file:///config/workspace/plex-jqh-omv/index.html" python3 tests/cold-radio-e2e.py
Or against the live deploy (post-merge gate): leave PWA_BASE unset.

Scenarios (mirror state-machine-sim.py):
  1. cold-launch-server-up-fast        — /status up → green ≤3 s
  2. cold-launch-server-off-fast       — /status down → red ≤3 s
  3. relay-fail-fallback-home-up       — /status ✕ → home ok → green; relay warn
                                         only after the 3rd-miss debounce (~16 s)
  4. relay-and-home-down               — /status ✕ → home ✕ → orange then red
                                         (after the v8.7 confirm re-probe);
                                         relay warn only after the 3rd-miss debounce
  5. cache-up-server-down-corrects-red — cache <60 s says up but the home was just
                                         stopped (relay down) → v8.6 reuses the
                                         cached green pre-paint (accepted trade-off)
                                         and the live probe corrects to red ≤3 s
 5b. cache-up-server-up-reused-green   — cache up + relay up → the reused green is
                                         confirmed by the live probe (no red/warn)
  6. relay-degraded-server-up          — /status 503 → home ok → green, no warn, WoL on
  7. relay-degraded-server-down        — /status 503 → home ✕ → red, no warn, WoL on
  8. resume-focus-only-converges-red   — bg → server dies → focus → red
  9. resume-no-event-self-heals-red    — bg → server dies → no event → red ≤3 s
 9b. clockjump-wake-stale-green-demoted — Date.now() jump alone (no event, no
                                         hidden flip) demotes the stale green
                                         → red (v8.10 prolonged-sleep fix)
 10. relay-single-miss-debounced-no-warn — lone /status ✕ then recover → green,
                                         NEVER warn, WoL stays enabled (debounce payoff)
 11. watchdog-reclaims-wedged-checking — stuck checking=true + server down → a
                                         re-probe reclaims the flag → red, not frozen green
 12. relay-up-extra-json-fields-greens — /status up with extra JSON fields
                                         (stale/age_s) → up → green + confident button
                                         (the parser tolerates fields it ignores)
 13. transient-relay-false-down-no-red — /status down once then up → orange then
                                         green, NEVER a red flash (the v8.7 fix:
                                         the user's red-that-was-green-a-moment-later)
 14. cache-down-server-actually-up-no-red — stale cached "down" + server up →
                                         orange (never a confident red pre-paint),
                                         greened by the live probe

Note: scenarios 3 and 4 sample past T+16 s because the v8.2 relay-down debounce
only hardens the warn on the THIRD consecutive miss. Since `route.abort()` is
instant here (not the real 8 s PROBE_TIMEOUT), the re-probe cadence is purely the
self-healing tick — v8.5: 8 s (was 15 s), so the 3rd miss lands ~T+16 s (was
~T+30 s). Scenario 11 exercises the `checking` watchdog directly (a real headless
browser can't reproduce the Android suspend that wedges the flag — that race is
covered by the offline state-machine sim).
"""

import os
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Route

CONFIG_HOST = "test.example.com"
RELAY_HOST = "r.example.com"
PWA_BASE = os.environ.get("PWA_BASE", "https://jqh63.github.io/plex-jqh-omv/")
PWA_URL = (
    f"{PWA_BASE}"
    f"?host={CONFIG_HOST}&mac=AABBCCDDEEFF"
    f"&relay=https://{RELAY_HOST}&token=x&apps=seerr,plexweb"
)
STATUS_LOCAL_KEY = "plex-jqh-omv-status"

# Engines to validate, in order. Chromium = the Blink baseline (Chrome /
# Android Chrome); WebKit = the Safari/iOS engine (Playwright's WebKit is the
# same WebCore/JSCore Safari ships, the best headless iOS approximation short
# of a real device). Default runs both; override with e.g. PWA_ENGINES=chromium.
# A WebKit run needs its system libs (libgtk-4, libgstreamer, libwoff2dec, …) —
# `playwright install-deps webkit` on a root-capable host, see tests/README. An
# engine that can't launch is SKIPPED with a note, never a hard failure.
ENGINES = [e.strip() for e in
           os.environ.get("PWA_ENGINES", "chromium,webkit").split(",") if e.strip()]
_CURRENT_ENGINE = "chromium"


def _launch(p):
    """Launch the engine selected for the current pass (read by every runner)."""
    return getattr(p, _CURRENT_ENGINE).launch()


def capture_state(page):
    return page.evaluate(
        """() => ({
        statusLabel: document.getElementById('statusLabel').innerText,
        dotClass: document.getElementById('statusDot').className,
        cardClass: document.getElementById('statusCard').className,
        fallbackClass: document.getElementById('fallbackLink') ? document.getElementById('fallbackLink').className : '',
        fallbackText: document.getElementById('fallbackLinkA') ? document.getElementById('fallbackLinkA').innerText : '',
        powerClass: document.getElementById('powerBtn') ? document.getElementById('powerBtn').className : '',
    })"""
    )


def is_red(s):
    return "offline" in s["dotClass"] or "offline" in s["cardClass"]


def is_warn(s):
    # Both "warn" (server up, relay down) and "promoted" (server down, relay
    # down) signal "relay unavailable" to the user — same visual semantics.
    return "warn" in s["fallbackClass"] or "promoted" in s["fallbackClass"]


def is_green(s):
    return "online" in s["dotClass"] and "online" in s["cardClass"]


def is_checking(s):
    # The orange "Vérification…" — the dot in its checking state and neither a
    # committed green nor red card. v8.7 shows this while a "down" verdict is
    # being re-confirmed (and on a cold open / a cached "down").
    return "checking" in s["dotClass"] and not is_red(s) and not is_green(s)


def is_wol_disabled(s):
    # The wake button goes to "power-btn unavailable" only when relayReachable
    # is false. A *degraded* (answered) /status failure keeps it enabled.
    return "unavailable" in s["powerClass"]


def is_button_confident(s):
    # The confident green "Serveur allumé" — power-btn.online. v8.6: lit on any
    # up verdict (cache reuse or live probe), no separate freshness gate.
    return "online" in s["powerClass"]


def is_button_checking(s):
    # v8.7 follow-up: the neutral "Vérification…" power button shown while the
    # card is orange (cold-open check or a down being re-confirmed), so the
    # button never sits on a stale confident green during a check.
    return "checking" in s["powerClass"]


def _relay_fulfill(route, verdict):
    h = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
    if verdict == "up":
        route.fulfill(status=200, headers=h, body='{"up": true, "stale": false, "age_s": 0}')
    elif verdict == "up-extra-fields":
        # Relay serving an up verdict with extra JSON fields the PWA ignores
        # (stale/age_s from the relay's server-side SWR cache). The parser keys
        # only on the `up` boolean, so this must behave exactly like "up".
        route.fulfill(status=200, headers=h, body='{"up": true, "stale": true, "age_s": 30}')
    elif verdict == "down":
        route.fulfill(status=200, headers=h, body='{"up": false, "stale": false, "age_s": null}')
    elif verdict == "degraded":
        # Relay ANSWERS with a degraded oracle (STATUS_TARGET_URL unset → 503).
        # Relay alive, /wol works — the PWA must keep it reachable + fall back.
        route.fulfill(status=503, headers=h, body='{"detail": "status target not configured"}')
    else:  # 'fail' → transport failure
        route.abort()


def run_scenario(p, name, relay_plan, home_plan, sample_delays_s, preseed_cache=None):
    """relay_plan(n) → 'up'|'down'|'degraded'|'fail' for the n-th relay /status
    call (1-indexed). home_plan(n) → 'ok'|'fail' for the n-th direct-home call.
    preseed_cache: inject {up, relayOk} under STATUS_LOCAL_KEY before nav."""
    print(f"\n## Scenario: {name}")
    counters = {"relay": 0, "home": 0}

    def handle(route: Route):
        parsed = urlparse(route.request.url)
        host = parsed.netloc
        if host == RELAY_HOST and parsed.path == "/status":
            counters["relay"] += 1
            _relay_fulfill(route, relay_plan(counters["relay"]))
            return
        if host == CONFIG_HOST or host.endswith("." + CONFIG_HOST):
            counters["home"] += 1
            route.fulfill(status=200, body="") if home_plan(counters["home"]) == "ok" else route.abort()
            return
        route.continue_()

    b = _launch(p)
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    if preseed_cache is not None:
        import json
        payload = json.dumps({
            "up": bool(preseed_cache.get("up")),
            "relayOk": bool(preseed_cache.get("relayOk", True)),
            "t": None,
        })
        ctx.add_init_script(
            f"try{{var p={payload};p.t=Date.now();"
            f"localStorage.setItem('{STATUS_LOCAL_KEY}',JSON.stringify(p));}}catch(e){{}}"
        )
    page = ctx.new_page()
    page.route("**/*", handle)
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#statusLabel", state="attached", timeout=10000)

    samples = []
    last_t = 0
    for t in sample_delays_s:
        page.wait_for_timeout(int((t - last_t) * 1000))
        last_t = t
        s = capture_state(page)
        samples.append((t, s))
        flags = [f for f, on in (("RED", is_red(s)), ("WARN", is_warn(s)), ("green", is_green(s))) if on]
        print(f"  T+{t}s: status={s['statusLabel']!r} fallback={s['fallbackText']!r} -> {','.join(flags) or '(neutral)'}")

    final = samples[-1][1]
    b.close()
    return {
        "name": name,
        "red_at": [t for t, s in samples if is_red(s)],
        "warn_at": [t for t, s in samples if is_warn(s)],
        "green_at": [t for t, s in samples if is_green(s)],
        "checking_at": [t for t, s in samples if is_checking(s)],
        "final_green": is_green(final),
        "final_red": is_red(final),
        "final_warn": is_warn(final),
        "final_wol_disabled": is_wol_disabled(final),
        "button_confident_at": [t for t, s in samples if is_button_confident(s)],
        "button_checking_at": [t for t, s in samples if is_button_checking(s)],
        "final_button_confident": is_button_confident(final),
        "counters": dict(counters),
    }


def _spoof_visibility(page, hidden, event):
    """Spoof document.hidden/visibilityState and optionally dispatch an event.
    `event` ∈ {"visibilitychange", "focus", "none"}."""
    page.evaluate(
        """([hidden, event]) => {
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden});
        Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => hidden ? 'hidden' : 'visible'});
        if (event === 'visibilitychange') document.dispatchEvent(new Event('visibilitychange'));
        else if (event === 'focus') window.dispatchEvent(new Event('focus'));
    }""",
        [hidden, event],
    )


def run_resume_scenario(p, name, relay_plan, fg_event, bg_at_s, fg_at_s, sample_delays_s, preseed_cache):
    """background → foreground resume. Loads a preseeded "up" cache (v8.6: reused
    as the confident green pre-paint on resume, with the live probe confirming or
    correcting), backgrounds at bg_at_s, returns to foreground at fg_at_s
    dispatching only `fg_event`.
    relay_plan(n) drives the n-th /status verdict so the server can be up
    before background and down after. sample_delays_s are offsets RELATIVE TO
    the foreground event. v8 must converge to red (not stay frozen green)."""
    print(f"\n## Resume scenario: {name} (fg_event={fg_event})")
    counters = {"relay": 0, "home": 0}

    def handle(route: Route):
        parsed = urlparse(route.request.url)
        host = parsed.netloc
        if host == RELAY_HOST and parsed.path == "/status":
            counters["relay"] += 1
            _relay_fulfill(route, relay_plan(counters["relay"]))
            return
        if host == CONFIG_HOST or host.endswith("." + CONFIG_HOST):
            counters["home"] += 1
            route.fulfill(status=200, body="")
            return
        route.continue_()

    b = _launch(p)
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    import json
    payload = json.dumps({"up": bool(preseed_cache.get("up")), "relayOk": bool(preseed_cache.get("relayOk", True)), "t": None})
    ctx.add_init_script(
        f"try{{var p={payload};p.t=Date.now();localStorage.setItem('{STATUS_LOCAL_KEY}',JSON.stringify(p));}}catch(e){{}}"
    )
    page = ctx.new_page()
    page.route("**/*", handle)
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#statusLabel", state="attached", timeout=10000)

    page.wait_for_timeout(int(bg_at_s * 1000))
    _spoof_visibility(page, hidden=True, event="visibilitychange")
    page.wait_for_timeout(int((fg_at_s - bg_at_s) * 1000))
    _spoof_visibility(page, hidden=False, event=fg_event)

    samples = []
    last_t = 0
    for t in sample_delays_s:
        page.wait_for_timeout(int((t - last_t) * 1000))
        last_t = t
        s = capture_state(page)
        samples.append((t, s))
        flags = [f for f, on in (("RED", is_red(s)), ("WARN", is_warn(s)), ("green", is_green(s))) if on]
        print(f"  fg+{t}s: status={s['statusLabel']!r} -> {','.join(flags) or '(neutral)'}")

    final = samples[-1][1]
    b.close()
    return {
        "name": name,
        "red_at": [t for t, s in samples if is_red(s)],
        "green_at": [t for t, s in samples if is_green(s)],
        "final_green": is_green(final),
        "final_red": is_red(final),
        "counters": dict(counters),
    }


def run_clockjump_scenario(p):
    """v8.10 clock-jump detector. The Android prolonged-sleep wake where
    document.hidden NEVER flips: no focus, no visibilitychange, no
    hidden→visible edge for the 1 s poll. The only wake signal is the
    Date.now() gap between poll ticks. A headless browser can't actually
    freeze its JS VM, so we simulate the jump by monkey-patching Date.now
    with a +120 s offset — the real detector in app.js sees the tick-to-tick
    gap (> SLEEP_JUMP_MS) on its next 1 s poll and routes through
    onForeground(). The server died "during the sleep" (relay flips to down
    at the same moment), so the app must demote the stale green to orange
    and converge to red — the v8.10 fix for the ~10 s false green."""
    name = "clockjump-wake-stale-green-demoted"
    print(f"\n## Clock-jump scenario: {name}")
    counters = {"relay": 0, "home": 0}
    relay_verdict = {"v": "up"}

    def handle(route: Route):
        parsed = urlparse(route.request.url)
        host = parsed.netloc
        if host == RELAY_HOST and parsed.path == "/status":
            counters["relay"] += 1
            _relay_fulfill(route, relay_verdict["v"])
            return
        if host == CONFIG_HOST or host.endswith("." + CONFIG_HOST):
            counters["home"] += 1
            route.fulfill(status=200, body="")
            return
        route.continue_()

    b = _launch(p)
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.route("**/*", handle)
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#statusLabel", state="attached", timeout=10000)
    page.wait_for_timeout(1500)
    pre = capture_state(page)  # live probe up → confident green

    # "Sleep": the server dies and the JS clock jumps +120 s — with NO
    # visibility event and document.hidden never having flipped. The next
    # 1 s poll tick must detect the jump on its own.
    relay_verdict["v"] = "down"
    page.evaluate(
        "() => { const real = Date.now.bind(Date); Date.now = () => real() + 120000; }"
    )

    samples = []
    last_t = 0
    for t in [1.5, 5]:
        page.wait_for_timeout(int((t - last_t) * 1000))
        last_t = t
        s = capture_state(page)
        samples.append((t, s))
        flags = [f for f, on in (("RED", is_red(s)), ("orange", is_checking(s)), ("green", is_green(s))) if on]
        print(f"  wake+{t}s: status={s['statusLabel']!r} -> {','.join(flags) or '(neutral)'}")

    final = samples[-1][1]
    b.close()
    return {
        "name": name,
        "pre_green": is_green(pre),
        # The demotion: shortly after the wake the card must no longer be the
        # stale confident green (orange or already red are both honest).
        "demoted_early": not is_green(samples[0][1]),
        "final_red": is_red(final),
        "final_green": is_green(final),
        "counters": dict(counters),
    }


def run_watchdog_scenario(p):
    """v8.2 `checking` watchdog. A real headless browser can't reproduce the
    Android suspend-mid-fetch that wedges `checking` (its timers run normally in
    foreground, so a probe always resolves within PROBE_TIMEOUT) — that race is
    the sim's job. Here we exercise the watchdog DIRECTLY through the real
    app.js: force a stuck `checking=true` with a `checkStartedAt` older than the
    watchdog budget (the wedge a never-resolving probe + missed resume event
    would leave), flip the server to down, then fire a re-probe trigger. With the
    watchdog, checkStatus reclaims the stale flag and repaints red; WITHOUT it,
    checkStatus early-returns and the app stays frozen on green (the bug)."""
    name = "watchdog-reclaims-wedged-checking"
    print(f"\n## Watchdog scenario: {name}")
    counters = {"relay": 0, "home": 0}
    relay_verdict = {"v": "up"}

    def handle(route: Route):
        parsed = urlparse(route.request.url)
        host = parsed.netloc
        if host == RELAY_HOST and parsed.path == "/status":
            counters["relay"] += 1
            _relay_fulfill(route, relay_verdict["v"])
            return
        if host == CONFIG_HOST or host.endswith("." + CONFIG_HOST):
            counters["home"] += 1
            route.fulfill(status=200, body="")
            return
        route.continue_()

    b = _launch(p)
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.route("**/*", handle)
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#statusLabel", state="attached", timeout=10000)
    page.wait_for_timeout(1500)
    pre = capture_state(page)  # relay up → green

    # Server goes down, AND simulate a wedged in-flight check: checking stuck
    # true with checkStartedAt far in the past (> CHECK_WATCHDOG_MS). app.js
    # declares these as top-level `var`s, so they live on window.
    relay_verdict["v"] = "down"
    page.evaluate("() => { window.checking = true; window.checkStartedAt = Date.now() - 60000; }")
    # Re-probe trigger (stands in for the self-healing tick) — the refresh
    # button calls checkStatus(). v8.7: the reclaimed re-probe sees "down" → it
    # paints orange and fires the confirm re-probe (DOWN_RECHECK_MS = 2.5 s), so
    # red lands ~2.5 s later, not instantly. Wait past it (3.5 s) — the property
    # under test is that the wedged flag is reclaimed and the app converges to red
    # (not frozen green), not the latency.
    page.click("#refreshBtn")
    page.wait_for_timeout(3500)
    post = capture_state(page)
    b.close()
    return {
        "name": name,
        "pre_green": is_green(pre),
        "final_red": is_red(post),
        "final_green": is_green(post),
        "counters": dict(counters),
    }


def collect_results():
    """Run the full scenario suite once, on the engine selected via the module
    global _CURRENT_ENGINE (set by main() before each call). Returns the list of
    (name, ok, result, want) tuples; main() prints the per-engine verdict."""
    results = []
    with sync_playwright() as p:
        r1 = run_scenario(p, "cold-launch-server-up-fast",
                          relay_plan=lambda n: "up", home_plan=lambda n: "ok",
                          sample_delays_s=[1, 3])
        ok1 = (bool(r1["green_at"]) and r1["green_at"][0] <= 3 and not r1["red_at"]
               and not r1["warn_at"] and r1["final_button_confident"])
        results.append(("cold-launch-server-up-fast", ok1, r1,
                        "green ≤T+3, no red, no warn, button confident on fresh up"))

        # v8.7: a genuine down shows orange first (the confirm re-probe), then
        # red — never a green. sample at T+1 catches the orange, T+3 the red.
        r2 = run_scenario(p, "cold-launch-server-off-fast",
                          relay_plan=lambda n: "down", home_plan=lambda n: "ok",
                          sample_delays_s=[1, 3])
        ok2 = (bool(r2["red_at"]) and r2["red_at"][0] <= 3 and not r2["green_at"]
               and not r2["warn_at"] and bool(r2["checking_at"])
               and 1 in r2["button_checking_at"])
        results.append(("cold-launch-server-off-fast", ok2, r2,
                        "orange card+button (T+1) then red ≤T+3, no green, no warn"))

        # v8.2: a sustained relay failure stays optimistic until RELAY_DOWN_MISSES
        # (3) consecutive misses. With instant-abort, misses land at the T=0 / T=8
        # / T=16 ticks (v8.5: 8 s tick), so the warn confirms ~T=16. Sample at 18 s
        # to catch it; every earlier sample must show NO warn (the false-alarm we
        # killed).
        r3 = run_scenario(p, "relay-fail-fallback-home-up",
                          relay_plan=lambda n: "fail", home_plan=lambda n: "ok",
                          sample_delays_s=[1, 3, 18, 26])
        ok3 = (r3["final_green"] and r3["final_warn"] and not r3["red_at"]
               and all(t >= 16 for t in r3["warn_at"]))
        results.append(("relay-fail-fallback-home-up", ok3, r3,
                        "green throughout; relay warn only after 3rd miss (~16 s); no red"))

        # v8.2: red (server down) is immediate — the up/down verdict is NOT
        # debounced — but the relay warn still waits for the 3rd-miss confirm.
        r4 = run_scenario(p, "relay-and-home-down",
                          relay_plan=lambda n: "fail", home_plan=lambda n: "fail",
                          sample_delays_s=[1, 3, 18, 26])
        ok4 = (r4["final_red"] and r4["final_warn"] and not r4["final_green"]
               and all(t >= 16 for t in r4["warn_at"]))
        results.append(("relay-and-home-down", ok4, r4,
                        "red immediate; relay warn only after 3rd miss (~16 s)"))

        # v8.6 trade-off + fast correction. The cache (<60 s) still says up, but
        # the home was just stopped so the relay answers down. v8.6 REUSES the
        # cached green pre-paint (the accepted brief cache-vs-reality window —
        # this replaces the v8.5 "never flash green" honesty), and the live probe
        # corrects it to red ≤3 s. The property under test is the fast correction,
        # not the absence of green.
        # v8.7: the reused green is held, then a "down" verdict shows orange (the
        # confirm re-probe) before committing red — green→orange→red, never the
        # bare green→red flash. checking_at catches the orange phase.
        r5 = run_scenario(p, "cache-up-server-down-corrects-red",
                          relay_plan=lambda n: "down", home_plan=lambda n: "ok",
                          sample_delays_s=[0, 1, 3], preseed_cache={"up": True, "relayOk": True})
        # v8.7 follow-up: the button must NOT stay a confident green while the
        # card is orange — it goes to the neutral "Vérification…" button at T+1
        # (the user's exact feedback: button green while a check is in progress).
        ok5 = (r5["final_red"] and bool(r5["red_at"]) and r5["red_at"][0] <= 3
               and bool(r5["checking_at"]) and 1 in r5["button_checking_at"]
               and 1 not in r5["button_confident_at"])
        results.append(("cache-up-server-down-corrects-red", ok5, r5,
                        "reused green → orange card+button → red ≤T+3 (button not green during check)"))

        # v8.6 — a cache up + a server still up: the reused green pre-paint is
        # confirmed by the live probe (no red/warn). Guards against the reuse
        # somehow flipping a genuinely-up server.
        r5b = run_scenario(p, "cache-up-server-up-reused-green",
                           relay_plan=lambda n: "up", home_plan=lambda n: "ok",
                           sample_delays_s=[1, 3], preseed_cache={"up": True, "relayOk": True})
        ok5b = r5b["final_green"] and not r5b["red_at"] and not r5b["warn_at"]
        results.append(("cache-up-server-up-reused-green", ok5b, r5b,
                        "reused green confirmed by live probe, no red/warn"))

        r6 = run_scenario(p, "relay-degraded-server-up",
                          relay_plan=lambda n: "degraded", home_plan=lambda n: "ok",
                          sample_delays_s=[1, 3])
        ok6 = r6["final_green"] and not r6["warn_at"] and not r6["red_at"] and not r6["final_wol_disabled"]
        results.append(("relay-degraded-server-up", ok6, r6,
                        "green, no warn, WoL enabled"))

        r7 = run_scenario(p, "relay-degraded-server-down",
                          relay_plan=lambda n: "degraded", home_plan=lambda n: "fail",
                          sample_delays_s=[1, 3])
        ok7 = (r7["final_red"] and not r7["final_warn"] and not r7["warn_at"]
               and not r7["final_green"] and not r7["final_wol_disabled"])
        results.append(("relay-degraded-server-down", ok7, r7,
                        "red, no warn, WoL enabled"))

        r8 = run_resume_scenario(p, "resume-focus-only-converges-red",
                                 relay_plan=lambda n: "up" if n == 1 else "down",
                                 fg_event="focus", bg_at_s=3, fg_at_s=6,
                                 sample_delays_s=[1, 3], preseed_cache={"up": True, "relayOk": True})
        ok8 = r8["final_red"] and not r8["final_green"]
        results.append(("resume-focus-only-converges-red", ok8, r8,
                        "red after focus, NOT frozen green"))

        r9 = run_resume_scenario(p, "resume-no-event-self-heals-red",
                                 relay_plan=lambda n: "up" if n == 1 else "down",
                                 fg_event="none", bg_at_s=3, fg_at_s=6,
                                 sample_delays_s=[3], preseed_cache={"up": True, "relayOk": True})
        ok9 = r9["final_red"] and not r9["final_green"] and bool(r9["red_at"]) and r9["red_at"][0] <= 3
        results.append(("resume-no-event-self-heals-red", ok9, r9,
                        "red ≤ fg+3 s via 1 s visibility poll"))

        # v8.1 payoff: a lone relay transport miss (slow-but-alive e2-micro /
        # last-mile blip) then recovery on the next tick must NEVER paint the
        # relay warn nor disable the wake button — relayReachable stays true
        # throughout. This is the false-alarm the debounce exists to kill.
        r10 = run_scenario(p, "relay-single-miss-debounced-no-warn",
                           relay_plan=lambda n: "fail" if n == 1 else "up",
                           home_plan=lambda n: "ok",
                           sample_delays_s=[1, 3, 16])
        ok10 = (r10["final_green"] and not r10["warn_at"] and not r10["red_at"]
                and not r10["final_wol_disabled"])
        results.append(("relay-single-miss-debounced-no-warn", ok10, r10,
                        "lone miss + recover → green, never warn, WoL stays enabled"))

        # v8.10 — prolonged-sleep wake with no event AND no hidden flip: the
        # clock-jump detector is the only wake signal. Stale green must demote
        # (orange or red) within ~1 detector tick and converge to red.
        r9b = run_clockjump_scenario(p)
        ok9b = (r9b["pre_green"] and r9b["demoted_early"] and r9b["final_red"]
                and not r9b["final_green"])
        results.append(("clockjump-wake-stale-green-demoted", ok9b, r9b,
                        "clock jump alone demotes stale green → red, no event needed"))

        r11 = run_watchdog_scenario(p)
        ok11 = r11["pre_green"] and r11["final_red"] and not r11["final_green"]
        results.append(("watchdog-reclaims-wedged-checking", ok11, r11,
                        "wedged checking reclaimed on re-probe → red, not frozen green"))

        # The relay's /status carries extra JSON fields (stale/age_s) from its
        # server-side SWR cache. app.js keys only on the `up` boolean and ignores
        # the rest, so an up-with-extra-fields verdict must green the card AND
        # light the confident green button, exactly like a plain up.
        r12 = run_scenario(p, "relay-up-extra-json-fields-greens",
                           relay_plan=lambda n: "up-extra-fields", home_plan=lambda n: "ok",
                           sample_delays_s=[1, 3])
        ok12 = (r12["final_green"] and not r12["red_at"] and not r12["final_wol_disabled"]
                and r12["final_button_confident"])
        results.append(("relay-up-extra-json-fields-greens", ok12, r12,
                        "up with extra JSON fields → green card + confident green button"))

        # v8.7 THE FIX — the user's report. The relay /status answers a transient
        # "down" once (server-side SWR cache caught a momentary home blip), then
        # "up". v8.6 committed red on that first down (the red-that-was-green-a-
        # moment-later, with no orange in between). v8.7 must paint orange
        # "Vérification…" and re-probe → green, NEVER a red flash.
        r13 = run_scenario(p, "transient-relay-false-down-no-red",
                           relay_plan=lambda n: "down" if n == 1 else "up",
                           home_plan=lambda n: "ok",
                           sample_delays_s=[1, 4])
        ok13 = (r13["final_green"] and not r13["red_at"] and not r13["warn_at"]
                and bool(r13["checking_at"]) and bool(r13["button_checking_at"])
                and 1 not in r13["button_confident_at"])
        results.append(("transient-relay-false-down-no-red", ok13, r13,
                        "transient down → orange card+button then green, NEVER red"))

        # v8.7 THE FIX — a stale cache says "down" but the server is actually up.
        # v8.6 pre-painted the cached down as a confident red on open (then the
        # probe corrected to green) — a red flash from a stale cache. v8.7 never
        # pre-paints red from a cache: orange until the live probe greens it.
        r14 = run_scenario(p, "cache-down-server-actually-up-no-red",
                           relay_plan=lambda n: "up", home_plan=lambda n: "ok",
                           sample_delays_s=[0, 1, 3], preseed_cache={"up": False, "relayOk": True})
        ok14 = r14["final_green"] and not r14["red_at"] and not r14["warn_at"]
        results.append(("cache-down-server-actually-up-no-red", ok14, r14,
                        "stale cached down → never a red flash, greens via live probe"))

    return results


def print_verdict(results, engine):
    print("\n" + "=" * 72)
    print(f"VERDICT (real browser E2E — v8.7 confirm-before-red model) — "
          f"engine={engine} base={PWA_BASE}")
    print("=" * 72)
    all_ok = True
    for name, ok, r, want in results:
        all_ok = all_ok and ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name} | want {want} | "
              f"green_at={r.get('green_at')} red_at={r.get('red_at')} "
              f"warn_at={r.get('warn_at', '-')} calls={r['counters']}")
    print("=" * 72)
    print(f"[{engine}] ALL PASS" if all_ok
          else f"[{engine}] AT LEAST ONE SCENARIO FAILED")
    return all_ok


def _short(e):
    """Most informative line of a Playwright launch error (the deps banner is a
    long box; surface the cause, not just 'BrowserType.launch:')."""
    lines = [ln.strip().strip("║").strip() for ln in str(e).splitlines()]
    lines = [ln for ln in lines if ln and "═" not in ln]
    for ln in lines:  # prefer the human-readable cause if the banner has one
        low = ln.lower()
        if "missing dependencies" in low or "executable doesn't exist" in low:
            return ln[:160]
    for ln in lines:  # else the first concrete .so / non-label line
        if ln.endswith(".so") or ".so." in ln:
            return f"missing lib {ln}"[:160]
    return (lines[0] if lines else str(e))[:160]


def main():
    # Validate on every requested engine (Chromium baseline + WebKit/Safari for
    # iOS). An engine whose browser can't launch here (missing system libs, not
    # installed) is SKIPPED with a note — it does NOT fail the run, so the
    # Chromium gate still works on a host without the WebKit deps. The real iOS
    # gold standard stays a physical iPhone; this is the headless first line.
    global _CURRENT_ENGINE
    overall_ok = True
    ran, skipped = [], []
    for eng in ENGINES:
        _CURRENT_ENGINE = eng
        with sync_playwright() as p:
            try:
                getattr(p, eng).launch().close()
            except Exception as e:
                skipped.append(eng)
                print(f"\n[SKIP] engine={eng}: cannot launch — {_short(e)}")
                print(f"       → install it on a root-capable host: "
                      f"python3 -m playwright install --with-deps {eng}")
                continue
        overall_ok = print_verdict(collect_results(), eng) and overall_ok
        ran.append(eng)
    print("\n" + "#" * 72)
    print(f"engines run: {', '.join(ran) or '(none)'}"
          + (f" | skipped: {', '.join(skipped)}" if skipped else ""))
    if not ran:
        print("NO ENGINE COULD RUN — install a browser (see tests/README.md)")
        return 2
    print("ALL ENGINES PASS" if overall_ok else "AT LEAST ONE ENGINE FAILED")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
