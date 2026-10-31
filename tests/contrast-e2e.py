#!/usr/bin/env python3
"""
WCAG AA contrast audit of every PWA page, with the OS in light AND dark mode.

Why (2026-10-31): the app is dark-only but declared no `color-scheme`, so
Chrome drew native controls (inputs, scrollbars) in the LIGHT theme; and the
dim text (`--text-dim #6b7194`) read at 2.2-4.1:1, made worse by stacked
opacities (`.section-title`, footer version). Nothing measured contrast, so it
went unnoticed. Ported from pock/tests/contrast.py.

For each page, each state and each OS scheme:
  - asserts `color-scheme` on :root contains `dark` (the palette is dark-only:
    a light OS must not get light native controls either);
  - computes the WCAG ratio of every visible text node against its effective
    background (alpha layers + ancestor opacity composited) and fails under
    AA: 4.5:1, or 3:1 for large text (>= 24px, or >= 18.66px bold).
Not covered (by construction): text over gradients/images, disabled controls
(WCAG-exempt), placeholders, SVG text, pseudo-element glyphs.

Network: everything off-origin is mocked (relay /status) or aborted, so the
page renders deterministic states and no request leaves the machine.

Run from the repo root:  python3 tests/contrast-e2e.py   (exit 0 = AA holds)
Single engine (chromium): computed colours do not depend on the engine.
"""

import json
import sys
import threading
import time
from contextlib import contextmanager
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from browser_guard import ensure as _ensure_browser

_ensure_browser()

from playwright.sync_api import sync_playwright  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT_DIR = Path(__file__).resolve().parent / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

# Placeholders only (public repo).
HOST = "test.example.com"
RELAY = "r.example.com"
QUERY = f"?host={HOST}&mac=AABBCCDDEEFF&relay=https://{RELAY}&token=x&apps=seerr,plexweb"

SETTINGS = "() => { showSettings(); }"

# page -> list of (state, relay verdict or None = everything aborted,
#                  selector to wait for, JS action). The wait matters: the
# status card dims itself in the unknown / no-network states, so a scan taken
# before the verdict lands audits a state the family never sees for long.
OPEN_DETAILS = "() => document.querySelectorAll('details').forEach(d => d.open = true)"
STATES = {
    "index.html": [
        ("up", "up", "#statusCard.online", None),
        ("down", "down", "#statusCard.offline, #statusCard.sleep", None),
        ("unknown", None, "#statusCard.unknown", None),
        ("nonet", "offline", "#statusCard.nonet", None),
        ("settings", "up", "#statusCard.online", SETTINGS),
    ],
    "fallback.html": [("params", None, None, None),
                      ("details-open", None, None, OPEN_DETAILS)],
    "debug.html": [("journal", None, None, None)],
}

SCAN = r"""() => {
  const P = s => (s.match(/[\d.]+/g) || []).map(Number);
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v/12.92 : ((v+0.055)/1.055)**2.4; };
                     return 0.2126*f(c[0]) + 0.7152*f(c[1]) + 0.0722*f(c[2]); };
  const alpha = c => c.length > 3 ? c[3] : 1;
  const root = P(getComputedStyle(document.documentElement).backgroundColor);
  const body = P(getComputedStyle(document.body).backgroundColor);
  const canvas = alpha(body) > 0 ? body.slice(0,3) : alpha(root) > 0 ? root.slice(0,3) : [255,255,255];
  const bgOf = el => {
    const layers = [];
    for (let e = el; e; e = e.parentElement) {
      const c = P(getComputedStyle(e).backgroundColor);
      if (c.length && alpha(c) > 0) { layers.push(c); if (alpha(c) >= 1) break; }
    }
    let base = (layers.length && alpha(layers.at(-1)) >= 1) ? layers.pop().slice(0,3) : canvas;
    for (let i = layers.length - 1; i >= 0; i--) { const c = layers[i], a = alpha(c);
      base = base.map((v,k) => v*(1-a) + c[k]*a); }
    return base;
  };
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    // Exempt like `disabled` (WCAG 1.4.3): a server-dependent tile while the
    // server is off — its click is blocked, the dimming IS the signal.
    if (el.closest('svg') || el.disabled || el.closest('.link-btn.server-dependent.offline')) continue;
    let text = [...el.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim())
                                 .map(n => n.textContent.trim()).join(' ');
    if (!text && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') &&
        !['checkbox','radio','range','color','hidden'].includes(el.type)) text = el.value;
    if (!text && el.tagName === 'SELECT') text = el.selectedOptions[0]?.text || '';
    if (!text) continue;
    const r = el.getBoundingClientRect(); if (!r.width || !r.height) continue;
    const cs = getComputedStyle(el); if (cs.visibility === 'hidden') continue;
    let op = 1; for (let e = el; e; e = e.parentElement) op *= parseFloat(getComputedStyle(e).opacity);
    if (op < 0.05) continue;
    const fg = P(cs.color), bg = bgOf(el), a = alpha(fg) * op;
    const mixed = fg.slice(0,3).map((v,k) => v*a + bg[k]*(1-a));
    const l1 = lum(mixed), l2 = lum(bg), ratio = (Math.max(l1,l2)+0.05) / (Math.min(l1,l2)+0.05);
    const size = parseFloat(cs.fontSize), bold = parseInt(cs.fontWeight) >= 700;
    // Tile pictograms are graphical objects (1.4.11: 3:1), not body text.
    const need = (size >= 24 || (bold && size >= 18.66) || el.classList.contains('link-icon')) ? 3 : 4.5;
    if (ratio < need) out.push(`${ratio.toFixed(2)} < ${need}  <${el.tagName.toLowerCase()} id="${el.id}" class="${el.className}"> "${text.slice(0,40)}" ${cs.color} op=${op.toFixed(2)}`);
  }
  return {scheme: getComputedStyle(document.documentElement).colorScheme, low: out};
}"""


def relay_body(verdict):
    return json.dumps({"up": verdict == "up", "stale": False, "age_s": 0,
                       "served_at": int(time.time())})


def router(origin, verdict):
    # A factory, not a default argument: Playwright passes the request as a
    # SECOND positional argument to a handler that accepts one, which silently
    # overwrote `verdict=` (every state then rendered as "relay unreachable").
    def handle(route):
        u = urlparse(route.request.url)
        if u.netloc == origin:
            return route.continue_()
        if verdict in ("up", "down") and u.netloc == RELAY and u.path == "/status":
            return route.fulfill(status=200, body=relay_body(verdict),
                                 headers={"Content-Type": "application/json",
                                          "Access-Control-Allow-Origin": "*"})
        return route.abort()
    return handle


@contextmanager
def serve_repo():
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(REPO_ROOT)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def main():
    failures = 0
    with serve_repo() as origin, sync_playwright() as p:
        browser = p.chromium.launch()
        for scheme in ("light", "dark"):
            for page_name, states in STATES.items():
                for state, verdict, wait_for, action in states:
                    ctx = browser.new_context(viewport={"width": 390, "height": 844},
                                              color_scheme=scheme,
                                              service_workers="block")

                    ctx.route("**/*", router(origin, verdict))
                    page = ctx.new_page()
                    page.goto(f"http://{origin}/{page_name}{QUERY}", wait_until="load")
                    if verdict == "offline":
                        ctx.set_offline(True)
                    if wait_for:
                        page.wait_for_selector(wait_for, state="attached", timeout=45000)
                    page.wait_for_timeout(800)
                    if action:
                        page.evaluate(action)
                        page.wait_for_timeout(500)
                    res = page.evaluate(SCAN)
                    name = f"{page_name.removesuffix('.html')}-{state}-{scheme}"
                    page.screenshot(path=str(SCREENSHOT_DIR / f"contrast-{name}.png"),
                                    full_page=True)
                    problems = list(res["low"])
                    if "dark" not in res["scheme"]:
                        problems.insert(0, f"color-scheme is '{res['scheme']}' — native "
                                           "controls render light on the dark palette")
                    print(("OK   " if not problems else "FAIL ") + name
                          + (f" ({len(problems)})" if problems else ""))
                    for line in problems:
                        print(f"       {line}")
                    failures += bool(problems)
                    ctx.close()
        browser.close()
    print(f"\n{failures} failing view(s)" if failures else "\nall views pass WCAG AA")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
