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
    streak. As of v8.4 it also models power-button honesty: the confident green
    "Serveur allumé" lights once a LIVE probe has settled this session (relay or
    home answered, regardless of the relay's SWR `stale` flag); only a cache
    pre-paint (before the first probe) paints the neutral "Vérification…" button.
    (v8.3 also gated on `not stale`, which left the button stuck orange because a
    healthy home is almost always served `stale:true` — fixed in v8.4.)
  - BuggyButtonApp: V8App's card logic verbatim, but with the PRE-v8.3 power
    button — it asserts the confident green on ANY up verdict, including straight
    from a cache pre-paint. The side-by-side baseline for the `button_contrast`
    scenarios: it paints the false green from a cache pre-paint exactly where the
    fixed V8App shows "Vérification…" until the first live probe lands.
  - BuggyCardApp: V8App's button logic verbatim, but with the PRE-v8.5 status
    CARD — it asserts the confident green "En ligne" card on ANY up verdict,
    including straight from a cache pre-paint. The baseline for the
    `card_contrast` scenarios: it paints the false green CARD from a cache
    pre-paint exactly where the fixed V8App shows the neutral "Vérification…"
    card until the first live probe lands. This is the reported bug — "the PWA
    shows green right after I stopped the homelab". v8.5 also shortens the poll
    interval (15 s → 8 s) so the corrected red lands ~2× sooner — asserted via
    `expect_red_by`.

A scenario passes for V8App if the final state matches the spec, no forbidden
paint (red / warn / checking) was emitted, the orange "Vérification…" card was
never shown longer than `max_orange_s` (the property that kills the 33 s bug),
the power button honours its freshness assertions (`expect_first_button` /
`expect_confident_button`), AND the status card honours its freshness assertions
(`expect_first_card` / `expect_confident_card`) and corrects to red by
`expect_red_by` when set. The `contrast` check asserts OldCascadeApp behaves
worse on the cold-radio scenarios; `button_contrast` / `card_contrast` assert
BuggyButtonApp / BuggyCardApp paint the false confident green the fix removes —
so the scenarios genuinely exercise the fix.

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
CHECK_INTERVAL = 8.0     # app.js STATUS_POLL_INTERVAL_MS — self-healing poll
                         # (foreground-only). v8.5: 15 s → 8 s to halve the
                         # post-flip correction window (the relay only learns a
                         # just-stopped home via a background SWR refresh, so the
                         # NEXT poll is what surfaces "down"). Relay-outage probes
                         # are bounded by PROBE_TIMEOUT, not this — see below.
STATUS_LOCAL_TTL = 60.0  # app.js STATUS_LOCAL_TTL_MS — localStorage paint TTL
# A check still "in flight" past this is presumed wedged (suspended-mid-fetch
# zombie probe, or a resume event that never fired) and the next re-probe
# trigger reclaims it. Sized at PROBE+HOME+slack so a legitimately slow probe
# (≤13 s worst case) is never preempted. Since v8.5 it EXCEEDS CHECK_INTERVAL
# (8 s), so a wedge is reclaimed on the first self-healing tick whose age clears
# the watchdog (~2 ticks ≈ 16 s worst case), not the next single tick — still
# guaranteed-eventually.
CHECK_WATCHDOG = PROBE_TIMEOUT + HOME_TIMEOUT + 1.0   # 14 s  (app.js CHECK_WATCHDOG_MS)
# Consecutive relay /status misses before the (advisory) "Relais injoignable"
# cosmetic hardens. A cold burstable e2-micro can miss across more than one
# 15 s tick; a real WoL failure surfaces instantly via postWol regardless, so
# the passive indicator can afford to be patient and avoid false alarms.
RELAY_DOWN_MISSES = 3    # app.js RELAY_DOWN_MISSES

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
    # A "zombie" fetch: started but NEVER resolves and never rejects. Models the
    # Android suspend-mid-fetch race the real PWA hits — the socket is torn down
    # by the OS during background and the in-flight fetch's abort timer is frozen
    # with it, so `checking=true` is left stuck. The whole point of the watchdog
    # is to reclaim that stuck flag; this lets the sim exercise it. Overrides
    # latency/ok when True.
    zombie: bool = False
    # For a relay /status SUCCESS (ok=True): the JSON body's `stale` flag. The
    # relay serves a stale-but-within-ceiling verdict during its 60 s SWR window
    # (the home may have just gone down). The PWA treats a stale up as NOT fresh
    # → the power button stays honest ("Vérification…") instead of asserting a
    # confident green. Ignored on failures / home outcomes.
    stale: bool = False


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
    # v8.3 power-button honesty assertions (None = not checked):
    #   expect_first_button — the FIRST button paint must equal this
    #     ("checking" | "on" | "wake" | "unavailable"); a cached/stale up must
    #     paint "checking" first, never a confident "on".
    #   expect_confident_button — True: a confident "on" must appear at some
    #     point (the fix must NOT over-suppress a genuinely fresh up); False:
    #     "on" must NEVER appear (a cache/stale-only scenario).
    #   button_contrast — also run BuggyButtonApp and assert it paints the false
    #     confident green where the fixed app does not, proving the scenario
    #     genuinely exercises the fix.
    expect_first_button: Optional[str] = None
    expect_confident_button: Optional[bool] = None
    button_contrast: bool = False
    # v8.5 status-card honesty assertions (None = not checked), mirroring the
    # button ones:
    #   expect_first_card — the FIRST card paint must equal this
    #     ("checking" | "online" | "offline"); a cached/stale up must paint the
    #     neutral "checking" first, never a confident "online".
    #   expect_confident_card — True: a confident "online" card must appear at
    #     some point (the fix must NOT over-suppress a genuinely fresh up);
    #     False: "online" must NEVER appear.
    #   expect_red_by — if set, an "offline" card paint must land at or before
    #     this time. Locks the v8.5 faster-correction (8 s poll): a "just
    #     stopped" home must flip to red within ~one poll, not ~15 s.
    #   card_contrast — also run BuggyCardApp and assert it paints the false
    #     confident green CARD where the fixed app does not.
    expect_first_card: Optional[str] = None
    expect_confident_card: Optional[bool] = None
    expect_red_by: Optional[float] = None
    card_contrast: bool = False


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
        self.button = None
        self.button_paints = []
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

    # ---- power button (v8.3) ----------------------------------------------
    def set_button(self, kind):
        # Record a button transition (deduped). The button reflects the SAME
        # up/down the card does, but its confident "on" is gated on freshness.
        if kind != self.button:
            self.button = kind
            self.button_paints.append((round(self.clock.now, 2), kind))

    def _button_for(self, up, fresh, relay_ok):
        # Fixed (v8.3) honesty: confident green "on" only on a FRESH up; a
        # cached / relay-stale up paints "checking". Down → the wake affordance,
        # or "unavailable" when the relay is unreachable too. BuggyButtonApp
        # overrides this with the pre-fix "any up → on".
        if up:
            return "on" if fresh else "checking"
        return "wake" if relay_ok else "unavailable"

    # ---- status card (v8.5) -----------------------------------------------
    def _card_for(self, up, fresh):
        # Fixed (v8.5) card honesty, mirroring the button: confident green
        # "online" only on a FRESH up; a cached / relay-stale up paints the
        # neutral "checking" ("Vérification…") instead of asserting "En ligne".
        # Down always paints "offline" (red is never a false-confidence problem
        # for a WoL app, and keeps the wake affordance prominent). BuggyCardApp
        # overrides this with the pre-fix "any up → online".
        if up:
            return "online" if fresh else "checking"
        return "offline"

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
    def _apply(self, up, relay_ok, fresh=True):
        self.checking = False
        self.relay_reachable = relay_ok
        self.has_confirmed_state = True
        self.is_online = up
        self.paint(self._card_for(up, fresh))
        self.set_button(self._button_for(up, fresh, relay_ok))
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
        # A cache pre-paint is never a fresh verdict → honest card AND button
        # (v8.5): an "up" cache paints the neutral "checking", not a confident
        # green, until the live probe confirms.
        self.paint(self._card_for(self.is_online, False))
        self.set_button(self._button_for(self.is_online, False, self.relay_reachable))

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
            # Same v8.5 honesty as the cold pre-paint: a resume cache repaint of
            # an "up" shows "checking", not a confident green.
            self.paint(self._card_for(self.is_online, False))
            self.set_button(self._button_for(self.is_online, False, self.relay_reachable))
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
    """v8.2 — one probe, one generous timeout, generation guard, no cascade;
    a `checking` watchdog so a wedged in-flight probe can't freeze re-probing;
    and an N-consecutive-miss debounce on the (advisory) relay-down cosmetic."""

    def __init__(self, clock, scenario):
        super().__init__(clock, scenario)
        self.probe_gen = 0
        self.check_started_at = 0.0
        # v8.2 — consecutive relay-miss counter feeding the relay-down debounce.
        self.relay_miss_streak = 0

    def check_status(self):
        if not self.config:
            return
        # Watchdog: a check still in flight past CHECK_WATCHDOG is presumed
        # wedged (zombie probe / missed resume event) — fall through and start a
        # fresh one rather than early-returning forever. The generation bump
        # below drops the stale probe if it ever resolves. This is the backstop
        # that the v8.0 generation guard was MISSING: dropping a stale probe
        # without resetting `checking` meant a never-resolving probe froze the
        # app (the "total KO, must kill" bug).
        if self.checking and (self.clock.now - self.check_started_at) < CHECK_WATCHDOG:
            return
        self.checking = True
        self.check_started_at = self.clock.now
        # Bump the generation at the START of every check so a stale in-flight
        # probe is dropped when it finally resolves.
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
        if out.zombie:
            return  # wedged probe — never resolves; leaves checking=True stuck
        if out.latency is None or out.latency >= PROBE_TIMEOUT:
            self.clock.after(PROBE_TIMEOUT, lambda: self._relay_done(gen, False, None, False, False))
        else:
            self.clock.after(out.latency, lambda: self._relay_done(gen, out.ok, out.up, out.answered, out.stale))

    def _relay_done(self, gen, ok, up, answered, stale=False):
        if ok:
            # A live relay verdict is confirmed-this-session regardless of the
            # relay's SWR `stale` flag (v8.4 — see app.js verdictFresh note): the
            # PWA polls every 15 s vs the relay's 5 s fresh window, so a healthy
            # home is almost always served `stale:true`. Gating the button on
            # `not stale` (v8.3) left it stuck orange. Only up:false (60 s
            # ceiling) moves the button off green. `stale` kept for tape parity.
            self._settle(gen, up=up, relay_ok=True, fresh=True)
            return
        # Relay failed. answered → alive but degraded (keep reachable);
        # transport → unreachable. Either way, one direct-home fallback.
        self._probe_home(gen, relay_ok=answered)

    def _probe_home(self, gen, relay_ok):
        out = self._next_home()
        # A direct-home probe is always a live, fresh reading.
        if out.latency is None or out.latency >= HOME_TIMEOUT:
            self.clock.after(HOME_TIMEOUT, lambda: self._settle(gen, up=False, relay_ok=relay_ok, fresh=True))
        else:
            self.clock.after(out.latency, lambda: self._settle(gen, up=out.ok, relay_ok=relay_ok, fresh=True))

    def _settle(self, gen, up, relay_ok, fresh=True):
        if gen != self.probe_gen:
            return  # superseded by a newer probe (resume race) — drop it
        # v8.2 N-consecutive-miss debounce on the relay-down cosmetic only
        # (mirrors checkStatus().then in app.js). A relay miss stays optimistic
        # (eff=True, no "Relais injoignable") until RELAY_DOWN_MISSES misses in a
        # row — a cold e2-micro can miss across more than one 15 s tick. Any
        # answered/successful probe resets the streak. The home up/down verdict
        # (`up`) is never debounced. Invariant: streak<RELAY_DOWN_MISSES while
        # reachable.
        if relay_ok:
            eff, self.relay_miss_streak = True, 0
        else:
            self.relay_miss_streak += 1
            eff = not (self.relay_miss_streak >= RELAY_DOWN_MISSES or not self.relay_reachable)
        self._apply(up, eff, fresh)


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


class BuggyButtonApp(V8App):
    """Pre-v8.3 power-button behaviour — the side-by-side baseline. The card
    machinery is identical to V8App (so the up/down verdict and timing are the
    same); only the button is wrong: it asserts the confident green "on" on ANY
    up verdict, ignoring freshness. That's the reported bug — the button shows
    "Serveur allumé" from a cache pre-paint or a relay stale=true verdict while
    the card is still showing "vérification…". The button_contrast scenarios run
    this app to prove they actually catch what the fix removes."""

    def _button_for(self, up, fresh, relay_ok):
        if up:
            return "on"
        return "wake" if relay_ok else "unavailable"


class BuggyCardApp(V8App):
    """Pre-v8.5 status-CARD behaviour — the side-by-side baseline. The button
    machinery is identical to V8App (so the up/down verdict and timing match);
    only the card is wrong: it asserts the confident green "online" on ANY up
    verdict, ignoring freshness. That's the reported bug — the card shows
    "En ligne" straight from a cache pre-paint (or a relay stale=true verdict)
    right after the home was stopped. The card_contrast scenarios run this app
    to prove they actually catch what the fix removes."""

    def _card_for(self, up, fresh):
        if up:
            return "online"
        return "offline"


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
    # v8.3 power-button honesty checks.
    confident = [p for p in app.button_paints if p[1] == "on"]
    if scenario.expect_first_button is not None:
        first = app.button_paints[0][1] if app.button_paints else None
        if first != scenario.expect_first_button:
            issues.append(f"first button {first!r} vs expected {scenario.expect_first_button!r}")
    if scenario.expect_confident_button is True and not confident:
        issues.append("expected a confident-green button ('on') but none painted")
    if scenario.expect_confident_button is False and confident:
        issues.append(f"unexpected confident-green button ('on') at {[p[0] for p in confident]}")
    # v8.5 status-card honesty checks (mirror the button ones).
    card_paints = [p for p in app.paints if p[1] in ("checking", "online", "offline")]
    confident_card = [p for p in card_paints if p[1] == "online"]
    if scenario.expect_first_card is not None:
        first_card = card_paints[0][1] if card_paints else None
        if first_card != scenario.expect_first_card:
            issues.append(f"first card {first_card!r} vs expected {scenario.expect_first_card!r}")
    if scenario.expect_confident_card is True and not confident_card:
        issues.append("expected a confident-green card ('online') but none painted")
    if scenario.expect_confident_card is False and confident_card:
        issues.append(f"unexpected confident-green card ('online') at {[p[0] for p in confident_card]}")
    if scenario.expect_red_by is not None:
        reds_t = [p[0] for p in app.paints if p[1] == "offline"]
        if not reds_t or min(reds_t) > scenario.expect_red_by:
            issues.append(f"red not reached by {scenario.expect_red_by}s "
                          f"(offline paints at {reds_t or 'none'})")
    return {
        "issues": issues,
        "max_orange": round(app.max_orange, 2),
        "final_online": app.is_online,
        "final_relay": app.relay_reachable,
        "paints": app.paints,
        "button_paints": app.button_paints,
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
        # and the relay-down warn appears only after the v8.2 debounce confirms
        # it (RELAY_DOWN_MISSES=3 consecutive misses). Detection survives a GCP
        # relay outage. Each miss settles ≈ PROBE+HOME ≈ 8.3 s (home up, fast
        # fallback). v8.5: the 8 s probe timeout exceeds the 8 s tick, so a tick
        # fired mid-probe is skipped and the effective re-probe cadence is ~16 s;
        # misses at ~8 / ~24 / ~40 s → the 3rd confirms the warn → horizon > ~40 s.
        # Green (home up) throughout.
        name="relay-timeout-fallback-home-up",
        relay_outcomes=[FetchOutcome(None, ok=False)],
        home_outcomes=[FetchOutcome(0.3, ok=True)],
        expect_final_online=True,
        expect_final_relay_reachable=False,
        forbid_red_flash=True,
        forbid_warn_flash=False,
        horizon=45.0,
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
            FetchOutcome(0.3, ok=True, up=True),     # T=16 tick — relay back
        ],
        home_outcomes=[FetchOutcome(0.3, ok=True)],
        expect_final_online=True,
        expect_final_relay_reachable=True,
        forbid_red_flash=True,
        forbid_warn_flash=True,
        horizon=20.0,
    ),
    Scenario(
        # Both relay and home down → red is immediate (the up/down verdict is
        # never debounced; each probe settles red ≈ PROBE+HOME ≈ 13 s). The relay
        # warn hardens only on the 3rd consecutive miss (~45 s at the v8.5 ~16 s
        # effective outage cadence). The KEY property: even a total outage holds
        # orange ≤ 13 s, never 33 s → horizon > ~45 s.
        name="relay-and-home-down-bounded-orange",
        relay_outcomes=[FetchOutcome(None, ok=False)],
        home_outcomes=[FetchOutcome(None, ok=False)],
        expect_final_online=False,
        expect_final_relay_reachable=False,
        forbid_red_flash=False,
        forbid_warn_flash=False,
        is_contrast=True,
        horizon=50.0,
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
        # v8.5 CARD — cache <60 s + server still up. The cache pre-paint must NOT
        # assert the confident green "En ligne" card; it paints the neutral
        # "Vérification…" (checking) and the live probe confirms green within a
        # probe. So a brief checking IS expected now (pre-v8.5 the cache painted
        # instant green and checking was forbidden here) — that instant green is
        # exactly the reported bug. BuggyCardApp paints the false green straight
        # from the cache → card_contrast proves the scenario exercises the fix.
        name="card-cache-up-honest-then-green",
        relay_outcomes=[FetchOutcome(0.3, ok=True, up=True)],
        oracle_cache={"up": True, "relay_ok": True},
        expect_final_online=True,
        expect_first_card="checking",
        expect_confident_card=True,
        card_contrast=True,
        horizon=5.0,
    ),
    Scenario(
        # v8.5 HEADLINE — the reported bug. The user stopped the homelab, then
        # reopens the PWA. The localStorage cache (<60 s) still says "up", and the
        # relay's first /status answers up but SWR-STALE: its last poll caught the
        # home while it was still up, and the background refresh that flips it to
        # down hasn't run yet. Pre-v8.5: instant green from the cache, then a green
        # that lingers ~15 s. Fixed: (1) the cache pre-paint shows the neutral
        # "checking", NOT a confident green (card_contrast vs BuggyCardApp);
        # (2) the live stale-up still greens the card briefly — we can't tell a
        # stale-about-to-flip from a healthy stale (the !stale constraint that the
        # v8.3 button regression taught us); BUT (3) the next 8 s poll catches the
        # relay's now-down verdict → red by ~8.3 s, not ~15 s (expect_red_by).
        name="card-just-stopped-no-instant-green-fast-correct",
        relay_outcomes=[
            FetchOutcome(0.3, ok=True, up=True, stale=True),   # T=0 stale "up" (home just died)
            FetchOutcome(0.3, ok=True, up=False),              # T=8 relay bg-refresh caught down
        ],
        oracle_cache={"up": True, "relay_ok": True},
        expect_final_online=False,
        expect_first_card="checking",
        expect_confident_card=True,   # the brief stale-up green is expected
        expect_red_by=9.0,
        card_contrast=True,
        forbid_red_flash=False,
        horizon=15.0,
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
        # the server died during background. v8.5: ticks every 8 s, so background
        # at 3 / foreground at 6 (no event), and the first post-foreground tick at
        # T=8 re-probes → red.
        name="resume-no-event-self-heals-to-red",
        relay_outcomes=[
            FetchOutcome(0.3, ok=True, up=True),    # T=0 cold check — up
            FetchOutcome(0.3, ok=True, up=False),   # T=8 self-healing tick — now down
            FetchOutcome(0.3, ok=True, up=False),
        ],
        oracle_cache={"up": True, "relay_ok": True},
        background_at=3.0,
        foreground_at=6.0,
        foreground_event="none",
        expect_final_online=False,
        forbid_red_flash=False,
        horizon=30.0,
    ),
    Scenario(
        # BUG 2 — frozen wedge (the "total KO, must kill the app" report).
        # The cold-launch probe is a ZOMBIE: started, then the app is suspended
        # mid-fetch and the socket dies, so it never resolves and leaves
        # checking=True stuck. NO resume event fires (Android PWA standalone
        # quirk). The server actually went down during the freeze. The ONLY
        # rescue is the self-healing tick — but with a stuck `checking` flag it
        # early-returns forever, so the app stays frozen until killed. The
        # watchdog must let a tick reclaim the stale flag and re-probe → red.
        # Without it, final stays online → FAIL. v8.5 note: the cache pre-paint
        # now shows the honest "Vérification…" (not a stale green) for the whole
        # wedge, so the orange is held until the reclaim — at 8 s ticks the
        # watchdog (14 s) clears on the 2nd tick (~16 s), so max_orange is raised
        # to 17 s. That long "checking" is the honest replacement for the old
        # frozen green; it's bounded by the watchdog and self-heals to red.
        name="zombie-probe-wedges-checking-self-heals",
        relay_outcomes=[
            FetchOutcome(None, zombie=True),         # #1 cold probe never resolves
            FetchOutcome(0.3, ok=True, up=False),    # #2 reclaimed probe — server down
        ],
        oracle_cache={"up": True, "relay_ok": True},  # honest "checking" pre-paint
        foreground_event="none",
        expect_final_online=False,
        forbid_red_flash=False,
        max_orange_s=17.0,
        horizon=50.0,
    ),
    Scenario(
        # BUG 1 — cold relay e2-micro slow across MORE than one tick. The relay
        # /status transport-misses on the first TWO probes (T=0 and the T=16
        # tick, the v8.5 ~16 s effective outage cadence) because the burstable VM
        # is cold, then recovers on the third. Home is up throughout. With
        # RELAY_DOWN_MISSES=3 two consecutive misses must NOT yet paint the false
        # "Relais injoignable" warn (the user's "message relais off"). The
        # relay-down indicator is purely advisory (a real WoL failure surfaces
        # instantly via postWol), so it must tolerate a multi-tick cold start: NO
        # warn, relay stays reachable, server shows green from the home fallback.
        name="cold-relay-two-tick-miss-no-false-warn",
        relay_outcomes=[
            FetchOutcome(None, ok=False),            # T=0 cold miss
            FetchOutcome(None, ok=False),            # T=16 still cold miss
            FetchOutcome(0.3, ok=True, up=True),     # T=32 relay warm
        ],
        home_outcomes=[FetchOutcome(0.3, ok=True)],
        expect_final_online=True,
        expect_final_relay_reachable=True,
        forbid_red_flash=True,
        forbid_warn_flash=True,
        horizon=50.0,
    ),
    Scenario(
        # v8.4 BUTTON — the reported bug AND the regression in one scenario.
        # Reopen with a <60 s localStorage cache (up); the relay answers up but
        # SWR-STALE (the steady state: PWA polls /15 s vs the relay's 5 s fresh
        # window). The cache pre-paint must NOT assert a confident green → first
        # button "checking". The probe then lands a live relay up (stale) and the
        # button MUST go green — it must NOT stay orange just because the relay
        # flagged stale (that was the v8.3 regression: stuck orange ~30 s+).
        # Fixed: checking → on. BuggyButton (pre-v8.3): on straight from cache.
        name="button-cache-up-stale-relay-honest-then-green",
        relay_outcomes=[FetchOutcome(0.3, ok=True, up=True, stale=True)],
        oracle_cache={"up": True, "relay_ok": True},
        expect_final_online=True,
        expect_first_button="checking",
        expect_confident_button=True,
        button_contrast=True,
        horizon=5.0,
    ),
    Scenario(
        # v8.4 BUTTON — regression guard. No cache; the relay answers up but
        # SWR-stale on every poll (the steady state for a healthy home). The
        # button MUST light the confident green — a stale-but-up relay is a real
        # server-side confirmation. The v8.3 bug left it stuck "checking" here.
        name="button-stale-relay-up-greens",
        relay_outcomes=[FetchOutcome(0.3, ok=True, up=True, stale=True)],
        expect_final_online=True,
        expect_first_button="on",
        expect_confident_button=True,
        horizon=20.0,
    ),
    Scenario(
        # v8.3 BUTTON — guard against over-suppression. A genuinely FRESH up
        # (relay up, stale=false, no cache) MUST light the confident green
        # "on" — the fix only gates cache/stale verdicts, not real ones.
        name="button-fresh-up-goes-confident-green",
        relay_outcomes=[FetchOutcome(0.3, ok=True, up=True)],
        expect_final_online=True,
        expect_first_button="on",
        expect_confident_button=True,
        horizon=5.0,
    ),
]


def fmt_paints(paints):
    return ", ".join(f"{t}s->{k}" for t, k in paints) or "(no transitions)"


def first_on(button_paints):
    # Time of the first confident-green ("on") button paint, or None.
    for t, k in button_paints:
        if k == "on":
            return t
    return None


def first_online_card(paints):
    # Time of the first confident-green ("online") card paint, or None.
    for t, k in paints:
        if k == "online":
            return t
    return None


def main():
    print("=" * 72)
    print("PWA v8 state-machine simulation — OldCascade (v7) vs V8")
    print("=" * 72)
    v8_pass = True
    contrast_ok = True
    button_contrast_ok = True
    card_contrast_ok = True
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
        # v8.3 button dimension: show the button timeline when the scenario
        # asserts on it, and (for button_contrast scenarios) prove BuggyButtonApp
        # paints the false confident green where the fixed app stays honest.
        if sc.expect_first_button is not None or sc.expect_confident_button is not None or sc.button_contrast:
            print(f"  [button     ] {fmt_paints(v8['button_paints'])}")
        if sc.button_contrast:
            buggy = evaluate(run(sc, BuggyButtonApp), sc)
            buggy_on, fixed_on = first_on(buggy["button_paints"]), first_on(v8["button_paints"])
            # The bug = a confident "on" the fixed app withholds: buggy lights it
            # while fixed never does, or buggy lights it strictly earlier.
            buggy_worse = buggy_on is not None and (fixed_on is None or buggy_on < fixed_on)
            if not buggy_worse:
                button_contrast_ok = False
                print(f"  [btn-contrast] FAIL  expected BuggyButton to assert a false "
                      f"confident green, but buggy_on={buggy_on} fixed_on={fixed_on}")
            else:
                print(f"  [btn-contrast] OK    buggy false-green at {buggy_on}s "
                      f"vs fixed {'never' if fixed_on is None else str(fixed_on)+'s'}  "
                      f"buggy: {fmt_paints(buggy['button_paints'])}")
        # v8.5 card dimension: prove BuggyCardApp paints the false confident green
        # CARD (from a cache pre-paint) where the fixed app stays neutral until a
        # live probe lands — the headline of the reported "green right after I
        # stopped the homelab" bug.
        if sc.card_contrast:
            buggy = evaluate(run(sc, BuggyCardApp), sc)
            buggy_oc, fixed_oc = first_online_card(buggy["paints"]), first_online_card(v8["paints"])
            buggy_worse = buggy_oc is not None and (fixed_oc is None or buggy_oc < fixed_oc)
            if not buggy_worse:
                card_contrast_ok = False
                print(f"  [card-contrast] FAIL  expected BuggyCard to assert a false "
                      f"confident green card, but buggy_oc={buggy_oc} fixed_oc={fixed_oc}")
            else:
                print(f"  [card-contrast] OK    buggy false-green card at {buggy_oc}s "
                      f"vs fixed {'never' if fixed_oc is None else str(fixed_oc)+'s'}  "
                      f"buggy: {fmt_paints(buggy['paints'])}")
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
    print(f"Button honesty (v8.3 fixed vs buggy baseline): "
          f"{'confirmed' if button_contrast_ok else 'BROKEN — see [btn-contrast] lines'}")
    print(f"Card honesty (v8.5 fixed vs buggy baseline): "
          f"{'confirmed' if card_contrast_ok else 'BROKEN — see [card-contrast] lines'}")
    return 0 if (v8_pass and contrast_ok and button_contrast_ok and card_contrast_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
