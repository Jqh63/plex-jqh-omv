#!/usr/bin/env python3
"""Accessibility pins for index.html.

Four things that were silently wrong and that no other suite looks at:

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

4. The blanket reduced-motion rule ALSO froze the wake halo and snapped the boot
   progress bar to 100% — essential feedback (WCAG 2.3.3 exempts it), not the
   decorative motion the setting targets. The whole wake looked broken on a
   Windows PC with animations off (2026-08-01). §4 pins the exemption.

Run: python3 tests/a11y-e2e.py   (exit 0 = all pins hold)
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

# Fail early and legibly if the engines' system libs are gone (a sandbox
# upgrade wipes /usr/lib while the binaries persist). No-op when healthy.
from browser_guard import ensure as _ensure_browser

_ensure_browser()

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
        pg.close()

        # --- 4. ESSENTIAL wake feedback survives reduced motion -------------
        # Symmetric to §2: the checking-dot pulse is decorative and MUST stop,
        # but the wake halo spin and the boot progress bar carry information —
        # a frozen halo reads "broken", a bar the blanket rule snaps to 100%
        # reads "done" mid-boot. WCAG 2.3.3 exempts essential motion. On a
        # Windows PC with animations off the whole wake looked KO (2026-08-01).
        # These pins FAIL on the unfixed CSS (halo duration ~0, bar transition
        # collapsed to .01ms) and hold only with the exemption in place.
        for reduce_motion in (False, True):
            ctx = b.new_context(viewport={"width": 360, "height": 780},
                                reduced_motion="reduce" if reduce_motion else "no-preference")
            pg = ctx.new_page()
            pg.goto(URL)
            pg.wait_for_selector("#mainScreen", state="visible", timeout=10000)
            pg.wait_for_timeout(300)
            # Enter the wake look without firing a real POST: the .sent class is
            # what drives both the halo (:has) and, via startCountdown, the bar.
            pg.evaluate("""() => {
                wolSent = true; wolStartTime = Date.now();
                document.getElementById('powerBtn').className = 'power-btn sent';
                startCountdown(0);
            }""")
            pg.wait_for_timeout(120)
            halo = pg.evaluate("""() => getComputedStyle(
                document.querySelector('.power-ring'), '::after').animationDuration""")
            halo_secs = float(halo.rstrip("s").replace("ms", "")) if halo else 0.0
            # Same parse caveat as §2: 0.01ms serialises as "1e-05s".
            results.append(check(pg, "wake halo keeps spinning (essential motion)",
                                 halo_secs > 0.5, f"animation-duration={halo}"))
            bar = pg.evaluate("""() => getComputedStyle(
                document.getElementById('powerProgressBar')).transitionDuration""")
            bar_secs = float(bar.rstrip("s").replace("ms", "")) if bar else 0.0
            # The bar transition is the remaining ETA (~80s fallback): a real,
            # long fill, never the ~0 the blanket reduced-motion rule imposes.
            results.append(check(pg, "boot bar animates over the ETA (essential motion)",
                                 bar_secs > 1.0, f"transition-duration={bar}"))
            # The suspend-resync in onForeground() re-arms the SAME bar transition
            # on every focus/visibilitychange. It was missed by the first fix and
            # re-armed the bar WITHOUT !important, so on a desktop PC the first
            # focus after the wake snapped the bar to 100% mid-boot (the exact
            # symptom Yann saw, 2026-08-01). Fire the resync and re-assert: this
            # FAILS on the pre-fix app.js (duration collapses to ~1e-05s under
            # reduce) and holds only once the resync sets it inline !important.
            pg.evaluate("() => { if (typeof onForeground === 'function') { try { onForeground(); } catch (e) {} } }")
            pg.wait_for_timeout(120)
            bar2 = pg.evaluate("""() => getComputedStyle(
                document.getElementById('powerProgressBar')).transitionDuration""")
            bar2_secs = float(bar2.rstrip("s").replace("ms", "")) if bar2 else 0.0
            results.append(check(pg, "boot bar survives the onForeground resync (essential motion)",
                                 bar2_secs > 1.0, f"transition-duration={bar2}"))
            ctx.close()

        b.close()

    print("ALL PASS" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
