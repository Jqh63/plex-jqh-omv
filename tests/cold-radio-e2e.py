"""
Real-browser E2E validation of plex-jqh-omv v4.3 cold-radio resume fix.

Drives the LIVE PWA at https://jqh63.github.io/plex-jqh-omv/ with
Playwright + Chromium headless. Routes intercepted by parsed URL host
(not substring — the navigation URL contains the test host as a query
param) to simulate three scenarios; paint events captured via DOM
polling; verdict printed.

Scenarios:
  1. resume cold-radio fail-then-OK (the user's bug)
       expect: NO red flash, NO warn flash, final green
  2. resume server really down
       expect: red appears honestly (~35 s after resume)
  3. resume relay really down
       expect: warn appears honestly (~32 s after resume)
"""

from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Route

CONFIG_HOST = "test.example.com"
RELAY_HOST = "r.example.com"
PWA_URL = (
    f"https://jqh63.github.io/plex-jqh-omv/"
    f"?host={CONFIG_HOST}&mac=AABBCCDDEEFF"
    f"&relay=https://{RELAY_HOST}&token=x&apps=seerr,plexweb"
)


def capture_state(page):
    return page.evaluate(
        """() => ({
        statusLabel: document.getElementById('statusLabel').innerText,
        dotClass: document.getElementById('statusDot').className,
        cardClass: document.getElementById('statusCard').className,
        fallbackClass: document.getElementById('fallbackLink') ? document.getElementById('fallbackLink').className : '',
        fallbackText: document.getElementById('fallbackLinkA') ? document.getElementById('fallbackLinkA').innerText : '',
    })"""
    )


def is_red(s):
    return "offline" in s["dotClass"] or "offline" in s["cardClass"]


def is_warn(s):
    return "warn" in s["fallbackClass"]


def is_green(s):
    return "online" in s["dotClass"] and "online" in s["cardClass"]


def simulate_visibility(page, hidden: bool):
    page.evaluate(
        f"""() => {{
        Object.defineProperty(document, 'visibilityState', {{value: '{"hidden" if hidden else "visible"}', configurable: true}});
        Object.defineProperty(document, 'hidden', {{value: {str(hidden).lower()}, configurable: true}});
        document.dispatchEvent(new Event('visibilitychange'));
    }}"""
    )


def run_scenario(p, name, route_plan, sample_delays_s, simulate_resume=True, preseed_cache=None):
    """If simulate_resume=True (default), the scenario does background→fg
    after the initial check (legacy resume behavior). If False, samples
    happen on the first cold-launch cycle without a visibility toggle —
    exercises the v4.5 status streak against the route_plan's first call
    indices (n=1 = initial status/probe fired by startApp()).

    preseed_cache (v5.0): if set, injects a localStorage entry under
    the v5.0 state cache key BEFORE page navigation, so startApp() loads
    it via loadCachedState() and paints the cached state immediately.
    Pass {'isOnline': True/False, 'relayReachable': True/False}."""
    print(f"\n## Scenario: {name}")
    counters = {"status": 0, "probe": 0}

    def handle(route: Route):
        url = route.request.url
        parsed = urlparse(url)
        host = parsed.netloc

        # Relay /health probe
        if host == RELAY_HOST and parsed.path == "/health":
            counters["probe"] += 1
            verdict = route_plan("probe", counters["probe"])
            if verdict == "ok":
                route.fulfill(
                    status=200,
                    headers={
                        "Content-Type": "application/json",
                        "Access-Control-Allow-Origin": "*",
                    },
                    body='{"status":"ok"}',
                )
            else:
                route.abort()
            return

        # Status probe (any subdomain of CONFIG_HOST OR the bare host itself)
        if host == CONFIG_HOST or host.endswith("." + CONFIG_HOST):
            counters["status"] += 1
            verdict = route_plan("status", counters["status"])
            if verdict == "ok":
                route.fulfill(status=200, body="")
            else:
                route.abort()
            return

        route.continue_()

    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    if preseed_cache is not None:
        # add_init_script runs before any page script — including app.js's
        # var initialization — so localStorage already has our entry when
        # loadCachedState() runs in startApp().
        import json
        payload = json.dumps({
            "isOnline": bool(preseed_cache.get("isOnline")),
            "relayReachable": bool(preseed_cache.get("relayReachable", True)),
            # Recent timestamp so the TTL check (5 min) passes.
            "savedAt": None,  # filled at runtime by Date.now()
        })
        ctx.add_init_script(
            f"try{{var p={payload};p.savedAt=Date.now();"
            f"localStorage.setItem('plex-jqh-omv-state',JSON.stringify(p));}}"
            f"catch(e){{}}"
        )
    page = ctx.new_page()
    page.route("**/*", handle)
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#statusLabel", state="attached", timeout=10000)
    # Let initial check complete + paint settle
    page.wait_for_timeout(2500)
    initial = capture_state(page)
    print(
        f"  T-resume: status={initial['statusLabel']!r} dot={initial['dotClass']!r} green={is_green(initial)}"
    )

    if simulate_resume:
        simulate_visibility(page, hidden=True)
        page.wait_for_timeout(300)
        simulate_visibility(page, hidden=False)

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
    final_green = is_green(samples[-1][1])
    b.close()
    return {
        "name": name,
        "red_at": red_at,
        "warn_at": warn_at,
        "green_at": green_at,
        "final_green": final_green,
        "counters": dict(counters),
    }


def main():
    with sync_playwright() as p:
        r1 = run_scenario(
            p,
            "resume cold-radio fail-then-OK (the v4.3 fix)",
            # initial status & probe call (n=1) ok, post-resume call (n=2) fails,
            # retry (n=3) succeeds, then everything ok.
            lambda kind, n: "fail" if n == 2 else "ok",
            sample_delays_s=[1, 3, 6, 11, 14],
        )
        r2 = run_scenario(
            p,
            "resume - server really down",
            # status n=1 ok (initial), then all status fail; probe always ok
            lambda kind, n: "fail" if (kind == "status" and n >= 2) else "ok",
            sample_delays_s=[3, 6, 11, 14, 36, 40],
        )
        r3 = run_scenario(
            p,
            "resume - relay really down",
            # Probe n=1 ok (initial), then all probe fail. v4.4 needs 2
            # consecutive post-window fails to flip — warn lands at ~T+62.
            lambda kind, n: "fail" if (kind == "probe" and n >= 2) else "ok",
            sample_delays_s=[3, 6, 14, 36, 65, 70],
        )
        r4 = run_scenario(
            p,
            "v4.4 bug: server down + cold-radio probe (user report 2026-05-25)",
            # Status fails from n=1 (server down from the start). Probe n=1
            # (window-deferred) and n=2 (post-window) both fail to simulate
            # cold-radio noise across the window+retry boundary, then n=3
            # succeeds (radio warm). MUST NOT produce a relay-down paint.
            lambda kind, n: (
                "fail" if kind == "status" else
                ("fail" if (kind == "probe" and n <= 2) else "ok")
            ),
            sample_delays_s=[3, 6, 14, 20, 36, 40],
        )
        r5 = run_scenario(
            p,
            "v4.5 bug: cold-launch server-up + cold-radio status (user report 2026-05-25)",
            # Status n=1 (initial), n=2 (in-window retry), n=3 (post-window
            # retry) all fail — cold radio leaks past window + retry. Status
            # n=4 (T+30 tick) succeeds (radio warm). Probe always OK.
            # v4.4 paints RED at T+10 (post-window n=3 fail flips immediately).
            # v4.5 streak defers n=3 (streak=1), recovers at T+30 → green
            # at ~T+30 directly. Sample at T+35 confirms green without RED.
            lambda kind, n: "fail" if (kind == "status" and n <= 3) else "ok",
            sample_delays_s=[3, 6, 14, 20, 35, 40],
            simulate_resume=False,
        )
        r6 = run_scenario(
            p,
            "v6.0: cold-launch server-up converges to green in ~3 s",
            # No cache (v6.0 dropped it). Server up + probe up. Initial
            # paint is orange "Vérification..." but both probe and status
            # succeed fast → green by T+3. Confirms the orange flash is
            # brief enough to be acceptable as the cache replacement.
            lambda kind, n: "ok",
            sample_delays_s=[1, 3, 6, 14, 36],
            simulate_resume=False,
        )
        r7 = run_scenario(
            p,
            "v6.0 user scenario 1: stale cache ignored, server off → red in ~3 s",
            # Preseed the LEGACY cache key (v6.0 ignores it). Server is off
            # (all status fail), relay up (probe ok). v6.0 path: probe ok
            # closes resume window + radioWarm=true → status fail at T+2
            # bypasses streak → setOffline IMMEDIATELY. User sees orange
            # for ~2 s then red, never the misleading green from cache.
            # Compare with the old behavior: green from cache at T+1, then
            # ~14 s of stale paint before flipping to red.
            lambda kind, n: "ok" if kind == "probe" else "fail",
            sample_delays_s=[1, 3, 6, 14, 25],
            simulate_resume=False,
            preseed_cache={"isOnline": True, "relayReachable": True},
        )
        r8 = run_scenario(
            p,
            "v6.0 user scenario 2: no cache, server off → red in ~3 s",
            # Cache expired (no preseed). Server off, relay up. Same fast
            # convergence path as r7 — the user's "30 min later re-open"
            # case (cache TTL expired).
            lambda kind, n: "ok" if kind == "probe" else "fail",
            sample_delays_s=[1, 3, 6, 14, 25],
            simulate_resume=False,
        )

    print("\n" + "=" * 72)
    print("VERDICT (real browser E2E on live PWA v6.0)")
    print("=" * 72)
    s1_ok = not r1["red_at"] and not r1["warn_at"] and r1["final_green"]
    print(
        f"[{'PASS' if s1_ok else 'FAIL'}] cold-radio fail-then-OK | "
        f"red_at={r1['red_at']} warn_at={r1['warn_at']} final_green={r1['final_green']} "
        f"calls={r1['counters']}"
    )
    s2_ok = bool(r2["red_at"]) and not r2["final_green"]
    print(
        f"[{'PASS' if s2_ok else 'FAIL'}] server really down    | "
        f"red_at={r2['red_at']} final_green={r2['final_green']} (want red, not green) "
        f"calls={r2['counters']}"
    )
    s3_ok = bool(r3["warn_at"]) and r3["final_green"]
    print(
        f"[{'PASS' if s3_ok else 'FAIL'}] relay really down     | "
        f"warn_at={r3['warn_at']} final_green={r3['final_green']} (want warn, still green) "
        f"calls={r3['counters']}"
    )
    # v4.4 fix: server-down + cold-radio probe noise must NOT produce warn
    # paint. Server is correctly red, but the relay banner must stay clean.
    s4_ok = bool(r4["red_at"]) and not r4["warn_at"] and not r4["final_green"]
    print(
        f"[{'PASS' if s4_ok else 'FAIL'}] v4.4 server-down + cold probe | "
        f"red_at={r4['red_at']} warn_at={r4['warn_at']} final_green={r4['final_green']} "
        f"(want red, no warn, not green) calls={r4['counters']}"
    )
    # v4.5 fix relaxed for v6.0: cold-launch with server actually UP +
    # cold-radio status noise. v6.0 explicitly trades a brief red flash
    # here (probe ok → radioWarm bypass on first status fail) for fast
    # convergence on the realistic server-off user scenarios. The
    # original v4.5 bug was 'serveur éteint' for ~1 min — v6.0 caps it
    # to recovery by T+25 once the regular tick re-checks with the (by
    # now) warm radio. So we keep the assertion light: final green, no
    # warn, and any red must clear by T+25.
    red_lingers = bool(r5["red_at"]) and r5["red_at"][-1] > 25
    s5_ok = not red_lingers and not r5["warn_at"] and r5["final_green"]
    print(
        f"[{'PASS' if s5_ok else 'FAIL'}] v6.0 cold-launch server-up + cold status | "
        f"red_at={r5['red_at']} warn_at={r5['warn_at']} final_green={r5['final_green']} "
        f"(want final green, no warn, red cleared by T+25) calls={r5['counters']}"
    )
    # v6.0: cold-launch server-up converges to green fast (no cache).
    # The orange "Vérification..." may show at T+1 but green MUST land
    # by T+3 (probe + status both succeed within a second).
    s6_ok = not r6["red_at"] and not r6["warn_at"] and r6["green_at"] and r6["green_at"][0] <= 3
    print(
        f"[{'PASS' if s6_ok else 'FAIL'}] v6.0 cold-launch server-up | "
        f"red_at={r6['red_at']} green_at={r6['green_at']} final_green={r6['final_green']} "
        f"(want green at T<=3, no red, no warn) calls={r6['counters']}"
    )
    # v6.0 user scenario 1: stale cache MUST be ignored. Server is off,
    # relay up — convergence to red at T~3 via probe-success bypass.
    # NEVER green (cache mustn't paint a stale online). RED by T+3.
    s7_ok = not r7["green_at"] and bool(r7["red_at"]) and r7["red_at"][0] <= 3 and not r7["final_green"]
    print(
        f"[{'PASS' if s7_ok else 'FAIL'}] v6.0 user1 stale-cache ignored, server off | "
        f"green_at={r7['green_at']} red_at={r7['red_at']} final_green={r7['final_green']} "
        f"(want NO green, red at T<=3) calls={r7['counters']}"
    )
    # v6.0 user scenario 2: no cache, server off — same fast convergence
    # to red via probe-success bypass. RED by T+3.
    s8_ok = not r8["green_at"] and bool(r8["red_at"]) and r8["red_at"][0] <= 3 and not r8["final_green"]
    print(
        f"[{'PASS' if s8_ok else 'FAIL'}] v6.0 user2 no-cache, server off | "
        f"green_at={r8['green_at']} red_at={r8['red_at']} final_green={r8['final_green']} "
        f"(want NO green, red at T<=3) calls={r8['counters']}"
    )


if __name__ == "__main__":
    main()
