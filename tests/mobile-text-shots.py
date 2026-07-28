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
    ("toast-server-off", "⚠ Serveur éteint — allume-le d'abord", True),
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

        b.close()
    print("screenshots ->", OUT)

if __name__ == '__main__':
    sys.exit(main())
