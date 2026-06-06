#!/usr/bin/env python3
"""
Deterministic state-machine simulator of app.js v8.7 status / probe timing.

Replays the relevant timer / fetch / resume logic on a synthetic clock so we
can verify the cold-radio resume behaviour without spinning up a browser.
Faster than the E2E (~50 ms vs ~2 min), so useful for tight iteration on the
timing logic. The browser E2E (`cold-radio-e2e.py`) is the source of truth —
this sim is a lightweight first line of defence.

## The model under test (v8.7)

`checkStatus()` fires `probe()`, which resolves EXACTLY ONCE to
`{up, relay_reachable}` and never rejects: one relay `/status` fetch
(`PROBE_TIMEOUT`, generous so a cold mobile radio warms inside the attempt) and,
on its failure, one direct-home fallback (`HOME_TIMEOUT`). No retry, no hold, no
streak. A `probe_gen` counter drops a stale in-flight probe that resolves after a
resume (the Android suspend-mid-fetch race). A `checking` watchdog
(`CHECK_WATCHDOG`) lets a wedged in-flight probe be reclaimed by a later tick. An
N-consecutive-miss debounce (`RELAY_DOWN_MISSES`) keeps the advisory "Relais
injoignable" cosmetic from crying wolf on a cold e2-micro.

### v8.7 — asymmetric verdict commit (confirm before red)

The headline of v8.7. The up/down verdict is no longer committed symmetrically:

- **UP → green, instantly** (optimistic and cheap — a confident green is
  reassuring and rarely wrong: the relay only says "up" after a real HEAD < 500).
- **DOWN → never on a single live verdict.** The first "down" paints the orange
  "Vérification…" card and fires ONE fast re-probe (`DOWN_RECHECK`); red lands
  only when `DOWN_CONFIRM` consecutive downs agree. Any "up" in between cancels
  back to green. The `down_streak` counter drives this; an `up` resets it.

This kills the transient FALSE RED the v8.6 raw single-probe verdict produced —
the user's report: "un rouge alors que c'était vert juste après, j'aurais dû
avoir au moins un orange pendant le check". Two real sources of a transient
`{up:false}`: (1) the relay's server-side SWR cache catching a momentary home
blip (HEAD returns ≥500 once), (2) a cold mobile radio whose relay /status AND
direct-home fallback both time out on the first cycle, then warm on the re-probe.
Both now show orange and self-correct to green, never a red flash. A genuine down
still reaches red, ~`DOWN_RECHECK` later — the accepted cost.

### Open / resume display rule (v8.7)

A recent (<60 s) localStorage verdict is reused on open/resume: an **up** cache
paints the confident green immediately (the real PWA also spins the refresh icon
to signal the in-flight re-check), and the live probe confirms or corrects. A
**down** cache is NOT pre-painted red (v8.7) — a stale cache must never show a
confident red; it shows the orange "Vérification…" card until the live probe(s)
settle. When nothing recent is cached, no verdict is shown → orange too.

`RawDownApp` is the contrast baseline for the false-red scenarios: it reproduces
the shipped v8.6 behaviour (a single live "down" commits red instantly, and a
cached "down" pre-paints red), so each `is_falsered_contrast` scenario proves the
v8.7 confirm-before-red fix is load-bearing. `OldCascadeApp` (the v7 cascade:
relay retry → home fallback → all-timeout HOLD → re-check) is kept as the
cold-radio orange-duration baseline — it reproduces the ~33 s orange that v8
removed, so each `is_contrast` scenario proves v8 is measurably better there.

A scenario passes for V8App if the final state matches the spec, no forbidden
paint (red / warn / checking) was emitted, the orange "Vérification…" card was
never shown longer than `max_orange_s`, and (when set) the card corrects to red
by `expect_red_by`. The `contrast` check asserts OldCascadeApp behaves worse on
the cold-radio scenarios; the `falsered` check asserts RawDownApp paints the
false red that V8App avoids.

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
STATUS_LOCAL_TTL = 60.0  # app.js STATUS_LOCAL_TTL_MS — localStorage reuse TTL
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
# tick; a real WoL failure surfaces instantly via postWol regardless, so the
# passive indicator can afford to be patient and avoid false alarms.
RELAY_DOWN_MISSES = 3    # app.js RELAY_DOWN_MISSES

# v8.7 — asymmetric down-confirmation. A live "down" verdict must be seen
# DOWN_CONFIRM times in a row before the red card is committed; the first
# unconfirmed down paints orange and re-probes after DOWN_RECHECK. Any "up"
# resets the streak. Tuned: 2 confirmations + a 2.5 s re-probe ≈ 2.5 s of honest
# orange before a genuine red — plenty to ride out a transient relay /status blip
# or a cold-radio first-cycle timeout, short enough that a truly-down server goes
# red promptly.
DOWN_CONFIRM = 2         # app.js DOWN_CONFIRM
DOWN_RECHECK = 2.5       # app.js DOWN_RECHECK_MS

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


@dataclass
class Scenario:
    name: str
    relay_outcomes: List[FetchOutcome] = field(default_factory=list)
    home_outcomes: List[FetchOutcome] = field(default_factory=list)
    has_relay: bool = True
    # localStorage status cache (<60 s). When set, both apps reuse it on cold
    # launch / resume as the confident green/red pre-paint before the probe
    # resolves.
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
    # When True, this scenario is a false-red contrast (v8.7): assert RawDownApp
    # (the shipped single-probe-down baseline) paints the forbidden red that
    # V8App's confirm-before-red avoids. Pairs with forbid_red_flash=True.
    is_falsered_contrast: bool = False
    horizon: float = 60.0
    # If set, an "offline" (red) card paint must land at or before this time.
    # Locks the v8.5 faster-correction (8 s poll): a "just stopped" home must
    # flip to red within ~one poll, not ~15 s — even when a recent cache reused
    # a green pre-paint.
    expect_red_by: Optional[float] = None


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

    # How a stale cached "down" is reused on open/resume: "offline" = pre-paint a
    # confident red (the shipped v8.6 / RawDownApp baseline); "checking" = paint
    # orange and let the live probe settle (v8.7 — never a confident red from a
    # stale cache). See _reuse_cache().
    PRE_PAINT_DOWN = "offline"

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
        self.down_streak = 0   # v8.7 — consecutive live "down" verdicts so far

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

    # ---- power button -----------------------------------------------------
    def set_button(self, kind):
        # Record a button transition (deduped). The button simply mirrors the
        # card's up/down verdict (v8.6 — no separate freshness gate).
        if kind != self.button:
            self.button = kind
            self.button_paints.append((round(self.clock.now, 2), kind))

    def _button_for(self, up, relay_ok):
        # up → confident green wake-done; down → the wake affordance, or
        # "unavailable" when the relay is unreachable too.
        if up:
            return "on"
        return "wake" if relay_ok else "unavailable"

    # ---- status card ------------------------------------------------------
    def _card_for(self, up):
        # The card mirrors the up/down verdict directly (v8.6).
        return "online" if up else "offline"

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
        self.paint(self._card_for(up))
        self.set_button(self._button_for(up, relay_ok))
        if not relay_ok:
            # Mirrors setFallbackState(): "Réveil indisponible" surfaces whether
            # the home is up (warn) or down (offline-relay-promoted).
            self.paint("warn-relay" if up else "offline-relay-promoted")

    # ---- lifecycle ---------------------------------------------------------
    def _reuse_cache(self):
        """Reuse a recent (<60 s) cache for an instant paint on open/resume.
        UP → confident green (optimistic; both apps). DOWN → governed by
        PRE_PAINT_DOWN: the shipped baseline pre-paints a confident red, while
        v8.7 refuses a confident red from a stale cache and shows orange
        'Vérification…' until the live probe settles. No-op without a cache."""
        cache = self.scenario.oracle_cache
        if cache is None:
            return
        self.relay_reachable = bool(cache.get("relay_ok", True))
        if bool(cache.get("up")):
            self.is_online = True
            self.has_confirmed_state = True
            self.paint(self._card_for(True))
            self.set_button(self._button_for(True, self.relay_reachable))
        elif self.PRE_PAINT_DOWN == "offline":
            self.is_online = False
            self.has_confirmed_state = True
            self.paint(self._card_for(False))
            self.set_button(self._button_for(False, self.relay_reachable))
        else:
            # v8.7 — stale "down" → orange, never a confident red. Leave
            # has_confirmed_state False so check_status() paints "checking".
            self.has_confirmed_state = False
            self.paint("checking")

    def start_app(self):
        self._reuse_cache()
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
        # suspended probe, reset the down streak (a stale down episode from
        # before the suspend must not count), reuse a recent cache (per
        # PRE_PAINT_DOWN) or drop confirmed-state so the re-probe shows orange,
        # then check_status.
        self.checking = False
        self._invalidate_inflight()
        self.down_streak = 0
        if self.scenario.oracle_cache is not None:
            self._reuse_cache()
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
    """v8.7 — one probe, one generous timeout, generation guard, no cascade; a
    `checking` watchdog so a wedged in-flight probe can't freeze re-probing; an
    N-consecutive-miss debounce on the (advisory) relay-down cosmetic; and the
    v8.7 asymmetric verdict commit — UP is instant, DOWN needs DOWN_CONFIRM
    consecutive verdicts with an orange re-check in between (see _settle)."""

    # v8.7 — a stale cached "down" shows orange, never a confident red.
    PRE_PAINT_DOWN = "checking"

    def __init__(self, clock, scenario):
        super().__init__(clock, scenario)
        self.probe_gen = 0
        self.check_started_at = 0.0
        # Consecutive relay-miss counter feeding the relay-down debounce.
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
        # N-consecutive-miss debounce on the relay-down cosmetic only (mirrors
        # checkStatus().then in app.js). A relay miss stays optimistic
        # (eff=True, no "Relais injoignable") until RELAY_DOWN_MISSES misses in a
        # row — a cold e2-micro can miss across more than one tick. Any
        # answered/successful probe resets the streak. Invariant:
        # streak<RELAY_DOWN_MISSES while reachable.
        if relay_ok:
            eff, self.relay_miss_streak = True, 0
        else:
            self.relay_miss_streak += 1
            eff = not (self.relay_miss_streak >= RELAY_DOWN_MISSES or not self.relay_reachable)
        # v8.7 asymmetric verdict commit. UP commits green instantly (optimistic)
        # and resets the down streak. DOWN is held: the first live "down" paints
        # orange and fires ONE fast re-probe; red is committed only once
        # DOWN_CONFIRM consecutive downs agree. An already-confirmed red (streak
        # already ≥ DOWN_CONFIRM) re-commits red without flickering back to orange.
        if up:
            self.down_streak = 0
            self._apply(True, eff)
            return
        self.down_streak += 1
        if self.down_streak >= DOWN_CONFIRM:
            self._apply(False, eff)
        else:
            # Unconfirmed down → orange "Vérification…" + one fast re-probe. Clear
            # `checking` ourselves (we are NOT settling) so the re-probe runs.
            self.checking = False
            self.paint("checking")
            self.clock.after(DOWN_RECHECK, self.check_status)


class RawDownApp(V8App):
    """Contrast baseline = the shipped v8.6 behaviour: a single live "down"
    commits red instantly, and a stale cached "down" pre-paints a confident red.
    This is the source of the user's false red — a transient relay /status
    "down" or a cold-radio double-timeout painted red, then flipped green a few
    seconds later. Kept side-by-side so each is_falsered_contrast scenario proves
    the v8.7 confirm-before-red fix is load-bearing."""

    # Shipped behaviour: a stale cached "down" pre-painted a confident red.
    PRE_PAINT_DOWN = "offline"

    def _settle(self, gen, up, relay_ok):
        if gen != self.probe_gen:
            return
        if relay_ok:
            eff, self.relay_miss_streak = True, 0
        else:
            self.relay_miss_streak += 1
            eff = not (self.relay_miss_streak >= RELAY_DOWN_MISSES or not self.relay_reachable)
        self._apply(up, eff)   # raw — no down-confirmation (the v8.6 behaviour)


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
        # THE cold-radio bug. Cold reopen with NO cache: the radio takes 6.5 s to
        # warm — past the old 5 s timeout but inside v8's 8 s budget. v8: orange
        # until the relay answers at 6.5 s → green, NO red. OldCascade: relay
        # times out at 5 s → retry (another 5 s) → home fallback (5 s) → HOLD 3 s
        # → re-check… long orange and a likely false red. is_contrast asserts old
        # does worse.
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
        # and the relay-down warn appears only after the debounce confirms it
        # (RELAY_DOWN_MISSES=3 consecutive misses). Detection survives a GCP
        # relay outage. Each miss settles ≈ PROBE+HOME ≈ 8.3 s (home up, fast
        # fallback). The 8 s probe timeout exceeds the 8 s tick, so a tick fired
        # mid-probe is skipped and the effective re-probe cadence is ~16 s; misses
        # at ~8 / ~24 / ~40 s → the 3rd confirms the warn → horizon > ~40 s.
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
        # DEBOUNCE PAYOFF. A single relay /status transport miss (a slow-but-alive
        # e2-micro or a last-mile blip), then the relay recovers on the next tick.
        # The lone miss must NEVER paint the "Relais injoignable" warn nor disable
        # the wake button — relay stays reachable throughout. (Not an is_contrast
        # scenario: the tape's repeat-last semantics make OldCascade consume both
        # relay outcomes at T=0 via its retry, so it can't be compared cleanly on
        # a single-miss-then-recover tape — this stands on its own as a
        # regression guard.)
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
        # Both relay and home down → a GENUINE total outage still reaches red.
        # v8.7 cost: each probe cycle is PROBE+HOME ≈ 13 s, and DOWN_CONFIRM=2
        # means two of them (+ the 2.5 s re-probe gap) before red ≈ 28.5 s of
        # honest orange. That's the deliberate trade for never flashing a false
        # red — and it's still better than the v7 cascade (~31 s, see contrast).
        # The relay warn hardens once the 3rd consecutive miss lands. Not a
        # "bounded ≤13 s" property anymore (v8.6 had that but at the price of the
        # false red); the property here is "a real outage converges to red".
        name="relay-and-home-down-confirms-red",
        relay_outcomes=[FetchOutcome(None, ok=False)],
        home_outcomes=[FetchOutcome(None, ok=False)],
        expect_final_online=False,
        expect_final_relay_reachable=False,
        forbid_red_flash=False,
        forbid_warn_flash=False,
        max_orange_s=29.0,
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
        # v8.7: each cycle is answered(0.3)+home-timeout(5) ≈ 5.3 s, ×2 confirms
        # (+2.5 s gap) → red ≈ 13 s, so the horizon must outlast that.
        horizon=16.0,
    ),
    Scenario(
        # v8.6 REUSE — cache <60 s + server still up. The cache pre-paint reuses
        # the confident green immediately and the live probe confirms it; green
        # throughout, no red, no warn. (Pre-v8.6 this painted a transient orange
        # "Vérification…" from the honesty gate; v8.6 reuses the recent verdict.)
        name="cache-up-server-up-reused-green",
        relay_outcomes=[FetchOutcome(0.3, ok=True, up=True)],
        oracle_cache={"up": True, "relay_ok": True},
        expect_final_online=True,
        forbid_red_flash=True,
        forbid_warn_flash=True,
        horizon=5.0,
    ),
    Scenario(
        # v8.7 THE FIX — the user's report. The relay's /status answers a
        # transient "down" once (its server-side SWR cache caught a momentary
        # home blip — a HEAD ≥ 500), then "up" on the next probe. v8.6 committed
        # red on the first down → the red-that-was-green-a-moment-later. v8.7
        # paints orange "Vérification…" and re-probes: green by ~3 s, NEVER red.
        # RawDownApp (is_falsered_contrast) reds at 0.3 s, proving the fix matters.
        name="transient-relay-false-down-no-red",
        relay_outcomes=[
            FetchOutcome(0.3, ok=True, up=False),    # transient false "down"
            FetchOutcome(0.3, ok=True, up=True),      # truth: up
        ],
        expect_final_online=True,
        forbid_red_flash=True,
        forbid_warn_flash=True,
        is_falsered_contrast=True,
        horizon=8.0,
    ),
    Scenario(
        # v8.7 THE FIX — cold-radio variant. On a cold mobile radio the relay
        # /status times out (8 s) AND the direct-home fallback times out (5 s) on
        # the first cycle → a "down" verdict at ~13 s, even though the server is
        # fine; the radio then warms and the re-probe gets "up". v8.6 flashed red
        # at 13 s; v8.7 holds orange the whole cold cycle and corrects to green at
        # ~15.8 s, NEVER red. Long orange is honest here (the radio really was
        # cold) — what matters is the absence of the false red.
        name="cold-radio-double-timeout-false-down-recovers",
        relay_outcomes=[
            FetchOutcome(None, ok=False),             # cold: relay times out
            FetchOutcome(0.3, ok=True, up=True),       # warm: relay answers up
        ],
        home_outcomes=[FetchOutcome(None, ok=False)],  # cold: home fallback also times out
        expect_final_online=True,
        expect_final_relay_reachable=True,
        forbid_red_flash=True,
        forbid_warn_flash=True,
        max_orange_s=16.5,
        is_falsered_contrast=True,
        horizon=20.0,
    ),
    Scenario(
        # v8.7 THE FIX — stale cache says "down", server is actually up. On
        # reopen v8.6 pre-painted the cached "down" as a confident red (then the
        # probe corrected to green) — a red flash from a stale cache. v8.7 never
        # pre-paints red from a cache: it shows orange until the live probe
        # settles green. RawDownApp reds at T=0 (the pre-paint), proving the fix.
        name="cache-down-server-actually-up-no-red",
        relay_outcomes=[FetchOutcome(0.3, ok=True, up=True)],
        oracle_cache={"up": False, "relay_ok": True},
        expect_final_online=True,
        forbid_red_flash=True,
        forbid_warn_flash=True,
        is_falsered_contrast=True,
        horizon=5.0,
    ),
    Scenario(
        # v8.7 REGRESSION GUARD — a stale cache says "down" AND the server really
        # is down. The fix must not make a genuine red unreachable: cache → orange,
        # then two consecutive live downs confirm → red by ~3 s. (Not a falsered
        # contrast — here red is correct and expected.)
        name="cache-down-server-still-down-confirms-red",
        relay_outcomes=[FetchOutcome(0.3, ok=True, up=False)],
        oracle_cache={"up": False, "relay_ok": True},
        expect_final_online=False,
        expect_final_relay_reachable=True,
        expect_red_by=4.0,
        forbid_red_flash=False,
        forbid_warn_flash=True,
        horizon=8.0,
    ),
    Scenario(
        # v8.6 TRADE-OFF + fast correction. The user stopped the homelab, then
        # reopens: the localStorage cache (<60 s) still says "up", so the app
        # reuses the confident green pre-paint (the accepted brief cache-vs-reality
        # window). The relay's first /status still answers up (its server-side SWR
        # cache caught the home while it was up), then the next 8 s poll catches
        # the relay's now-down verdict → red by ~8.3 s, not ~15 s (expect_red_by).
        # v8.7: the second poll's "down" is the FIRST down → orange + a 2.5 s
        # re-probe; the third (repeat-last "down") confirms → red ≈ 11 s. One
        # DOWN_RECHECK slower than v8.6's ~8 s, the deliberate cost of never
        # flashing a false red. Still far better than the 15 s pre-v8.5 window.
        name="cache-up-server-just-stopped-corrects-red-fast",
        relay_outcomes=[
            FetchOutcome(0.3, ok=True, up=True),    # T=0 relay still cached "up"
            FetchOutcome(0.3, ok=True, up=False),   # T=8 relay bg-refresh caught down
        ],
        oracle_cache={"up": True, "relay_ok": True},
        expect_final_online=False,
        expect_red_by=12.0,
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
        # the server died during background. Ticks every 8 s, so background at 3 /
        # foreground at 6 (no event), and the first post-foreground tick at T=8
        # re-probes → red.
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
        # Frozen wedge (the "total KO, must kill the app" report). The cold-launch
        # probe is a ZOMBIE: started, then the app is suspended mid-fetch and the
        # socket dies, so it never resolves and leaves checking=True stuck. NO
        # resume event fires (Android PWA standalone quirk). The server actually
        # went down during the freeze. The ONLY rescue is the self-healing tick —
        # but with a stuck `checking` flag it early-returns forever, so the app
        # stays frozen until killed. The watchdog must let a tick reclaim the
        # stale flag and re-probe → red. Without it, final stays online → FAIL.
        # v8.6 note: the cache pre-paint reuses the confident green for the whole
        # wedge (the accepted trade-off — a stale green, not an orange, until the
        # watchdog reclaims at ~16 s and corrects to red). So there's no orange to
        # bound here; the property under test is the self-heal to red.
        name="zombie-probe-wedges-checking-self-heals",
        relay_outcomes=[
            FetchOutcome(None, zombie=True),         # #1 cold probe never resolves
            FetchOutcome(0.3, ok=True, up=False),    # #2 reclaimed probe — server down
        ],
        oracle_cache={"up": True, "relay_ok": True},  # reused green during the wedge
        foreground_event="none",
        expect_final_online=False,
        forbid_red_flash=False,
        horizon=50.0,
    ),
    Scenario(
        # Cold relay e2-micro slow across MORE than one tick. The relay /status
        # transport-misses on the first TWO probes (T=0 and the T=16 tick, the
        # ~16 s effective outage cadence) because the burstable VM is cold, then
        # recovers on the third. Home is up throughout. With RELAY_DOWN_MISSES=3
        # two consecutive misses must NOT yet paint the false "Relais injoignable"
        # warn (the user's "message relais off"). The relay-down indicator is
        # purely advisory (a real WoL failure surfaces instantly via postWol), so
        # it must tolerate a multi-tick cold start: NO warn, relay stays
        # reachable, server shows green from the home fallback.
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
]


def fmt_paints(paints):
    return ", ".join(f"{t}s->{k}" for t, k in paints) or "(no transitions)"


def main():
    print("=" * 72)
    print("PWA v8.7 state-machine simulation — V8 fix vs baselines (RawDown, OldCascade)")
    print("=" * 72)
    v8_pass = True
    contrast_ok = True
    falsered_ok = True
    for sc in SCENARIOS:
        print(f"\n## {sc.name}")
        v8 = evaluate(run(sc, V8App), sc)
        v8_verdict = "PASS" if not v8["issues"] else "FAIL"
        if v8_verdict != "PASS":
            v8_pass = False
        print(f"  [v8.7 fixed ] {v8_verdict}  orange_max={v8['max_orange']}s  "
              f"paints: {fmt_paints(v8['paints'])}")
        if v8["issues"]:
            print(f"                 issues: {'; '.join(v8['issues'])}")
        # False-red contrast: the shipped single-probe-down baseline must paint a
        # red that the v8.7 confirm-before-red avoids. If it doesn't, the scenario
        # isn't actually exercising the fix.
        if sc.is_falsered_contrast:
            raw = evaluate(run(sc, RawDownApp), sc)
            raw_reds = bool(any(i.startswith("unexpected RED") for i in raw["issues"]))
            if not raw_reds:
                falsered_ok = False
                print(f"  [falsered   ] FAIL  expected RawDown to flash a false red, "
                      f"but none seen — paints: {fmt_paints(raw['paints'])}")
            else:
                reds = [t for t, k in raw["paints"] if k == "offline"]
                print(f"  [falsered   ] OK    RawDown flashes the false red at {reds} "
                      f"(v8.7 avoids it)")
        # Cold-radio contrast: the v7 cascade must do measurably worse — longer
        # orange and/or a forbidden paint v8 avoids.
        if sc.is_contrast:
            old = evaluate(run(sc, OldCascadeApp), sc)
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
    print(f"v8.7 fixed: {'all scenarios PASS' if v8_pass else 'AT LEAST ONE SCENARIO FAILED'}")
    print(f"False-red (v8.7 avoids the red RawDown flashes): "
          f"{'confirmed' if falsered_ok else 'BROKEN — see [falsered] lines'}")
    print(f"Contrast (v8 better than v7 on cold-radio): "
          f"{'confirmed' if contrast_ok else 'BROKEN — see [contrast] lines'}")
    return 0 if (v8_pass and contrast_ok and falsered_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
