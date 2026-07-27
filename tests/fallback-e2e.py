#!/usr/bin/env python3
"""Real-browser E2E for fallback.html — the manual-wake page.

Why this exists (2026-07-27). This page had NO coverage at all while quietly
becoming the family's real degraded path: since v8.53 it is *promoted* to a
full-size red call to action on any failed wake, not just an unreachable relay.
It is also the one page whose audience cannot debug it — the whole design bet
is "a non-technical reader follows numbered steps", so a value that renders as
a placeholder, or a copy button that silently does nothing, breaks the only
fallback they have and nobody would ever hear about it.

Scope on purpose: the page is static (no relay, no home, no fetch), so this
needs none of the network mocking the other suites carry.

  python3 tests/fallback-e2e.py                     # working tree
  PWA_BASE=http://127.0.0.1:8123/ python3 …         # local server (clipboard!)

⚠️ The clipboard cases need a SECURE CONTEXT: navigator.clipboard is undefined
on file://, so those two assertions are skipped there and the run says so.
Serve over http://127.0.0.1 (a trusted origin) to exercise them for real.
"""
import os
import sys

from playwright.sync_api import sync_playwright

_LOCAL_BASE = "file://" + os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "fallback.html"))
PWA_BASE = os.environ.get("PWA_BASE") or _LOCAL_BASE
if PWA_BASE == "deployed":
    PWA_BASE = "https://jqh63.github.io/plex-jqh-omv/"
BASE = PWA_BASE if PWA_BASE.endswith(".html") else PWA_BASE.rstrip("/") + "/fallback.html"
ENGINE = os.environ.get("PWA_ENGINES", "chromium").split(",")[0].strip()

# Deliberately DIFFERENT from the page's own placeholder (AABBCCDDEEFF): with
# the same value, "the command carries the real MAC" cannot be distinguished
# from "the command fell back to the example" — the assertion would pass on a
# page that ignored the parameter entirely.
MAC = "0011223344FF"
MAC_FMT = "00:11:22:33:44:FF"
PLACEHOLDER_MAC = "AABBCCDDEEFF"
HOST = "home.example.test"
IP = "203.0.113.10"


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return cond


def txt(page, sel):
    # text_content, NOT inner_text: the Linux/macOS command lives inside a
    # collapsed <details> ("avancé"), and inner_text returns "" for anything
    # not rendered — which read as three app failures on the first run when the
    # command was in fact filled correctly. Assert on the DOM, not on layout.
    return (page.text_content(sel) or "").strip()


def scenario_params_and_commands(p):
    """The page must render the user's OWN values, never the placeholders."""
    print("\n## params-and-commands-are-filled-with-the-real-values")
    b = getattr(p, ENGINE).launch()
    page = b.new_context(viewport={"width": 390, "height": 844}).new_page()
    page.goto(f"{BASE}?mac={MAC}&host={HOST}&port=9", wait_until="load")

    ok = check("the MAC is rendered colon-formatted and uppercase",
               txt(page, "#paramMac") == MAC_FMT, txt(page, "#paramMac"))
    ok &= check("the host is rendered", txt(page, "#paramHost") == HOST,
                txt(page, "#paramHost"))
    ok &= check("the IP row stays hidden when no ?ip= was provided",
                not page.is_visible("#paramIpRow"))

    ps, cmd = txt(page, "#psLine"), txt(page, "#cmdLine")
    # The placeholder MAC is what the page falls back to when the param is
    # missing/invalid. Seeing it here would mean the reader copies a command
    # that wakes nothing — the exact silent failure this page cannot afford.
    ok &= check("the PowerShell command carries the real MAC, not the example",
                MAC in ps and PLACEHOLDER_MAC not in ps, ps[:70])
    ok &= check("the PowerShell command targets the configured host",
                HOST in ps, ps[-70:])
    ok &= check("the wakeonlan command is fully filled, no placeholder left",
                MAC_FMT in cmd and HOST in cmd
                and "example.com" not in cmd, cmd)
    b.close()
    return ok


def scenario_ip_overrides_host(p):
    """?ip= exists FOR the DNS-outage case, so it must win in the commands —
    a command that still says the domain is useless exactly when it is needed."""
    print("\n## ip-param-wins-over-the-domain-in-the-copyable-commands")
    b = getattr(p, ENGINE).launch()
    page = b.new_context().new_page()
    page.goto(f"{BASE}?mac={MAC}&host={HOST}&port=9&ip={IP}", wait_until="load")

    ok = check("the IP row becomes visible", page.is_visible("#paramIpRow"))
    ok &= check("the IP is rendered", txt(page, "#paramIp") == IP, txt(page, "#paramIp"))
    ps, cmd = txt(page, "#psLine"), txt(page, "#cmdLine")
    ok &= check("the PowerShell command uses the IP, not the domain",
                IP in ps and HOST not in ps, ps[-70:])
    ok &= check("the wakeonlan command uses the IP, not the domain",
                IP in cmd and HOST not in cmd, cmd)

    # Control: a malformed ?ip= must be dropped, not propagated into a command
    # the reader would paste. Without this the two assertions above would pass
    # on an implementation that never validates anything.
    page.goto(f"{BASE}?mac={MAC}&host={HOST}&port=9&ip=999.999.1", wait_until="load")
    ok &= check("a malformed ?ip= is ignored and the domain is used instead",
                not page.is_visible("#paramIpRow") and HOST in txt(page, "#cmdLine"),
                txt(page, "#cmdLine"))
    b.close()
    return ok


def scenario_copy_to_clipboard(p):
    print("\n## click-to-copy, and its honest failure state")
    if BASE.startswith("file://"):
        print("  SKIP  navigator.clipboard is undefined on file:// — "
              "re-run with PWA_BASE=http://127.0.0.1:PORT/ to cover this")
        return True
    b = getattr(p, ENGINE).launch()
    ctx = b.new_context()
    try:
        ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    except Exception:
        pass
    page = ctx.new_page()
    page.goto(f"{BASE}?mac={MAC}&host={HOST}&port=9", wait_until="load")

    page.click("#paramMac")
    page.wait_for_timeout(300)
    pasted = page.evaluate("() => navigator.clipboard.readText()")
    ok = check("clicking a value actually puts it on the clipboard",
               pasted == MAC_FMT, repr(pasted))
    ok &= check("the copied value is acknowledged visually",
                "copied" in page.get_attribute("#paramMac", "class"),
                page.get_attribute("#paramMac", "class"))

    # The degraded path matters as much: a copy that silently does nothing on a
    # phone leaves the reader stuck with no idea why. Stub the API away and
    # assert the page SAYS so.
    page.goto(f"{BASE}?mac={MAC}&host={HOST}&port=9", wait_until="load")
    page.evaluate("() => { Object.defineProperty(navigator, 'clipboard', "
                  "{value: undefined, configurable: true}); }")
    page.click("#paramHost")
    page.wait_for_timeout(300)
    ok &= check("a clipboard failure is surfaced, never silent",
                "copy-fail" in page.get_attribute("#paramHost", "class")
                and page.is_visible("#paramHost + .copy-hint, .param.copy-failed .copy-hint"),
                page.get_attribute("#paramHost", "class"))
    b.close()
    return ok


def scenario_app_links(p):
    """The per-OS sections are the actual instructions for the family."""
    print("\n## the per-OS app links are present and point somewhere real")
    b = getattr(p, ENGINE).launch()
    page = b.new_context().new_page()
    page.goto(f"{BASE}?mac={MAC}&host={HOST}&port=9", wait_until="load")
    hrefs = page.eval_on_selector_all("a.cta", "els => els.map(e => e.href)")
    ok = check("both store links are rendered", len(hrefs) >= 2, str(hrefs))
    ok &= check("they point at the app stores",
                any("play.google.com" in h for h in hrefs)
                and any("apple.com" in h for h in hrefs), str(hrefs))
    ok &= check("no store link is left as a placeholder",
                all("example" not in h for h in hrefs), str(hrefs))
    b.close()
    return ok


def main():
    print("=" * 72)
    print(f"FALLBACK page E2E — engine={ENGINE} base={BASE}")
    print("=" * 72)
    with sync_playwright() as p:
        try:
            getattr(p, ENGINE).launch().close()
        except Exception as e:
            print(f"[SKIP] engine={ENGINE}: cannot launch — {str(e)[:90]}")
            return 0
        ok = scenario_params_and_commands(p)
        ok &= scenario_ip_overrides_host(p)
        ok &= scenario_copy_to_clipboard(p)
        ok &= scenario_app_links(p)
    print("\n" + "=" * 72)
    print("ALL PASS" if ok else "FAILURES — see above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
