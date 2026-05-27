// Static relay preconnect — loaded BEFORE app.js so the TCP+TLS handshake
// to the relay begins while app.js is still downloading and parsing. Cuts
// ~500-1500 ms off the first /status fetch on cold mobile radio.
//
// The relay URL is per-user (configured via ?relay= URL param or saved in
// localStorage by the settings UI), so this script reads it from those
// sources at page-parse time. CSP-clean: same-origin .js file, no inline
// scripts, no nonce/hash juggling. Tiny on purpose (no dependencies, no
// minifier needed) — visual audit in 30 s.
(function(){
  try {
    var relay = new URLSearchParams(location.search).get('relay');
    if (!relay) {
      var cfg = localStorage.getItem('plex-jqh-omv-cfg');
      if (cfg) relay = (JSON.parse(cfg) || {}).relay;
    }
    if (relay && /^https:\/\//.test(relay)) {
      var l = document.createElement('link');
      l.rel = 'preconnect';
      l.href = new URL(relay).origin;
      l.crossOrigin = 'anonymous';
      document.head.appendChild(l);
    }
  } catch (e) {}
})();
