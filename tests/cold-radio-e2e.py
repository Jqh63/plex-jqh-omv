"""
Real-browser E2E validation of plex-jqh-omv v8 (single-probe status model).

Drives the PWA with Playwright + Chromium headless. The route handler
intercepts the relay's `/status` endpoint (the single PWA fetch) and the
direct-home fallback. Paint events are captured via DOM polling; verdict
printed at the end.

What this E2E covers vs. the offline sim (`state-machine-sim.py`):
- The sim verifies the v8 state-machine semantics + timing bounds (orange
  never held past one PROBE+HOME) on a synthetic clock — fast, deterministic.
- This E2E verifies that the actual `app.js` wires into those semantics
  through real fetch + timer + visibilitychange paths in Chromium. It's the
  gate before declaring a release usable.

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
                                         only after the v8.2 3rd-miss debounce (~30 s)
  4. relay-and-home-down               — /status ✕ → home ✕ → red immediate;
                                         relay warn only after the 3rd-miss debounce
  5. card-cache-up-server-down-no-false-green — cache <60 s says up but the home
                                         was just stopped (relay down) → the card
                                         must NEVER flash green (v8.5 honesty),
                                         red ≤3 s
 5b. card-cache-up-server-up-confirms-green — cache up + relay up → honest
                                         "Vérification…" resolves to green (the
                                         fix must not over-suppress a real up)
  6. relay-degraded-server-up          — /status 503 → home ok → green, no warn, WoL on
  7. relay-degraded-server-down        — /status 503 → home ✕ → red, no warn, WoL on
  8. resume-focus-only-converges-red   — bg → server dies → focus → red
  9. resume-no-event-self-heals-red    — bg → server dies → no event → red ≤3 s
 10. relay-single-miss-debounced-no-warn — lone /status ✕ then recover → green,
                                         NEVER warn, WoL stays enabled (debounce payoff)
 11. watchdog-reclaims-wedged-checking — stuck checking=true + server down → a
                                         re-probe reclaims the flag → red, not frozen green
 12. button-stale-relay-up-greens      — relay serves a stale up (steady state) →
                                         button lights the confident green (v8.4;
                                         guards the v8.3 stuck-orange regression)

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


def is_wol_disabled(s):
    # The wake button goes to "power-btn unavailable" only when relayReachable
    # is false. A *degraded* (answered) /status failure keeps it enabled.
    return "unavailable" in s["powerClass"]


def is_button_confident(s):
    # v8.3: the confident green "Serveur allumé" — power-btn.online. Asserted
    # only on a FRESH up verdict.
    return "online" in s["powerClass"]


def is_button_checking(s):
    # v8.3: the honest "Vérification…" button on a cached / relay-stale up.
    return "checking" in s["powerClass"]


def _relay_fulfill(route, verdict):
    h = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
    if verdict == "up":
        route.fulfill(status=200, headers=h, body='{"up": true, "stale": false, "age_s": 0}')
    elif verdict == "up-stale":
        # Relay serving a stale-but-within-ceiling verdict (home may have just
        # gone down inside the 60 s SWR window). The PWA keeps the up value but
        # must NOT light the confident green button — v8.3 button honesty.
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

    b = p.chromium.launch()
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
        "final_green": is_green(final),
        "final_red": is_red(final),
        "final_warn": is_warn(final),
        "final_wol_disabled": is_wol_disabled(final),
        "button_confident_at": [t for t, s in samples if is_button_confident(s)],
        "final_button_confident": is_button_confident(final),
        "final_button_checking": is_button_checking(final),
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
    """background → foreground resume. Loads a preseeded "up" cache (v8.5: shown
    as the honest "Vérification…", not a confident green, until a live probe
    confirms), backgrounds at bg_at_s, returns to foreground at fg_at_s
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

    b = p.chromium.launch()
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

    b = p.chromium.launch()
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
    # Re-probe trigger (stands in for the 15 s self-healing tick) — the refresh
    # button calls checkStatus().
    page.click("#refreshBtn")
    page.wait_for_timeout(2000)
    post = capture_state(page)
    b.close()
    return {
        "name": name,
        "pre_green": is_green(pre),
        "final_red": is_red(post),
        "final_green": is_green(post),
        "counters": dict(counters),
    }


def main():
    results = []
    with sync_playwright() as p:
        r1 = run_scenario(p, "cold-launch-server-up-fast",
                          relay_plan=lambda n: "up", home_plan=lambda n: "ok",
                          sample_delays_s=[1, 3])
        ok1 = (bool(r1["green_at"]) and r1["green_at"][0] <= 3 and not r1["red_at"]
               and not r1["warn_at"] and r1["final_button_confident"])
        results.append(("cold-launch-server-up-fast", ok1, r1,
                        "green ≤T+3, no red, no warn, button confident on fresh up"))

        r2 = run_scenario(p, "cold-launch-server-off-fast",
                          relay_plan=lambda n: "down", home_plan=lambda n: "ok",
                          sample_delays_s=[1, 3])
        ok2 = bool(r2["red_at"]) and r2["red_at"][0] <= 3 and not r2["green_at"] and not r2["warn_at"]
        results.append(("cold-launch-server-off-fast", ok2, r2,
                        "red ≤T+3, no green, no warn"))

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

        # v8.5 card honesty — THE reported bug. The cache (<60 s) still says up,
        # but the home was just stopped so the relay answers down. Pre-v8.5 the
        # cache pre-paint flashed the confident green at T=0; v8.5 paints the
        # neutral "Vérification…" and the live probe lands red. The card must
        # NEVER show green here — that's the deterministic regression guard (old
        # code → green at T=0; new code → no green at all).
        r5 = run_scenario(p, "card-cache-up-server-down-no-false-green",
                          relay_plan=lambda n: "down", home_plan=lambda n: "ok",
                          sample_delays_s=[0, 1, 3], preseed_cache={"up": True, "relayOk": True})
        ok5 = (not r5["green_at"]) and r5["final_red"] and bool(r5["red_at"]) and r5["red_at"][0] <= 3
        results.append(("card-cache-up-server-down-no-false-green", ok5, r5,
                        "no green at all (cache no longer flashes green), red ≤T+3"))

        # v8.5 — the fix must not OVER-suppress: a cache up + a server that is
        # still up resolves the honest "Vérification…" to a confident green once
        # the live probe confirms.
        r5b = run_scenario(p, "card-cache-up-server-up-confirms-green",
                           relay_plan=lambda n: "up", home_plan=lambda n: "ok",
                           sample_delays_s=[1, 3], preseed_cache={"up": True, "relayOk": True})
        ok5b = r5b["final_green"] and not r5b["red_at"] and not r5b["warn_at"]
        results.append(("card-cache-up-server-up-confirms-green", ok5b, r5b,
                        "honest checking resolves to green, no red/warn"))

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

        r11 = run_watchdog_scenario(p)
        ok11 = r11["pre_green"] and r11["final_red"] and not r11["final_green"]
        results.append(("watchdog-reclaims-wedged-checking", ok11, r11,
                        "wedged checking reclaimed on re-probe → red, not frozen green"))

        # v8.4 button honesty + regression guard: the relay serves a STALE up —
        # the STEADY STATE on a healthy home (the PWA polls /15 s vs the relay's
        # 5 s fresh window, so almost every poll is stale:true). The button MUST
        # light the confident green: a stale-but-up relay is a real server-side
        # confirmation. v8.3 gated the green on !stale and left the button stuck
        # orange ~30 s+ on a perfectly up home — the regression this guards.
        r12 = run_scenario(p, "button-stale-relay-up-greens",
                           relay_plan=lambda n: "up-stale", home_plan=lambda n: "ok",
                           sample_delays_s=[1, 3])
        ok12 = (r12["final_green"] and not r12["red_at"] and not r12["final_wol_disabled"]
                and r12["final_button_confident"] and not r12["final_button_checking"])
        results.append(("button-stale-relay-up-greens", ok12, r12,
                        "green card + confident green button on a stale-but-up relay (no stuck orange)"))

    print("\n" + "=" * 72)
    print(f"VERDICT (real browser E2E — v8 single-probe model) — base={PWA_BASE}")
    print("=" * 72)
    all_ok = True
    for name, ok, r, want in results:
        all_ok = all_ok and ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name} | want {want} | "
              f"green_at={r.get('green_at')} red_at={r.get('red_at')} "
              f"warn_at={r.get('warn_at', '-')} calls={r['counters']}")
    print("=" * 72)
    print("ALL PASS" if all_ok else "AT LEAST ONE SCENARIO FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
