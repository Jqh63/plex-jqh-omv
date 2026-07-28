#!/usr/bin/env python3
"""Render pins for the v8.58 status-tile crossfade.

The defect: the card border and the dot glide (transition .5s / .4s) while the
label and sub were replaced by `textContent` in a single frame. For half a
second the tile read "Éteint" over a card already turning green — one change,
two halves moving at different speeds. paintTile() crosses the words over
inside that same window.

Why this file exists rather than an assertion bolted onto cold-radio-e2e.py:
every defect found on this tile in the last week was found by looking at the
RENDER, never by an assertion on state (see BACKLOG). So these pins measure
computed style over time, not textContent at rest.

Three pins:

1. `state change fades the text` — 80 ms into a change the text block must be
   part-way transparent AND still showing the OLD word. This is the pin that
   FAILS against v8.57 (opacity 1, new word already painted).
2. `the swap completes` — by 450 ms the new word is up and opacity is back to
   1. Guards the failure mode where a fade starts and never resolves, leaving
   the tile blank; v8.57 passes it, which is the point (it is a completeness
   guard, not a regression pin).
3. `an identical repaint does NOT fade` — the 8 s status poll re-enters
   setOnline/setOffline every cycle. Without the `tilePainted` no-op guard the
   nominal tile would blink every 8 s, i.e. the fix would have made the app
   WORSE in the state the family sees most. Negative assertion, so it has a
   positive control: delete the guard in app.js and this pin must go FAIL.

Run: python3 tests/tile-crossfade-e2e.py   (exit 0 = all pins hold)
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Same config shape as a11y-e2e.py: wolReady() needs relay + token too, else
# the power section stays hidden and the tile renders in a state no phone sees.
URL = ("file://" + str(ROOT / "index.html")
       + "?host=myserver.example.com&relay=https://wol.example.com"
       + "&mac=AABBCCDDEEFF&token=demo&title=Plex+jqh+omv")

# The text block, addressed WITHOUT #statusText on purpose: that id ships with
# the fix, so selecting it would make the test error out against the old code
# instead of failing it. This selector resolves in both versions.
TEXT_SEL = ".status-left > div:last-child"


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def sample(pg):
    return pg.evaluate(
        "sel => ({opacity: parseFloat(getComputedStyle(document.querySelector(sel)).opacity),"
        " label: document.getElementById('statusLabel').textContent})", TEXT_SEL)


def main():
    results = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 360, "height": 780})
        pg.goto(URL)
        pg.wait_for_selector("#mainScreen", state="visible", timeout=10000)

        # Settle on a known state first. The first paint is deliberately NOT
        # faded (nothing to cross over from), so pin 1 must be measured on a
        # LATER change — measuring the first one would pass for the wrong
        # reason.
        pg.evaluate("window.setOnline && setOnline(false)")
        pg.wait_for_timeout(700)
        before = sample(pg)
        results.append(check("baseline settled on 'En ligne'",
                             before["label"] == "En ligne" and before["opacity"] > 0.95,
                             f"label={before['label']!r} opacity={before['opacity']}"))

        # --- pin 1: the change fades, and the words wait for the fade -------
        pg.evaluate("setOffline()")
        pg.wait_for_timeout(80)
        mid = sample(pg)
        results.append(check("80 ms into a state change the text is fading",
                             mid["opacity"] < 0.9,
                             f"opacity={mid['opacity']}"))
        results.append(check("80 ms in, the OLD word is still on screen "
                             "(words and colour move together)",
                             mid["label"] == "En ligne",
                             f"label={mid['label']!r}"))

        # --- pin 2: it completes ------------------------------------------
        pg.wait_for_timeout(400)
        after = sample(pg)
        results.append(check("the swap completes: new word, full opacity",
                             after["label"] != "En ligne" and after["opacity"] > 0.95,
                             f"label={after['label']!r} opacity={after['opacity']}"))

        # --- pin 3: identical repaints must not blink ----------------------
        # Mirrors what the 8 s poll does: re-enter the same paint repeatedly.
        settled = sample(pg)
        dips = pg.evaluate(
            """async sel => {
                 const el = document.querySelector(sel);
                 let min = 1;
                 for (let i = 0; i < 3; i++) {
                   setOffline();
                   for (let j = 0; j < 12; j++) {
                     min = Math.min(min, parseFloat(getComputedStyle(el).opacity));
                     await new Promise(r => setTimeout(r, 20));
                   }
                 }
                 return min;
               }""", TEXT_SEL)
        results.append(check("an identical repaint does not fade "
                             "(no blink every 8 s in the nominal state)",
                             dips > 0.95,
                             f"min opacity over 3 repaints={dips} "
                             f"(state={settled['label']!r})"))

        b.close()

    print()
    if all(results):
        print(f"ALL PASS ({len(results)} pins)")
        return 0
    print(f"FAIL ({sum(1 for r in results if not r)}/{len(results)} pins)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
