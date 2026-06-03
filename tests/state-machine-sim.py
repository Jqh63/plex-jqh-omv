#!/usr/bin/env python3
"""
Deterministic state-machine simulator of app.js v8 status / probe timing.

Replays the relevant timer / fetch / resume logic on a synthetic clock so we
can verify the cold-radio resume behaviour without spinning up a browser.
Faster than the E2E (~50 ms vs ~2 min), so useful for tight iteration on the
timing logic. The browser E2E (`cold-radio-e2e.py`) is the source of truth —
this sim is a lightweight first line of defence.

## Why v8 collapses the old sim

Earlier versions (v4→v7) accumulated a *ladder* of apps here — Buggy / V43 /
V44 / V45 / Fixed / Live / Oracle — each adding one more cold-radio defence
(retry chain, two fail-streaks, all-timeout HOLD, adaptive tick). They all
fought the SAME root cause: a 5 s status timeout was too tight against a cold
mobile radio (~3 s to warm) + TLS handshake, so the fetch timed out and the
code cascaded — up to ~33 s of orange/"reconnexion…" on reopen. That is the
IRL bug ("PWA en background, réouverture → check orange 30 s ou plus").

v8 deletes the whole pile and replaces it with ONE generous-timeout probe plus
a generation guard. So this sim is now just two implementations side by side:

  - OldCascadeApp: the v7 cascade (relay retry → home fallback → all-timeout
    HOLD → re-check). Kept ONLY as the contrast baseline — it reproduces the
    ~33 s orange so each scenario can prove v8 fixes it.
  - V8App: the current logic. checkStatus() fires probe(), which resolves
    EXACTLY ONCE to {up, relay_reachable} and never rejects. A probe_gen
    counter drops a stale in-flight probe that resolves after a resume (the
    Android suspend-mid-fetch race). One relay attempt (PROBE_TIMEOUT), and on
    its failure one direct-home fallback (HOME_TIMEOUT). No retry, no hold, no
    streak.

A scenario passes for V8App if the final state matches the spec, no forbidden
paint (red / warn / checking) was emitted, AND the orange "Vérification…" card
was never shown longer than `max_orange_s` (the property that kills the 33 s
bug). The `contrast` check asserts OldCascadeApp actually behaves worse on the
cold-radio scenarios, so the scenarios genuinely exercise the fix.

Run:
  python3 tests/state-machine-sim.py

Requires Python 3.12+, no other deps. See tests/README.md for context.
"""

import heapq
from dataclasses import dataclass, field
from typing import Optional, List

# v8 constants — mirror app.js. PROBE_TIMEOUT is generous on purpose: it must
# outlast a cold mobile-radio TCP+TLS handshake so the first attempt succeeds
# rather than timing out into the fallback.
PROBE_TIMEOUT = 8.0      # app.js PROBE_TIMEOUT_MS — relay /status budget
HOME_TIMEOUT = 5.0       # app.js HOME_FALLBACK_TIMEOUT_MS — direct-home fallback
CHECK_INTERVAL = 15.0    # app.js self-healing poll (foreground-only)
STATUS_LOCAL_TTL = 60.0  # app.js STATUS_LOCAL_TTL_MS — localStorage paint TTL

# OldCascadeApp (v7) constants — the baseline we're proving better than.
OLD_STATUS_TIMEOUT = 5.0  # v7.1 STATUS_FETCH_TIMEOUT_MS
OLD_HOLD_RECHECK = 3.0    # v7.6 HOLD_RECHECK_MS


@dataclass
class FetchOutcome:
    """One fetch result. latency=None means the fetch times out at the
    caller-defined budget (PROBE_TIMEOUT for relay, HOME_TIMEOUT for home)."""
    latency: Optional[float]
    ok: bool = True
    # For a relay /status response: the JSON body's `up` boolean (ignored on a
    # home-fallback outcome — there `ok` alone carries up/down).
    up: bool = True
    # For a FAILED relay /status outcome (ok=False): distinguishes an *answered*
    # failure (relay returned 503/404/bad-shape → alive, /wol still works, only
    # the oracle is degraded) from a *transport* failure (timeout/network →
    # relay unreachable). On an answered failure the relay stays reachable (wake
    # button enabled) and we fall back to direct-home; on a transport failure
    # the relay is marked down. Ignored when ok=True.
    answered: bool = False


@dataclass
class Scenario:
    name: str
    relay_outcomes: List[FetchOutcome] = field(default_factory=list)
    home_outcomes: List[FetchOutcome] = field(default_factory=list)
    has_relay: bool = True
    # localStorage status cache (<60 s). When set, both apps pre-paint it on
    # cold launch / resume before the probe resolves.
    oracle_cache: Optional[dict] = None
    # Background / foreground transitions. background_at hides the app (ticks
    # pause); foreground_at returns it; foreground_event is which event fires
    # on return ("visibilitychange" | "focus" | "none").
    background_at: float = 0.0
    foreground_at: float = 0.0
    foreground_event: str = "visibilitychange"
    expect_final_online: bool = True
    expect_final_relay_reachable: bool = True
    forbid_red_flash: bool = True
    forbid_warn_flash: bool = True
    forbid_checking_paint: bool = False
    # The v8 headline property: the orange "Vérification…" card must never be
    # shown for longer than this. Worst legitimate case is one PROBE_TIMEOUT +
    # one HOME_TIMEOUT (a genuine relay+home outage) ≈ 13 s. The old cascade
    # could hold orange ~33 s — these bounds are what fail the old app.
    max_orange_s: float = PROBE_TIMEOUT + HOME_TIMEOUT + 0.5
    # When True, this scenario is a cold-radio contrast: assert OldCascadeApp
    # does WORSE (longer orange and/or wrong forbidden paint) so the scenario
    # genuinely exercises the fix.
    is_contrast: bool = False
    horizon: float = 60.0


class Clock:
    def __init__(self):
        self.now = 0.0
        self.events = []
        self._seq = 0

    def after(self, delta, cb):
        self._seq += 1
        heapq.heappush(self.events, (self.now + delta, self._seq, cb))

    def run_until(self, t_end):
        while self.events and self.events[0][0] <= t_end:
            when, _, cb = heapq.heappop(self.events)
            self.now = when
            cb()
        self.now = t_end


class BaseApp:
    """Shared plumbing: cache pre-paint, the self-healing tick, background /
    foreground handling, paint bookkeeping (including orange-duration
    tracking). Subclasses implement the probe via _start_probe()."""

    def __init__(self, clock, scenario):
        self.clock = clock
        self.scenario = scenario
        self.config = True
        self.is_online = False
        self.relay_reachable = True
        self.checking = False
        self.has_confirmed_state = False
        self.hidden = False
        self._relay_i = 0
        self._home_i = 0
        self.paints = []
        self._orange_start = None
        self.max_orange = 0.0

    # ---- paint bookkeeping -------------------------------------------------
    def paint(self, kind):
        self.paints.append((round(self.clock.now, 2), kind))
        if kind == "checking":
            if self._orange_start is None:
                self._orange_start = self.clock.now
        elif kind in ("online", "offline"):
            if self._orange_start is not None:
                self.max_orange = max(self.max_orange, self.clock.now - self._orange_start)
                self._orange_start = None

    def _close_orange_at_horizon(self, t_end):
        # Orange still on screen at the horizon counts as orange held until then.
        if self._orange_start is not None:
            self.max_orange = max(self.max_orange, t_end - self._orange_start)

    # ---- fetch outcome tape ------------------------------------------------
    # Repeat-the-LAST outcome once the tape is exhausted (not a fixed "up"
    # default). v8 and OldCascade consume a different number of fetches per
    # cycle (v8: 1 relay + maybe 1 home; old: up to 2 relay + 1 home + a hold
    # re-check), so a fixed default would silently diverge the two — an outage
    # scenario would have the old app's extra retry land on a default "up".
    # Repeating the last outcome keeps a "relay down" tape down for both,
    # however many fetches each app makes. An empty tape falls back to ok/up.
    def _next_relay(self):
        outs = self.scenario.relay_outcomes
        if not outs:
            return FetchOutcome(0.1, ok=True, up=True)
        out = outs[min(self._relay_i, len(outs) - 1)]
        self._relay_i += 1
        return out

    def _next_home(self):
        outs = self.scenario.home_outcomes
        if not outs:
            return FetchOutcome(0.1, ok=True)
        out = outs[min(self._home_i, len(outs) - 1)]
        self._home_i += 1
        return out

    # ---- shared settle -----------------------------------------------------
    def _apply(self, up, relay_ok):
        self.checking = False
        self.relay_reachable = relay_ok
        self.has_confirmed_state = True
        self.is_online = up
        self.paint("online" if up else "offline")
        if not relay_ok:
            # Mirrors setFallbackState(): "Réveil indisponible" surfaces whether
            # the home is up (warn) or down (offline-relay-promoted).
            self.paint("warn-relay" if up else "offline-relay-promoted")

    # ---- lifecycle ---------------------------------------------------------
    def _pre_paint_cache(self):
        cache = self.scenario.oracle_cache
        if cache is None:
            return
        self.relay_reachable = bool(cache.get("relay_ok", True))
        self.is_online = bool(cache.get("up"))
        self.has_confirmed_state = True
        self.paint("online" if self.is_online else "offline")

    def start_app(self):
        self._pre_paint_cache()
        self.check_status()
        self._schedule_tick()

    def _schedule_tick(self):
        # Self-healing poll: never cancelled on background; no-ops while hidden
        # and fires a fresh check on the first tick after foreground.
        def tick():
            if not self.hidden:
                self.check_status()
            self.clock.after(CHECK_INTERVAL, tick)
        self.clock.after(CHECK_INTERVAL, tick)

    def on_background(self):
        self.hidden = True

    def on_foreground(self):
        self.hidden = False
        if self.scenario.foreground_event in ("focus", "visibilitychange"):
            self._resume()
        # "none": the self-healing tick re-probes within CHECK_INTERVAL.

    def _resume(self):
        # app.js onForeground: clear the checking guard, invalidate any
        # suspended probe, re-paint cache (or drop confirmed-state so the
        # re-probe shows orange), then check_status.
        self.checking = False
        self._invalidate_inflight()
        cache = self.scenario.oracle_cache
        if cache is not None:
            self.relay_reachable = bool(cache.get("relay_ok", True))
            self.is_online = bool(cache.get("up"))
            self.has_confirmed_state = True
            self.paint("online" if self.is_online else "offline")
        else:
            self.has_confirmed_state = False
        self.check_status()

    def _invalidate_inflight(self):
        pass  # overridden by V8App's generation guard

    def check_status(self):
        if self.checking or not self.config:
            return
        self.checking = True
        if not self.has_confirmed_state:
            self.paint("checking")
        self._start_probe()

    def _start_probe(self):
        raise NotImplementedError


class V8App(BaseApp):
    """v8.0/8.1 — one probe, one generous timeout, generation guard, no
    cascade; plus the v8.1 1-tick debounce on the relay-down cosmetic."""

    def __init__(self, clock, scenario):
        super().__init__(clock, scenario)
        self.probe_gen = 0
        # v8.1 — 1-tick debounce on the relay-DOWN cosmetic (see app.js).
        self.relay_down_pending = False

    def check_status(self):
        # Mirror app.js: bump the generation at the START of every check so a
        # stale in-flight probe is dropped when it finally resolves.
        if self.checking or not self.config:
            return
        self.checking = True
        self.probe_gen += 1
        if not self.has_confirmed_state:
            self.paint("checking")
        self._start_probe()

    def _invalidate_inflight(self):
        # onForeground bumps probe_gen (belt-and-braces with the bump inside
        # check_status) so a suspended probe resolving late is a no-op.
        self.probe_gen += 1

    def _start_probe(self):
        gen = self.probe_gen
        if not self.scenario.has_relay:
            self._probe_home(gen, relay_ok=True)
            return
        out = self._next_relay()
        if out.latency is None or out.latency >= PROBE_TIMEOUT:
            self.clock.after(PROBE_TIMEOUT, lambda: self._relay_done(gen, False, None, False))
        else:
            self.clock.after(out.latency, lambda: self._relay_done(gen, out.ok, out.up, out.answered))

    def _relay_done(self, gen, ok, up, answered):
        if ok:
            self._settle(gen, up=up, relay_ok=True)
            return
        # Relay failed. answered → alive but degraded (keep reachable);
        # transport → unreachable. Either way, one direct-home fallback.
        self._probe_home(gen, relay_ok=answered)

    def _probe_home(self, gen, relay_ok):
        out = self._next_home()
        if out.latency is None or out.latency >= HOME_TIMEOUT:
            self.clock.after(HOME_TIMEOUT, lambda: self._settle(gen, up=False, relay_ok=relay_ok))
        else:
            self.clock.after(out.latency, lambda: self._settle(gen, up=out.ok, relay_ok=relay_ok))

    def _settle(self, gen, up, relay_ok):
        if gen != self.probe_gen:
            return  # superseded by a newer probe (resume race) — drop it
        # v8.1 1-tick debounce on the relay-down cosmetic only (mirrors the
        # checkStatus().then debounce in app.js). A lone relay miss stays
        # optimistic (eff=True, no "Relais injoignable"); the alarm hardens
        # only on a second consecutive miss. The home up/down verdict (`up`)
        # is unaffected. Invariant: relay_down_pending → relay_reachable.
        if relay_ok:
            eff, self.relay_down_pending = True, False
        elif self.relay_down_pending or not self.relay_reachable:
            eff, self.relay_down_pending = False, False  # 2nd miss / already down → confirm
        else:
            eff, self.relay_down_pending = True, True     # 1st miss → stay optimistic this tick
        self._apply(up, eff)


class OldCascadeApp(BaseApp):
    """v7 cascade — the contrast baseline. relay (timeout) → retry relay →
    home fallback → all-timeout HOLD (one neutral cycle) → re-check. Reproduces
    the ~33 s orange on a fully-cold radio so each contrast scenario proves v8
    is better. Not a faithful copy of every v7 nuance — just enough to surface
    the long-orange / false-paint pathology v8 removes."""

    def __init__(self, clock, scenario):
        super().__init__(clock, scenario)
        self.all_timeout_streak = 0

    def _start_probe(self):
        if not self.scenario.has_relay:
            self._home(relay_ok=True)
            return
        self._relay(attempt=0)

    def _relay(self, attempt):
        out = self._next_relay()
        if out.latency is None or out.latency >= OLD_STATUS_TIMEOUT:
            self.clock.after(OLD_STATUS_TIMEOUT, lambda: self._relay_done(attempt, False, None, False))
        else:
            self.clock.after(out.latency, lambda: self._relay_done(attempt, out.ok, out.up, out.answered))

    def _relay_done(self, attempt, ok, up, answered):
        if ok:
            self.all_timeout_streak = 0
            self._apply(up, relay_ok=True)
            return
        if answered:
            self._home(relay_ok=True)
            return
        if attempt == 0:
            self._relay(attempt=1)  # retry transport failures (adds OLD_STATUS_TIMEOUT)
            return
        self._home(relay_ok=False)

    def _home(self, relay_ok):
        out = self._next_home()
        if out.latency is None or out.latency >= OLD_STATUS_TIMEOUT:
            self.clock.after(OLD_STATUS_TIMEOUT, lambda: self._home_done(False, relay_ok))
        else:
            self.clock.after(out.latency, lambda: self._home_done(out.ok, relay_ok))

    def _home_done(self, home_ok, relay_ok):
        if home_ok:
            self.all_timeout_streak = 0
            self._apply(True, relay_ok)
            return
        if (not relay_ok) and self.all_timeout_streak < 1:
            # all-timeout HOLD: one neutral cycle (orange "reconnexion…"), then
            # re-check after OLD_HOLD_RECHECK. This is what stretches the orange.
            self.all_timeout_streak += 1
            self.checking = False
            self.paint("checking")  # neutral hold reads as orange to the user
            self.clock.after(OLD_HOLD_RECHECK, self.check_status)
            return
        self.all_timeout_streak = 0
        self._apply(False, relay_ok)


def run(scenario, app_class):
    clock = Clock()
    app = app_class(clock, scenario)
    clock.after(0, app.start_app)
    if scenario.background_at > 0:
        clock.after(scenario.background_at, app.on_background)
    if scenario.foreground_at > 0:
        clock.after(scenario.foreground_at, app.on_foreground)
    clock.run_until(scenario.horizon)
    app._close_orange_at_horizon(scenario.horizon)
    return app


def evaluate(app, scenario):
    red = [p for p in app.paints if p[1] == "offline"]
    warn = [p for p in app.paints if p[1] in ("warn-relay", "offline-relay-promoted")]
    checking = [p for p in app.paints if p[1] == "checking"]
    issues = []
    if scenario.forbid_red_flash and red:
        issues.append(f"unexpected RED at {[p[0] for p in red]}")
    if scenario.forbid_warn_flash and warn:
        issues.append(f"unexpected WARN at {[p[0] for p in warn]}")
    if scenario.forbid_checking_paint and checking:
        issues.append(f"unexpected CHECKING paint at {[p[0] for p in checking]}")
    if app.is_online != scenario.expect_final_online:
        issues.append(f"final online={app.is_online} vs {scenario.expect_final_online}")
    if app.relay_reachable != scenario.expect_final_relay_reachable:
        issues.append(f"final relay={app.relay_reachable} vs {scenario.expect_final_relay_reachable}")
    if round(app.max_orange, 2) > scenario.max_orange_s:
        issues.append(f"orange held {round(app.max_orange,1)}s > max {scenario.max_orange_s}s")
    return {
        "issues": issues,
        "max_orange": round(app.max_orange, 2),
        "final_online": app.is_online,
        "final_relay": app.relay_reachable,
        "paints": app.paints,
    }


SCENARIOS = [
    Scenario(
        # Happy path: relay says home is up → green in <500 ms.
        name="cold-launch-server-up-fast",
        relay_outcomes=[FetchOutcome(0.3, ok=True, up=True)],
        expect_final_online=True,
        horizon=5.0,
    ),
    Scenario(
        # Relay says home is down → red in <500 ms (RED expected).
        name="cold-launch-server-off-fast",
        relay_outcomes=[FetchOutcome(0.3, ok=True, up=False)],
        expect_final_online=False,
        forbid_red_flash=False,
        horizon=5.0,
    ),
    Scenario(
        # THE bug. Cold reopen: the radio takes 6.5 s to warm — past the old
        # 5 s timeout but inside v8's 8 s budget. v8: relay answers at 6.5 s →
        # green, orange ≤ ~6.5 s, NO red. OldCascade: relay times out at 5 s →
        # retry (another 5 s) → home fallback (5 s) → HOLD 3 s → re-check… long
        # orange and a likely false red. is_contrast asserts old does worse.
        name="cold-reopen-radio-warms-at-6.5s-no-false-red",
        relay_outcomes=[FetchOutcome(6.5, ok=True, up=True)],
        expect_final_online=True,
        forbid_red_flash=True,
        max_orange_s=7.0,
        is_contrast=True,
        horizon=40.0,
    ),
    Scenario(
        # Relay transport-fails (timeout) on EVERY probe, home is up → green,
        # and the relay-down warn appears only after the v8.1 debounce confirms
        # it (2nd consecutive miss, ~1 tick later). Detection survives a GCP
        # relay outage. First settle ≤ PROBE+HOME ≈ 13 s (green, no warn yet),
        # confirm warn after the T=15 tick re-probes → needs horizon > ~24 s.
        name="relay-timeout-fallback-home-up",
        relay_outcomes=[FetchOutcome(None, ok=False)],
        home_outcomes=[FetchOutcome(0.3, ok=True)],
        expect_final_online=True,
        expect_final_relay_reachable=False,
        forbid_red_flash=True,
        forbid_warn_flash=False,
        horizon=30.0,
    ),
    Scenario(
        # v8.1 DEBOUNCE PAYOFF. A single relay /status transport miss (a
        # slow-but-alive e2-micro or a last-mile blip), then the relay recovers
        # on the next tick. The lone miss must NEVER paint the "Relais
        # injoignable" warn nor disable the wake button — relay stays reachable
        # throughout. (Not an is_contrast scenario: the tape's repeat-last
        # semantics make OldCascade consume both relay outcomes at T=0 via its
        # retry, so it can't be compared cleanly on a single-miss-then-recover
        # tape — this stands on its own as a v8.1 regression guard.)
        name="relay-single-miss-debounced-no-warn",
        relay_outcomes=[
            FetchOutcome(None, ok=False),           # T=0 lone transport miss
            FetchOutcome(0.3, ok=True, up=True),     # T=15 tick — relay back
        ],
        home_outcomes=[FetchOutcome(0.3, ok=True)],
        expect_final_online=True,
        expect_final_relay_reachable=True,
        forbid_red_flash=True,
        forbid_warn_flash=True,
        horizon=20.0,
    ),
    Scenario(
        # Both relay and home down → red + warn. Full outage; settles ≈ 13 s.
        # The KEY property: even a total outage holds orange ≤ 13 s, never 33 s.
        name="relay-and-home-down-bounded-orange",
        relay_outcomes=[FetchOutcome(None, ok=False)],
        home_outcomes=[FetchOutcome(None, ok=False)],
        expect_final_online=False,
        expect_final_relay_reachable=False,
        forbid_red_flash=False,
        forbid_warn_flash=False,
        is_contrast=True,
        horizon=40.0,
    ),
    Scenario(
        # Relay ANSWERS degraded (503 STATUS_TARGET_URL unset / 404 legacy),
        # home up → green, NO warn, relay stays reachable (wake button enabled).
        name="relay-answered-degraded-server-up",
        relay_outcomes=[FetchOutcome(0.3, ok=False, answered=True)],
        home_outcomes=[FetchOutcome(0.3, ok=True)],
        expect_final_online=True,
        expect_final_relay_reachable=True,
        forbid_red_flash=True,
        forbid_warn_flash=True,
        horizon=5.0,
    ),
    Scenario(
        # Degraded oracle, home actually down → red (server down) but NO warn
        # and relay stays reachable so the user can still fire a WoL. The IRL
        # "red relay + WoL gone while it was fine" case.
        name="relay-answered-degraded-server-down",
        relay_outcomes=[FetchOutcome(0.3, ok=False, answered=True)],
        home_outcomes=[FetchOutcome(None, ok=False)],
        expect_final_online=False,
        expect_final_relay_reachable=True,
        forbid_red_flash=False,
        forbid_warn_flash=True,
        horizon=10.0,
    ),
    Scenario(
        # localStorage cache <60 s + server still up → instant green pre-paint,
        # probe confirms. No orange flash.
        name="stale-cache-paint-then-confirm",
        relay_outcomes=[FetchOutcome(0.3, ok=True, up=True)],
        oracle_cache={"up": True, "relay_ok": True},
        expect_final_online=True,
        forbid_checking_paint=True,
        horizon=5.0,
    ),
    Scenario(
        # GENERATION GUARD. The cold-launch probe (relay #1) is slow — latency
        # 7.9 s, just inside PROBE_TIMEOUT, carrying a now-stale up=True. A
        # resume fires at T=2 (foreground) while #1 is still in flight: it bumps
        # probe_gen and starts the resume probe (relay #2, up=False — the server
        # died), which settles red at ~2.3 s. At T=7.9 the orphaned #1 resolves
        # up=True but its generation is stale → dropped. Final MUST be red. A
        # broken guard repaints green at T=7.9 → final green → FAIL.
        # (The sim doesn't pause timers across background like Android does, so
        # we approximate the suspend-mid-fetch race with a short in-flight
        # window; the E2E exercises the real visibilitychange timing.)
        name="resume-race-stale-inflight-probe-dropped",
        relay_outcomes=[
            FetchOutcome(7.9, ok=True, up=True),    # #1: in-flight, stale "up"
            FetchOutcome(0.3, ok=True, up=False),   # #2: resume probe, truth = down
        ],
        foreground_at=2.0,
        foreground_event="visibilitychange",
        expect_final_online=False,
        forbid_red_flash=False,
        horizon=12.0,
    ),
    Scenario(
        # Resume with NO foreground event (Android PWA standalone quirk): the
        # self-healing tick must re-probe on its own and converge to red after
        # the server died during background. background at 8, no event on return,
        # first post-foreground tick at T=15 re-probes → red.
        name="resume-no-event-self-heals-to-red",
        relay_outcomes=[
            FetchOutcome(0.3, ok=True, up=True),    # T=0 cold check — up
            FetchOutcome(0.3, ok=True, up=False),   # T=15 self-healing tick — now down
            FetchOutcome(0.3, ok=True, up=False),
        ],
        oracle_cache={"up": True, "relay_ok": True},
        background_at=8.0,
        foreground_at=12.0,
        foreground_event="none",
        expect_final_online=False,
        forbid_red_flash=False,
        horizon=30.0,
    ),
]


def fmt_paints(paints):
    return ", ".join(f"{t}s->{k}" for t, k in paints) or "(no transitions)"


def main():
    print("=" * 72)
    print("PWA v8 state-machine simulation — OldCascade (v7) vs V8")
    print("=" * 72)
    v8_pass = True
    contrast_ok = True
    for sc in SCENARIOS:
        print(f"\n## {sc.name}")
        v8 = evaluate(run(sc, V8App), sc)
        old = evaluate(run(sc, OldCascadeApp), sc)
        v8_verdict = "PASS" if not v8["issues"] else "FAIL"
        if v8_verdict != "PASS":
            v8_pass = False
        print(f"  [V8App      ] {v8_verdict}  orange_max={v8['max_orange']}s  "
              f"paints: {fmt_paints(v8['paints'])}")
        if v8["issues"]:
            print(f"                 issues: {'; '.join(v8['issues'])}")
        print(f"  [OldCascade ] orange_max={old['max_orange']}s  "
              f"paints: {fmt_paints(old['paints'])}")
        # Contrast: on cold-radio scenarios the old cascade must do measurably
        # worse — either hold orange longer than v8's bound, or emit a forbidden
        # paint v8 avoids. If it doesn't, the scenario isn't exercising the fix.
        if sc.is_contrast:
            old_worse = bool(old["issues"]) or old["max_orange"] > v8["max_orange"]
            if not old_worse:
                contrast_ok = False
                print(f"  [contrast   ] FAIL  expected OldCascade worse than V8, "
                      f"but old_orange={old['max_orange']} v8_orange={v8['max_orange']} "
                      f"old_issues={old['issues']}")
            else:
                print(f"  [contrast   ] OK    old worse "
                      f"(orange {old['max_orange']}s vs {v8['max_orange']}s, "
                      f"old_issues={old['issues'] or 'none'})")
    print("\n" + "=" * 72)
    print(f"V8App: {'all scenarios PASS' if v8_pass else 'AT LEAST ONE SCENARIO FAILED'}")
    print(f"Contrast (v8 better than v7 on cold-radio): "
          f"{'confirmed' if contrast_ok else 'BROKEN — see [contrast] lines'}")
    return 0 if (v8_pass and contrast_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
