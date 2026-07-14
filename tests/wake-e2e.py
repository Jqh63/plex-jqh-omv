#!/usr/bin/env python3
"""Real-browser E2E for the WAKE paths — the mechanics `cold-radio-e2e.py` does NOT
cover, and where the 2026-07-14 bug lived.

`cold-radio-e2e.py` drives the status/probe state machine (green/red/orange,
fallback, resume). It never fires a wake, so the countdown, the wake state and the
retry POSTs were entirely untested in a browser. That is exactly why the bug shipped.

Everything here turns on one fact about mobile: **Android does not KILL a
backgrounded PWA, it FREEZES it.** Pending timers do not run — they queue, and fire
all at once on resume — and reopening RESUMES the page rather than reloading it, so
`startApp()` never re-runs and the wake state survives. Client-side flags therefore
outlive a freeze; only the wall clock tells the truth. Playwright's clock API models
both halves faithfully (`fast_forward` = the thaw, `set_system_time` = time passing
with nothing having run yet).

## What it pins

1. `stale-wake-does-not-survive-a-freeze` (v8.33) — THE reported bug. A wake tapped
   last night, page frozen, app reopened this morning: no phantom countdown, no
   power button stuck in "sent". Note it needs `set_system_time`, not
   `fast_forward`: the latter also fires the thawed poll timer, which reaps the wake
   on its own, and the test would pass even WITHOUT the fix — proving nothing.

2. `frozen-retry-does-not-thaw-into-a-phantom-wake` (v8.32) — a defensive guard. A
   retry thawed long after its wake must not POST: it would fire a magic packet
   nobody asked for and re-arm the relay's `waking` signal for every open PWA.
   Guarded by a CONTROL scenario — a SHORT thaw (inside the retry window) must still
   POST. Without it, "no phantom POST" could just mean "no timer pending", and the
   test would pass for the wrong reason.

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


# --------------------------------------------------------------------------
# 3. v8.33 — a stale wake must not survive a freeze and paint on resume
# --------------------------------------------------------------------------
def scenario_stale_wake_on_resume(p):
    """THE reported sequence (2026-07-14). Android FREEZES a backgrounded PWA — it
    does not kill it. Reopening RESUMES the page: startApp() never re-runs, so
    wolSent / wolStartTime / the countdown survive intact from last night's wake.
    The user reopens the app and is shown a boot countdown for a wake that ended
    hours ago, on a home that is off, with the power button locked in "sent".

    Note what this scenario proves is NOT a relay artefact: the relay serves plain
    `down` throughout (no `waking`), and zero /wol is POSTed. The phantom countdown
    is pure client-side state outliving a freeze."""
    print("\n## stale-wake-does-not-survive-a-freeze (v8.33)")
    counters = {"relay": 0, "home": 0, "wol": 0}
    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.route("**/*", _mk_handler(counters, lambda n: "down", lambda n: "fail"))
    page.clock.install()
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#powerBtn", state="attached", timeout=10000)
    page.wait_for_timeout(500)

    page.click("#powerBtn")             # last night's wake
    page.wait_for_timeout(500)
    mid = card(page)
    print(f"  tapped power → {mid['label']!r}")
    ok = check("a fresh wake shows the countdown", is_starting(mid))

    # The screen locks: the page is frozen mid-wake, then resumed the next morning.
    #
    # `set_system_time` jumps the wall clock WITHOUT running any pending timer —
    # which is precisely the instant the user experiences: the page is back on
    # screen, still painted with last night's state, and nothing has ticked yet.
    # Using fast_forward here instead would ALSO fire the thawed wolPollTimer,
    # whose WOL_TIMEOUT_MS check reaps the wake on its own — the assertions below
    # would then pass even WITHOUT the v8.33 fix, and prove nothing. Isolating the
    # resume is what makes this a real regression test: pre-fix, onForeground()
    # touched neither wolSent nor the countdown, so the phantom card survived here.
    # NB: clock.install() keeps the REAL epoch, so the jump must be computed from
    # the page's own Date.now() — passing a bare "24 h in ms" would set the clock to
    # 1970+1d, i.e. 54 years BACKWARDS, making the wake's age negative and silently
    # disarming the very guard under test.
    page.clock.set_system_time(page.evaluate("() => Date.now() + 24*3600*1000"))
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(1500)
    resumed = card(page)
    pwr = page.evaluate("() => document.getElementById('powerBtn').className")
    print(f"  reopened 24 h later → {resumed['label']!r} power={pwr!r}")

    ok &= check("NO phantom countdown on reopen (the bug)",
                not is_starting(resumed), f"card={resumed['label']!r}")
    ok &= check("the power button is usable again (not stuck in 'sent')",
                "sent" not in pwr, f"class={pwr!r}")
    ok &= check("still no /wol POSTed by any of this",
                counters["wol"] == 1, f"wol POSTs={counters['wol']} (the tap only)")
    b.close()
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
        ok = scenario_phantom_retry(p)
        ok &= scenario_stale_wake_on_resume(p)

    print("\n" + "=" * 72)
    print("ALL PASS" if ok else "FAILURES — see above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
