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


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return cond


def _body(verdict):
    if verdict == "up":
        return '{"up": true, "stale": false, "age_s": 1, "source": "heartbeat"}'
    # The IRL shape: the home's own last words, still standing.
    return '{"up": false, "stale": false, "age_s": 549, "source": "heartbeat"}'


def run(p, name, verdict, window_inside, expect_present, expect_absent):
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
    page.goto(_url(PWA_BASE, _window(window_inside)), wait_until="load")
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


def main():
    print(f"Paint journal E2E — engine={ENGINE} base={PWA_BASE}")
    with sync_playwright() as p:
        ok = run(
            p, "off-window cold open on a home that declared DOWN (the IRL case)",
            verdict="down", window_inside=False,
            # The blue schedule presumption, then the committed red. If a GREEN
            # ever appears in this sequence again, its reason will be right here.
            expect_present=["presume-off-window", "verdict-down"],
            expect_absent=["presume-in-window", "verdict-up", "cache-prepaint-open"],
        )
        ok &= run(
            p, "in-window cold open on a home that is UP (positive control)",
            verdict="up", window_inside=True,
            expect_present=["presume-in-window", "verdict-up"],
            expect_absent=["presume-off-window", "verdict-down"],
        )
    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
