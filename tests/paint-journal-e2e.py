#!/usr/bin/env python3
"""Real-browser E2E for the paint journal (v8.70) — app.js logPaint + debug.html.

Why this exists (2026-07-30). A family report — "Éteint (prévu)" → GREEN →
éteint, on a home that had declared a clean shutdown 9 minutes earlier — could
not be replayed: the relay logged only its own state transitions and the client
kept no trace at all. Every candidate had to be excluded by reading code, which
is precisely the "diagnose by elimination" the instrument-first rule exists to
prevent.

So the pins here are on the INSTRUMENT itself, and they assert on what a reader
of debug.html actually SEES, not just on the localStorage ring: a journal that
records perfectly and renders nothing would be worthless in the one situation it
is built for (a phone, on holiday, 800 km from a terminal).

Each scenario carries its own negative control — the entry that must be ABSENT.
Without it, "the expected reason is in the journal" would pass on a journal that
logged every reason on every paint.

  python3 tests/paint-journal-e2e.py
  PWA_BASE=deployed python3 tests/paint-journal-e2e.py     # post-merge gate
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

# Fail early and legibly if the engines' system libs are gone (a sandbox
# upgrade wipes /usr/lib while the binaries persist). No-op when healthy.
from browser_guard import ensure as _ensure_browser

_ensure_browser()

CONFIG_HOST = "test.example.com"
RELAY_HOST = "r.example.com"
_LOCAL_BASE = "file://" + os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "index.html"))
PWA_BASE = os.environ.get("PWA_BASE") or _LOCAL_BASE
if PWA_BASE == "deployed":
    PWA_BASE = "https://jqh63.github.io/plex-jqh-omv/"
ENGINE = os.environ.get("PWA_ENGINES", "chromium").split(",")[0].strip()
PAINT_LOG_KEY = "plex-jqh-omv-paints"


def _url(base, window, extra=""):
    return (f"{base}?host={CONFIG_HOST}&mac=AABBCCDDEEFF"
            f"&relay=https://{RELAY_HOST}&token=x&apps=seerr,plexweb"
            f"&window={window}&poll=2{extra}")


def _debug_url():
    return (PWA_BASE[:-len("index.html")] + "debug.html"
            if PWA_BASE.endswith("index.html")
            else PWA_BASE.rstrip("/") + "/debug.html")


def _window(inside):
    """A window string that is open (or closed) RIGHT NOW, wall-clock relative.
    Hardcoding the production 13h50-00h10 would make the suite pass or fail
    depending on the hour it runs — the exact class of flake that makes a
    verdict background noise."""
    now = datetime.now()
    if inside:
        lo, hi = now - timedelta(hours=1), now + timedelta(hours=1)
    else:
        lo, hi = now + timedelta(hours=1), now + timedelta(hours=2)
    return f"{lo.strftime('%Hh%M')}-{hi.strftime('%Hh%M')}"


def _window_ended(minutes_ago):
    """A CLOSED window whose end boundary is `minutes_ago` in the past. Lets a
    scenario place a persisted verdict on either side of that boundary — the
    only thing that distinguishes "measured after the shutdown, so the home was
    woken out of plan" from "measured before it, so the schedule has since had
    its say"."""
    now = datetime.now()
    hi = now - timedelta(minutes=minutes_ago)
    lo = hi - timedelta(hours=3)
    return f"{lo.strftime('%Hh%M')}-{hi.strftime('%Hh%M')}"


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return cond


def _body(verdict):
    # v8.73 — every body carries the relay's build stamp, because the running
    # relay does (relay/app.py) and the PWA now refuses verdicts from bodies it
    # cannot prove are live. A fixture without it models a relay that no longer
    # exists — and it showed: this whole suite went to `stale-body-exhausted`.
    sa = f', "served_at": {int(time.time())}'
    if verdict == "up":
        return '{"up": true, "stale": false, "age_s": 1, "source": "heartbeat"' + sa + '}'
    if verdict == "down-pull-confirmed":
        # The relay's stale-beat demotion (2026-07-30): a confirm-gated pull
        # that contradicts a beat it post-dates. Commits red at once, like a
        # last-gasp — re-confirming it client-side would move the false-green
        # window instead of closing it.
        return ('{"up": false, "stale": false, "confirmed": true, '
                '"age_s": 8, "source": "pull"' + sa + '}')
    # The IRL shape: the home's own last words, still standing.
    return '{"up": false, "stale": false, "age_s": 549, "source": "heartbeat"' + sa + '}'


def run(p, name, verdict, window, expect_present, expect_absent, seed_prior=None):
    print(f"\n## {name}")
    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    seen = {"relay": 0}

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc == RELAY_HOST and parsed.path == "/status":
            seen["relay"] += 1
            route.fulfill(status=200, body=_body(verdict), headers={
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
            })
            return
        if parsed.netloc == CONFIG_HOST or parsed.netloc.endswith("." + CONFIG_HOST):
            route.fulfill(status=200, body="")
            return
        route.continue_()

    page = ctx.new_page()
    page.route("**/*", handle)
    if seed_prior is not None:
        # Same origin, but NOT the app: debug.html never paints, so the ring the
        # scenario then reads contains only what the app decided.
        up, minutes_old = seed_prior
        page.goto(_debug_url(), wait_until="load")
        page.evaluate(
            "([up,ms,k,pk])=>{localStorage.setItem(k,JSON.stringify("
            "{up:up,relayOk:true,t:Date.now()-ms}));localStorage.removeItem(pk);}",
            [up, int(minutes_old * 60000), "plex-jqh-omv-status", PAINT_LOG_KEY])
    page.goto(_url(PWA_BASE, window), wait_until="load")
    # Long enough for the pre-paint AND the settling probe — the whole point is
    # that BOTH are recorded, in order.
    page.wait_for_timeout(4000)

    ring = page.evaluate(f"JSON.parse(localStorage.getItem('{PAINT_LOG_KEY}')||'[]')")
    reasons = [e["w"] for e in ring]
    ok = check("the relay was actually consulted (mock alive)", seen["relay"] >= 1,
               f"{seen['relay']} call(s)")
    ok &= check("the journal is not empty", bool(ring), json.dumps(reasons))
    for w in expect_present:
        ok &= check(f"journal records {w!r}", w in reasons, json.dumps(reasons))
    for w in expect_absent:
        ok &= check(f"journal does NOT record {w!r} (control)", w not in reasons,
                    json.dumps(reasons))
    ok &= check("every entry carries a timestamp and a card",
                all(isinstance(e.get("t"), int) and e.get("c") for e in ring))

    # The render layer: what a reader of debug.html sees. Asserted separately
    # because a perfect ring behind a blank <pre> is the failure mode that
    # matters (memory: assert on what the eye sees, not on the state behind it).
    page.goto(_debug_url(), wait_until="load")
    page.wait_for_timeout(500)
    rendered = (page.text_content("#paintLog") or "").strip()
    ok &= check("debug.html renders the journal", bool(rendered) and "vide" not in rendered,
                rendered[:120])
    for w in expect_present:
        ok &= check(f"debug.html shows {w!r}", w in rendered, rendered[:200])
    # Guarded on an empty ring: with the instrumentation disabled (the control
    # run that proves these pins can fail) this must report a FAIL, not raise.
    ok &= check("the newest entry is rendered FIRST",
                bool(rendered) and bool(reasons)
                and rendered.splitlines()[0].split("←")[-1].strip().startswith(reasons[-1]),
                rendered.splitlines()[0] if rendered else "")
    b.close()
    return ok


def run_dated_render(p):
    """An entry from another day must be dated in the render.

    Since the collapse fix the ring can span days, and the journal is read to
    reconstruct a chronology — an entry from Tuesday that renders like this
    morning's is worse than no timestamp at all. Control: today's entry must
    NOT be dated, otherwise "always prefix the date" would pass too and every
    normal read would carry noise.
    """
    print("\n## an entry from another day is dated (and today's is not)")
    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.goto(_debug_url(), wait_until="load")
    old_ms = int((datetime.now() - timedelta(days=2)).timestamp() * 1000)
    day = (datetime.now() - timedelta(days=2))
    page.evaluate(
        "([k,old])=>localStorage.setItem(k,JSON.stringify(["
        "{t:old,t0:old,c:'offline',w:'presume-off-window'},"
        "{t:Date.now(),t0:Date.now(),c:'online',w:'verdict-up'}]))",
        [PAINT_LOG_KEY, old_ms])
    page.reload(wait_until="load")
    page.wait_for_timeout(300)
    rendered = (page.text_content("#paintLog") or "").strip()
    expect_day = f"{day.day:02d}/{day.month:02d}"
    lines = rendered.splitlines()
    ok = check(f"the 2-day-old entry carries {expect_day}",
               any(expect_day in l and "presume-off-window" in l for l in lines), rendered[:160])
    ok &= check("today's entry is NOT dated (control)",
                any("verdict-up" in l and expect_day not in l and "/" not in l.split("←")[0]
                    for l in lines), rendered[:160])
    b.close()
    return ok


def run_relay_silent_stabilises(p):
    """A LONG relay outage must settle on the unknown card, not oscillate.

    IRL 2026-07-31, in Yann's journal during a "dead URL" test: while the cache
    held, every cycle logged `cache-prepaint-open online` → `kept-verdict ←
    relay-silent`, no flip. Once the cache aged past STATUS_LOCAL_TTL_MS, every
    cycle repainted `presume-in-window online` and then `unknown ← relay-silent`
    ~1 s later — SIX green→grey round trips in 50 s.

    Both paths are individually right; their COMPOSITION is the defect.
    setUnknown() leaves hasConfirmedState false, so the next tick re-enters the
    pre-paint guard (`!hasConfirmedState`), re-presumes green, and gets demoted
    again. The rule this pins: a presumption the relay has already refuted in
    the same episode must not be replayed — the exact parallel of the v8.52
    guard that forbids re-presuming during an in-flight down confirmation.

    Positive control in the same run: when the relay comes back up, the card
    must still repaint green. Without it, "never presume again, ever" would
    also pass — and the app would stay grey forever after one blip.
    """
    print("\n## a long relay outage settles on unknown instead of oscillating")
    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    state = {"silent": True, "relay": 0}

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc == RELAY_HOST and parsed.path == "/status":
            state["relay"] += 1
            if state["silent"]:
                route.abort()
                return
            route.fulfill(status=200, body=_body("up"), headers={
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
            })
            return
        if parsed.netloc == CONFIG_HOST or parsed.netloc.endswith("." + CONFIG_HOST):
            # The direct-home fallback must fail too — a silent RELAY that the
            # home answers behind is a different scenario (and not the one that
            # oscillated). Everything else (the page's own assets) goes through.
            route.abort()
            return
        route.continue_()

    page = ctx.new_page()
    page.route("**/*", handle)
    # A prior that is UP but older than STATUS_LOCAL_TTL_MS (60 s) and younger
    # than PRESUME_STALE_MAX_MS (30 min): exactly the regime where the cache no
    # longer holds a verdict but the in-window presumption still fires. That is
    # the window in which the oscillation was observed.
    page.goto(_debug_url(), wait_until="load")
    page.evaluate(
        "([k,pk])=>{localStorage.setItem(k,JSON.stringify("
        "{up:true,relayOk:true,t:Date.now()-5*60000}));localStorage.removeItem(pk);}",
        ["plex-jqh-omv-status", PAINT_LOG_KEY])

    base = f"{PWA_BASE}?host={CONFIG_HOST}&mac=AABBCCDDEEFF&relay=https://{RELAY_HOST}"
    base += f"&token=x&apps=seerr,plexweb&window={_window(True)}&poll=400"
    page.goto(base, wait_until="load")
    page.wait_for_timeout(6000)          # ~15 poll cycles at 400 ms

    ring = page.evaluate(f"JSON.parse(localStorage.getItem('{PAINT_LOG_KEY}')||'[]')")
    reasons = [e["w"] for e in ring]
    ok = check("the relay was actually consulted (mock alive)", state["relay"] >= 3,
               f"{state['relay']} call(s)")
    ok &= check("the outage reached the unknown card", "relay-silent" in reasons,
                json.dumps(reasons))

    # THE PIN. Not "how many unknowns" — how many times the card went BACK to a
    # green presumption after the relay had already refuted it. Zero is the
    # spec; the shipped code produces one per poll cycle.
    first_unknown = next((i for i, e in enumerate(ring)
                          if e["w"] == "relay-silent" and e["c"] == "unknown"), None)
    replays = [e["w"] for e in ring[(first_unknown or 0) + 1:]
               if e["w"].startswith("presume-")]
    ok &= check("no presumption is replayed after the relay refuted it",
                first_unknown is not None and not replays,
                f"{len(replays)} replay(s): {json.dumps(replays)}")

    # And the card the user is left looking at is the honest one.
    ok &= check("the card ends on unknown, not green",
                bool(ring) and ring[-1]["c"] == "unknown",
                json.dumps([(e["c"], e["w"]) for e in ring[-4:]]))

    # POSITIVE CONTROL — the relay comes back; green must return. Without this
    # the fix could legally be "never presume again" and freeze the card grey.
    state["silent"] = False
    page.wait_for_timeout(3000)
    ring2 = page.evaluate(f"JSON.parse(localStorage.getItem('{PAINT_LOG_KEY}')||'[]')")
    ok &= check("positive control: the card repaints green when the relay returns",
                bool(ring2) and ring2[-1]["c"] == "online",
                json.dumps([(e["c"], e["w"]) for e in ring2[-4:]]))
    b.close()
    return ok


def run_slow_relay_stabilises(p):
    """Same defect, but with a relay that HANGS instead of refusing.

    The v8.72 pin used route.abort() — an instant refusal. The IRL failure of a
    GCP e2-micro is the other shape: the request hangs until PROBE_TIMEOUT_MS
    (8 s) gives up. The oscillation mechanism does not depend on the delay, but
    "does not depend on" was an argument, not a measurement — and the whole
    reason this bug existed is that two individually-correct paths composed
    badly. So the slow shape gets its own pin.

    The relay request is left PENDING (no fulfill/abort): only the client's own
    AbortController ends it. The home fallback still fails fast, so a cycle is
    ~8 s rather than ~16 s.
    """
    print("\n## a SLOW (hanging) relay also settles on unknown")
    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    state = {"relay": 0}

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc == RELAY_HOST and parsed.path == "/status":
            state["relay"] += 1
            return  # left pending on purpose — the client times out
        if parsed.netloc == CONFIG_HOST or parsed.netloc.endswith("." + CONFIG_HOST):
            route.abort()
            return
        route.continue_()

    page = ctx.new_page()
    page.route("**/*", handle)
    page.goto(_debug_url(), wait_until="load")
    page.evaluate(
        "([k,pk])=>{localStorage.setItem(k,JSON.stringify("
        "{up:true,relayOk:true,t:Date.now()-5*60000}));localStorage.removeItem(pk);}",
        ["plex-jqh-omv-status", PAINT_LOG_KEY])

    url = (f"{PWA_BASE}?host={CONFIG_HOST}&mac=AABBCCDDEEFF&relay=https://{RELAY_HOST}"
           f"&token=x&apps=seerr,plexweb&window={_window(True)}&poll=400")
    page.goto(url, wait_until="load")
    page.wait_for_timeout(32000)   # ~3 probe timeouts at 8 s

    ring = page.evaluate(f"JSON.parse(localStorage.getItem('{PAINT_LOG_KEY}')||'[]')")
    reasons = [e["w"] for e in ring]
    ok = check("the hanging relay was actually hit", state["relay"] >= 2,
               f"{state['relay']} call(s)")
    ok &= check("a slow outage still reaches the unknown card",
                "relay-silent" in reasons, json.dumps(reasons))
    first_unknown = next((i for i, e in enumerate(ring)
                          if e["w"] == "relay-silent" and e["c"] == "unknown"), None)
    replays = [e["w"] for e in ring[(first_unknown or 0) + 1:]
               if e["w"].startswith("presume-")]
    ok &= check("no presumption is replayed after a SLOW refutation",
                first_unknown is not None and not replays,
                f"{len(replays)} replay(s): {json.dumps(replays)}")
    ok &= check("the card ends on unknown", bool(ring) and ring[-1]["c"] == "unknown",
                json.dumps([(e["c"], e["w"]) for e in ring[-4:]]))
    b.close()
    return ok


def run_presumption_is_labelled(p):
    """v8.73 — RENDER pin for the family-visible half of the fix.

    IRL 2026-08-04: the card said "Éteint", flashed "En ligne", then "Éteint"
    again — and NOTHING on screen ever said the first two were guesses. A
    presumption and a measured verdict were pixel-identical, so a family reader
    could only conclude the app was unreliable.

    The claim is carried by `#statusAge` — the line that otherwise reports the
    MEASUREMENT ("vérifié il y a…"), which makes "non vérifié" its exact
    counterpart and costs no layout (the element is already reserved).

    Asserted on the RENDERED text, not on cardKind: a state variable that no
    one can see would pass a pin that measures nothing."""
    print("\n## presumption is labelled 'non vérifié' on screen")
    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    served = {"n": 0}

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc == RELAY_HOST and parsed.path == "/status":
            served["n"] += 1
            # Never answer: the presumption is what stays on screen, which is
            # exactly the window the family reads on a cold open.
            return
        if parsed.netloc == CONFIG_HOST or parsed.netloc.endswith("." + CONFIG_HOST):
            route.fulfill(status=200, body="")
            return
        route.continue_()

    page.route("**/*", handle)
    page.goto(_url(PWA_BASE, _window(False)), wait_until="load")
    page.wait_for_timeout(1200)
    label = (page.text_content("#statusLabel") or "").strip()
    age = (page.text_content("#statusAge") or "").strip()
    ok = check("the sleep presumption is on screen", label == "Éteint", label)
    ok &= check("it says it was NOT verified", age == "non vérifié", repr(age))
    b.close()

    # POSITIVE CONTROL — without it, "always print non vérifié" would pass the
    # pin above while lying on every measured verdict, which is the whole thing
    # the label exists to distinguish.
    b = getattr(p, ENGINE).launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()

    def handle_live(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc == RELAY_HOST and parsed.path == "/status":
            route.fulfill(status=200, body=_body("up"), headers={
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
            })
            return
        if parsed.netloc == CONFIG_HOST or parsed.netloc.endswith("." + CONFIG_HOST):
            route.fulfill(status=200, body="")
            return
        route.continue_()

    page.route("**/*", handle_live)
    page.goto(_url(PWA_BASE, _window(True)), wait_until="load")
    page.wait_for_timeout(2000)
    age2 = (page.text_content("#statusAge") or "").strip()
    ok &= check("a MEASURED verdict carries no such label (control)",
                age2 == "", repr(age2))
    b.close()
    return ok


def run_wake_journal_labels(p):
    """During a wake, the journal must record the card the user SEES.

    IRL 2026-08-16 07:00, Yann's journal, read on a phone: a tap at 07:00:37
    then FIFTEEN entries `offline ← verdict-down [... waking]` spanning 43 s,
    then green. Read literally — and the journal's own contract says to read it
    literally ("`card` = what the user sees") — the wake spent 43 s painting a
    red "hors ligne" at the user before working.

    It never did. setOffline() hands over to setStarting() while wolSent is
    true, on purpose, so the card read "Démarrage…" throughout. The branch
    logged the verdict it had COMPUTED, not the card that landed.

    Why this went unseen for so long: wake-e2e asserts on the DOM (it catches a
    wrong card) and this suite asserts on the ring (it catches a wrong reason).
    Nothing compared the two, so a label that contradicted the screen was
    invisible to both. That is the pin below.

    Positive control in the same run, and it is what makes the assertion mean
    anything: the SAME relay body with NO tap must still journal `offline`.
    Without it, "never log offline" would pass on a build that logs nothing at
    all — and would hide a real red.
    """
    print("\n## during a wake the journal names the card on screen, not the verdict")
    ok = True
    for tapped in (True, False):
        label = "with a local tap (wake in flight)" if tapped else "without a tap (control)"
        b = getattr(p, ENGINE).launch()
        ctx = b.new_context(viewport={"width": 390, "height": 844})
        seen = {"relay": 0, "woken": False}

        def handle(route):
            parsed = urlparse(route.request.url)
            if parsed.netloc == RELAY_HOST and parsed.path == "/status":
                seen["relay"] += 1
                # The IRL shape, in the order it actually happens: a plain
                # heartbeat-sourced "down" until the wake is fired, then the
                # relay starts advertising `waking`. Serving `waking` from the
                # first poll would model something else entirely — the app would
                # ADOPT a remote wake (enterRemoteWaking) before any tap, the
                # power button would be owned by the countdown, and the scenario
                # would never exercise the local-wake path it is about.
                sa = f', "served_at": {int(time.time())}'
                wk = ', "waking": true' if seen["woken"] else ""
                route.fulfill(status=200, headers={
                    "Content-Type": "application/json",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "*",
                }, body='{"up": false, "stale": false, "age_s": 14282, '
                        '"source": "heartbeat"' + wk + sa + '}')
                return
            if parsed.netloc == RELAY_HOST and parsed.path == "/wol":
                seen["woken"] = True
                route.fulfill(status=200, body='{"ok": true}', headers={
                    "Content-Type": "application/json",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "*",
                })
                return
            if parsed.netloc == CONFIG_HOST or parsed.netloc.endswith("." + CONFIG_HOST):
                route.fulfill(status=200, body="")
                return
            route.continue_()

        page = ctx.new_page()
        page.route("**/*", handle)
        # Off-window, so the pre-paint is the blue schedule presumption and any
        # `offline` in the ring can only have come from the verdict branch.
        page.goto(_url(PWA_BASE, _window(False)), wait_until="load")
        page.wait_for_selector("#statusLabel", state="attached", timeout=10000)
        # Let the first probe settle before tapping: sendWol() refuses on
        # `!wolReady()`, so clicking into the cold-open check would silently
        # do nothing and the scenario would assert on a wake that never was.
        page.wait_for_timeout(3000)
        if tapped:
            page.click("#powerBtn")
        # Several poll cycles (poll=2), so the down-verdict branch runs
        # repeatedly under the wake — the 15-entry burst, in miniature.
        page.wait_for_timeout(7000)

        ring = page.evaluate(f"JSON.parse(localStorage.getItem('{PAINT_LOG_KEY}')||'[]')")
        # Only the entries the verdict branch produced; the schedule presumption
        # legitimately paints `offline` before any probe and is not at issue.
        # Scoped to AFTER the tap on the wake run: the cold-open verdict that
        # precedes it is a genuine red on a genuinely-off home, correctly
        # labelled `offline`, and demanding "never offline" over the whole ring
        # would have pinned the wrong rule — the bug is the label DURING a wake,
        # not the label of a down.
        start = 0
        if tapped:
            taps = [i for i, e in enumerate(ring) if e.get("w") == "local-tap"]
            ok &= check("[tap] the tap reached the app (mock alive)", bool(taps),
                        json.dumps([e.get("w") for e in ring]))
            start = (taps[-1] + 1) if taps else len(ring)
        verdicts = [e for e in ring[start:] if e.get("w") == "verdict-down"]
        cards = sorted({e.get("c") for e in verdicts})
        # The LABEL, not the card's class: setStarting() leaves statusCard at the
        # plain 'status-card' and says "Démarrage…" in the tile — the wake state
        # is legible to the user there and nowhere else. Asserting on the class
        # would have been an assertion on the wrong element, which is the exact
        # trap that let this bug live (see the docstring).
        card_now = (page.text_content("#statusLabel") or "").strip()
        ok &= check(f"[{label}] the verdict branch ran (mock alive)",
                    bool(verdicts), json.dumps([e.get("w") for e in ring]))
        if tapped:
            # The screen first — the assertion is "the journal agrees with THIS",
            # so a run where the card was not actually the wake card would prove
            # nothing (and this is the half wake-e2e already owns).
            ok &= check("[tap] the card on screen reads 'Démarrage…'",
                        card_now.startswith("Démarrage"), card_now)
            ok &= check("[tap] verdict-down entries are labelled 'waking', never 'offline'",
                        cards == ["waking"], json.dumps(cards))
        else:
            ok &= check("[control] without a tap the same body still logs 'offline'",
                        cards == ["offline"], json.dumps(cards))
        b.close()
    return ok


def main():
    print(f"Paint journal E2E — engine={ENGINE} base={PWA_BASE}")
    with sync_playwright() as p:
        ok = run(
            p, "off-window cold open on a home that declared DOWN (the IRL case)",
            verdict="down", window=_window(False),
            # The blue schedule presumption, then the committed red. If a GREEN
            # ever appears in this sequence again, its reason will be right here.
            expect_present=["presume-off-window", "verdict-down"],
            expect_absent=["presume-in-window", "verdict-up", "cache-prepaint-open"],
        )
        ok &= run(
            p, "in-window cold open on a home that is UP (positive control)",
            verdict="up", window=_window(True),
            expect_present=["presume-in-window", "verdict-up"],
            expect_absent=["presume-off-window", "verdict-down"],
        )
        # IRL 2026-07-30, 07:59 — home woken by hand hours after the window
        # closed, app reopened 3 min later: the card flashed "Éteint (prévu)"
        # before the probe corrected it to green. A MEASURED verdict, taken
        # after the window's end boundary, knows something the schedule cannot.
        ok &= run(
            p, "off-window reopen after a manual wake (measured prior outranks the schedule)",
            verdict="up", window=_window_ended(90), seed_prior=(True, 3),
            expect_present=["presume-prior-outranks-window", "verdict-up"],
            expect_absent=["presume-off-window"],
        )
        # The control that keeps the rule honest: same shape, but the prior was
        # measured BEFORE the window closed — the scheduled shutdown has
        # happened since, so the schedule wins and the blue card is right.
        # Without this, "always trust an up prior off-window" would also pass,
        # and every nightly reopen would flash green.
        ok &= run(
            p, "off-window reopen with a prior measured BEFORE the close (control)",
            verdict="down", window=_window_ended(10), seed_prior=(True, 25),
            expect_present=["presume-off-window", "verdict-down"],
            expect_absent=["presume-prior-outranks-window", "verdict-up"],
        )
        # The client half of the relay's stale-beat demotion. Negative control:
        # 'down-unconfirmed' — its presence would mean the PWA re-litigated a
        # verdict two legs already agreed on, adding ~16 s of orange.
        ok &= run(
            p, "a confirm-gated pull-down commits red without the orange detour",
            verdict="down-pull-confirmed", window=_window(True),
            expect_present=["verdict-down"],
            expect_absent=["down-unconfirmed", "verdict-up"],
        )
        ok &= run_dated_render(p)
        ok &= run_presumption_is_labelled(p)
        ok &= run_relay_silent_stabilises(p)
        ok &= run_slow_relay_stabilises(p)
        ok &= run_wake_journal_labels(p)
    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
