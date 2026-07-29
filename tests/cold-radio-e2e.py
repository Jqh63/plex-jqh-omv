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
  python3 tests/cold-radio-e2e.py   (défaut = working tree)
Against the live deploy (post-merge gate): PWA_BASE=deployed.

Scenarios (mirror state-machine-sim.py):
  1. cold-launch-server-up-fast        — /status up → green ≤3 s
  2. cold-launch-server-off-fast       — /status down → red ≤3 s
  3. opaque-fallback-shows-unknown-not-green — /status ✕ → home ok → "Statut
                                         inconnu", NEVER green (v8.65: an opaque
                                         no-cors fulfil identifies nothing);
                                         relay warn only after the 3rd-miss
                                         debounce (~16 s)
  4. relay-and-home-unreachable-shows-unknown — /status ✕ → home ✕ → unknown, no
                                         red (a blocked fallback is no more
                                         evidence than a fulfilled one)
  5. cache-up-server-down-corrects-red — cache <60 s says up but the home was just
                                         stopped (relay down) → v8.6 reuses the
                                         cached green pre-paint (accepted trade-off)
                                         and the live probe corrects to red ≤3 s
 5b. cache-up-server-up-reused-green   — cache up + relay up → the reused green is
                                         confirmed by the live probe (no red/warn)
  6. relay-degraded-shows-unknown      — /status 503 → home ok → unknown, no warn,
                                         WoL on (the oracle is off, so we say so)
  7. relay-degraded-home-down-still-unknown — /status 503 → home ✕ → unknown, no
                                         red, no warn, WoL on
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
# Default = the WORKING TREE (file:// on this checkout) so a local fix is what
# the suite actually exercises. The deployed GitHub Pages site is opt-in:
# PWA_BASE=deployed (post-merge gate) or any explicit URL. Bitten 2026-07-19:
# with the deployed default, a v8.49 regression read as a "pre-existing flaky".
_LOCAL_BASE = "file://" + os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "index.html"))
PWA_BASE = os.environ.get("PWA_BASE") or _LOCAL_BASE
if PWA_BASE == "deployed":
    PWA_BASE = "https://jqh63.github.io/plex-jqh-omv/"
PWA_URL = (
    f"{PWA_BASE}"
    f"?host={CONFIG_HOST}&mac=AABBCCDDEEFF"
    f"&relay=https://{RELAY_HOST}&token=x&apps=seerr,plexweb"
)
# 2026-07-29 — the RELAY-LESS install, i.e. what a forker gets who deploys the
# page without standing up a relay. It is a different oracle, not a degraded
# one: `probe()` takes its own branch (`if(!config.relay)`) where the direct-home
# fetch IS the verdict. That branch sits on the most critical path in the app and
# had ZERO coverage until now — kept, and therefore tested, after Yann assumed
# the "public forkable repo" perimeter on 2026-07-29.
PWA_URL_NO_RELAY = (
    f"{PWA_BASE}"
    f"?host={CONFIG_HOST}&mac=AABBCCDDEEFF&apps=seerr,plexweb"
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

# v8.66 — drive the app on a shortened status poll (`?poll=`, a test-only knob,
# see app.js). Nearly all of this suite's runtime was DEAD WAIT on the 8 s
# cadence: the relay-down warn only hardens on the 3rd consecutive miss
# (RELAY_DOWN_MISSES=3), so proving it needed samples at T+18 and T+26 — 82 s of
# the 158 s per engine. At 2 s the misses land at ~0/2/4 s and the SAME property
# is asserted, with a sample before the warn as the positive control that it
# still doesn't cry wolf. Kept at 2 s rather than the 200 ms floor so the margin
# between "one miss" and "three misses" stays wider than webkit's jitter.
POLL_MS = int(os.environ.get("PWA_POLL_MS", "2000"))
P = POLL_MS / 1000.0        # poll cadence in seconds, for the delay arithmetic
_CURRENT_ENGINE = "chromium"


def _launch(p):
    """Launch the engine selected for the current pass (read by every runner)."""
    return getattr(p, _CURRENT_ENGINE).launch()


def capture_state(page):
    return page.evaluate(
        """() => ({
        statusLabel: document.getElementById('statusLabel').innerText,
        statusSub: document.getElementById('statusSub') ? document.getElementById('statusSub').innerText : '',
        dotClass: document.getElementById('statusDot').className,
        cardClass: document.getElementById('statusCard').className,
        fallbackClass: document.getElementById('fallbackLink') ? document.getElementById('fallbackLink').className : '',
        powerHidden: (() => { const e = document.getElementById('powerSection');
                              return !e || getComputedStyle(e).display === 'none'; })(),
        onLine: navigator.onLine,
        fallbackText: document.getElementById('fallbackLinkA') ? document.getElementById('fallbackLinkA').innerText : '',
        powerClass: document.getElementById('powerBtn') ? document.getElementById('powerBtn').className : '',
    })"""
    )


# 2026-07-28 — the "no network" state: the app knows nothing and no tap can help,
# so the card is hollow (dashed, unlit) and the power button is hidden. Told
# apart from every other state by FORM, not hue (v8.54).
def is_nonet(s):
    return "nonet" in s["cardClass"] and "nonet" in s["dotClass"]


# v8.65 — "Statut inconnu": the relay never answered, so there is NO oracle we
# can authenticate and the card claims nothing. Same unlit form as is_nonet
# (deliberate — it must not read as a verdict) but its own class, so a silent
# relay is never confused with a phone that has no network.
def is_unknown(s):
    return "unknown" in s["cardClass"] and "unknown" in s["dotClass"]


def is_red(s):
    return "offline" in s["dotClass"] or "offline" in s["cardClass"]


def is_warn(s):
    # Both "warn" (server up, relay down) and "promoted" (server down, relay
    # down) signal "relay unavailable" to the user — same visual semantics.
    # v8.53: `promoted` now ALSO means "a wake attempt failed" (wakeFailed).
    # Harmless here — this suite never fires a wake, so the only thing that can
    # promote the link in these scenarios is still relay reachability. The wake
    # meaning is covered in wake-e2e.py.
    return "warn" in s["fallbackClass"] or "promoted" in s["fallbackClass"]


def is_green(s):
    return "online" in s["dotClass"] and "online" in s["cardClass"]


def is_checking(s):
    # The orange "Vérification…" — the dot in its checking state and neither a
    # committed green nor red card. v8.7 shows this while a "down" verdict is
    # being re-confirmed (and on a cold open / a cached "down").
    return "checking" in s["dotClass"] and not is_red(s) and not is_green(s)


def is_wol_disabled(s):
    # v8.53 — the wake button is NEVER disabled any more. It used to render
    # `power-btn unavailable` (pointer-events:none) on a presumed-unreachable
    # relay; that presumption comes from status-poll misses, which are as often
    # the phone's own connectivity, so it produced a dead button on a wake that
    # would have worked. The relay-down warning now lives entirely in the
    # promoted fallback link (is_warn) and in postWol's failure toast.
    #
    # Kept as an assertion (rather than deleted) so the property is PINNED: the
    # scenarios below assert `not is_wol_disabled(...)`, and this now fails loudly
    # if anything ever re-introduces a pointer-events:none wake button.
    return "unavailable" in s["powerClass"] or "not-allowed" in s["powerClass"]


def is_button_confident(s):
    # The confident green "Serveur allumé" — power-btn.online. v8.6: lit on any
    # up verdict (cache reuse or live probe), no separate freshness gate.
    return "online" in s["powerClass"]


def is_button_checking(s):
    # v8.7 follow-up: the neutral "Vérification…" power button shown while the
    # card is orange (cold-open check or a down being re-confirmed), so the
    # button never sits on a stale confident green during a check.
    return "checking" in s["powerClass"]


def _relay_fulfill(route, verdict, aborted=None):
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
    elif verdict == "down-declared":
        # v8.48 — heartbeat-sourced down: the home's own clean-shutdown last-gasp.
        route.fulfill(status=200, headers=h,
                      body='{"up": false, "stale": false, "age_s": 2, "source": "heartbeat"}')
    elif verdict == "up-degraded":
        # v8.48 — host up, probed app (Seerr) not ready yet.
        route.fulfill(status=200, headers=h,
                      body='{"up": true, "stale": false, "age_s": 2, "degraded": true, "source": "heartbeat"}')
    elif verdict == "degraded":
        # Relay ANSWERS with a degraded oracle (STATUS_TARGET_URL unset → 503).
        # Relay alive, /wol works — the PWA must keep it reachable + fall back.
        route.fulfill(status=503, headers=h, body='{"detail": "status target not configured"}')
    elif verdict == "stall":
        # The relay is ANSWERING nothing yet — the real off-hours shape: the home
        # drops the probe's packets, so the relay sits on FIRST+RETRY (~7 s) before
        # it can say "down". Leaving the route unfulfilled models that wait exactly;
        # the PWA's own PROBE_TIMEOUT_MS eventually fires underneath.
        return
    else:  # 'fail' → transport failure
        if aborted is not None:
            aborted.add(route.request.url)
        route.abort()


def is_sleep(s):
    # v8.31 — the calm blue "Éteint (prévu)" card of a scheduled shutdown. Distinct
    # from is_red (the `offline` outage classes) and from is_checking (orange).
    return "sleep" in s["dotClass"] and "sleep" in s["cardClass"]


def _window_excluding_now(inside):
    """Return an "HHhMM-HHhMM" uptime window that either CONTAINS now (inside=True)
    or does not (inside=False), in the browser's local clock — which is this
    container's clock (Playwright inherits the system TZ)."""
    import datetime
    n = datetime.datetime.now()
    m = n.hour * 60 + n.minute
    lo, hi = ((m - 60) % 1440, (m + 60) % 1440) if inside else ((m + 60) % 1440, (m + 120) % 1440)
    f = lambda v: f"{v // 60:02d}h{v % 60:02d}"
    return f"{f(lo)}-{f(hi)}"


# --------------------------------------------------------------------------
# Route-interception loss detector (2026-07-27)
#
# Playwright's WebKit drops route interception for SOME requests: the first
# /status is served by the handler, a later one escapes to the real network and
# dies on DNS ("No address associated with hostname"). The app then receives a
# genuine transport failure and correctly commits red — so the scenario "fails"
# while the code under test behaved perfectly. Three scenarios had been red on
# WebKit for that reason alone, long enough that the whole engine's verdict had
# become background noise.
#
# The danger is not the noise, it is what the noise HIDES: on those runs WebKit
# is not testing what the scenario describes, so a real iOS regression would be
# invisible. The family does use iOS (an iPhone wake shows in the relay log), so
# that coverage matters.
#
# Any request to a mock host that reaches the real network is, by construction,
# a harness failure and never an app failure — the mock hosts do not exist. Flag
# it, and report such a scenario as SKIP-ENV rather than burying it in FAILs.
_MOCK_HOSTS = (RELAY_HOST, CONFIG_HOST)


def _watch_interception(page, flag, deliberate=None):
    """deliberate: set of request URLs the handler aborted ON PURPOSE (a mocked
    network failure). Without it, every scenario that simulates a dead leg looks
    like lost interception — and since a lost-interception FAIL is downgraded to
    SKIP-ENV, a REAL failure in those scenarios would be silently swallowed.
    Found while adding the offline scenarios, whose failures were reported
    SKIP-ENV until this was fixed."""
    def on_failed(req):
        host = urlparse(req.url).netloc
        if deliberate is not None and req.url in deliberate:
            return
        if any(host == h or host.endswith("." + h) for h in _MOCK_HOSTS):
            flag["lost"] = True
    page.on("requestfailed", on_failed)


def run_scenario(p, name, relay_plan, home_plan, sample_delays_s, preseed_cache=None,
                 url_extra="", offline=False, restore_online_at_s=None, phase=None,
                 no_relay=False):
    """relay_plan(n) → 'up'|'down'|'degraded'|'fail' for the n-th relay /status
    call (1-indexed). home_plan(n) → 'ok'|'fail' for the n-th direct-home call.
    preseed_cache: inject {up, relayOk} under STATUS_LOCAL_KEY before nav.
    offline: start with the browser context offline — navigator.onLine is false
    AND requests fail, which is what a phone in airplane mode does (setting the
    flag alone would let fetches succeed and test a state that cannot exist).
    restore_online_at_s: bring the network back at that sample, to pin that the
    app leaves the state on its own.
    phase: a dict flipped to {"online": True} at that moment, so the plans can
    key on the RADIO rather than on a call count. Necessary, not cosmetic: route
    interception answers even while the context is offline, so a plan that
    served "up" on its second call greened the tile with the radio still off —
    a state that cannot happen on a real phone.
    no_relay: drive the RELAY-LESS install (no `&relay=` in the URL). relay_plan
    is then never consulted — there is no relay to call — and home_plan alone
    decides. A scenario that passes `no_relay=True` and still expects relay calls
    is asserting on a request that cannot exist."""
    print(f"\n## Scenario: {name}")
    counters = {"relay": 0, "home": 0}
    _aborted = set()

    def handle(route: Route):
        parsed = urlparse(route.request.url)
        host = parsed.netloc
        if host == RELAY_HOST and parsed.path == "/status":
            counters["relay"] += 1
            _relay_fulfill(route, relay_plan(counters["relay"]), _aborted)
            return
        if host == CONFIG_HOST or host.endswith("." + CONFIG_HOST):
            counters["home"] += 1
            if home_plan(counters["home"]) == "ok":
                route.fulfill(status=200, body="")
            else:
                _aborted.add(route.request.url)
                route.abort()
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
    _iflag = {"lost": False}
    _watch_interception(page, _iflag, _aborted)
    page.route("**/*", handle)
    base = PWA_URL_NO_RELAY if no_relay else PWA_URL
    page.goto(base + url_extra + "&poll=" + str(POLL_MS), wait_until="load")
    # AFTER the navigation on purpose: WebKit refuses to load even a file:// URL
    # in an offline context (30 s goto timeout). The app's first probe is aborted
    # by the plans anyway, and the tile only reads navigator.onLine when that
    # probe settles ~2 s later — long after this line.
    if offline:
        ctx.set_offline(True)
    page.wait_for_selector("#statusLabel", state="attached", timeout=10000)

    samples = []
    last_t = 0
    for t in sample_delays_s:
        page.wait_for_timeout(int((t - last_t) * 1000))
        last_t = t
        if restore_online_at_s is not None and t >= restore_online_at_s and not ctx.is_closed():
            ctx.set_offline(False)
            if phase is not None:
                phase["online"] = True
            restore_online_at_s = None
        s = capture_state(page)
        samples.append((t, s))
        flags = [f for f, on in (("RED", is_red(s)), ("WARN", is_warn(s)), ("green", is_green(s))) if on]
        print(f"  T+{t}s: status={s['statusLabel']!r} fallback={s['fallbackText']!r} -> {','.join(flags) or '(neutral)'}")

    final = samples[-1][1]
    b.close()
    return {
        "name": name,
        "interception_lost": _iflag["lost"],
        "red_at": [t for t, s in samples if is_red(s)],
        "warn_at": [t for t, s in samples if is_warn(s)],
        "green_at": [t for t, s in samples if is_green(s)],
        "checking_at": [t for t, s in samples if is_checking(s)],
        "sleep_at": [t for t, s in samples if is_sleep(s)],
        "nonet_at": [t for t, s in samples if is_nonet(s)],
        "unknown_at": [t for t, s in samples if is_unknown(s)],
        "power_hidden_at": [t for t, s in samples if s["powerHidden"]],
        "final_nonet": is_nonet(final),
        "final_unknown": is_unknown(final),
        "final_sleep": is_sleep(final),
        "final_green": is_green(final),
        "final_red": is_red(final),
        "final_warn": is_warn(final),
        "final_wol_disabled": is_wol_disabled(final),
        "final_sub": final["statusSub"],
        "final_label": final["statusLabel"],
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
    _aborted = set()

    def handle(route: Route):
        parsed = urlparse(route.request.url)
        host = parsed.netloc
        if host == RELAY_HOST and parsed.path == "/status":
            counters["relay"] += 1
            _relay_fulfill(route, relay_plan(counters["relay"]), _aborted)
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
    _iflag = {"lost": False}
    _watch_interception(page, _iflag, _aborted)
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
        "interception_lost": _iflag["lost"],
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
    _iflag = {"lost": False}
    _watch_interception(page, _iflag)
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
        "interception_lost": _iflag["lost"],
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
    _iflag = {"lost": False}
    _watch_interception(page, _iflag)
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
    # Re-probe trigger. v8.53 — this used to click the refresh button as a
    # stand-in for the self-healing tick; the button is gone, so the test now
    # waits for the REAL mechanism, which is what the property was always about:
    # nothing but the 8 s self-healing poll can reclaim a wedged `checking`, and
    # it must. Budget = one poll (8 s, up to two before the watchdog age clears)
    # + the v8.7 confirm re-probe (DOWN_RECHECK_MS = 2.5 s) + slack. The property
    # under test is convergence to red (not frozen green), not the latency.
    page.wait_for_timeout(21000)
    post = capture_state(page)
    b.close()
    return {
        "name": name,
        "interception_lost": _iflag["lost"],
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
        # (3) consecutive misses. With instant-abort, misses land one poll apart,
        # so the warn confirms at ~2·P. Sample at 3·P and 5·P to catch it; every
        # earlier sample must show NO warn (the false-alarm we killed) — that
        # earlier sample is the positive control, not decoration.
        # v8.65 THE FIX — the IRL false green of 2026-07-29. The relay fetch
        # fails and the direct-home `no-cors` fallback SUCCEEDS (here: the mock
        # answers; IRL: a foreign wifi's captive portal, or the still-powered box
        # answering :443 in front of a shut-down host). Up to v8.64 that opaque
        # fulfil was promoted to {up:true} and painted a real green, refuted by
        # the warmed relay seconds later. An opaque response identifies nothing,
        # so the card must now say "Statut inconnu" — never green, never red.
        r3 = run_scenario(p, "opaque-fallback-shows-unknown-not-green",
                          relay_plan=lambda n: "fail", home_plan=lambda n: "ok",
                          sample_delays_s=[1, 3, 3 * P, 5 * P])
        ok3 = (r3["final_unknown"] and r3["final_warn"] and not r3["red_at"]
               and not r3["green_at"] and all(t >= 2 * P for t in r3["warn_at"]))
        results.append(("opaque-fallback-shows-unknown-not-green", ok3, r3,
                        "unknown card, NEVER green; relay warn only after 3rd miss (~16 s); no red"))

        # v8.2: red (server down) is immediate — the up/down verdict is NOT
        # debounced — but the relay warn still waits for the 3rd-miss confirm.
        # v8.65 — symmetric to scenario 3: a fallback that FAILS is no more
        # evidence than one that succeeds (the wifi may be blocking us), so a
        # silent relay lands on "unknown", not on the alarming red. A real red
        # still comes from the only thing that can prove it: the relay answering
        # up:false (scenario 2).
        r4 = run_scenario(p, "relay-and-home-unreachable-shows-unknown",
                          relay_plan=lambda n: "fail", home_plan=lambda n: "fail",
                          sample_delays_s=[1, 3, 3 * P, 5 * P])
        ok4 = (r4["final_unknown"] and r4["final_warn"] and not r4["final_green"]
               and not r4["red_at"] and all(t >= 2 * P for t in r4["warn_at"]))
        results.append(("relay-and-home-unreachable-shows-unknown", ok4, r4,
                        "unknown card, no red; relay warn only after 3rd miss (~16 s)"))

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

        # v8.65 — a 503 relay is alive (WoL still works) but its ORACLE is off.
        # The opaque home fallback would "confirm" up here; refused, same as
        # scenario 3. An admin misconfiguration now reads as one instead of being
        # papered over by a guess that is right on a home wifi and wrong abroad.
        r6 = run_scenario(p, "relay-degraded-shows-unknown",
                          relay_plan=lambda n: "degraded", home_plan=lambda n: "ok",
                          sample_delays_s=[1, 3])
        ok6 = (r6["final_unknown"] and not r6["green_at"] and not r6["warn_at"]
               and not r6["red_at"] and not r6["final_wol_disabled"])
        results.append(("relay-degraded-shows-unknown", ok6, r6,
                        "unknown, no green/red/warn, WoL enabled"))

        # Symmetry check for the degraded oracle: being right by luck is still a
        # guess, so no red either. The wake button stays armed — the one thing
        # the user actually needs when we don't know.
        r7 = run_scenario(p, "relay-degraded-home-down-still-unknown",
                          relay_plan=lambda n: "degraded", home_plan=lambda n: "fail",
                          sample_delays_s=[1, 3])
        ok7 = (r7["final_unknown"] and not r7["red_at"] and not r7["final_warn"]
               and not r7["warn_at"] and not r7["final_green"]
               and not r7["final_wol_disabled"])
        results.append(("relay-degraded-home-down-still-unknown", ok7, r7,
                        "unknown, no red/warn, WoL enabled"))

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
                           sample_delays_s=[1, 3, 4 * P])
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

        # v8.31 — cold open DURING the nightly shutdown, with the relay still paying
        # its ~7 s FIRST+RETRY against a home that drops the packets ("stall"). The
        # schedule already says "off", so the blue "Éteint (prévu)" card + the wake
        # button must be on screen within a second, not after the timeout. Fails on
        # v8.30, which painted the orange "Vérification…" for the whole wait.
        r15 = run_scenario(p, "cold-open-outside-window-presumes-sleep",
                           relay_plan=lambda n: "stall", home_plan=lambda n: "fail",
                           sample_delays_s=[1, 3],
                           url_extra="&window=" + _window_excluding_now(inside=False))
        ok15 = r15["sleep_at"] == [1, 3] and not r15["checking_at"] and not r15["green_at"]
        results.append(("cold-open-outside-window-presumes-sleep", ok15, r15,
                        "outside window + slow relay → sleep card at once, never orange"))

        # Positive control for r15 — the SAME stalled relay INSIDE the window must
        # never paint the "éteint" card. Without this, r15 could pass on a
        # presumption that fires everywhere (which would paint a false "éteint" over
        # a home that is simply slow to answer at 4 p.m.).
        # v8.60 — inside the window the card is now the presumed GREEN, not orange:
        # the schedule is a prior too. What r15 asserts (no blind sleep card) still
        # holds, and r15d below proves the presumption is correctable.
        r15b = run_scenario(p, "cold-open-inside-window-presumes-up",
                            relay_plan=lambda n: "stall", home_plan=lambda n: "fail",
                            sample_delays_s=[1, 3],
                            url_extra="&window=" + _window_excluding_now(inside=True))
        ok15b = r15b["green_at"] == [1, 3] and not r15b["sleep_at"]
        results.append(("cold-open-inside-window-presumes-up", ok15b, r15b,
                        "inside window + slow relay → green at once, never a presumed sleep"))

        # 2026-07-28 — THE PHONE has no network (airplane mode, no signal). Until now
        # this state was only covered by reading the code and by a render
        # fixture: run_scenario had no way to take the network away, so the one
        # state where the app must NOT offer its button was the one state never
        # exercised end to end. The card must go hollow (form, not hue — v8.54)
        # and the power button must be HIDDEN: no relay is reachable, so a tap
        # could only fail. Never red (that colour is an instruction to call the
        # admin, and the admin can do nothing about a phone with no signal),
        # never green.
        # The plans must FAIL too: an offline browser context still lets
        # Playwright's own route interception answer, so a plan that served "up"
        # would test a phone with no radio and a working relay — a state that
        # cannot exist. Failing both legs is what airplane mode does.
        r16 = run_scenario(p, "offline-phone-hollow-card-no-button",
                           relay_plan=lambda n: "fail", home_plan=lambda n: "fail",
                           sample_delays_s=[3, 5], offline=True,
                           url_extra="&window=" + _window_excluding_now(inside=True))
        ok16 = (r16["nonet_at"] == [3, 5] and r16["power_hidden_at"] == [3, 5]
                and not r16["red_at"] and not r16["green_at"])
        results.append(("offline-phone-hollow-card-no-button", ok16, r16,
                        "no network → hollow card + hidden button, never red/green"))

        # 2026-07-28 — and it must LEAVE that state when the signal comes back. The
        # app has no 'online' listener, so recovery rides the 8 s poll: sampled
        # at T+14, ~10 s after the radio returns at T+4. If that ever becomes
        # too slow to live with, THIS is the pin that will have to move.
        _phase = {"online": False}
        r16b = run_scenario(p, "offline-phone-recovers-when-network-returns",
                            relay_plan=lambda n: "up" if _phase["online"] else "fail",
                            home_plan=lambda n: "ok" if _phase["online"] else "fail",
                            sample_delays_s=[3, 4, 4 + 3 * P], offline=True,
                            restore_online_at_s=4, phase=_phase,
                            url_extra="&window=" + _window_excluding_now(inside=True))
        # v8.65 — the T+4 sample now races the `online` listener (the radio is
        # restored microseconds before the sample, and recovery no longer waits
        # for the poll), so only the T+3 hollow state is pinned; what matters is
        # that it STARTS hollow and ENDS green with the button back.
        ok16b = (r16b["nonet_at"][:1] == [3] and r16b["final_green"]
                 and not r16b["final_nonet"] and 14 not in r16b["power_hidden_at"])
        results.append(("offline-phone-recovers-when-network-returns", ok16b, r16b,
                        "network back → leaves the hollow card, button returns, greens"))

        # v8.60 positive control — the in-window green presumption must NOT fire
        # over a freshly persisted "down": during a real outage the family re-opens
        # the app, and a green flash on every open would be the mirror of the red
        # flash v8.7 banned. Same stalled relay, same window, cache says down →
        # orange. Without this case, the presumption above could pass while being
        # blind to everything the client already knows.
        r15d = run_scenario(p, "cold-open-inside-window-cached-down-still-checks",
                            relay_plan=lambda n: "stall", home_plan=lambda n: "fail",
                            sample_delays_s=[1, 3],
                            preseed_cache={"up": False, "relayOk": True},
                            url_extra="&window=" + _window_excluding_now(inside=True))
        ok15d = r15d["checking_at"] == [1, 3] and not r15d["green_at"]
        results.append(("cold-open-inside-window-cached-down-still-checks", ok15d, r15d,
                        "inside window + cached down → orange, no green flash"))

        # v8.31 — the presumption must be CORRECTABLE: same off-hours open, but the
        # home is actually up (auto-WoL by home-watch, or another family member woke
        # it). The instant sleep card must flip green as soon as the relay answers.
        r15c = run_scenario(p, "outside-window-presumed-sleep-corrects-green",
                            relay_plan=lambda n: "up", home_plan=lambda n: "ok",
                            sample_delays_s=[3],
                            url_extra="&window=" + _window_excluding_now(inside=False))
        ok15c = r15c["final_green"] and not r15c["final_sleep"]
        results.append(("outside-window-presumed-sleep-corrects-green", ok15c, r15c,
                        "presumed sleep + home actually up → corrects to green"))

        # v8.48 — a heartbeat-declared down (the home's own last-gasp) commits red
        # at once: no orange re-confirmation detour, even from a reused green
        # pre-paint. Contrast with cache-up-server-down-corrects-red, where the
        # probed "down" MUST show the orange first.
        # Discriminating samples: with a PROBED down, T+2 is still orange (the
        # DOWN_RECHECK re-probe fires at 2.5 s, red lands ≥T+3 — see r2); a
        # DECLARED down must already be red at T+2, with no orange re-check.
        # v8.53 — a declared down is now painted the CALM blue, not the alarming
        # red, and inside the uptime window at that. Rationale (relay log, July
        # 2026): the home's shutdown is gated on the AM5 being on, not on the
        # clock alone, so it stops inside its own window most evenings — 8 of the
        # last 14 shutdowns, typically ~22h30. Keying the wording on the window
        # alone painted every one of those normal stops as an outage. The red is
        # now reserved for SILENCE, which is the only thing a crash produces.
        # The instant-commit property (no orange re-check detour) is unchanged
        # and still asserted, which is what v8.48 was about.
        r16 = run_scenario(p, "heartbeat-declared-down-instant-calm",
                           relay_plan=lambda n: "down-declared", home_plan=lambda n: "ok",
                           sample_delays_s=[2, 3],
                           url_extra="&window=" + _window_excluding_now(inside=True))
        ok16 = (r16["final_sleep"] and not r16["final_red"] and not r16["checking_at"]
                and "teint" in r16["final_label"])
        results.append(("heartbeat-declared-down-instant-calm", ok16, r16,
                        "declared down INSIDE the window → calm blue 'Éteint', "
                        "no orange detour, never the outage red"))

        # v8.48 — up+degraded paints the green card with the explanatory sub
        # ("services en démarrage…") instead of the generic one. v8.63 shortened it:
        # the long form was cut by 13 px at 320 px CSS.
        r17 = run_scenario(p, "up-degraded-sub-label",
                           relay_plan=lambda n: "up-degraded", home_plan=lambda n: "ok",
                           sample_delays_s=[1, 3])
        ok17 = (r17["final_green"] and not r17["red_at"]
                and "services en démarrage" in r17["final_sub"])
        results.append(("up-degraded-sub-label", ok17, r17,
                        "green card with 'services en démarrage…' sub"))

        # v8.54 — the red card INSTRUCTS instead of describing. Unexplained
        # silence is the one thing the family cannot act on alone, so the sub
        # tells them the only useful thing: report it. A relay we cannot reach
        # lands here too (probe() resolves up:false on a relay miss), which is
        # deliberate — a broken relay is a real problem, not a shrug.
        # Fails on v8.53, which said "serveur éteint".
        r18 = run_scenario(p, "unexplained-down-says-contact-admin",
                           relay_plan=lambda n: "down", home_plan=lambda n: "fail",
                           sample_delays_s=[3, 4],
                           url_extra="&window=" + _window_excluding_now(inside=True))
        ok18 = (r18["final_red"] and not r18["final_sleep"]
                and "administrateur" in r18["final_sub"])
        results.append(("unexplained-down-says-contact-admin", ok18, r18,
                        "unexplained silence → red card instructing to contact the admin"))

        # v8.54 — the two blue states collapsed into one. A scheduled stop and a
        # declared stop asked for the SAME user action (press the button); only
        # the auto-wake time differed, and that lives in the sub now. The label
        # must be the bare "Éteint" — fails on v8.53, which said "Éteint (prévu)"
        # outside the window. r16 above is the other half: the declared stop
        # inside the window must reach the SAME label.
        r19 = run_scenario(p, "scheduled-off-single-blue-label",
                           relay_plan=lambda n: "down", home_plan=lambda n: "fail",
                           sample_delays_s=[3],
                           url_extra="&window=" + _window_excluding_now(inside=False))
        ok19 = (r19["final_sleep"] and r19["final_label"].strip() == "Éteint"
                and "réveil auto" in r19["final_sub"])
        results.append(("scheduled-off-single-blue-label", ok19, r19,
                        "scheduled off → bare 'Éteint' label, auto-wake time in the sub"))

        # ------------------------------------------------------------------
        # 2026-07-29 — THE RELAY-LESS INSTALL (a fork's default).
        #
        # `probe()` takes a separate branch when no relay is configured, where
        # the direct-home fetch IS the verdict instead of being ignored (v8.65
        # removed it from the relayed path precisely because an opaque response
        # identifies nothing — here it is the only oracle there is, and a fork
        # that ships without a relay accepts that weaker evidence knowingly).
        # That branch had no test at all while sitting on the critical path.
        #
        # These pin what a forker actually gets. `no_relay=True` also means the
        # relay counter MUST stay 0: if it moves, the app called a relay it was
        # never given, and the scenario is not testing the branch it claims to.
        r20 = run_scenario(p, "no-relay-home-up-greens",
                           relay_plan=lambda n: "fail", home_plan=lambda n: "ok",
                           sample_delays_s=[3], no_relay=True,
                           url_extra="&window=" + _window_excluding_now(inside=True))
        ok20 = (r20["final_green"] and not r20["final_red"] and not r20["final_warn"]
                and r20["counters"]["relay"] == 0 and r20["counters"]["home"] > 0)
        results.append(("no-relay-home-up-greens", ok20, r20,
                        "no relay + home reachable → green off the direct probe, zero relay calls"))

        # The other half. Also pins that the relay-down WARN never fires here:
        # there is no relay to be down, and telling a forker his relay is
        # unreachable when he never configured one is a lie the UI must not tell.
        r21 = run_scenario(p, "no-relay-home-down-reds-without-relay-warn",
                           relay_plan=lambda n: "up", home_plan=lambda n: "fail",
                           sample_delays_s=[3, 5], no_relay=True,
                           url_extra="&window=" + _window_excluding_now(inside=True))
        ok21 = (r21["final_red"] and not r21["final_green"] and not r21["final_warn"]
                and r21["counters"]["relay"] == 0)
        results.append(("no-relay-home-down-reds-without-relay-warn", ok21, r21,
                        "no relay + home unreachable → red, never the relay-down warn"))

        # And the wake button must be HIDDEN, not offered-then-broken: wolReady()
        # requires a relay + token, so there is no way to wake anything. Pinning
        # this is what keeps a future "always arm the button" idea from silently
        # shipping a button that cannot work in this mode. Home is up here, so a
        # hidden button cannot be confused with the offline-phone masking.
        ok22 = (r20["power_hidden_at"] == [3])
        results.append(("no-relay-wake-button-hidden", ok22, r20,
                        "no relay → no wake possible → button hidden, not broken"))

    return results


def print_verdict(results, engine):
    print("\n" + "=" * 72)
    print(f"VERDICT (real browser E2E — v8.7 confirm-before-red model) — "
          f"engine={engine} base={PWA_BASE}")
    print("=" * 72)
    all_ok = True
    skipped = 0
    for name, ok, r, want in results:
        # A scenario that failed WHILE the harness lost route interception says
        # nothing about the app: the mock host does not resolve, so the request
        # that escaped got a real DNS error and the app reacted correctly to it.
        # Report it as SKIP-ENV so it can never be mistaken for a regression —
        # and, more importantly, so the remaining FAILs stay meaningful.
        env = (not ok) and r.get("interception_lost")
        if env:
            skipped += 1
        else:
            all_ok = all_ok and ok
        tag = "SKIP-ENV" if env else ("PASS" if ok else "FAIL")
        print(f"[{tag}] {name} | want {want} | "
              f"green_at={r.get('green_at')} red_at={r.get('red_at')} "
              f"warn_at={r.get('warn_at', '-')} calls={r['counters']}")
    print("=" * 72)
    if skipped:
        print(f"[{engine}] {skipped} scenario(s) SKIPPED — Playwright lost route "
              f"interception on this engine (a mock host reached the real "
              f"network). Not an app verdict; see _watch_interception.")
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
    # 80 % of the family is on Android, so chromium is the engine that matters
    # day to day and PWA_ENGINES=chromium is the right ITERATION mode. Say so
    # loudly on a partial run: a green partial run reads exactly like a green
    # full one, and the remaining 20 % are on iOS/WebKit.
    if set(ran) != {"chromium", "webkit"}:
        print("⚠ PARTIAL ENGINE RUN — iteration mode. Re-run both engines "
              "(the default) before merging.")
    print("ALL ENGINES PASS" if overall_ok else "AT LEAST ONE ENGINE FAILED")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
