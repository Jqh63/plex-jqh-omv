#!/usr/bin/env python3
"""
Pins the VISUAL STATE of the settings fields — that a field the family cannot
edit does not paint like one they can.

Why this suite exists (2026-09-07). `cfgWindow` has been correctly locked since
v8 (`disabled` when the relay owns the window) and the hint said so, yet the
input rendered pixel-identical to an editable one: the sheet had a `:disabled`
rule for `.test-btn` and NONE for `.field input`. Measured first, on both
engines, before writing a line of fix: normal / disabled / readonly computed to
the exact same color, background, opacity and border on Chromium AND WebKit — so
this was never a per-engine default, and no amount of engine-sniffing would have
found it. These users are non-technical family: a field that looks editable and
is not produces exactly the doubt this UI exists to remove.

What is pinned, and why each row:
  A. LOCKED WINDOW  — readonly (not disabled: the value must stay selectable and
     present in the accessibility tree) AND visibly distinct from a live field.
  B. RELAY-DEPENDENT — the token and the connectivity test are inert with no
     relay URL, and become live on the keystroke that provides one.
  C. CONTROL POSITIVE — an ordinary field must stay fully normal. Without this
     row a rule that greyed out every input would pass the suite.
  D. IN-FLIGHT TEST — a keystroke must not re-enable a button mid-request.

Run: python3 tests/settings-field-states-e2e.py
"""

import http.server
import os
import socketserver
import sys
import threading

from browser_guard import ensure as _ensure_browser

_ensure_browser()

from playwright.sync_api import sync_playwright  # noqa: E402

REPO = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))
# Distinctness is asserted on VISIBLE properties only. An earlier version of this
# suite included `cursor`, and it passed against the unfixed code: a disabled input
# gets a different UA cursor with no rule at all. That is invisible on a phone --
# the device this family actually uses -- so the row proved nothing. Cursor is
# still pinned, separately, as a pointer affordance.
STYLE = ("e=>{const s=getComputedStyle(e);"
         "return {op:s.opacity,bs:s.borderStyle,bg:s.backgroundColor}}")
CURSOR = "e=>getComputedStyle(e).cursor"

failures = []


def style(pg, sel):
    """None when the element does not exist, so a missing hook fails ONE row
    instead of aborting the run before the second engine."""
    return pg.eval_on_selector(sel, STYLE) if pg.query_selector(sel) else None


def text(pg, sel):
    return pg.eval_on_selector(sel, "e=>e.textContent") if pg.query_selector(sel) else ""


def prop(pg, sel, js):
    return pg.eval_on_selector(sel, js) if pg.query_selector(sel) else None


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


def serve():
    os.chdir(REPO)
    h = http.server.SimpleHTTPRequestHandler
    httpd = socketserver.TCPServer(("127.0.0.1", 0), h)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def run(engine, port):
    print(f"\n=== {engine} ===")
    with sync_playwright() as p:
        b = getattr(p, engine).launch()
        pg = b.new_page()
        pg.goto(f"http://127.0.0.1:{port}/index.html")
        pg.wait_for_function("()=>typeof showSettings==='function'")

        # --- A. relay-owned window
        pg.evaluate(
            "()=>{config={host:'h.example',port:'9',relay:'https://r.example',"
            "winSrc:'relay',window:'13h50-00h10'};showSettings();}"
        )
        win = style(pg, "#cfgWindow")
        live = style(pg, "#cfgHost")
        check(pg.eval_on_selector("#cfgWindow", "e=>e.readOnly") is True,
              "A1 relay-owned window is readonly")
        check(pg.eval_on_selector("#cfgWindow", "e=>e.disabled") is False,
              "A2 ... and NOT disabled (stays selectable / in the a11y tree)")
        check(win != live, f"A3 ... and paints differently from a live field ({win} vs {live})")
        check(prop(pg, "#cfgWindow", CURSOR) != "text",
              "A3b ... and does not offer a text caret (pointer devices only)")
        check(pg.eval_on_selector("#cfgWindow", "e=>e.value") == "13h50-00h10",
              "A4 ... while still showing the relay value")

        # --- C. control positive: an ordinary field stays normal
        check(live["op"] == "1" and live["bs"] == "solid",
              "C1 an ordinary field is untouched (opacity 1, solid border)")

        # --- A'. window editable again when the relay does not own it
        pg.evaluate("()=>{config={host:'h.example',port:'9'};showSettings();}")
        check(pg.eval_on_selector("#cfgWindow", "e=>e.readOnly") is False,
              "A5 window is editable again when the relay does not own it")
        check(style(pg, "#cfgWindow") == live,
              "A6 ... and paints exactly like a live field")

        # --- B. relay-dependent fields
        check(pg.eval_on_selector("#cfgToken", "e=>e.disabled") is True,
              "B1 no relay -> token is inert")
        check(pg.eval_on_selector("#testRelayBtn", "e=>e.disabled") is True,
              "B2 no relay -> 'test the relay' is inert")
        check("Renseigner d'abord" in text(pg, "#cfgTokenHint"),
              "B3 ... and the hint says what to do instead")
        tok = style(pg, "#cfgToken")
        check(tok != live, f"B4 ... and the inert token paints differently ({tok} vs {live})")

        pg.fill("#cfgRelay", "https://r.example")
        check(pg.eval_on_selector("#cfgToken", "e=>e.disabled") is False,
              "B5 typing a relay URL makes the token live again")
        check(pg.eval_on_selector("#testRelayBtn", "e=>e.disabled") is False,
              "B6 ... and the test button too")
        check("X-Token" in text(pg, "#cfgTokenHint"),
              "B7 ... and the hint returns to its normal wording")

        # --- D. an in-flight test must not be re-enabled by a keystroke
        # Tolerates the helper being absent (pre-fix code), so this row FAILS
        # instead of aborting the run before the second engine.
        still = pg.evaluate(
            "()=>{const b=document.getElementById('testRelayBtn');"
            "b.dataset.testing='1';b.disabled=true;"
            "if(typeof syncRelayDependentFields!=='function')return null;"
            "syncRelayDependentFields();return b.disabled;}")
        check(still is True, "D1 a test in flight survives a re-render / keystroke")

        # --- E. iOS auto-zoom trigger (reported by Yann, 2026-09-07)
        # Safari magnifies the page on focus for ANY text control under 16px and
        # never zooms back out. The sandbox cannot reproduce that magnification --
        # no engine here emulates it -- so the pin is on its CAUSE, which is the
        # part we control and the part that regresses. A real iPhone stays the
        # only witness that the zoom is gone.
        sizes = pg.eval_on_selector_all(
            ".field input",
            "els=>els.map(e=>[e.id,parseFloat(getComputedStyle(e).fontSize)])")
        check(len(sizes) >= 8, f"E0 the audit actually saw the fields ({len(sizes)} found)")
        small = [i for i, px in sizes if px < 16]
        check(not small, f"E1 no text field under 16px (iOS would zoom on focus): {small}")

        # --- F. the 16px bump must not cost Android or desktop anything
        # Raising the inputs to 16px (iOS auto-zoom) makes every field wider, and
        # `text-overflow-e2e.py` audits tile labels and toasts -- never this
        # screen. So its green proved nothing here. Sweep the family's real range:
        # 256/280/300 px is what Android's *display size* slider produces on a
        # 360 px phone, 412 px a large one, 1280 px desktop Chrome.
        PROBE = """()=>{
          const over=[...document.querySelectorAll('.field input,.field label,.hint,button')]
            .filter(e=>e.getBoundingClientRect().right>window.innerWidth+0.5)
            .map(e=>e.id||e.tagName);
          return {page:document.documentElement.scrollWidth, vw:window.innerWidth, over:over};}"""
        for w in (256, 280, 300, 320, 360, 412, 1280):
            pg.set_viewport_size({"width": w, "height": 800})
            pg.evaluate(
                "()=>{config={host:'monserveur.exemple.com',port:'9',"
                "relay:'https://wol.exemple.com',winSrc:'relay',window:'13h50-00h10',"
                "mac:'AABBCCDDEEFF',apps:'seerr,plexweb'};showSettings();}")
            r = pg.evaluate(PROBE)
            check(r["page"] <= r["vw"] + 0.5 and not r["over"],
                  f"F{w} settings fit at {w}px (page={r['page']} vw={r['vw']} over={r['over']})")

        # Positive control: without it, "nothing overflows" would also pass on a
        # selector that matches nothing. Measured armed 2026-09-07.
        pg.set_viewport_size({"width": 256, "height": 800})
        pg.evaluate("()=>{const e=document.getElementById('cfgHost');"
                    "e.style.fontSize='48px';e.style.width='600px';}")
        check(pg.evaluate(PROBE)["over"] == ["cfgHost"],
              "F! the overflow probe is armed (a forced-wide field IS caught)")

        b.close()


def main():
    httpd, port = serve()
    try:
        for engine in ("chromium", "webkit"):
            run(engine, port)
    finally:
        httpd.shutdown()
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
