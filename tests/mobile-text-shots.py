#!/usr/bin/env python3
"""Ad-hoc screenshot helper — render the PWA at a narrow mobile width (360px)
and capture the screens / toasts / power-labels whose French strings were the
longest, to eyeball that nothing overflows or clips after the v8.9 shortening +
toast wrap. NOT part of the regression suite — a visual aid for one review."""
import os, sys, pathlib
from playwright.sync_api import sync_playwright

BASE = os.environ.get('PWA_BASE', 'file:///config/workspace/plex-jqh-omv/index.html')
# chromium (default) | webkit — webkit renders on the Safari engine but needs
# its system libs (see tests/README.md § Engines); falls back with a clear error.
ENGINE = os.environ.get('PWA_ENGINE', 'chromium')
OUT = pathlib.Path(__file__).parent / 'screenshots'
OUT.mkdir(exist_ok=True)
CFG = ('?host=myserver.example.com&relay=https://wol.example.com'
       '&mac=AABBCCDDEEFF&token=demo&title=Plex+jqh+omv')

# Longest toasts / labels we just shortened — render each in situ.
TOASTS = [
    ("toast-auth-refused", "⚠ Relais : accès refusé — réveil manuel ↓", True),
    ("toast-not-started", "⚠ Pas démarré — réessaie ou réveil manuel ↓", True),
    ("toast-relay-unreachable", "⚠ Relais injoignable — réveil manuel ↓", True),
    ("toast-wake-progress", "⏳ Réveil en cours — patiente", True),
    ("toast-server-off", "⚠ Serveur éteint — allume-le", True),
    ("toast-started-ok", "✓ Serveur démarré avec succès", False),
]

def main():
    with sync_playwright() as p:
        b = getattr(p, ENGINE).launch()
        # 360px = a common narrow Android width (Pixel-class); DPR 3 for crispness.
        ctx = b.new_context(viewport={'width': 360, 'height': 780}, device_scale_factor=3)
        page = ctx.new_page()
        page.goto(BASE + CFG)
        page.wait_for_selector('#mainScreen', state='visible', timeout=10000)
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / 'main-360.png'))

        # Power-label worst cases (set DOM directly — we test rendering, not logic).
        # v8.54: 'Allumer (relais incertain)' is gone with the state it framed —
        # the button now always carries the plain label. The longest strings the
        # tile can render moved to the STATUS SUB, so that is what needs the
        # narrow-phone shot now (both v8.13 and v8.14 had to cut copy that
        # wrapped on an S24 once Android font scaling kicked in).
        page.evaluate("""() => {
          document.getElementById('statusCard').className = 'status-card nonet';
          document.getElementById('statusDot').className = 'status-dot nonet';
          document.getElementById('statusLabel').textContent = 'Pas de connexion';
          document.getElementById('statusSub').textContent = 'vérifie ta connexion';
          // Inline style, like the app does — a class cannot hide this element
          // (it already carries an inline display; that is how the first cut of
          // v8.54 shipped a hide that did nothing, caught only on the render).
          document.getElementById('powerSection').style.display = 'none';
        }""")
        page.wait_for_timeout(200)
        page.screenshot(path=str(OUT / 'status-nonet-360.png'))

        page.evaluate("""() => {
          document.getElementById('statusCard').className = 'status-card offline';
          document.getElementById('statusDot').className = 'status-dot offline';
          document.getElementById('statusLabel').textContent = 'Hors ligne';
          document.getElementById('statusSub').textContent = "contacte l'administrateur";
          document.getElementById('powerSection').style.display = 'flex';
        }""")
        page.wait_for_timeout(200)
        page.screenshot(path=str(OUT / 'status-contact-admin-360.png'))

        page.evaluate("""() => {
          const l = document.getElementById('powerLabel');
          l.textContent = 'Démarrage un peu long — patiente…';
          l.className = 'power-label sent';
        }""")
        page.wait_for_timeout(200)
        page.screenshot(path=str(OUT / 'power-starting-360.png'))

        for name, msg, warn in TOASTS:
            page.evaluate("([m,w]) => window.showToast(m, w, 9000)", [msg, warn])
            page.wait_for_timeout(500)
            page.screenshot(path=str(OUT / f'{name}-360.png'))

        # v8.54 — the status card must keep the SAME height in every state.
        # This is an assertion, not a shot: v8.54 made the nominal green sub
        # empty, which collapsed the line and shrank the card 85 -> 69 px. The
        # green<->red<->degraded flips happen on an 8 s poll, so that was a
        # 16 px jump shifting the whole page under the user's thumb, several
        # times a minute. Screenshots would not have caught it — each one looks
        # fine on its own; only comparing them does.
        # The tallest case (label + sub + verdict age) is included on purpose:
        # it is what the reserved height has to be sized on, and it is the one
        # a "3 states look fine" check would miss.
        heights, centring = {}, {}
        for name, cls, lbl, sub, age in (
            ("green-nominal", "online", "En ligne", "", ""),
            ("green-degraded", "online", "En ligne", "services en démarrage…", ""),
            ("blue-off", "sleep", "Éteint", "réveil auto à 13h50", ""),
            ("red-unexpected", "offline", "Hors ligne", "contacte l'administrateur", ""),
            ("hollow-no-network", "nonet", "Pas de connexion", "vérifie ta connexion", ""),
            ("red-with-verdict-age", "offline", "Hors ligne", "contacte l'administrateur",
             "vérifié il y a 5 min"),
        ):
            page.evaluate("""([c,l,s,a]) => {
              document.getElementById('statusCard').className = 'status-card ' + c;
              document.getElementById('statusDot').className = 'status-dot ' + c;
              document.getElementById('statusLabel').textContent = l;
              document.getElementById('statusSub').textContent = s;
              document.getElementById('statusAge').textContent = a;
            }""", [cls, lbl, sub, age])
            page.wait_for_timeout(120)
            m = page.evaluate("""() => {
              const c = document.getElementById('statusCard').getBoundingClientRect();
              const l = document.getElementById('statusLabel').getBoundingClientRect();
              return {h: c.height, top: l.top - c.top, bottom: c.bottom - l.bottom};
            }""")
            heights[name] = round(m["h"], 1)
            centring[name] = (round(m["top"], 1), round(m["bottom"], 1))
        b.close()

    if len(set(heights.values())) != 1:
        print("FAIL status card height varies by state:", heights)
        return 1
    # v8.56 — height alone is not enough: v8.55 kept it constant by padding the
    # SUB line, which left a lone "En ligne" clinging to the top of the card
    # over ~48 px of dead space. When the label is the only line, it must sit
    # in the middle. Tolerance covers the label's line box, not a real offset.
    top, bottom = centring["green-nominal"]
    if abs(top - bottom) > 6:
        print(f"FAIL lone label not vertically centred: top={top} bottom={bottom}")
        return 1
    print(f"PASS status card height stable across {len(heights)} states "
          f"({next(iter(heights.values()))} px); lone label centred "
          f"(top={top} bottom={bottom})")
    print("screenshots ->", OUT)

if __name__ == '__main__':
    sys.exit(main())
