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


def run_scenario(p, name, route_plan, sample_delays_s):
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
    final_green = is_green(samples[-1][1])
    b.close()
    return {
        "name": name,
        "red_at": red_at,
        "warn_at": warn_at,
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
            lambda kind, n: "fail" if (kind == "probe" and n >= 2) else "ok",
            sample_delays_s=[3, 6, 11, 14, 36, 40],
        )

    print("\n" + "=" * 72)
    print("VERDICT (real browser E2E on live PWA v4.3)")
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


if __name__ == "__main__":
    main()
