#!/usr/bin/env python3
"""Render pins for v8.61 — nothing below the tile may move when a line appears.

Reported by the user: "la ligne qui apparaît pour le timer ou même sur l'état
du relais fait déplacer le layout". Measured at 360 px before the fix:

  * the wake progress bar toggled `display` -> everything below it dropped by
    10,5 px the instant a wake started, and the power button rose by as much —
    the button moving under the thumb at the exact moment it is pressed;
  * promoting the manual-wake link (relay unreachable, or a failed wake) grew
    it by 6,5 px — bigger type AND a margin flip from -4 to 6 px — pushing the
    whole list of app links down.

Both boxes now reserve their tallest state and change only opacity/type, the
same reservation doctrine as the tile's own min-height (v8.53/#154).

These pins measure ABSOLUTE POSITIONS of the elements below the change, not
the styles that produce them: a future refactor that reintroduces the shift by
another route (a margin, a display toggle, an extra line) still fails here.
Verified FAILING against v8.60 on the two shifting states.

Run: python3 tests/layout-stability-e2e.py   (exit 0 = all pins hold)
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

# Sub-pixel jitter from font metrics is not a layout shift; a real one here was
# 6,5 px at the smallest.
TOLERANCE_PX = 1.0

PROBE = """() => {
  const top = s => {
    const e = document.querySelector(s);
    if (!e) return null;
    return Math.round(e.getBoundingClientRect().top * 10) / 10;
  };
  return {
    powerBtn: top('#powerBtn'),
    powerLabel: top('#powerLabel'),
    fallback: top('#fallbackLink'),
    links: top('#linksContainer'),
    footer: top('.footer'),
  };
}"""


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def moved(base, now):
    """Elements whose top moved more than the tolerance, with the delta."""
    out = {}
    for k, v in base.items():
        if v is None or now.get(k) is None:
            continue
        d = round(now[k] - v, 1)
        if abs(d) > TOLERANCE_PX:
            out[k] = d
    return out


def main():
    results = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 360, "height": 780})
        pg.goto(URL)
        pg.wait_for_selector("#mainScreen", state="visible", timeout=10000)
        pg.wait_for_timeout(400)
        base = pg.evaluate(PROBE)
        print(f"  baseline: {base}")

        # --- pin 0: the install hint (v8.72) ------------------------------
        # Reported by the user: the "Astuce" banner "décale le centrage
        # vertical". It was revealed from a 3 s setTimeout, and `body` is a
        # centred flex column, so everything above it moved UP 3 s after the
        # page settled. Two halves, both verified FAILING against d607b44:
        # at 400 ms the banner was still display:none (pin 0a), and the page
        # then shifted 33,5 px when it appeared (pin 0b).
        # file:// is not standalone, so the banner is in scope here.
        hint_shown = pg.evaluate(
            "() => getComputedStyle(document.getElementById('installHint'))"
            ".display !== 'none'")
        results.append(check("the install hint is in the layout at the first paint",
                             hint_shown, f"shown={hint_shown}"))

        pg.wait_for_timeout(3400)  # past the old 3 s reveal
        d = moved(base, pg.evaluate(PROBE))
        results.append(check("...and nothing moves in the seconds that follow",
                             not d, f"moved={d}"))

        # --- pin 1: the wake progress bar ---------------------------------
        # Driven through the class the countdown actually sets, so the pin
        # tracks the real mechanism rather than a test-only shortcut.
        pg.evaluate("document.getElementById('powerProgress').classList.add('active')")
        pg.wait_for_timeout(150)
        d = moved(base, pg.evaluate(PROBE))
        results.append(check("the wake progress bar does not move the page",
                             not d, f"moved={d}"))

        pg.evaluate("document.getElementById('powerProgress').classList.remove('active')")
        pg.wait_for_timeout(150)
        d = moved(base, pg.evaluate(PROBE))
        results.append(check("...nor when it goes away", not d, f"moved={d}"))

        # --- pin 2: promoting the manual-wake link ------------------------
        # Both promotions: orange "warn" (relay down while the home is up) and
        # red "promoted" (home down too). They differ in type and colour, and
        # must differ in nothing else.
        pg.evaluate("relayReachable=false;isOnline=true;setFallbackState()")
        pg.wait_for_timeout(400)
        d = moved(base, pg.evaluate(PROBE))
        results.append(check("the orange relay warning does not move the page",
                             not d, f"moved={d}"))

        pg.evaluate("isOnline=false;setFallbackState()")
        pg.wait_for_timeout(400)
        d = moved(base, pg.evaluate(PROBE))
        results.append(check("...nor the red promoted link", not d, f"moved={d}"))

        # --- pin 3: both at once ------------------------------------------
        # The real wake-that-failed sequence: countdown running AND the link
        # promoted. Two reservations that each work alone could still add up.
        pg.evaluate("document.getElementById('powerProgress').classList.add('active')")
        pg.wait_for_timeout(150)
        d = moved(base, pg.evaluate(PROBE))
        results.append(check("a failed wake (bar + promoted link) does not move the page",
                             not d, f"moved={d}"))

        # --- pin 4: the promotion is still VISIBLE ------------------------
        # Positive control. Reserving space could be "achieved" by making the
        # promotion do nothing at all, which would pass every pin above while
        # deleting the feature. The promoted link must still be bigger type at
        # full opacity, in the alarm colour.
        style = pg.evaluate(
            """() => {
                 const s = getComputedStyle(document.querySelector('#fallbackLink'));
                 const a = getComputedStyle(document.querySelector('#fallbackLinkA'));
                 return {fs: parseFloat(s.fontSize), op: parseFloat(s.opacity), col: a.color};
               }""")
        results.append(check("the promotion still reads as an alarm",
                             style["fs"] >= 13 and style["op"] > 0.95
                             and style["col"] != "rgb(107, 113, 148)",
                             f"style={style}"))

        # --- pin 5: the controls still fit a small phone ------------------
        # Reserving space costs height (~33 px at 360x640), so pin what the
        # user must reach without scrolling: the tile, the power button and the
        # manual-wake link. NOT the footer — it sat below the fold on a 640 px
        # screen before this change too (measured: bottom=681 on v8.60), so
        # asserting it would pin something that was never true.
        pg.set_viewport_size({"width": 360, "height": 640})
        pg.wait_for_timeout(200)
        box = pg.evaluate(
            """() => {
                 const b = s => Math.round(
                   document.querySelector(s).getBoundingClientRect().bottom);
                 return {card: b('#statusCard'), btn: b('#powerBtn'),
                         link: b('#fallbackLink')};
               }""")
        results.append(check("tile, button and manual link fit a 360x640 screen",
                             max(box.values()) <= 640, f"bottoms={box}"))

        b.close()

    print()
    ok = all(results)
    print("ALL PINS HOLD" if ok else "AT LEAST ONE PIN FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
