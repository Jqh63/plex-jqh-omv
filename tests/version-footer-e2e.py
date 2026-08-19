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

Run: python3 tests/version-footer-e2e.py
"""

import http.server
import os
import re
import socketserver
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


def _serve(directory):
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=directory, **kw)
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
        finally:
            httpd.shutdown()
        browser.close()

    print("=" * 60)
    print("ALL PASS" if not issues else "FAIL:\n  - " + "\n  - ".join(issues))
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
