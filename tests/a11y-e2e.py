#!/usr/bin/env python3
"""Accessibility pins for index.html.

Three things that were silently wrong and that no other suite looks at:

1. The power button announced a STATIC "Allumer le serveur" through a
   hardcoded aria-label, while its visible label went on to say "Réveil… 45 s"
   then "Serveur allumé". A screen-reader user was told to press a button that
   had already done its job. Now aria-labelledby points at the visible label,
   so the two can never drift — this test is what keeps them tied.

2. Two INFINITE animations (the checking dot's pulse, the wake halo's spin) ran
   regardless of the OS "reduce motion" setting, which exists precisely to stop
   that for people who get motion sickness. The information must survive the
   setting; only the movement stops.

3. No :focus-visible anywhere: every control gave feedback on :active only, and
   -webkit-tap-highlight-color:transparent removes even the platform default.
   Keyboard and iOS Switch Control users had no visible selection.

Run: python3 tests/a11y-e2e.py   (exit 0 = all pins hold)
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Same config as mobile-text-shots.py: the power section only renders when
# wolReady() is satisfied, which needs the relay and token too — a host+mac URL
# leaves the button legitimately hidden and the test looks broken.
URL = ("file://" + str(ROOT / "index.html")
       + "?host=myserver.example.com&relay=https://wol.example.com"
       + "&mac=AABBCCDDEEFF&token=demo&title=Plex+jqh+omv")


def check(page, label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main():
    results = []
    with sync_playwright() as p:
        b = p.chromium.launch()

        # --- 1. accessible name follows the visible label -------------------
        pg = b.new_page(viewport={"width": 360, "height": 780})
        pg.goto(URL)
        pg.wait_for_selector("#mainScreen", state="visible", timeout=10000)
        pg.wait_for_timeout(400)
        # No hardcoded aria-label: it would WIN over aria-labelledby and freeze
        # the announced name — the exact defect this pins.
        has_static = pg.evaluate(
            "!!document.getElementById('powerBtn').getAttribute('aria-label')")
        results.append(check(pg, "power button has no frozen aria-label",
                             not has_static))
        names = []
        for text in ("Allumer le serveur", "Réveil… 45 s", "Serveur allumé"):
            pg.evaluate("t => document.getElementById('powerLabel').textContent = t", text)
            pg.wait_for_timeout(60)
            names.append(pg.evaluate("""() => {
                const b = document.getElementById('powerBtn');
                const id = b.getAttribute('aria-labelledby');
                return id ? document.getElementById(id).textContent : null;
            }"""))
        results.append(check(pg, "accessible name tracks the visible label",
                             names == ["Allumer le serveur", "Réveil… 45 s", "Serveur allumé"],
                             " -> ".join(map(repr, names))))
        pg.close()

        # --- 2. reduced motion actually stops the infinite animations -------
        for reduce_motion in (False, True):
            ctx = b.new_context(viewport={"width": 360, "height": 780},
                                reduced_motion="reduce" if reduce_motion else "no-preference")
            pg = ctx.new_page()
            pg.goto(URL)
            pg.wait_for_selector("#mainScreen", state="visible", timeout=10000)
            pg.wait_for_timeout(300)
            pg.evaluate("() => document.getElementById('statusDot').className = 'status-dot checking'")
            pg.wait_for_timeout(120)
            dur = pg.evaluate("""() => getComputedStyle(
                document.getElementById('statusDot')).animationDuration""")
            # Under "reduce" the shared rule collapses every duration to ~0.
            # Parse SECONDS — do not test the string: the browser serialises
            # 0.01 ms as "1e-05s", so a startswith("0") check calls a working
            # fix a failure.
            secs = float(dur.rstrip("s").replace("ms", "")) if dur else 0.0
            stopped = secs < 0.05
            results.append(check(pg,
                                 f"checking dot pulse {'stopped' if reduce_motion else 'runs'}"
                                 f" (reduced-motion={reduce_motion})",
                                 stopped if reduce_motion else not stopped,
                                 f"animation-duration={dur}"))
            if reduce_motion:
                # The INFORMATION must survive: the dot is still visibly orange.
                opacity = float(pg.evaluate(
                    "() => getComputedStyle(document.getElementById('statusDot')).opacity"))
                results.append(check(pg, "dot stays visible without motion",
                                     opacity >= 0.5, f"opacity={opacity}"))
            ctx.close()

        # --- 3. focus-visible is defined and reachable ----------------------
        pg = b.new_page(viewport={"width": 360, "height": 780})
        pg.goto(URL)
        pg.wait_for_selector("#mainScreen", state="visible", timeout=10000)
        pg.wait_for_timeout(300)
        pg.keyboard.press("Tab")
        pg.wait_for_timeout(120)
        outline = pg.evaluate("""() => {
            const el = document.activeElement;
            if (!el || el === document.body) return null;
            const s = getComputedStyle(el);
            return {tag: el.tagName, width: s.outlineWidth, style: s.outlineStyle};
        }""")
        # Require OUR ring, not merely "something": Chromium draws a default
        # focus ring (outline-style:auto) even with no rule at all, so a
        # "style != none" check passes on the unfixed code and proves nothing.
        # outline-style:solid + 2px is what the :focus-visible rule sets, and
        # the UA default never produces it.
        ok = (bool(outline) and outline["style"] == "solid"
              and outline["width"] == "2px")
        results.append(check(pg, "keyboard focus shows OUR focus ring", ok, str(outline)))
        b.close()

    print("ALL PASS" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
