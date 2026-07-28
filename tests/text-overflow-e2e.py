#!/usr/bin/env python3
"""Truncation audit — every user-visible string, at the narrow widths and font
scales the family actually has.

Reported by Yann (2026-07-28): "les sous-titres souvent coupés". The mechanism
is not the copy, it is the box: .status-label and .status-sub are
`white-space:nowrap;overflow:hidden;text-overflow:ellipsis`, and .status-sub is
MONOSPACE 12px — the widest font in the app. So a sub that overflows is silently
cut with an ellipsis instead of wrapping, and nothing in the suite noticed.

Measures scrollWidth vs clientWidth (the only honest truncation signal — the
rendered text still reads fine in the DOM) for each string, across widths and
Android font scales. Reports the overflow in px so copy can be cut by the right
amount rather than by guesswork.
"""
import os, sys
from playwright.sync_api import sync_playwright

BASE = os.environ.get('PWA_BASE', 'https://jqh63.github.io/plex-jqh-omv/')

# Widths: 320 = smallest Android still in use, 360 = Pixel-class (the width the
# v8.13/v8.14 truncations were found at), 384/412 = large phones.
WIDTHS = [320, 360, 384, 412]
# ⚠️ NO font-scale axis, deliberately. The first version of this file varied
# documentElement.fontSize and reported the SAME overflow at every scale — the
# axis was inert, because all 32 font sizes in index.html are in px, none in
# rem. That is itself a finding (the family's Android font-size setting does
# nothing here), but a scale column that cannot move is worse than none: it
# would read as proof that large text is safe.

# (element id, label, sub) — every pair paintTile() can render, from app.js.
TILE_STATES = [
    ('En ligne', ''),
    ('En ligne', 'services en démarrage…'),
    ('Vérification...', 'interrogation du relais…'),
    ('Vérification...', 'nouvelle tentative…'),
    ('Démarrage…', 'réveil en cours'),
    ('Pas de connexion', 'vérifie ta connexion'),
    ('Éteint', 'réveil auto à 13h50'),
    ('Éteint', 'arrêt normal du serveur'),
    ('Hors ligne', "contacte l'administrateur"),
]

TOASTS = [
    '⏳ Serveur démarré — patiente',
    '⏳ Réveil en cours — patiente',
    "⚠ Serveur éteint — allume-le d'abord",
    '⚡ Demande de réveil envoyée',
    '⚠ Pas démarré — réessaie',
    '⚠ Relais injoignable',
    '⚠ Relais : serveur introuvable',
    '⚠ Trop d\'essais — patiente une minute',
    '⚠ Relais : accès refusé (config)',
    '✓ Serveur démarré avec succès',
]

MEASURE = """(args) => {
  const [label, sub] = args;
  document.getElementById('statusLabel').textContent = label;
  document.getElementById('statusSub').textContent = sub;
  const out = {};
  for (const id of ['statusLabel', 'statusSub']) {
    const el = document.getElementById(id);
    out[id] = {over: el.scrollWidth - el.clientWidth, w: el.clientWidth};
  }
  return out;
}"""

# Toasts wrap (no nowrap), so the failure mode is height, not ellipsis: more
# than two lines on a phone is unreadable in the 4.5 s it is shown.
# ⚠️ getBoundingClientRect().height includes PADDING — dividing it by the line
# height reported 3 lines for a 4-word toast on the first pass. Measure against
# a ONE-LINE baseline rendered in the same box instead, so the padding cancels.
MEASURE_TOAST = """(text) => {
  const t = document.getElementById('toast');
  t.classList.add('show');
  t.textContent = 'x';
  const one = t.getBoundingClientRect().height;
  const lh = parseFloat(getComputedStyle(t).lineHeight) ||
             parseFloat(getComputedStyle(t).fontSize) * 1.2;
  t.textContent = text;
  const h = t.getBoundingClientRect().height;
  return {lines: Math.round((h - one) / lh) + 1, w: Math.round(t.getBoundingClientRect().width)};
}"""


def main():
    failures, notes = [], []
    with sync_playwright() as p:
        b = p.chromium.launch()
        for width in WIDTHS:
            for scale in [1.0]:
                ctx = b.new_context(viewport={'width': width, 'height': 780})
                page = ctx.new_page()
                page.goto(BASE)
                # Reveal the main screen without provisioning a real config.
                page.evaluate("""() => {
                  document.getElementById('settingsScreen').style.display='none';
                  document.getElementById('mainScreen').style.display='block';
                }""")
                if scale != 1.0:
                    page.evaluate("(s) => {document.documentElement.style.fontSize=(16*s)+'px';"
                                  "document.body.style.zoom='';}", scale)
                for label, sub in TILE_STATES:
                    m = page.evaluate(MEASURE, [label, sub])
                    for eid, res in m.items():
                        if res['over'] > 0:
                            failures.append((width, scale, eid, label if eid == 'statusLabel' else sub,
                                             res['over'], res['w']))
                for text in TOASTS:
                    r = page.evaluate(MEASURE_TOAST, text)
                    if r['lines'] > 2:
                        notes.append((width, scale, text, r['lines']))
                ctx.close()
        b.close()

    print('=' * 72)
    if failures:
        print('TRUNCATED (scrollWidth > clientWidth — the family sees an ellipsis):\n')
        print(f"{'w':>4} {'scale':>5} {'element':<12} {'over':>5}  text")
        for width, scale, eid, text, over, _ in failures:
            print(f'{width:>4} {scale:>5} {eid:<12} {over:>4}px  {text!r}')
    else:
        print('No tile label or sub is truncated at any width x font scale.')
    if notes:
        print('\nTOASTS over two lines:')
        for width, scale, text, lines in notes:
            print(f'{width:>4} {scale:>5} {lines} lines  {text!r}')
    print('=' * 72)
    return 1 if failures or notes else 0


if __name__ == '__main__':
    sys.exit(main())
