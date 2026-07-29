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

# Widths: the phone's own CSS width is DIVIDED by Android's *display size*
# setting (~/1.3 on "très grand"), so a 360 px phone renders at ~277 px. Yann is
# on "grand" and his mother on "très grand" — 256/280 are her class, not an
# exotic device. 320 = "grande" on a 412 px phone, 360 = Pixel-class at default.
WIDTHS = [256, 280, 300, 320, 360, 384, 412]
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
    ('Statut inconnu', 'relais injoignable'),   # v8.65 — relay silent, no verdict
]

TOASTS = [
    '⏳ Serveur démarré — patiente',
    '⏳ Réveil en cours — patiente',
    '⚠ Serveur éteint — allume-le',
    '⚡ Demande de réveil envoyée',
    '⚠ Pas démarré — réessaie',
    '⚠ Relais injoignable',
    '⚠ Relais : serveur introuvable',
    '⚠ Trop d\'essais — patiente',
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
MEASURE_BOX = """(args) => {
  document.getElementById('statusLabel').textContent = args[0];
  document.getElementById('statusSub').textContent = args[1];
  document.getElementById('powerSection').style.display = 'flex';
  const c = document.getElementById('statusCard').getBoundingClientRect();
  const b = document.getElementById('powerSection').getBoundingClientRect();
  return {card: Math.round(c.height), btn: Math.round(b.top)};
}"""

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
    failures, notes, shifts = [], [], []
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
                # Two lines is the readable ceiling for a toast shown 4,5 s —
                # but below 300 px CSS (Android "très grand" display size) even
                # short copy needs three, and cutting every message to fit 256 px
                # would cost clarity on the widths everyone else uses. So the
                # ceiling follows the viewport instead of being one number.
                max_lines = 2 if width > 300 else 3
                # The <=300 px wrap adds a second line, so the card must RESERVE
                # it — otherwise this fix reintroduces exactly the layout shift
                # v8.61 removed (the power button moving under the thumb).
                # Positive control: a sub long enough to take a third line MUST
                # move the button, or this pin is measuring nothing.
                probe = [page.evaluate(MEASURE_BOX, [lab, sub]) for lab, sub in TILE_STATES]
                probe.append(page.evaluate(MEASURE_BOX, ['Hors ligne', 'x ' * 40]))
                if width <= 300 and len({b['btn'] for b in probe}) == 1:
                    print(f'[control FAILED] w={width}: a 3-line sub did not move the button — '
                          'this pin cannot detect a layout shift.')
                    shifts.append((width, ['control'], ['control']))
                boxes = [page.evaluate(MEASURE_BOX, [lab, sub]) for lab, sub in TILE_STATES]
                heights = sorted({b['card'] for b in boxes})
                tops = sorted({b['btn'] for b in boxes})
                if len(heights) > 1 or len(tops) > 1:
                    shifts.append((width, heights, tops))
                for text in TOASTS:
                    r = page.evaluate(MEASURE_TOAST, text)
                    if r['lines'] > max_lines:
                        notes.append((width, scale, text, r['lines']))
                ctx.close()
        b.close()

    print('=' * 72)
    if shifts:
        print('CARD HEIGHT NOT RESERVED (the wrap under 300 px moves what is below):\n')
        for width, heights, tops in shifts:
            print(f'{width:>4}  card heights={heights}  button top={tops}')
        print()
    if failures:
        print('TRUNCATED (scrollWidth > clientWidth — the family sees an ellipsis):\n')
        print(f"{'w':>4} {'scale':>5} {'element':<12} {'over':>5}  text")
        for width, scale, eid, text, over, _ in failures:
            print(f'{width:>4} {scale:>5} {eid:<12} {over:>4}px  {text!r}')
    else:
        print('No tile label or sub is truncated at any width x font scale.')
    if notes:
        print('\nTOASTS over the readable ceiling (2 lines, 3 under 300 px):')
        for width, scale, text, lines in notes:
            print(f'{width:>4} {scale:>5} {lines} lines  {text!r}')
    print('=' * 72)
    return 1 if failures or notes or shifts else 0


if __name__ == '__main__':
    sys.exit(main())
