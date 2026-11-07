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
  F. APPS INJECTION       — same host, `?apps=x@evil.example/`: prompts (F7),
     and even once accepted no tile may link outside the home host.
  G. BARE SAME HOST       — `?host=<same>` alone: silent AND keeps relay/mac
     (F9: it used to wipe them, hiding the wake button).
  H. STATUS CHANGE        — same host, `?status=evil.example`: prompts (F9).
  I. SETTINGS HOST CHANGE — saving another host in the settings drops the old
     home's `status`/`rescue` (they kept aiming the direct probe and the rescue
     link at the old home — simulated 2026-11-07).
  J. SETTINGS SAME HOST   — positive control: a same-host save keeps them.
  K. NO GREEN INHERITED   — after a host change (settings OR accepted link) the
     old home's cached green is not pre-painted on the new one; a same-host
     save still pre-paints it (positive control).

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
EVIL_APPS = "host=home.example&apps=seerr,x@evil.example/%3F"
BARE = "host=home.example"
EVIL_STATUS = "host=home.example&status=evil.example"

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
        hrefs = page.evaluate("Array.from(document.querySelectorAll('a[href]')).map(a => a.href)")
        scenario.hrefs = hrefs
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

    seen, cfg, _ = scenario(HOME, EVIL_APPS, answer=False)
    check(engine, "F apps injection prompts", len(seen) == 1, f"dialogs={seen}")
    seen, cfg, _ = scenario(HOME, EVIL_APPS, answer=True)
    evil = [h for h in scenario.hrefs if "evil.example" in h.split("?")[0].split("#")[0].split("/")[2]]
    check(engine, "F accepted: no tile links outside the home host", not evil, f"hrefs={evil}")

    seen, cfg, _ = scenario(HOME, BARE, answer=False)
    check(engine, "G bare same host is silent", not seen, f"dialogs={seen}")
    check(engine, "G bare same host keeps relay and mac",
          cfg.get("relay") == "https://relay.home.example" and cfg.get("mac") == "aabbccddee01",
          f"cfg={cfg}")

    seen, cfg, _ = scenario(HOME, EVIL_STATUS, answer=False)
    check(engine, "H status change prompts",
          len(seen) == 1 and not cfg.get("status"), f"dialogs={seen} cfg={cfg}")

    # I-K: settings path + cached verdict. Probes are aborted (offline box), so
    # any "online" paint can only come from the cache pre-paint.
    FULL = HOME + "&status=seerr.home.example&rescue=https://relay.home.example/r/x"
    UP = ("localStorage.setItem('plex-jqh-omv-status',"
          "JSON.stringify({up:true,relayOk:true,degraded:false,t:Date.now()}))")

    def paints(page):
        raw = page.evaluate("localStorage.getItem('plex-jqh-omv-paints')") or "[]"
        return [e.get("w") for e in json.loads(raw)]

    def via_settings(new_host):
        ctx = browser.new_context(service_workers="block")
        ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
        page = ctx.new_page()
        page.goto(f"{base}/index.html?{FULL}", wait_until="load")
        page.evaluate(UP)
        page.evaluate("localStorage.removeItem('plex-jqh-omv-paints')")
        page.evaluate("showSettings()")
        page.wait_for_timeout(300)
        page.fill("#cfgHost", new_host)
        page.evaluate("saveConfig()")
        page.wait_for_timeout(300)
        cfg = json.loads(page.evaluate("localStorage.getItem('plex-jqh-omv-cfg')"))
        w = paints(page)
        ctx.close()
        return cfg, w

    cfg, w = via_settings("other.example")
    check(engine, "I settings host change drops status/rescue",
          not cfg.get("status") and not cfg.get("rescue"), f"cfg={cfg}")
    check(engine, "K settings host change: no inherited green",
          "cache-prepaint-open" not in w, f"paints={w}")
    cfg, w = via_settings("home.example")
    check(engine, "J settings same host keeps status/rescue",
          cfg.get("status") == "seerr.home.example" and cfg.get("rescue"), f"cfg={cfg}")
    check(engine, "K same-host save still pre-paints (control)",
          "cache-prepaint-open" in w, f"paints={w}")

    ctx = browser.new_context(service_workers="block")
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
    page = ctx.new_page()
    page.goto(f"{base}/index.html?{HOME}", wait_until="load")
    page.evaluate(UP)
    page.evaluate("localStorage.removeItem('plex-jqh-omv-paints')")
    page.on("dialog", lambda d: d.accept())
    page.goto(f"{base}/index.html?{EVIL}", wait_until="load")
    w = paints(page)
    ctx.close()
    check(engine, "K accepted link host change: no inherited green",
          "cache-prepaint-open" not in w, f"paints={w}")

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
