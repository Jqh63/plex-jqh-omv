#!/usr/bin/env python3
"""
Pins that a provisioning LINK cannot silently repoint an already configured app
at another server (2026-09-27, claude-security candidate on `readUrlParams`).

Before the fix, `?host=evil.example&relay=https://evil.example` rebuilt the
stored config from scratch with no prompt: the Seerr/Plex tiles, the wake relay
and the rescue page all derive from those fields, so one forged link turned a
trusted home-screen icon into a Plex-login phishing page.

Rows:
  A. FIRST PROVISIONING   — no stored config: silent, adopted (positive control:
     a fix that prompted on every link would ruin the family's onboarding).
  B. SAME-SERVER REFRESH  — the desktop bookmark re-sends the same params on
     every open: must stay silent (the v8.79 case).
  C. HOST CHANGE, REFUSED — prompt shown, stored config untouched, params
     stripped from the address bar anyway.
  D. HOST CHANGE, ACCEPTED — prompt shown, new config adopted.
  E. RELAY CHANGE ONLY    — same host, other relay: still a server change.

Run: python3 tests/reprovision-confirm-e2e.py   (PWA_ENGINES=chromium to narrow)
"""

import functools
import http.server
import json
import os
import socketserver
import sys
import threading

from browser_guard import ensure as _ensure_browser

_ensure_browser()

from playwright.sync_api import sync_playwright  # noqa: E402

REPO = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))
HOME = "host=home.example&relay=https://relay.home.example&mac=AA:BB:CC:DD:EE:01"
EVIL = "host=evil.example&relay=https://relay.evil.example"
OTHER_RELAY = "host=home.example&relay=https://relay.evil.example"

failures = []


def check(engine, row, ok, detail=""):
    print(f"  [{engine}] {'ok  ' if ok else 'FAIL'} {row}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{engine}: {row} {detail}")


def _serve():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    handler = functools.partial(Quiet, directory=REPO)
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run(engine, pw, base):
    browser = getattr(pw, engine).launch()

    def scenario(first, second, answer):
        """Loads `first` (if any) then `second` in one profile; returns
        (dialog messages seen on the 2nd load, stored config, final URL)."""
        ctx = browser.new_context(service_workers="block")
        # Nothing but the local server: the app's probes must not leave the box.
        ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
        page = ctx.new_page()
        seen = []

        def on_dialog(d):
            seen.append(d.message)
            d.accept() if answer else d.dismiss()

        if first:
            page.goto(f"{base}/index.html?{first}", wait_until="load")
            seen.clear()
        page.on("dialog", on_dialog)
        page.goto(f"{base}/index.html?{second}", wait_until="load")
        cfg = json.loads(page.evaluate("localStorage.getItem('plex-jqh-omv-cfg')") or "{}")
        url = page.url
        ctx.close()
        return seen, cfg, url

    seen, cfg, _ = scenario(None, HOME, answer=True)
    check(engine, "A first provisioning is silent", not seen, f"dialogs={seen}")
    check(engine, "A first provisioning is adopted", cfg.get("host") == "home.example", f"cfg={cfg}")

    seen, cfg, _ = scenario(HOME, HOME, answer=False)
    check(engine, "B same-server refresh is silent", not seen, f"dialogs={seen}")

    seen, cfg, url = scenario(HOME, EVIL, answer=False)
    check(engine, "C host change prompts", len(seen) == 1 and "evil.example" in seen[0], f"dialogs={seen}")
    check(engine, "C refused keeps the stored server",
          cfg.get("host") == "home.example" and cfg.get("relay") == "https://relay.home.example",
          f"cfg={cfg}")
    check(engine, "C refused still strips the params", "?" not in url, f"url={url}")

    seen, cfg, _ = scenario(HOME, EVIL, answer=True)
    check(engine, "D accepted adopts the new server",
          len(seen) == 1 and cfg.get("host") == "evil.example", f"dialogs={seen} cfg={cfg}")

    seen, cfg, _ = scenario(HOME, OTHER_RELAY, answer=False)
    check(engine, "E relay-only change prompts",
          len(seen) == 1 and cfg.get("relay") == "https://relay.home.example", f"dialogs={seen} cfg={cfg}")

    browser.close()


def main():
    engines = os.environ.get("PWA_ENGINES", "chromium,webkit").split(",")
    srv = _serve()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with sync_playwright() as pw:
        for engine in engines:
            run(engine.strip(), pw, base)
    srv.shutdown()
    if failures:
        print(f"\nFAIL — {len(failures)} row(s)")
        sys.exit(1)
    print("\nPASS")


if __name__ == "__main__":
    main()
