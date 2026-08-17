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

1. `stale-wake-does-not-survive-a-freeze` (v8.33) and its REMOTE twin (v8.43) —
   THE reported bug, in both flavours: a wake this device tapped, and a wake it
   merely ADOPTED from the relay (the AM5's logon task POSTs /wol on purpose so every
   PWA shows the countdown). The user watches that countdown, pockets the phone
   mid-boot, and finds it still ticking the next morning. The remote flavour is the
   one hit in practice, and `wolSent` does not catch it — the phone never tapped.

   Two traps, both of which produced a green-but-worthless test on the first pass:
   - assert on the COUNTDOWN (`powerProgress`), not the status card: the card is
     repainted to "Vérification…" within ~200 ms while the countdown keeps ticking
     underneath for seconds — that is what the user actually sees;
   - jump time with `set_system_time`, not `fast_forward`: the latter also fires the
     thawed poll timer, which reaps the wake on its own, so the test would pass even
     WITHOUT the fix.

2. `stale-REMOTE-wake-does-not-survive-a-freeze` (v8.45) — the SAME bug via the wake
   the phone merely ADOPTED from the relay (the AM5's logon task POSTs /wol on purpose
   so every PWA shows the countdown). This is the variant actually hit in practice, and
   `wolSent` does not catch it — the phone never tapped. Assert on the COUNTDOWN
   (`powerProgress`), not the status card: the card is repainted to "Vérification…"
   within ~200 ms while the countdown keeps ticking underneath for seconds. That very
   mistake made a first pass of this test report the bug as "self-corrects in 200 ms".

Runs against the LIVE deploy by default (post-merge gate), like cold-radio-e2e:
  python3 tests/wake-e2e.py
  python3 tests/wake-e2e.py   (défaut = working tree)
"""

import os
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import Route, sync_playwright

RELAY_HOST = "relay.example.test"
CONFIG_HOST = "home.example.test"
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
ENGINE = os.environ.get("PWA_ENGINES", "chromium").split(",")[0].strip()

JSON_H = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}


# v8.73 — the PWA now refuses any /status body it cannot prove the relay built
# JUST NOW (`served_at`, see isLiveBody in app.js). These scenarios time-travel
# the PAGE clock while the fixtures stamp from the HOST clock, so a +24 h jump
# would make every subsequent body look replayed and this suite would test the
# rejection path by accident. The skew is threaded explicitly instead: when the
# page's wall clock moves, the relay's does too — that is what "wall clock"
# means, and the fixture has to model it rather than sit still.
_clock_skew_s = [0]


def _reset_clock_skew():
    """Per-scenario, NOT cumulative. Found by the bench: two scenarios each add
    +24 h and the global carried the total into every scenario that ran after
    them, so their fixtures were stamped a day or two in the future and the
    liveness gate refused every body — a whole suite failing for a defect that
    lived in the harness. Each scenario starts on a fresh page, so it must start
    on a fresh clock too."""
    _clock_skew_s[0] = 0


def _served_at():
    return int(time.time()) + _clock_skew_s[0]


def _status_body(verdict):
    """`waking:N` = the relay reports a wake in progress, N seconds old. It is only
    ever served alongside up=false — a booting home is a down home."""
    if verdict == "up":
        return f'{{"up": true, "stale": false, "age_s": 0, "eta_s": 80, "served_at": {_served_at()}}}'
    # `up-degraded` = the home answers HTTP but the probed app (Seerr) is still
    # 5xx-ing. The relay serves this for the ~20-30 s between the host booting
    # and the apps being ready; it is up=true, so every "is it up" branch says
    # yes while the thing the user is about to tap is not there yet.
    if verdict == "up-degraded":
        return f'{{"up": true, "stale": false, "age_s": 0, "eta_s": 80, "degraded": true, "served_at": {_served_at()}}}'
    if verdict.startswith("waking:"):
        age = int(verdict.split(":", 1)[1])
        return ('{"up": false, "stale": false, "age_s": null, '
                f'"waking": true, "wake_age_s": {age}, "eta_s": 80, '
                f'"served_at": {_served_at()}}}')
    return f'{{"up": false, "stale": false, "age_s": null, "eta_s": 80, "served_at": {_served_at()}}}'


def _mk_handler(counters, relay_plan, home_plan, wol_status=200):
    def handle(route: Route):
        parsed = urlparse(route.request.url)
        host, path = parsed.netloc, parsed.path
        if host == RELAY_HOST and path == "/wol":
            counters["wol"] += 1
            if wol_status != 200:
                route.fulfill(status=wol_status, headers=JSON_H,
                              body='{"detail": "refused"}')
                return
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
    # NB: `powerLabel` / `powerProgress` are what carry the COUNTDOWN ("Réveil…
    # environ 62s" + the progress bar). Asserting on the status card alone hides the
    # bug: setRechecking() repaints the card to "Vérification…" while the countdown
    # keeps right on ticking underneath. That mistake made a first pass of this test
    # report a stale wake as "corrected in 200 ms" when it was in fact still running.
    return page.evaluate(
        """() => ({
        label: document.getElementById('statusLabel').innerText,
        sub: document.getElementById('statusSub').innerText,
        dot: document.getElementById('statusDot').className,
        card: document.getElementById('statusCard').className,
        power: document.getElementById('powerLabel').innerText,
        progress: document.getElementById('powerProgress').className,
        fallback: document.getElementById('fallbackLink').className,
        fallbackText: document.getElementById('fallbackLinkA').innerText,
    })"""
    )


def is_counting_down(s):
    """The user-visible countdown: the progress bar is active. This — not the status
    card — is what "un compteur à 62 s" means."""
    return "active" in s["progress"]


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
    _reset_clock_skew()
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
    _clock_skew_s[0] += 24 * 3600
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


def scenario_stale_remote_wake_on_resume(p):
    """The AM5 variant of the stale-wake bug — the one the user actually hits.

    The previous morning's wake was fired by the AM5's logon task, NOT from the
    phone. That task POSTs /wol to the relay ON PURPOSE (runbook wol-am5-windows-task:
    "relais GCP d'abord → statut « wake en cours » + countdown partagés dans toutes
    les PWA"). So the phone's PWA, sitting in the background, ADOPTS the wake from
    the relay's `waking` flag: remoteWaking = true, "Démarrage…" painted, countdown
    running. Then Android freezes the page with that state.

    Next morning the user reopens the app and is shown yesterday's countdown. Note
    `wolSent` is FALSE throughout — the phone never tapped anything — so the v8.33
    reap (which keys on wolSent) does NOT catch this one. That is the hole.
    """
    _reset_clock_skew()
    print("\n## stale-REMOTE-wake-does-not-survive-a-freeze (v8.34 — the AM5 variant)")
    counters = {"relay": 0, "home": 0, "wol": 0}
    state = {"waking": True}   # yesterday: the AM5's wake is in progress

    def relay_plan(n):
        return "waking:18" if state["waking"] else "down"

    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.route("**/*", _mk_handler(counters, relay_plan, lambda n: "fail"))
    page.clock.install()
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#statusLabel", state="attached", timeout=10000)
    page.wait_for_timeout(800)

    adopted = card(page)
    print(f"  AM5 wake adopted → card={adopted['label']!r} countdown={adopted['power']!r}")
    ok = check("the PWA adopts the AM5 wake (countdown running, no tap)",
               is_counting_down(adopted) and counters["wol"] == 0,
               f"countdown={adopted['power']!r} wol POSTs={counters['wol']}")

    # Overnight. The relay's waking signal expired long ago (TTL 150 s); the home
    # has since shut down. The page was frozen the whole time and is now reopened.
    state["waking"] = False
    page.clock.set_system_time(page.evaluate("() => Date.now() + 24*3600*1000"))
    _clock_skew_s[0] += 24 * 3600
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(1500)

    resumed = card(page)
    print(f"  reopened 24 h later → card={resumed['label']!r} countdown={resumed['power']!r} "
          f"bar={resumed['progress']!r}")
    # Assert on the COUNTDOWN, not the card: the card gets repainted to
    # "Vérification…" within ~200 ms while the countdown keeps ticking underneath
    # for seconds. The countdown is what the user sees and reports.
    ok &= check("NO phantom countdown still running from yesterday's AM5 wake",
                not is_counting_down(resumed),
                f"progress bar={resumed['progress']!r}")
    ok &= check("the PWA never POSTed a WoL of its own",
                counters["wol"] == 0, f"wol POSTs={counters['wol']}")
    b.close()
    return ok


def scenario_remote_wake_outlives_the_waking_signal(p):
    """v8.53 — an ADOPTED wake whose boot outlasts the relay's WAKE_SIGNAL_TTL_S.

    The relay only advertises `waking` for WAKE_SIGNAL_TTL_S (150 s). A boot that
    runs longer (cold J5005, fsck, a wake that never lands) therefore stops being
    advertised WHILE the PWA still has remoteWaking = true and a countdown ticking.
    The next "down" verdict then lands in setRechecking(), whose early-return only
    covered `wolSent` — so on an ADOPTED wake it fell through and painted
    "Vérification…" over the card while the power label kept counting down.

    Two widgets telling the user two different stories at the same instant. This
    is the behaviour tests/README.md described as a trap to write tests AROUND
    ("the card is repainted to Vérification… in ~200 ms while the countdown keeps
    ticking") — it was the defect itself, not a fixture quirk. THIS scenario
    asserts on the card on purpose: it is the widget that was lying.

    Distinct from the stale-wake reap above: nothing is frozen and nothing is
    stale here — the wake is live, in-window, and legitimately still running.
    """
    _reset_clock_skew()
    print("\n## remote-wake-outlives-the-relay-waking-signal (v8.53)")
    counters = {"relay": 0, "home": 0, "wol": 0}
    state = {"waking": True}

    def relay_plan(n):
        return "waking:18" if state["waking"] else "down"

    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.route("**/*", _mk_handler(counters, relay_plan, lambda n: "fail"))
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#statusLabel", state="attached", timeout=10000)
    page.wait_for_timeout(800)

    adopted = card(page)
    print(f"  wake adopted → card={adopted['label']!r} countdown={adopted['power']!r}")
    ok = check("the PWA adopts the wake (countdown running, no tap)",
               is_counting_down(adopted) and counters["wol"] == 0,
               f"countdown={adopted['power']!r} wol POSTs={counters['wol']}")

    # The relay's waking signal expires mid-boot. The home is still down, the
    # countdown is still legitimately running on this device. No freeze, no
    # resume — just the next few 8 s polls landing on a bare "down".
    state["waking"] = False

    # SAMPLE, don't snapshot. The contradiction window is only DOWN_RECHECK_MS
    # (2.5 s) wide: the first bare "down" paints it, and the confirming re-probe
    # commits red — which stops the countdown — right after. A single wait_for_
    # timeout lands past it and the test passes against the bug (verified: a 12 s
    # snapshot reported card='Hors ligne', countdown stopped, all green on the
    # unfixed app.js). Poll across the whole window instead and fail on ANY
    # instant where the two widgets disagree.
    contradictions = []
    for _ in range(40):
        page.wait_for_timeout(300)
        s = card(page)
        if is_counting_down(s) and "rification" in s["label"]:
            contradictions.append(s)

    print(f"  waking signal expired mid-boot → {len(contradictions)} sample(s) with "
          f"a 'Vérification…' card over a running countdown")
    ok &= check("the card never contradicts a countdown that is still running",
                not contradictions,
                f"e.g. card={contradictions[0]['label']!r} "
                f"countdown={contradictions[0]['power']!r}" if contradictions else "")
    b.close()
    return ok


def scenario_failed_wake_promotes_the_manual_page(p):
    """v8.53 — a REFUSED wake must lead to the manual-wake page.

    fallback.html is a real family procedure (parameters in copy-to-clipboard
    fields, then a free WoL app per OS with numbered steps) — not admin-only
    documentation. But it was only ever promoted on `relayReachable === false`,
    the one case where the phone reached nobody. A wake refused by a reachable
    relay (401/403 config, 502 target resolution, 429) left the link at 11 px
    and 55 % opacity under the button, while the toast that said "réveil manuel
    ↓" faded after 5 s.

    Uses 401 because it settles in one round-trip; the timeout path (5 min) and
    the transport path set the same flag.
    """
    _reset_clock_skew()
    print("\n## failed-wake-promotes-the-manual-wake-page (v8.53)")
    counters = {"relay": 0, "home": 0, "wol": 0}

    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.route("**/*", _mk_handler(counters, lambda n: "down", lambda n: "fail",
                                   wol_status=401))
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#statusLabel", state="attached", timeout=10000)
    page.wait_for_timeout(3000)

    before = card(page)
    ok = check("the link is discreet while nothing has failed",
               "promoted" not in before["fallback"],
               f"class={before['fallback']!r} text={before['fallbackText']!r}")

    page.click("#powerBtn")
    page.wait_for_timeout(1500)
    after = card(page)
    print(f"  wake refused (401) → fallback class={after['fallback']!r} "
          f"text={after['fallbackText']!r}")
    ok &= check("the refused wake promotes the manual page",
                "promoted" in after["fallback"],
                f"class={after['fallback']!r}")
    ok &= check("the promoted link says what it offers",
                "comment faire" in after["fallbackText"],
                f"text={after['fallbackText']!r}")
    ok &= check("the wake button is left usable for a retry",
                "unavailable" not in after["power"] and counters["wol"] == 1,
                f"power={after['power']!r} wol POSTs={counters['wol']}")
    b.close()
    return ok


def scenario_failed_wake_says_contact_admin(p):
    """v8.68 — the POSITIVE CONTROL for the alarming red, which now has exactly one
    cause: a wake that was attempted and failed.

    The red used to be reached by any down the app could not explain as "orderly",
    where "orderly" meant the relay's last-gasp — a signal that expires after
    HEARTBEAT_TTL_S = 45 s. So the nominal evening shutdown (gated on the AM5, so it
    usually lands INSIDE the uptime window) was calm blue for forty-five seconds and
    then told the family to call the admin for the rest of the night. That half is
    pinned in cold-radio-e2e (`unexplained-down-stays-calm-no-admin-shout`), which
    never fires a wake and must therefore never see the red; this is the other half,
    and without it "no red" could be passed by deleting the state altogether.

    Same 401 as the scenario above (settles in one round-trip); the transport
    failure and the 5-min timeout set the same `wakeFailed`.
    """
    _reset_clock_skew()
    print("\n## failed-wake-says-contact-admin (v8.68)")
    counters = {"relay": 0, "home": 0, "wol": 0}

    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.route("**/*", _mk_handler(counters, lambda n: "down", lambda n: "fail",
                                   wol_status=401))
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#statusLabel", state="attached", timeout=10000)
    page.wait_for_timeout(3000)

    before = card(page)
    print(f"  down, no wake attempted → {before['label']!r} / {before['sub']!r}")
    ok = check("a plain down is calm and does not name the admin",
               not is_red(before) and "administrateur" not in before["sub"],
               f"card={before['card']!r} sub={before['sub']!r}")

    page.click("#powerBtn")
    page.wait_for_timeout(1500)
    after = card(page)
    print(f"  wake refused (401) → {after['label']!r} / {after['sub']!r}")
    ok &= check("a FAILED wake turns the card red", is_red(after),
                f"card={after['card']!r} dot={after['dot']!r}")
    ok &= check("and only then does it say to contact the admin",
                "administrateur" in after["sub"], f"sub={after['sub']!r}")
    b.close()
    return ok


def scenario_adopted_wake_holds_the_screen(p):
    """v8.72 — the screen must stay on for a wake the phone ADOPTED, not only for one
    it tapped.

    Reported 2026-08-01: the AM5's logon task fires the wake, the phone adopts it and
    paints the countdown — and the screen locks ~30 s into an ~80 s boot, which is the
    exact symptom v8.18 shipped the wake lock to kill. The hole: `acquireWakeLock()`
    early-returned on `!wolSent`, a flag that is false by construction on an adopted
    wake, and `enterRemoteWaking()` never asked for the lock at all.

    `navigator.wakeLock` does not exist in headless Chromium (and never on file://), so
    the API is stubbed BEFORE load and the requests are counted. The stub also records
    releases, which is what makes the settle half of the assertion real rather than a
    "it asked once" formality.
    """
    _reset_clock_skew()
    print("\n## adopted-wake-holds-the-screen (v8.72)")
    counters = {"relay": 0, "home": 0, "wol": 0}
    state = {"waking": True}

    def relay_plan(n):
        return "waking:18" if state["waking"] else "up"

    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.route("**/*", _mk_handler(counters, relay_plan, lambda n: "ok"))
    # `navigator.wakeLock = …` is silently DROPPED here: Chromium exposes wakeLock as
    # a getter-only accessor on Navigator.prototype, so a plain assignment on the
    # instance is a no-op in sloppy mode (it DOES work on about:blank, where the API
    # is not exposed — which is how a first pass of this stub read as "0 requests,
    # bug confirmed" against a FIXED app.js). Override the accessor itself.
    page.add_init_script("""
      window.__wl = {req: 0, rel: 0};
      Object.defineProperty(Navigator.prototype, 'wakeLock', {configurable: true, get: function(){
        return {request: function(){
          window.__wl.req++;
          return Promise.resolve({release: function(){
            window.__wl.rel++; return Promise.resolve();
          }, addEventListener: function(){}});
        }};
      }});
    """)
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#statusLabel", state="attached", timeout=10000)
    page.wait_for_timeout(1200)

    adopted = card(page)
    wl = page.evaluate("() => window.__wl")
    print(f"  AM5 wake adopted → countdown={adopted['power']!r} wakeLock={wl}")
    ok = check("the adopted wake is actually running (fixture sanity)",
               is_counting_down(adopted) and counters["wol"] == 0,
               f"countdown={adopted['power']!r} wol POSTs={counters['wol']}")
    ok &= check("the screen is held during an ADOPTED wake (the bug)",
                wl["req"] >= 1 and wl["req"] > wl["rel"],
                f"requests={wl['req']} releases={wl['rel']}")
    # Every poll re-enters enterRemoteWaking(): exactly ONE lock must exist, or the
    # orphans outlive the boot and the screen never sleeps again.
    page.wait_for_timeout(9000)
    wl_poll = page.evaluate("() => window.__wl")
    ok &= check("and held ONCE, not re-minted on every poll",
                wl_poll["req"] == 1, f"requests={wl_poll['req']}")

    # The home comes up: the hold must end, otherwise the phone never sleeps again.
    state["waking"] = False
    page.wait_for_timeout(9000)
    settled = card(page)
    wl2 = page.evaluate("() => window.__wl")
    print(f"  home up → card={settled['label']!r} wakeLock={wl2}")
    ok &= check("and released once the home is up",
                is_green(settled) and wl2["rel"] >= 1,
                f"card={settled['card']!r} releases={wl2['rel']}")
    b.close()
    return ok


def scenario_degraded_resume_does_not_go_green_early(p):
    """v8.72 — the 2026-08-03 IRL bug: green 29 s before Seerr was reachable.

    Exact replay of what the relay and the app's own paint journal recorded.
    07:44:20 the AM5 fires /wol; 07:44:58 Yann opens the PWA on desktop Chrome;
    07:45:02 the page ADOPTS the wake; 07:45:08 the home starts answering
    `up degraded` (host awake, Seerr still 5xx) and the v8.49 hold correctly
    withholds green — for 16 s. Then a resume fires (a click back into the
    window is enough on desktop) and the card goes green, 29 s before the first
    non-degraded poll at 07:45:54.

    Two defects, both needed to produce it, so BOTH are asserted:
      1. the hold cached a bare `up`, so the pre-paint had a confident green to
         replay — written by the very branch that was withholding it;
      2. that pre-paint calls setOnline(), which clears `remoteWaking` and stops
         the countdown, so the later degraded polls no longer matched the hold
         either. The green did not just come early, it came to STAY.

    The positive control is the last leg: once the home is genuinely ready, the
    green must land. Without it, "never go green" would pass this test.
    """
    _reset_clock_skew()
    print("\n## degraded-resume-does-not-paint-green-early (v8.72 — IRL 2026-08-03)")
    counters = {"relay": 0, "home": 0, "wol": 0}
    state = {"phase": "waking"}

    def relay_plan(n):
        return {"waking": "waking:40", "degraded": "up-degraded", "ready": "up"}[state["phase"]]

    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.route("**/*", _mk_handler(counters, relay_plan, lambda n: "fail"))
    page.goto(PWA_URL, wait_until="load")
    page.wait_for_selector("#statusLabel", state="attached", timeout=10000)
    page.wait_for_timeout(800)

    adopted = card(page)
    ok = check("the PWA adopts the AM5 wake (countdown running, no tap of ours)",
               is_counting_down(adopted) and counters["wol"] == 0,
               f"countdown={adopted['power']!r} wol POSTs={counters['wol']}")

    # The host comes up but Seerr is not ready: relay serves up+degraded.
    state["phase"] = "degraded"
    page.wait_for_timeout(9000)          # let at least one degraded poll land
    held = card(page)
    print(f"  degraded poll landed → card={held['label']!r} countdown={held['power']!r}")
    ok &= check("the v8.49 hold still withholds green on a degraded up",
                not is_green(held), f"card={held['label']!r} dot={held['dot']!r}")

    # THE BUG: a resume. On desktop Chrome any click back into the window fires
    # this; on Android it is the app switcher. No time travel — the wake is live
    # and legitimately in progress, which is exactly why nothing may reap it.
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(1500)

    resumed = card(page)
    print(f"  resumed mid-boot → card={resumed['label']!r} countdown={resumed['power']!r} "
          f"bar={resumed['progress']!r}")
    ok &= check("NO green pre-painted from the degraded cache on resume",
                not is_green(resumed), f"card={resumed['label']!r} dot={resumed['dot']!r}")
    ok &= check("the wake survives the resume (countdown still running)",
                is_counting_down(resumed), f"progress bar={resumed['progress']!r}")

    # A degraded poll AFTER the resume: the hold must still apply, i.e. the
    # pre-paint must not have cleared remoteWaking underneath it. This is the
    # half that made the early green permanent rather than momentary.
    page.wait_for_timeout(9000)
    still = card(page)
    ok &= check("a degraded poll after the resume is still held (wake not cleared)",
                not is_green(still), f"card={still['label']!r} dot={still['dot']!r}")

    # POSITIVE CONTROL — services ready: the green must land, or this scenario
    # would be satisfied by an app that never goes green at all.
    state["phase"] = "ready"
    page.wait_for_timeout(9000)
    ready = card(page)
    print(f"  services ready → card={ready['label']!r}")
    ok &= check("green lands once the home answers non-degraded (positive control)",
                is_green(ready), f"card={ready['label']!r} dot={ready['dot']!r}")
    b.close()
    return ok


def scenario_late_adopted_wake_still_shows_a_timer(p):
    """v8.77 — a PWA opened cold LATE into someone else's wake must still show a
    moving timer, not a frozen string.

    Reported 2026-08-17: "la deuxième PWA ouverte après le démarrage dit juste
    démarrage en cours sans timer". The window is structural, not rare — the relay
    advertises `waking` for WAKE_SIGNAL_TTL_S (150 s) while the ETA is ~80 s, so
    every cold open in that 70 s tail arrives with wake_age > ETA. startCountdown
    then clamped the anchor to the ETA (`min(elapsed, etaMs)`), which pinned
    countdownEndsAt to "now" and left the label on "Réveil… presque prêt" — and
    past the -30 s threshold on a bare "Démarrage long…" that never changed again.

    Two properties, and the SECOND is the one the user reported:
      1. the countdown is armed at all (adoption works — it always did);
      2. the label MOVES. Two samples 3 s apart must differ. A frozen widget is
         indistinguishable from a hung app, which is what "sans timer" means.

    Positive control in the same run: an EARLY adopter (wake_age 10 s) must show
    the classic "environ Ns". Without it, "the label moves" would also pass on an
    app that shows a meaningless ticking string in every state.
    """
    _reset_clock_skew()
    print("\n## late-adopted-wake-still-shows-a-timer (v8.77)")
    ok = True

    def run(wake_age):
        counters = {"relay": 0, "home": 0, "wol": 0}
        b = getattr(p, ENGINE).launch()
        ctx = b.new_context(viewport={"width": 390, "height": 844})
        page = ctx.new_page()
        page.route("**/*", _mk_handler(counters, lambda n: f"waking:{wake_age}",
                                       lambda n: "fail"))
        page.goto(PWA_URL, wait_until="load")
        page.wait_for_selector("#statusLabel", state="attached", timeout=10000)
        page.wait_for_timeout(1200)
        first = card(page)
        page.wait_for_timeout(3000)
        second = card(page)
        b.close()
        return counters, first, second

    # wake_age 110 s > the fixture's eta_s of 80 s — the reported case.
    counters, first, second = run(110)
    print(f"  late adopter (wake_age 110s, eta 80s) → {first['power']!r} "
          f"then {second['power']!r}")
    ok &= check("a late adopter arms the countdown without tapping",
                is_counting_down(first) and counters["wol"] == 0,
                f"progress={first['progress']!r} wol POSTs={counters['wol']}")
    ok &= check("its timer MOVES (not a frozen 'presque prêt' / 'Démarrage long…')",
                first["power"] != second["power"],
                f"{first['power']!r} == {second['power']!r}")

    # Positive control: the ordinary early adoption still reads as a countdown.
    _, early_first, early_second = run(10)
    print(f"  early adopter (wake_age 10s) → {early_first['power']!r} "
          f"then {early_second['power']!r}")
    ok &= check("control: an early adopter still counts down in seconds",
                "environ" in early_first["power"]
                and early_first["power"] != early_second["power"],
                f"{early_first['power']!r} → {early_second['power']!r}")
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
        ok = scenario_stale_wake_on_resume(p)
        ok &= scenario_stale_remote_wake_on_resume(p)
        ok &= scenario_remote_wake_outlives_the_waking_signal(p)
        ok &= scenario_failed_wake_promotes_the_manual_page(p)
        ok &= scenario_failed_wake_says_contact_admin(p)
        ok &= scenario_adopted_wake_holds_the_screen(p)
        ok &= scenario_degraded_resume_does_not_go_green_early(p)
        ok &= scenario_late_adopted_wake_still_shows_a_timer(p)

    print("\n" + "=" * 72)
    print("ALL PASS" if ok else "FAILURES — see above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
