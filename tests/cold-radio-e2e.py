"""
Real-browser E2E validation of plex-jqh-omv v7.0 (relay-as-oracle).

Drives the LIVE PWA at https://jqh63.github.io/plex-jqh-omv/ with
Playwright + Chromium headless. The route handler intercepts the
relay's `/status` endpoint (the single PWA fetch since v7.0) and the
direct-home fallback. Paint events are captured via DOM polling;
verdict printed at the end.

What this E2E covers vs. the offline sim (`state-machine-sim.py`):
- The sim verifies the state-machine semantics on a synthetic clock —
  fast, deterministic, side-by-side with the historical v4-v6 apps.
- This E2E verifies that the live deployed PWA (the actual `app.js` on
  GitHub Pages) wires into those semantics through real fetch+timer
  paths in Chromium. It's the gate before declaring a release usable.

Scenarios (one per ADR `2026-05-27-pwa-plex-jqh-omv-relay-as-oracle`
§Phase 2):
  1. v7-cold-launch-server-up-fast      — /status up → green <2 s
  2. v7-cold-launch-server-off-fast     — /status down → red <2 s
  3. v7-relay-timeout-fallback-home-up  — /status ✕✕ → home ok → green + warn
  4. v7-relay-timeout-fallback-home-down — /status ✕✕ → home ✕ → red + warn
  5. v7-stale-cache-paint-then-refresh  — localStorage <60 s → green instant
  6. v7-status-with-1-retry             — /status ✕→✓ → green, no red flash
"""

import os
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Route

CONFIG_HOST = "test.example.com"
RELAY_HOST = "r.example.com"
# PWA_BASE defaults to the live GitHub Pages deploy (the post-merge gate).
# Override it to validate the working tree BEFORE merge — the PWA is flat
# HTML/JS so file:// works:
#   PWA_BASE="file:///config/workspace/plex-jqh-omv/index.html" python3 tests/cold-radio-e2e.py
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
    # down) signal "relay unavailable" to the user — same visual semantics in
    # `setFallbackState()`. Match either.
    return "warn" in s["fallbackClass"] or "promoted" in s["fallbackClass"]


def is_green(s):
    return "online" in s["dotClass"] and "online" in s["cardClass"]


def is_wol_disabled(s):
    # The wake button goes to "power-btn unavailable" only when relayReachable
    # is false. A1 fix: an *answered* /status failure must keep it enabled.
    return "unavailable" in s["powerClass"]


def run_scenario(p, name, relay_plan, home_plan, sample_delays_s, preseed_cache=None):
    """relay_plan(n) → 'up' | 'down' | 'fail' for the n-th relay /status call
    (1-indexed). 'up'/'down' return a JSON body; 'fail' aborts the request
    (timeout/network-error on the PWA side).

    home_plan(n) → 'ok' | 'fail' for the n-th direct-home call (no-cors).
    Only consulted when the PWA falls back to fetchHomeDirectly().

    preseed_cache: if set, inject {up, relayOk} under STATUS_LOCAL_KEY
    before navigation, so app.js's readLocalStatus() paints immediately.
    """
    print(f"\n## Scenario: {name}")
    counters = {"relay": 0, "home": 0}

    def handle(route: Route):
        url = route.request.url
        parsed = urlparse(url)
        host = parsed.netloc

        if host == RELAY_HOST and parsed.path == "/status":
            counters["relay"] += 1
            verdict = relay_plan(counters["relay"])
            if verdict == "up":
                route.fulfill(
                    status=200,
                    headers={
                        "Content-Type": "application/json",
                        "Access-Control-Allow-Origin": "*",
                    },
                    body='{"up": true, "stale": false, "age_s": 0}',
                )
            elif verdict == "down":
                route.fulfill(
                    status=200,
                    headers={
                        "Content-Type": "application/json",
                        "Access-Control-Allow-Origin": "*",
                    },
                    body='{"up": false, "stale": false, "age_s": null}',
                )
            elif verdict == "degraded":
                # Relay ANSWERS with a degraded oracle (e.g. STATUS_TARGET_URL
                # unset → 503). Relay is alive, /wol works — A1 fix: the PWA
                # must keep it reachable and fall back to direct-home.
                route.fulfill(
                    status=503,
                    headers={
                        "Content-Type": "application/json",
                        "Access-Control-Allow-Origin": "*",
                    },
                    body='{"detail": "status target not configured"}',
                )
            else:  # 'fail'
                route.abort()
            return

        if host == CONFIG_HOST or host.endswith("." + CONFIG_HOST):
            counters["home"] += 1
            verdict = home_plan(counters["home"])
            if verdict == "ok":
                route.fulfill(status=200, body="")
            else:
                route.abort()
            return

        route.continue_()

    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    if preseed_cache is not None:
        import json
        payload = json.dumps({
            "up": bool(preseed_cache.get("up")),
            "relayOk": bool(preseed_cache.get("relayOk", True)),
            "t": None,  # filled at runtime by Date.now()
        })
        ctx.add_init_script(
            f"try{{var p={payload};p.t=Date.now();"
            f"localStorage.setItem('{STATUS_LOCAL_KEY}',JSON.stringify(p));}}"
            f"catch(e){{}}"
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
        flags = []
        if is_red(s):
            flags.append("RED")
        if is_warn(s):
            flags.append("WARN")
        if is_green(s):
            flags.append("green")
        print(
            f"  T+{t}s: status={s['statusLabel']!r} fallback={s['fallbackText']!r} -> {','.join(flags) or '(neutral)'}"
        )

    red_at = [t for t, s in samples if is_red(s)]
    warn_at = [t for t, s in samples if is_warn(s)]
    green_at = [t for t, s in samples if is_green(s)]
    final_state = samples[-1][1]
    b.close()
    return {
        "name": name,
        "red_at": red_at,
        "warn_at": warn_at,
        "green_at": green_at,
        "final_green": is_green(final_state),
        "final_red": is_red(final_state),
        "final_warn": is_warn(final_state),
        "final_wol_disabled": is_wol_disabled(final_state),
        "counters": dict(counters),
    }


def _spoof_visibility(page, hidden, event):
    """Spoof document.hidden/visibilityState and optionally dispatch an event.

    `event` ∈ {"visibilitychange", "focus", "none"}. The PWA's handlers read
    document.hidden, so the spoofed getter is honored. "none" flips the flag
    without dispatching anything — the Android quirk where the foreground
    return carries no event at all.
    """
    page.evaluate(
        """([hidden, event]) => {
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden});
        Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => hidden ? 'hidden' : 'visible'});
        if (event === 'visibilitychange') document.dispatchEvent(new Event('visibilitychange'));
        else if (event === 'focus') window.dispatchEvent(new Event('focus'));
    }""",
        [hidden, event],
    )


def run_resume_scenario(p, name, relay_plan, home_plan, fg_event,
                        bg_at_s, fg_at_s, sample_delays_s, preseed_cache):
    """v7.7 — background → foreground resume test.

    Loads the PWA (preseeded green), backgrounds it at `bg_at_s` (spoof hidden
    + visibilitychange), then returns to foreground at `fg_at_s` dispatching
    only `fg_event`. `relay_plan(n)` drives the n-th /status verdict so the
    server can be "up" before background and "down" after. `sample_delays_s`
    are offsets RELATIVE TO THE FOREGROUND event. The bug: a focus-only / no-
    event return leaves the pre-v7.7 PWA frozen on the stale green; v7.7 must
    converge to red.
    """
    print(f"\n## Resume scenario: {name} (fg_event={fg_event})")
    counters = {"relay": 0, "home": 0}

    def handle(route: Route):
        url = route.request.url
        parsed = urlparse(url)
        host = parsed.netloc
        if host == RELAY_HOST and parsed.path == "/status":
            counters["relay"] += 1
            verdict = relay_plan(counters["relay"])
            if verdict == "up":
                route.fulfill(status=200, headers={"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
                              body='{"up": true, "stale": false, "age_s": 0}')
            elif verdict == "down":
                route.fulfill(status=200, headers={"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
                              body='{"up": false, "stale": false, "age_s": null}')
            else:
                route.abort()
            return
        if host == CONFIG_HOST or host.endswith("." + CONFIG_HOST):
            counters["home"] += 1
            route.fulfill(status=200, body="") if home_plan(counters["home"]) == "ok" else route.abort()
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
    _spoof_visibility(page, hidden=True, event="visibilitychange")   # go background
    page.wait_for_timeout(int((fg_at_s - bg_at_s) * 1000))
    _spoof_visibility(page, hidden=False, event=fg_event)            # return to foreground

    samples = []
    last_t = 0
    for t in sample_delays_s:
        page.wait_for_timeout(int((t - last_t) * 1000))
        last_t = t
        s = capture_state(page)
        samples.append((t, s))
        flags = []
        if is_red(s):
            flags.append("RED")
        if is_warn(s):
            flags.append("WARN")
        if is_green(s):
            flags.append("green")
        print(f"  fg+{t}s: status={s['statusLabel']!r} -> {','.join(flags) or '(neutral)'}")

    final_state = samples[-1][1]
    b.close()
    return {
        "name": name,
        "red_at": [t for t, s in samples if is_red(s)],
        "green_at": [t for t, s in samples if is_green(s)],
        "final_green": is_green(final_state),
        "final_red": is_red(final_state),
        "counters": dict(counters),
    }


def main():
    with sync_playwright() as p:
        r1 = run_scenario(
            p,
            "v7-cold-launch-server-up-fast",
            relay_plan=lambda n: "up",
            home_plan=lambda n: "ok",
            sample_delays_s=[1, 3],
        )
        r2 = run_scenario(
            p,
            "v7-cold-launch-server-off-fast",
            relay_plan=lambda n: "down",
            home_plan=lambda n: "ok",
            sample_delays_s=[1, 3],
        )
        r3 = run_scenario(
            p,
            "v7-relay-timeout-fallback-home-up",
            # Two relay failures triggers fallback; home then answers ok.
            # v7.1 timeout = 5 s × 2 attempts + home call → settle ~T+10.1.
            relay_plan=lambda n: "fail",
            home_plan=lambda n: "ok",
            sample_delays_s=[3, 12, 18, 24],
        )
        r4 = run_scenario(
            p,
            "v7-relay-timeout-fallback-home-down",
            # All three legs time out → setOffline at ~T+15.
            relay_plan=lambda n: "fail",
            home_plan=lambda n: "fail",
            sample_delays_s=[3, 12, 18, 24],
        )
        r5 = run_scenario(
            p,
            "v7-stale-cache-paint-then-refresh",
            relay_plan=lambda n: "up",
            home_plan=lambda n: "ok",
            sample_delays_s=[0, 1, 3],
            preseed_cache={"up": True, "relayOk": True},
        )
        r6 = run_scenario(
            p,
            "v7-status-with-1-retry",
            # First relay call fails → PWA retries → second call succeeds → green.
            relay_plan=lambda n: "fail" if n == 1 else "up",
            home_plan=lambda n: "ok",
            sample_delays_s=[1, 5, 8],
        )
        r7 = run_scenario(
            p,
            "v7-relay-answered-degraded-server-up",
            # Relay answers 503 (degraded oracle) → fall back to home (up).
            # A1 fix: green, NO warn banner, wake button stays enabled.
            relay_plan=lambda n: "degraded",
            home_plan=lambda n: "ok",
            sample_delays_s=[1, 3],
        )
        r8 = run_scenario(
            p,
            "v7-relay-answered-degraded-server-down",
            # Relay answers 503, home actually down. A1 fix: red (server down)
            # but NO warn and the wake button stays ENABLED so the user can
            # still fire a WoL — this is the IRL case that motivated the fix.
            relay_plan=lambda n: "degraded",
            home_plan=lambda n: "fail",
            sample_delays_s=[1, 3],
        )
        r9 = run_scenario(
            p,
            "v7-all-timeout-while-green-holds-not-red",
            # v7.6 — start green (preseed), then a full all-timeout cycle (relay
            # ✕✕ + home ✕). Must HOLD green, not flash red. The held re-check
            # (~+3 s) then sees the relay up → stays green. Kills the IRL FP
            # (green → red+relais → force-refresh → green).
            relay_plan=lambda n: "fail" if n <= 2 else "up",
            home_plan=lambda n: "fail",
            sample_delays_s=[1, 5, 7],
            preseed_cache={"up": True, "relayOk": True},
        )
        # v7.7 — resume bug. Server up at load (relay call #1), goes off during
        # background (calls #2+ return "down"). Foreground returns via FOCUS
        # only — pre-v7.7 stays frozen green; v7.7 re-probes on focus → red.
        r10 = run_resume_scenario(
            p,
            "v7.7-resume-focus-only-converges-to-red",
            relay_plan=lambda n: "up" if n == 1 else "down",
            home_plan=lambda n: "ok",
            fg_event="focus",
            bg_at_s=3, fg_at_s=6,
            sample_delays_s=[1, 3],
            preseed_cache={"up": True, "relayOk": True},
        )
        # v7.7 — same, but the foreground return carries NO event at all. Only
        # the self-healing 15 s interval can rescue it. Sample past one tick.
        r11 = run_resume_scenario(
            p,
            "v7.7-resume-no-event-self-heals-to-red",
            relay_plan=lambda n: "up" if n == 1 else "down",
            home_plan=lambda n: "ok",
            fg_event="none",
            bg_at_s=3, fg_at_s=6,
            sample_delays_s=[16, 18],
            preseed_cache={"up": True, "relayOk": True},
        )
        # v7.9 — same no-event resume, but sample within ~3 s of foreground.
        # Pre-v7.9 the fast path is the 15 s self-healing tick, so a sample
        # at fg+3 still shows the stale green (final_red=False → FAIL). v7.9
        # adds a 1 s visibility-transition poll that detects the hidden→visible
        # flip without any DOM event firing → re-probe inside ~2 s → red.
        # This is the regression test for the IRL family report
        # "il faut attendre au moins 15 s pour voir le statut passer à rouge".
        r12 = run_resume_scenario(
            p,
            "v7.9-resume-no-event-fast-path-red-within-3s",
            relay_plan=lambda n: "up" if n == 1 else "down",
            home_plan=lambda n: "ok",
            fg_event="none",
            bg_at_s=3, fg_at_s=6,
            sample_delays_s=[3],
            preseed_cache={"up": True, "relayOk": True},
        )

    print("\n" + "=" * 72)
    print("VERDICT (real browser E2E on live PWA v7.0)")
    print("=" * 72)

    s1_ok = r1["green_at"] and r1["green_at"][0] <= 3 and not r1["red_at"] and not r1["warn_at"]
    print(
        f"[{'PASS' if s1_ok else 'FAIL'}] v7-cold-launch-server-up-fast | "
        f"green_at={r1['green_at']} (want green ≤T+3, no red, no warn) calls={r1['counters']}"
    )

    s2_ok = r2["red_at"] and r2["red_at"][0] <= 3 and not r2["green_at"] and not r2["warn_at"]
    print(
        f"[{'PASS' if s2_ok else 'FAIL'}] v7-cold-launch-server-off-fast | "
        f"red_at={r2['red_at']} green_at={r2['green_at']} (want red ≤T+3, no green, no warn) calls={r2['counters']}"
    )

    # Fallback path: green + warn. The relay-down warn must persist; the
    # home-up state should settle by T+10 (timeout 3 s × 2 + home call).
    s3_ok = r3["final_green"] and r3["final_warn"] and not r3["red_at"]
    print(
        f"[{'PASS' if s3_ok else 'FAIL'}] v7-relay-timeout-fallback-home-up | "
        f"green_at={r3['green_at']} warn_at={r3['warn_at']} red_at={r3['red_at']} "
        f"(want final green+warn, no red) calls={r3['counters']}"
    )

    # Full outage: red + warn.
    s4_ok = r4["final_red"] and r4["final_warn"] and not r4["final_green"]
    print(
        f"[{'PASS' if s4_ok else 'FAIL'}] v7-relay-timeout-fallback-home-down | "
        f"red_at={r4['red_at']} warn_at={r4['warn_at']} final_green={r4['final_green']} "
        f"(want final red+warn) calls={r4['counters']}"
    )

    # Cache paint should land at the first sample (T+0) before any fetch returns.
    s5_ok = r5["green_at"] and r5["green_at"][0] == 0 and not r5["red_at"]
    print(
        f"[{'PASS' if s5_ok else 'FAIL'}] v7-stale-cache-paint-then-refresh | "
        f"green_at={r5['green_at']} red_at={r5['red_at']} (want green at T=0) calls={r5['counters']}"
    )

    # Retry path: no red flash mid-transition, final green.
    s6_ok = r6["final_green"] and not r6["red_at"] and not r6["warn_at"]
    print(
        f"[{'PASS' if s6_ok else 'FAIL'}] v7-status-with-1-retry | "
        f"green_at={r6['green_at']} red_at={r6['red_at']} (want final green, no red) calls={r6['counters']}"
    )

    # A1 fix — degraded oracle, home up: green, NO warn, button stays enabled.
    s7_ok = r7["final_green"] and not r7["warn_at"] and not r7["red_at"] and not r7["final_wol_disabled"]
    print(
        f"[{'PASS' if s7_ok else 'FAIL'}] v7-relay-answered-degraded-server-up | "
        f"green_at={r7['green_at']} warn_at={r7['warn_at']} wol_disabled={r7['final_wol_disabled']} "
        f"(want green, no warn, WoL enabled) calls={r7['counters']}"
    )

    # A1 fix — degraded oracle, home down: red but NO warn, button stays
    # ENABLED (the IRL "red relay + WoL gone while it was fine" case).
    s8_ok = (
        r8["final_red"] and not r8["final_warn"] and not r8["warn_at"]
        and not r8["final_green"] and not r8["final_wol_disabled"]
    )
    print(
        f"[{'PASS' if s8_ok else 'FAIL'}] v7-relay-answered-degraded-server-down | "
        f"red_at={r8['red_at']} warn_at={r8['warn_at']} wol_disabled={r8['final_wol_disabled']} "
        f"(want red, no warn, WoL enabled) calls={r8['counters']}"
    )

    # A1/v7.6 — all-timeout blip on a green server must hold, never flash red.
    s9_ok = r9["final_green"] and not r9["red_at"] and not r9["warn_at"]
    print(
        f"[{'PASS' if s9_ok else 'FAIL'}] v7-all-timeout-while-green-holds-not-red | "
        f"green_at={r9['green_at']} red_at={r9['red_at']} warn_at={r9['warn_at']} "
        f"(want green held, no red, no warn) calls={r9['counters']}"
    )

    # v7.7 — focus-only resume must converge to red (not stay frozen green).
    s10_ok = r10["final_red"] and not r10["final_green"]
    print(
        f"[{'PASS' if s10_ok else 'FAIL'}] v7.7-resume-focus-only-converges-to-red | "
        f"red_at={r10['red_at']} green_at={r10['green_at']} final_green={r10['final_green']} "
        f"(want red after focus, NOT frozen green) calls={r10['counters']}"
    )

    # v7.7 — no-event resume must converge to red via the self-healing interval.
    s11_ok = r11["final_red"] and not r11["final_green"]
    print(
        f"[{'PASS' if s11_ok else 'FAIL'}] v7.7-resume-no-event-self-heals-to-red | "
        f"red_at={r11['red_at']} green_at={r11['green_at']} final_green={r11['final_green']} "
        f"(want red via self-healing tick, NOT frozen green) calls={r11['counters']}"
    )

    # v7.9 — no-event resume must converge to red WITHIN 3 s (fast-path poll).
    s12_ok = r12["final_red"] and not r12["final_green"] and r12["red_at"] and r12["red_at"][0] <= 3
    print(
        f"[{'PASS' if s12_ok else 'FAIL'}] v7.9-resume-no-event-fast-path-red-within-3s | "
        f"red_at={r12['red_at']} green_at={r12['green_at']} final_green={r12['final_green']} "
        f"(want red ≤ fg+3s via 1 s visibility poll) calls={r12['counters']}"
    )


if __name__ == "__main__":
    main()
