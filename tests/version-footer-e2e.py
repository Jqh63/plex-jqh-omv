#!/usr/bin/env python3
"""
Pins the version label the family actually SEES (`#footerVersion`, bottom of the
app) and the parser behind it.

Why this suite exists (2026-07-29): the footer had ZERO coverage. Asked whether
the version was visible anywhere, I grepped, concluded "nowhere in the UI" and
said so — wrong, and it took Yann to correct me. A rendered string with no pin is
a string nobody is checking.

Two layers, on purpose:

  A. PARSER — `pickCacheLabel(names)` (version.js) evaluated in the page against
     synthetic cache-name lists. Covers what a live run cannot stage: the update
     beat where the OLD and the NEW cache coexist, an unrelated cache sitting
     next to ours, several dated caches, garbage. Runs on file://.

  B. RENDER — the real thing over http:// (a service worker needs a real origin;
     `file://` cannot register one), waiting for the SW to activate and asserting
     the footer reads `v<gen> · <date>`. Layer A can pass while the footer stays
     "—" — that is exactly the class of bug the render pins exist for.

  C. RULES (2026-11-07) — `versionState` (✦, 24 h) and `latestState` (✓/⟳)
     evaluated on synthetic inputs.
  D. MARKS — rendered footer with the `sw.js` re-read replaced in the page:
     same name ⇒ ✓ alone, newer ⇒ ⟳, offline ⇒ no mark; ✦ after a label change,
     and a tap shows the detail.
  E. CONTRAST — ✓ and ✦ forced visible, >= 4.5:1 on index and fallback.
  F. REAL SERVER — a temp copy served, sw.js rewritten after the SW took
     control: the page must read the NEW name and sw.js must not sit in the SW
     cache. D simulates the server inside the page and could not see the SW
     serving sw.js from its cache (pock#68 — same trap, same pin).

Run: python3 tests/version-footer-e2e.py
"""

import http.server
import os
import re
import shutil
import socketserver
import subprocess
import tempfile
import sys
import threading

# Fail early and legibly if the engines' system libs are gone (a sandbox
# upgrade wipes /usr/lib while the binaries persist). No-op when healthy.
from browser_guard import ensure as _ensure_browser

_ensure_browser()

REPO = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))
LABEL_RE = re.compile(r"^v\d+ · \d{4}-\d{2}-\d{2}[a-z]?$")

# (names, expected label, why) — the "why" is the point of each row.
PARSER_CASES = [
    ([], None, "no cache at all → no claim (the placeholder stays)"),
    (["plex-jqh-omv-v8-2026-07-29a"], "v8 · 2026-07-29a", "nominal"),
    (["plex-jqh-omv-v8-2026-07-29a", "plex-jqh-omv-v8-2026-07-28a"],
     "v8 · 2026-07-29a", "two dated caches → the NEWER one wins"),
    (["plex-jqh-omv-v8-2026-07-29b", "plex-jqh-omv-v8-2026-07-29a"],
     "v8 · 2026-07-29b", "same day, second deploy → the letter breaks the tie"),
    # THE transition case: the update beat, old + new alive together. A parser
    # that just took the first match, or that ranked lexically without favouring
    # the dated format, would print the legacy label here — i.e. tell the user
    # they are still on the old build while they are on the new one.
    (["plex-jqh-omv-v8.66", "plex-jqh-omv-v8-2026-07-29a"],
     "v8 · 2026-07-29a", "legacy + dated coexist → the dated format wins"),
    (["plex-jqh-omv-v8.66"], "v8.66",
     "legacy alone → still readable (only during the transition update)"),
    (["plex-jqh-omv-v8.9", "plex-jqh-omv-v8.66"], "v8.66",
     "two legacy caches → zero-padded, so .66 beats .9 (string sort would not)"),
    (["some-other-app-v9-2026-08-01a"], None, "another app's cache → ignored"),
    (["plex-jqh-omv-garbage", "plex-jqh-omv"], None, "unparseable → no claim"),
]


H = 3600 * 1000
NOW = 1_000_000_000_000
# (raw stored, label, now, expected {store, updated}, why)
STATE_CASES = [
    (None, "v8 · A", NOW, {"store": {"label": "v8 · A"}, "updated": None},
     "first visit → remember, no ✦"),
    ("{bad", "v8 · A", NOW, {"store": {"label": "v8 · A"}, "updated": None},
     "corrupt storage → treated as a first visit"),
    ('{"label":"v8 · A"}', "v8 · B", NOW,
     {"store": {"label": "v8 · B", "prev": "v8 · A", "at": NOW},
      "updated": {"prev": "v8 · A", "at": NOW}}, "label changed → ✦ with « avant »"),
    ('{"label":"v8 · B","prev":"v8 · A","at":%d}' % (NOW - 23 * H), "v8 · B", NOW,
     {"store": None, "updated": {"prev": "v8 · A", "at": NOW - 23 * H}},
     "same label, 23 h later → ✦ still shown"),
    ('{"label":"v8 · B","prev":"v8 · A","at":%d}' % (NOW - 25 * H), "v8 · B", NOW,
     {"store": None, "updated": None}, "same label, 25 h later → ✦ gone"),
]
A, B = "plex-jqh-omv-v8-2026-11-07a", "plex-jqh-omv-v8-2026-11-07b"
LATEST_CASES = [
    (A, A, "latest", "server == running → ✓"),
    (A, B, "newer", "server newer → ⟳"),
    (B, A, None, "server older (CDN behind) → no verdict"),
    (A, None, None, "server unreadable → no verdict"),
    (None, A, None, "no cache yet → no verdict"),
    (A, "other-app-v9-2026-11-07a", None, "foreign name → no verdict"),
]

# Replaces the answer of fetch('sw.js', {cache:'no-store'}) — the ✓/⟳ check —
# without touching SW registration (which does not go through window.fetch).
FAKE_SERVER = """
const realFetch = window.fetch;
window.fetch = function (u, o) {
  if (String(u).indexOf('sw.js') >= 0 && o && o.cache === 'no-store' && window.__srv) {
    if (window.__srv === 'OFFLINE') return Promise.reject(new TypeError('offline'));
    return Promise.resolve(new Response("var CACHE = '" + window.__srv + "';"));
  }
  return realFetch.apply(this, arguments);
};
"""
VISIBLE = "s => { const e = document.querySelector(s); return !!e && !e.hidden; }"
RATIO = """id => { const P = s => s.match(/[\\d.]+/g).map(Number);
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92
                     : ((v + 0.055) / 1.055) ** 2.4; };
                     return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  let bg = [0, 0, 0];
  for (let x = document.getElementById(id); x; x = x.parentElement) {
    const c = P(getComputedStyle(x).backgroundColor);
    if (c.length < 4 || c[3] > 0) { bg = c; break; } }
  const a = lum(P(getComputedStyle(document.getElementById(id)).color)), b = lum(bg);
  return (Math.max(a, b) + .05) / (Math.min(a, b) + .05); }"""
PAGES = ["index.html?host=example.invalid&mac=AA:BB:CC:DD:EE:FF", "fallback.html"]
SEEN = "plex-jqh-omv-version-seen"


def _check(issues, ok, msg):
    print(f"  [{'PASS' if ok else 'FAIL'}] {msg}")
    if not ok:
        issues.append(msg)


def _serve(directory):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = lambda *a, **kw: Quiet(*a, directory=directory, **kw)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright absent — pip install playwright && playwright install")
        return 2

    issues = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            print(f"chromium ne se lance pas ({e}) — suite ignorée")
            return 2

        # ---- A. parser ----------------------------------------------------
        page = browser.new_page()
        page.goto("file://" + os.path.join(REPO, "index.html"), wait_until="load")
        for names, expected, why in PARSER_CASES:
            got = page.evaluate("names => window.pickCacheLabel(names)", names)
            ok = got == expected
            print(f"  [{'PASS' if ok else 'FAIL'}] parser: {why}\n"
                  f"         {names} → {got!r} (attendu {expected!r})")
            if not ok:
                issues.append(f"parser {why}: {got!r} != {expected!r}")
        page.close()

        # ---- B. render ----------------------------------------------------
        httpd, port = _serve(REPO)
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{port}/index.html?host=example.invalid"
                      "&mac=AA:BB:CC:DD:EE:FF", wait_until="load")
            # The SW must activate and precache before a cache name exists.
            try:
                page.wait_for_function(
                    "() => { const el = document.getElementById('footerVersion');"
                    " return el && el.textContent.trim() !== '—'; }", timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            shown = page.locator("#footerVersion").inner_text().strip()
            ok = bool(LABEL_RE.match(shown))
            print(f"  [{'PASS' if ok else 'FAIL'}] render: le pied de page affiche "
                  f"la generation + la date → {shown!r}")
            if not ok:
                issues.append(f"footer rendu = {shown!r}, attendu 'v<gen> · <date>'")
            # It must match what sw.js actually declares — a footer that renders a
            # plausible-but-wrong label is the drift this whole scheme removes.
            declared = re.search(r"var CACHE = '([^']+)'",
                                 open(os.path.join(REPO, "sw.js")).read()).group(1)
            m = re.search(r"-v(\d+)-(\d{4}-\d{2}-\d{2}[a-z]?)$", declared)
            if not m:
                issues.append(f"sw.js CACHE {declared!r} ne suit pas la convention "
                              "plex-jqh-omv-v<gen>-<YYYY-MM-DD><lettre?>")
                print(f"  [FAIL] convention: CACHE = {declared!r}")
            else:
                want = f"v{m.group(1)} · {m.group(2)}"
                ok2 = shown == want
                print(f"  [{'PASS' if ok2 else 'FAIL'}] accord sw.js ↔ pied de page: "
                      f"{shown!r} vs {want!r}")
                if not ok2:
                    issues.append(f"footer {shown!r} != sw.js {want!r}")
            ctx.close()

            # ---- C. rules -------------------------------------------------
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{port}/fallback.html", wait_until="load")
            for raw, label, now, want, why in STATE_CASES:
                got = page.evaluate("a => versionState(a[0], a[1], a[2])", [raw, label, now])
                _check(issues, got == want, f"rule ✦: {why} → {got}")
            for cur, srv, want, why in LATEST_CASES:
                got = page.evaluate("a => latestState(a[0], a[1])", [cur, srv])
                _check(issues, got == want, f"rule ✓/⟳: {why} → {got!r}")
            ctx.close()

            # ---- D. marks against a simulated server ----------------------
            for pg in PAGES:
                for srv, ok_, new_ in (("SAME", True, False), ("plex-jqh-omv-v8-2099-01-01a", False, True),
                                       ("OFFLINE", False, False)):
                    ctx = browser.new_context()
                    ctx.add_init_script(FAKE_SERVER + f"window.__srv = {srv!r};"
                                        .replace("'SAME'", "'" + declared + "'"))
                    page = ctx.new_page()
                    # Only index.html registers the SW: reach fallback the way a
                    # phone does, once the app has installed it.
                    page.goto(f"http://127.0.0.1:{port}/{PAGES[0]}", wait_until="load")
                    page.wait_for_function("() => navigator.serviceWorker.controller", timeout=20000)
                    page.goto(f"http://127.0.0.1:{port}/{pg}", wait_until="load")
                    page.wait_for_timeout(1500)
                    got = (page.evaluate(VISIBLE, "#vLatest"), page.evaluate(VISIBLE, "#vNewer"),
                           page.evaluate(VISIBLE, "#vUpdated"))
                    _check(issues, got == (ok_, new_, False),
                           f"marks {pg.split('?')[0]} server={srv[:12]} → ✓⟳✦ {got}")
                    if new_:
                        t = page.get_attribute("#vNewer", "title")
                        _check(issues, t == "Version plus récente publiée : v8 · 2099-01-01a"
                               " — rechargement automatique, appuyer pour recharger maintenant",
                               f"⟳ detail → {t!r}")
                        page.evaluate("window.__beforeReload = 1")
                        try:
                            with page.expect_navigation(timeout=10000):
                                page.click("#vNewer")
                        except Exception:  # noqa: BLE001
                            pass
                        _check(issues, page.evaluate("window.__beforeReload") is None,
                               "tap ⟳ → page reloaded")
                    if ok_:
                        page.click("#vLatest")
                        d = page.locator(".v-detail")
                        _check(issues, d.is_visible() and d.inner_text().startswith(
                            "Dernière version publiée (vérifié à "), "tap ✓ → detail shown")
                        page.click("#vLatest")
                        _check(issues, not d.is_visible(), "second tap → detail hidden")
                    ctx.close()

            # ---- D'/E. ✦ after a label change + contrast ------------------
            for pg in PAGES:
                ctx = browser.new_context()
                ctx.add_init_script(FAKE_SERVER + f"window.__srv = '{declared}';")
                page = ctx.new_page()
                page.goto(f"http://127.0.0.1:{port}/{PAGES[0]}", wait_until="load")
                page.wait_for_function("() => navigator.serviceWorker.controller", timeout=20000)
                page.goto(f"http://127.0.0.1:{port}/{pg}", wait_until="load")
                page.wait_for_function(f"() => localStorage.getItem('{SEEN}')", timeout=20000)
                page.evaluate(f"localStorage.setItem('{SEEN}', JSON.stringify({{label: 'v7 · old'}}))")
                page.reload(wait_until="load")
                try:
                    page.wait_for_selector("#vUpdated:not([hidden])", timeout=10000)
                    page.wait_for_selector("#vLatest:not([hidden])", timeout=10000)
                except Exception:  # noqa: BLE001
                    pass
                t = page.get_attribute("#vUpdated", "title") or ""
                _check(issues, t.startswith("Mis à jour le ") and t.endswith("— avant : v7 · old"),
                       f"✦ {pg.split('?')[0]} → {t!r}")
                for mid in ("vUpdated", "vLatest"):
                    r = page.evaluate(RATIO, mid)
                    _check(issues, r >= 4.5, f"contrast {mid} {pg.split('?')[0]} → {r:.2f}")
                ctx.close()
        finally:
            httpd.shutdown()

        # ---- F. real server, sw.js rewritten mid-flight -------------------
        with tempfile.TemporaryDirectory() as tmp:
            files = subprocess.run(["git", "-C", REPO, "ls-files"], capture_output=True,
                                   text=True, check=True).stdout.split()
            for f in files:
                if not f.startswith(("tests/", "relay/", ".")):
                    os.makedirs(os.path.dirname(os.path.join(tmp, f)) or tmp, exist_ok=True)
                    shutil.copy(os.path.join(REPO, f), os.path.join(tmp, f))
            # The CSP says `connect-src https:` — right in production (same-origin
            # https), but this server is http. Widen ONLY the temp copy; the
            # production read is checked on the public URL after deploy.
            for name in ("index.html", "fallback.html"):
                f = os.path.join(tmp, name)
                txt = open(f).read().replace("connect-src https:", "connect-src 'self' https:")
                open(f, "w").write(txt)
            httpd, port = _serve(tmp)
            try:
                ctx = browser.new_context()
                page = ctx.new_page()
                page.goto(f"http://127.0.0.1:{port}/{PAGES[0]}", wait_until="load")
                page.wait_for_function("() => navigator.serviceWorker.controller", timeout=20000)
                page.reload(wait_until="load")
                page.wait_for_function("() => { const e = document.getElementById('vLatest');"
                                       " return e && !e.hidden; }", timeout=20000)
                sw = os.path.join(tmp, "sw.js")
                txt = open(sw).read().replace(declared, "plex-jqh-omv-v8-2099-01-01a")
                open(sw, "w").write(txt)
                seen = page.evaluate("""() => fetch('sw.js', {cache: 'no-store'}).then(r => r.text())
                                          .then(t => (/var CACHE = '([^']+)'/.exec(t) || [])[1])""")
                _check(issues, seen == "plex-jqh-omv-v8-2099-01-01a", f"real server: page re-reads {seen!r}")
                cached = page.evaluate("() => caches.keys().then(ks => Promise.all(ks.map(k =>"
                                       " caches.open(k).then(c => c.match('sw.js', {ignoreSearch: true})))))"
                                       ".then(rs => rs.some(Boolean))")
                _check(issues, not cached, "real server: sw.js absent from every SW cache")
                ctx.close()
            finally:
                httpd.shutdown()
        browser.close()

    print("=" * 60)
    print("ALL PASS" if not issues else "FAIL:\n  - " + "\n  - ".join(issues))
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
