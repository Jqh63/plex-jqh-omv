#!/usr/bin/env python3
"""What does the card SHOW on a flaky mobile link?

Written as an exploration (2026-08-16, "des passages en indispo avant de devenir
ok"), kept as a PIN: it is the only layer that answers "how many seconds of
non-green did a healthy home cause" without waiting a week of field reports.
`SCENARIOS` below carries the v8.76 grace pin and its positive control — the pin
was verified FAILING against pre-v8.76 app.js (0,3 s of 'Statut inconnu') before
the fix landed. Exploratory mode is still there: set LATENCIES=…

The home is UP for the whole run and never changes; the relay always holds the
right answer. ONLY the latency of /status varies. So every non-green thing the
user sees is a NETWORK artefact, not a state change — which is exactly the
complaint ("des passages en indispo avant de devenir ok").

Samples the VISIBLE label 4x/s: the paint ring records decisions, this records
the screen, and the screen is what the complaint is about.

Two instrument bugs were found the hard way building this, both worth keeping
in mind before trusting any number it prints:

  1. Latency via time.sleep() inside a SYNC route handler stalls Playwright's
     own dispatch thread, not the request. It produced a 0.3 s "Statut inconnu"
     attributed to an 8 s timeout — a timing that cannot be true. Hence the
     async API: `await asyncio.sleep()` delays the response and nothing else.
  2. Serving the relay from a local http:// server bypassed the whole relay
     path in silence — validRelay() requires https://, so config.relay stayed
     unset and the app ran its relay-LESS branch. The tell was `src=poll
     sa=none` in the journal instead of `src=hb`. A scenario that exercises the
     wrong code path still prints a confident result.

  LATENCIES=9.5,0.3,0.3 RUN_S=45 python3 flaky-net-sim.py
"""
import asyncio, json, os, sys, time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from playwright.async_api import async_playwright

CONFIG_HOST, RELAY_HOST = "test.example.com", "r.example.com"
BASE = "file://" + os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "index.html"))
BASE = os.environ.get("PWA_BASE", BASE)
ENGINE = os.environ.get("PWA_ENGINES", "chromium")
PAINT_LOG_KEY = "plex-jqh-omv-paints"
UNKNOWN_LABEL = "Statut inconnu"

# A latency above PROBE_TIMEOUT_MS (8 s) + HOME_FALLBACK_TIMEOUT_MS (5 s) is a
# MISS from the app's point of view: both legs expire before the body lands.
MISS, FAST = 14.0, 0.3

# name -> (latency tape, run seconds, max seconds of "Statut inconnu" tolerated)
# The tape repeats. `None` = exploratory, no verdict.
SCENARIOS = [
    # v8.76 pin. Cold open, ONE missed probe, home UP: the answer lands right
    # after, so the flash bought nothing. Must FAIL against pre-v8.76 app.js
    # (measured 0,3 s of unknown there) — that is the whole point of the pin.
    ("cold-open-one-miss-holds-presumption", [MISS, FAST], 40, 0.0),
    # Positive control. Without it, "no unknown" would also pass on an app that
    # never says unknown at all — which is the faux vert #192/#193 closed.
    ("cold-open-three-misses-still-says-unknown", [MISS, MISS, MISS, FAST], 70, None),
    # A settled verdict is immune to latency (measured: kept-verdict).
    ("settled-verdict-survives-intermittent-timeouts",
     [FAST, FAST, MISS, FAST, MISS, FAST], 80, 0.0),
]


def window_open():
    now = datetime.now()
    return (f"{(now - timedelta(hours=1)).strftime('%Hh%M')}-"
            f"{(now + timedelta(hours=1)).strftime('%Hh%M')}")


async def run(latencies, run_s):
    """Play one latency tape and return (served, screen timeline, paint ring)."""
    served, n, t0 = [], [0], [time.time()]
    LATENCIES, RUN_S = latencies, run_s

    async with async_playwright() as p:
        b = await getattr(p, ENGINE).launch()
        ctx = await b.new_context(viewport={"width": 390, "height": 844})
        page = await ctx.new_page()

        async def handle(route):
            u = urlparse(route.request.url)
            if u.netloc == RELAY_HOST and u.path == "/status":
                lat = LATENCIES[n[0] % len(LATENCIES)]
                n[0] += 1
                served.append((round(time.time() - t0[0], 1), lat))
                await asyncio.sleep(lat)          # the flaky link
                try:
                    await route.fulfill(status=200, headers={
                        "Content-Type": "application/json",
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Headers": "*",
                    }, body=json.dumps({"up": True, "stale": False, "age_s": 2,
                                        "source": "heartbeat",
                                        "served_at": int(time.time())}))
                except Exception:
                    pass                          # client already aborted
                return
            if u.netloc == CONFIG_HOST or u.netloc.endswith("." + CONFIG_HOST):
                await route.fulfill(status=200, body="")
                return
            await route.continue_()

        await page.route("**/*", handle)
        url = (f"{BASE}?host={CONFIG_HOST}&mac=AABBCCDDEEFF&relay=https://{RELAY_HOST}"
               f"&token=x&apps=seerr,plexweb&window={window_open()}")
        t0[0] = time.time()
        await page.goto(url, wait_until="load")

        seen, start = [], time.time()
        while time.time() - start < RUN_S:
            try:
                lbl = (await page.text_content("#statusLabel") or "").strip()
                sub = (await page.text_content("#statusSub") or "").strip()
            except Exception:
                break
            if not seen or seen[-1][1] != lbl:
                seen.append((round(time.time() - start, 1), lbl, sub))
            await page.wait_for_timeout(250)

        ring = await page.evaluate(
            f"JSON.parse(localStorage.getItem('{PAINT_LOG_KEY}')||'[]')")
        await b.close()
    return served, seen, ring


def report(name, served, seen, ring, run_s):
    print(f"\n=== PROFIL RESEAU JOUE ({len(served)} requetes /status) ===")
    for t, lat in served:
        print(f"  t+{t:>6.1f}s  latence {lat:>5.1f}s" +
              ("   <-- DEPASSE le budget client 8 s" if lat > 8 else ""))

    print(f"\n=== CE QUE L'ECRAN A MONTRE ({len(seen)} etat(s) en {run_s}s) ===")
    tot = {}
    for i, (t, lbl, sub) in enumerate(seen):
        end = seen[i + 1][0] if i + 1 < len(seen) else run_s
        tot[lbl] = tot.get(lbl, 0) + (end - t)
        print(f"  t+{t:>6.1f}s -> t+{end:>6.1f}s  ({end - t:>5.1f}s)  {lbl!r} / {sub!r}")

    print("\n=== JOURNAL (ce que l'app dit avoir fait) ===")
    for e in ring:
        c = f" x{e['n']}" if e.get("n") else ""
        print(f"  {e['c']:<15} <- {e['w']:<26}{c}  {e.get('d','')}")

    print("\n=== BILAN — la maison etait UP tout du long ===")
    for lbl, s in sorted(tot.items(), key=lambda kv: -kv[1]):
        print(f"  {s:>6.1f}s ({100*s/run_s:>3.0f}%)  {lbl!r}")
    return tot


async def main():
    # Exploratory mode — an explicit tape prints, asserts nothing.
    if os.environ.get("LATENCIES"):
        lat = [float(x) for x in os.environ["LATENCIES"].split(",")]
        run_s = int(os.environ.get("RUN_S", "150"))
        report("exploration", *(await run(lat, run_s)), run_s)
        return 0

    fails = []
    for name, lat, run_s, max_unknown_s in SCENARIOS:
        print(f"\n{'='*72}\n### {name}\n{'='*72}")
        served, seen, ring = await run(lat, run_s)
        tot = report(name, served, seen, ring, run_s)
        unknown_s = tot.get(UNKNOWN_LABEL, 0.0)
        if max_unknown_s is None:
            # Positive control: this one MUST reach unknown, or the pins above
            # are satisfied by an app that never says "I don't know".
            ok = unknown_s > 0
            print(f"\n  [{'PASS' if ok else 'FAIL'}] controle positif — "
                  f"{UNKNOWN_LABEL!r} attendu, vu {unknown_s:.1f}s")
        else:
            ok = unknown_s <= max_unknown_s
            print(f"\n  [{'PASS' if ok else 'FAIL'}] {UNKNOWN_LABEL!r} "
                  f"{unknown_s:.1f}s (budget {max_unknown_s:.1f}s)")
        if not ok:
            fails.append(name)

    print(f"\n{'='*72}")
    if fails:
        print(f"FAIL ({len(fails)}/{len(SCENARIOS)}) : " + ", ".join(fails))
        return 1
    print(f"ALL PASS ({len(SCENARIOS)} scenarios)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
