#!/usr/bin/env python3
"""Real-browser E2E for the WAKE paths (v8.31 + v8.32) — the mechanics the
2026-07-14 fixes touched, and which `cold-radio-e2e.py` does NOT cover.

`cold-radio-e2e.py` drives the status/probe state machine (green/red/orange,
fallback, resume). It never fires a wake, so the countdown, the adoption of a
remote wake, and the retry POSTs were entirely untested in a browser. Both
2026-07-14 bugs lived exactly there. This file closes that gap.

## What it pins

1. `remote-wake-survives-relay-miss` (v8.31) — a wake fired by ANOTHER device is
   adopted from the relay's `waking` flag. Mid-boot the relay probe fails and the
   PWA falls back to its direct-home probe, whose verdict CANNOT carry `waking`.
   The countdown must hold ("Démarrage…"), never flash red, and settle green when
   the home answers. Pre-fix this committed red ~10 s in on a booting home.

2. `frozen-retry-does-not-thaw-into-a-phantom-wake` (v8.32) — Android freezes a
   backgrounded PWA: pending setTimeouts queue and ALL fire at once on resume.
   Playwright's clock API reproduces that exactly (`fast_forward` runs pending
   timers with Date.now() jumped). A wake tapped, then thawed ~24 h later, must
   POST nothing. Pre-fix the four retries fired on thaw, re-arming the relay's
   `waking` signal and painting a phantom countdown on EVERY open PWA.

   Guarded by a CONTROL scenario: a SHORT thaw (inside the retry window) must
   still POST. Without it, "no phantom POST" could just mean "no timer pending"
   and the test would pass for the wrong reason.

Runs against the LIVE deploy by default (post-merge gate), like cold-radio-e2e:
  python3 tests/wake-e2e.py
  PWA_BASE="file:///config/workspace/plex-jqh-omv/index.html" python3 tests/wake-e2e.py
"""

import os
import sys
from urllib.parse import urlparse

from playwright.sync_api import Route, sync_playwright

RELAY_HOST = "relay.example.test"
CONFIG_HOST = "home.example.test"
PWA_BASE = os.environ.get("PWA_BASE", "https://jqh63.github.io/plex-jqh-omv/")
PWA_URL = (
    f"{PWA_BASE}"
    f"?host={CONFIG_HOST}&mac=AABBCCDDEEFF"
    f"&relay=https://{RELAY_HOST}&token=x&apps=seerr,plexweb"
)
ENGINE = os.environ.get("PWA_ENGINES", "chromium").split(",")[0].strip()

JSON_H = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}


def _status_body(verdict):
    """`waking:N` = the relay reports a wake in progress, N seconds old. It is only
    ever served alongside up=false — a booting home is a down home."""
    if verdict == "up":
        return '{"up": true, "stale": false, "age_s": 0, "eta_s": 80}'
    if verdict.startswith("waking:"):
        age = int(verdict.split(":", 1)[1])
        return ('{"up": false, "stale": false, "age_s": null, '
                f'"waking": true, "wake_age_s": {age}, "eta_s": 80}}')
    return '{"up": false, "stale": false, "age_s": null, "eta_s": 80}'


def _mk_handler(counters, relay_plan, home_plan):
    def handle(route: Route):
        parsed = urlparse(route.request.url)
        host, path = parsed.netloc, parsed.path
        if host == RELAY_HOST and path == "/wol":
            counters["wol"] += 1
            route.fulfill(status=200, headers=JSON_H, body='{"sent": true}')
            return
        if host == RELAY_HOST and path == "/status":
            counters["relay"] += 1
            v = relay_plan(counters["relay"])
            if v == "fail":
                route.abort()
                return
            route.fulfill(status=200, headers=JSON_H, body=_status_body(v))
            return
        if host == CONFIG_HOST or host.endswith("." + CONFIG_HOST):
            counters["home"] += 1
            if home_plan(counters["home"]) == "ok":
                route.fulfill(status=200, body="")
            else:
                route.abort()
            return
        route.continue_()

    return handle


def card(page):
    return page.evaluate(
        """() => ({
        label: document.getElementById('statusLabel').innerText,
        dot: document.getElementById('statusDot').className,
        card: document.getElementById('statusCard').className,
    })"""
    )


def is_red(s):
    return "offline" in s["dot"] or "offline" in s["card"]


def is_green(s):
    return "online" in s["dot"] and "online" in s["card"]


def is_starting(s):
    # The wake card — setStarting() paints "Démarrage…" with the checking dot.
    return "marrage" in s["label"]


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return cond


# --------------------------------------------------------------------------
# 1. v8.31 — an adopted remote wake survives a probe that cannot see it
# --------------------------------------------------------------------------
def scenario_remote_wake(p):
    print("\n## remote-wake-survives-relay-miss (v8.31)")
    counters = {"relay": 0, "home": 0, "wol": 0}

    # The wake was fired elsewhere (the AM5's logon task). Mid-boot the relay probe
    # dies twice (cold radio) — the direct-home fallback answers "down" with no
    # waking flag — then the relay is heard from again and the home finishes booting.
    def relay_plan(n):
        return {1: "waking:18", 2: "fail", 3: "fail", 4: "waking:32"}.get(n, "up")

    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.route("**/*", _mk_handler(counters, relay_plan, lambda n: "fail"))
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#statusLabel", state="attached", timeout=10000)

    samples = []
    for t in (1, 3, 6, 10, 14, 20, 30):
        page.wait_for_timeout(1000 if not samples else (t - samples[-1][0]) * 1000)
        s = card(page)
        samples.append((t, s))
        tags = [f for f, on in (("RED", is_red(s)), ("green", is_green(s)),
                                ("starting", is_starting(s))) if on]
        print(f"  T+{t}s: {s['label']!r} -> {','.join(tags) or '(neutral)'}")

    b.close()
    reds = [t for t, s in samples if is_red(s)]
    starts = [t for t, s in samples if is_starting(s)]
    final_green = is_green(samples[-1][1])

    ok = True
    ok &= check("the boot countdown is shown (wake adopted from the relay)",
                bool(starts), f"'Démarrage…' at {starts}")
    ok &= check("NEVER flashes red while the home is booting",
                not reds, f"red at {reds}" if reds else "no red at any sample")
    ok &= check("settles green once the home answers", final_green,
                f"final={samples[-1][1]['label']!r}")
    ok &= check("fired no WoL of its own (adoption must not POST)",
                counters["wol"] == 0, f"wol POSTs={counters['wol']}")
    return ok


# --------------------------------------------------------------------------
# 2. v8.32 — a frozen retry must not thaw into a phantom wake
# --------------------------------------------------------------------------
def _tap_and_thaw(p, thaw, label):
    """Tap power, then jump the clock by `thaw` (Playwright's clock runs every
    pending timer at once on a jump — the Android freeze/thaw semantics).
    Returns the number of POST /wol before and after the jump."""
    counters = {"relay": 0, "home": 0, "wol": 0}
    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.route("**/*", _mk_handler(counters, lambda n: "down", lambda n: "fail"))
    # Fake timers must be installed before the app schedules anything.
    page.clock.install()
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#powerBtn", state="attached", timeout=10000)
    page.wait_for_timeout(500)          # let the first probe settle (real time)

    page.click("#powerBtn")
    page.wait_for_timeout(500)          # the initial POST goes out immediately
    before = counters["wol"]

    page.clock.fast_forward(thaw)       # ← the thaw: every pending retry fires now
    page.wait_for_timeout(1500)         # let any POST actually reach the route
    after = counters["wol"]
    b.close()
    print(f"  [{label}] POSTs before thaw={before}, after thaw={after} "
          f"(thaw={thaw})")
    return before, after


def scenario_phantom_retry(p):
    print("\n## frozen-retry-does-not-thaw-into-a-phantom-wake (v8.32)")
    ok = True

    # CONTROL — a SHORT freeze (resumed inside the retry window) MUST still retry.
    # This proves the timers are genuinely pending and that this harness would
    # catch a phantom POST. Without it, the assertion below could pass simply
    # because nothing was scheduled.
    before, after = _tap_and_thaw(p, "00:00:20", "control: 20 s freeze")
    ok &= check("CONTROL — a short freeze still fires its retry (UDP-loss cover intact)",
                after > before, f"{after - before} retry POST(s) on thaw")

    # THE BUG — the page thaws ~24 h later (tapped last night, reopened this
    # morning). Pre-fix: the four pending retries all fired here, re-arming the
    # relay's `waking` signal → phantom countdown on every open PWA.
    before, after = _tap_and_thaw(p, "24:00:00", "bug: 24 h freeze")
    ok &= check("a 24 h thaw fires NO phantom WoL POST",
                after == before, f"{after - before} POST(s) after thaw "
                                 f"(pre-fix: 4)")
    return ok


def main():
    print("=" * 72)
    print(f"WAKE-path E2E (v8.31 + v8.32) — engine={ENGINE} base={PWA_BASE}")
    print("=" * 72)
    with sync_playwright() as p:
        try:
            getattr(p, ENGINE).launch().close()
        except Exception as e:
            print(f"[SKIP] engine={ENGINE}: cannot launch — {str(e)[:90]}")
            print("       → ssh omv-deploy setup-codeserver-browser")
            return 0
        ok = scenario_remote_wake(p)
        ok &= scenario_phantom_retry(p)

    print("\n" + "=" * 72)
    print("ALL PASS" if ok else "FAILURES — see above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
