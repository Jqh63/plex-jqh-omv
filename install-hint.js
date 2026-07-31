// Decides BEFORE the first paint whether the "Astuce : ajouter à l'écran
// d'accueil" banner takes part in the layout.
//
// Reported by the user: the banner "décale le centrage vertical". `body` is a
// centred flex column, so a block revealed AFTER the page is painted re-centres
// everything above it — the tile and the power button visibly jump. The old
// code revealed it from a 3 s setTimeout in app.js, i.e. always after paint.
//
// Two constraints shape this file:
//   * it must run in <head>, before the body is parsed — so it can only set
//     classes on <html>; the banner's text variant is picked by CSS from
//     `hint-ios`, not by touching an element that does not exist yet;
//   * CSP is `script-src 'self'` — an inline <script> would be blocked, hence a
//     file of its own rather than three lines in the page.
//
// No space is reserved: the banner is either in the layout from the very first
// paint, or never. Installed apps (the family's normal case) never see it.
(function () {
  try {
    var nav = window.navigator;
    // navigator.standalone is the iOS-only legacy signal; display-mode covers
    // Android and modern iOS. Either one means "already installed".
    if (nav.standalone) return;
    if (window.matchMedia && window.matchMedia('(display-mode:standalone)').matches) return;
    var d = document.documentElement;
    d.className += ' hint-visible';
    // iPadOS 13+ reports as "Macintosh" — the touch points tell it apart, so a
    // family member on an iPad gets the share-sheet wording, not Chrome's menu.
    var ua = nav.userAgent || '';
    if (/iPad|iPhone|iPod/.test(ua) || (/Macintosh/.test(ua) && nav.maxTouchPoints > 1))
      d.className += ' hint-ios';
  } catch (e) { /* no hint is better than a broken page */ }
})();
