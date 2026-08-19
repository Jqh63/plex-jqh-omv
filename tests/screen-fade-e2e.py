#!/usr/bin/env python3
"""Render pins for the v8.59 screen fade and toast timing.

Two changes, one rule: a toast ACKNOWLEDGES A GESTURE; anything describing a
STATE belongs on the tile, where it persists.

1. Screen changes (main <-> settings) were a hard cut between two `display`
   values — the only navigation the app has. switchScreen() fades out, swaps,
   fades in. Pinned by measuring computed opacity mid-flight, because a fade
   that silently no-ops (the classList add/remove collapsing into one style
   recalculation without the reflow between them) leaves every assertion on
   `display` green while the cut is still there.

2. Toast durations were calibrated for an ack (3 s) and read too fast for
   messages carrying an explanation. Pinned as constants + one behavioural
   pin: the warm-up hint must stand down when the tile's sub already says the
   same thing.

Run: python3 tests/screen-fade-e2e.py   (exit 0 = all pins hold)
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

# Fail early and legibly if the engines' system libs are gone (a sandbox
# upgrade wipes /usr/lib while the binaries persist). No-op when healthy.
from browser_guard import ensure as _ensure_browser

_ensure_browser()

ROOT = pathlib.Path(__file__).resolve().parent.parent
URL = ("file://" + str(ROOT / "index.html")
       + "?host=myserver.example.com&relay=https://wol.example.com"
       + "&mac=AABBCCDDEEFF&token=demo&title=Plex+jqh+omv")


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def opacity(pg, sel):
    return pg.evaluate(
        "s => parseFloat(getComputedStyle(document.querySelector(s)).opacity)", sel)


def main():
    results = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 360, "height": 780})
        pg.goto(URL)
        pg.wait_for_selector("#mainScreen", state="visible", timeout=10000)
        pg.wait_for_timeout(300)

        # --- boot must NOT have faded ------------------------------------
        results.append(check("the first screen is painted instantly at boot",
                             opacity(pg, "#mainScreen") > 0.95,
                             f"opacity={opacity(pg, '#mainScreen')}"))

        # --- pin 1: leaving the main screen fades it out -----------------
        # Sampled mid-flight; against v8.58 the element is display:none by now
        # and getComputedStyle reports opacity 1, so this pin fails there.
        pg.evaluate("showSettings()")
        pg.wait_for_timeout(90)
        out = pg.evaluate(
            """() => {
                 const m = document.getElementById('mainScreen');
                 return {op: parseFloat(getComputedStyle(m).opacity),
                         disp: getComputedStyle(m).display};
               }""")
        results.append(check("the outgoing screen fades instead of cutting",
                             out["op"] < 0.9 and out["disp"] != "none",
                             f"opacity={out['op']} display={out['disp']}"))

        # --- pin 2: the incoming screen fades IN -------------------------
        # The reflow-dependent half: without it the settings screen appears at
        # full opacity the instant it is displayed.
        pg.wait_for_timeout(120)
        incoming = opacity(pg, "#settingsScreen")
        results.append(check("the incoming screen fades in",
                             incoming < 0.95,
                             f"opacity={incoming}"))

        # --- pin 3: it completes, and the field is focused ---------------
        pg.wait_for_timeout(400)
        settled = opacity(pg, "#settingsScreen")
        focused = pg.evaluate("document.activeElement && document.activeElement.id")
        results.append(check("settings settles fully opaque with the host field focused",
                             settled > 0.95 and focused == "cfgHost",
                             f"opacity={settled} focus={focused!r}"))

        # --- pin 4: toast durations are the calibrated ones --------------
        consts = pg.evaluate("({t: window.TOAST_MS, l: window.TOAST_LONG_MS})")
        results.append(check("toast durations give time to read",
                             consts["t"] == 4500 and consts["l"] == 7000,
                             f"default={consts['t']} long={consts['l']}"))

        # --- pin 5: no toast repeats what the tile already says ----------
        # The failure toasts used to end in "— réveil manuel ↓" while
        # setFallbackState() promotes that same link permanently. Scope the
        # check to showToast CALL SITES: the first cut of this pin grepped the
        # whole file and fired on two comments explaining the change, i.e. it
        # was observing the wrong thing — the very trap this suite exists for.
        src = (ROOT / "app.js").read_text(encoding="utf-8")
        calls = [l for l in src.splitlines()
                 if "showToast(" in l and not l.lstrip().startswith("//")]
        offenders = [l.strip() for l in calls if "réveil manuel ↓" in l]
        results.append(check("failure toasts no longer repeat the promoted link",
                             not offenders and len(calls) >= 10,
                             f"{len(calls)} call sites, offenders={offenders}"))

        # 2026-07-29 — in-app help must not describe a label the app never
        # paints. v8.53 merged the two blue labels into a bare « Éteint », but
        # the settings hint (in BOTH index.html and the JS that rewrites it)
        # kept promising « Éteint (prévu) » for months. Help that names a string
        # the user will never see is worse than no help — it makes them doubt
        # they are on the right screen, and nothing in the suite could see it:
        # every pin asserted on the TILE, and the tile was right.
        # Same scoping discipline as the pin above: skip comment lines, or the
        # comments explaining this very change would trip it.
        ghost = "Éteint (prévu)"
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        stale = [f"app.js:{i+1}" for i, l in enumerate(src.splitlines())
                 if ghost in l and not l.lstrip().startswith("//")]
        stale += [f"index.html:{i+1}" for i, l in enumerate(html.splitlines())
                  if ghost in l and "<!--" not in l]
        results.append(check("no user-visible string promises a label the app dropped",
                             not stale, f"ghost={ghost!r} offenders={stale}"))

        b.close()

    print()
    if all(results):
        print(f"ALL PASS ({len(results)} pins)")
        return 0
    print(f"FAIL ({sum(1 for r in results if not r)}/{len(results)} pins)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
