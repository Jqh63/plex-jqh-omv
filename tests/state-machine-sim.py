#!/usr/bin/env python3
"""
Deterministic state-machine simulator of app.js status / probe timing.

Replays the relevant timer / fetch / visibilitychange logic on a
synthetic clock so we can verify the cold-radio resume race without
spinning up a browser. Faster than the E2E (~50 ms vs ~2 min), so
useful for tight iteration on timing logic. The browser E2E
(`cold-radio-e2e.py`) is the source of truth — this sim is a
lightweight first line of defence.

Six implementations side-by-side:
  - BuggyApp: pre-v4.3 logic (justResumed flag, checkStatus retry only)
  - V43App:   v4.3 logic (resumeUntil window, both handlers defer once)
  - V44App:   v4.4 logic (v4.3 + 2-fail probe streak)
  - V45App:   v4.5 logic (v4.4 + 2-fail status streak)
  - FixedApp: v5.0 logic (v4.5 + cached-state paint on startup + adaptive
              tick after defer instead of waiting the regular tick).
              v5.1 retuned the constants for tight detection. v5.3 trims
              the status timeout further (3 s → 2 s) so steady-state
              server-dies-mid-tick detection completes ≤ 24 s — see
              v51-steady-state-server-dies-mid-tick for the fenced bound.
  - LiveApp:  v6.0 logic (FixedApp − cache + probe-success closes resume
              window AND bypasses the 2-fail status streak). Targets the
              two user-reported scenarios (cold open with off server) that
              took 14-16 s to converge to red — now ~2 s. Trade-off: the
              "server up + cold radio only on the status path" pessimistic
              scenario flashes red for ~13 s before recovering (acknowledged
              via allow_live_to_fail=True).

For each scenario we feed sequences of (latency, ok) outcomes for the
status and probe fetches. latency=None means the fetch times out at
the caller-defined timeout (5 s status, 2.5 s probe).

A scenario passes if the final state matches the spec AND no
forbidden paint event was emitted at any point in the timeline. The
canonical bug is "RED flash on resume when fetches eventually
succeed" — that's the cold-radio-fail-then-OK scenario.

Run:
  python3 tests/state-machine-sim.py

Requires Python 3.12+, no other deps. See tests/README.md for context.
"""

import heapq
from dataclasses import dataclass, field
from typing import Optional, Callable, List

STATUS_TIMEOUT = 2.0  # v5.3: 3 s → 2 s. Caps the orange "Vérification..."
                      # card on cold launch without cache. Typical RTT to the
                      # home box is <500 ms; 2 s is generous. The 2-fail
                      # streak still absorbs cold-radio transient blips.
PROBE_TIMEOUT = 2.5
CHECK_INTERVAL = 15.0  # v5.1: 30 s → 15 s. Halves the "status up while server
                       # is actually down" worst-case window. Foreground-only
                       # tick (Android pauses timers in background), so battery
                       # impact is bound to the user's screen-on time.
RESUME_RETRY = 5.0
RESUME_WINDOW = 6.0   # seconds — must match RESUME_GRACE_MS / 1000 in app.js
ADAPTIVE_TICK = 5.0   # v5.1: 10 s → 5 s. Bridges the gap between a streak=1
                      # fail and the next regular tick. Must stay > 0 so that
                      # back-to-back failures can't fire faster than the
                      # 3 s status timeout allows.


@dataclass
class FetchOutcome:
    """latency=None means the fetch times out at the caller-defined timeout."""
    latency: Optional[float]
    ok: bool = True
    # v7.0 — when this FetchOutcome describes a relay /status response, `up`
    # carries the JSON body's `up` boolean. Pre-v7 apps ignore it.
    up: bool = True
    # v7.5 — for a FAILED relay /status outcome (ok=False), distinguishes an
    # *answered* failure (the relay returned an HTTP response: 503/404/5xx/bad
    # shape → relay alive, /wol still works) from a *transport* failure
    # (timeout/network → relay unreachable). OracleApp keeps the relay
    # reachable on an answered failure (wake button stays enabled) and skips
    # the retry (deterministic). Ignored when ok=True. See ADR 2026-05-27
    # addendum + app.js fetchStatusFromRelay().
    answered: bool = False


@dataclass
class Scenario:
    name: str
    status_outcomes: List[FetchOutcome] = field(default_factory=list)
    probe_outcomes: List[FetchOutcome] = field(default_factory=list)
    # v7.0 — outcomes for the relay's GET /status (single PWA fetch). When
    # populated, OracleApp runs the scenario; pre-v7 apps are skipped.
    oracle_outcomes: List[FetchOutcome] = field(default_factory=list)
    # v7.0 — outcomes for the direct-home fallback fetch (no-cors). Only
    # consumed by OracleApp after oracle_outcomes are exhausted with failures.
    home_fallback_outcomes: List[FetchOutcome] = field(default_factory=list)
    resume_at: float = 0.0   # 0 = cold start, >0 = background→foreground at that time
    # v7.7 — resume layered-defence. Model the poll interval being paused while
    # the PWA is in the background and which event (if any) fires on the way
    # back to foreground. `resume_at` (above) is the legacy "visibilitychange
    # fired cleanly" path; these three let a scenario exercise the Android PWA
    # standalone case where the interval was cleared on background and the
    # foreground return does NOT carry a visibilitychange.
    #   background_at:    >0 → app goes hidden at this time (interval pauses)
    #   foreground_at:    >0 → app returns to foreground at this time
    #   foreground_event: which event fires on return —
    #                     "visibilitychange" | "focus" | "none"
    background_at: float = 0.0
    foreground_at: float = 0.0
    foreground_event: str = "visibilitychange"
    # v5.0 — if set, app starts with isOnline/relayReachable from the cache
    # and hasConfirmedState=True (so fire_status won't paint "checking").
    # Mirrors what loadCachedState() returns on cold launch.
    cached_state: Optional[dict] = None
    # v7.0 — localStorage status cache (<60 s window). OracleApp pre-paints
    # this on cold launch before firing /status.
    oracle_cache: Optional[dict] = None
    expect_final_online: bool = True
    expect_final_relay_reachable: bool = True
    forbid_red_flash: bool = True
    forbid_warn_flash: bool = True
    # v5.0 — when True, fail if a "checking" paint event was recorded.
    # Use for cached-state scenarios where the prior state should stay
    # visible (no orange "Vérification..." flash).
    forbid_checking_paint: bool = False
    # v6.0 — when True, LiveApp is allowed to fail this scenario without
    # failing the overall sim. Use for the pessimistic "server up + cold
    # radio only on status path" scenario that LiveApp trades a brief red
    # flash for fast convergence on the realistic user scenarios.
    allow_live_to_fail: bool = False
    horizon: float = 40.0


class Clock:
    def __init__(self):
        self.now = 0.0
        self.events = []
        self._seq = 0

    def at(self, when, cb):
        self._seq += 1
        heapq.heappush(self.events, (when, self._seq, cb))

    def after(self, delta, cb):
        self.at(self.now + delta, cb)

    def run_until(self, t_end):
        while self.events and self.events[0][0] <= t_end:
            when, _, cb = heapq.heappop(self.events)
            self.now = when
            cb()
        self.now = t_end


class App:
    """Base — subclasses define on_status_fail / on_probe_fail / window logic."""

    # v5.0 — flip True in FixedApp only. When False, fire_status always
    # paints "checking" regardless of has_confirmed_state (pre-v5.0 behavior).
    SKIP_CHECKING_PAINT_WHEN_CONFIRMED = False
    # v5.0 — flip True in FixedApp only. When False, start_app ignores any
    # cached_state in the scenario (pre-v5.0 didn't persist state).
    LOAD_CACHED_STATE = False

    def __init__(self, clock, scenario):
        self.clock = clock
        self.scenario = scenario
        self.config = True
        self.is_online = True
        self.relay_reachable = True
        self.checking = False
        self.relay_probing = False
        self.check_interval_id = None
        self.just_resumed = False        # BuggyApp flag
        self.resume_until = 0.0          # V43App+ timestamp
        self.resume_retry_id = None
        # v5.0 — set True once setOnline/setOffline has fired. While False,
        # fire_status paints "checking"; while True, fire_status leaves the
        # prior visual alone and only records "spinning" (sub-text only).
        self.has_confirmed_state = False
        self._status_i = 0
        self._probe_i = 0
        self.paints = []

    def paint(self, kind):
        self.paints.append((round(self.clock.now, 2), kind))

    def _next_status(self):
        if self._status_i >= len(self.scenario.status_outcomes):
            return FetchOutcome(latency=0.1, ok=True)
        out = self.scenario.status_outcomes[self._status_i]
        self._status_i += 1
        return out

    def _next_probe(self):
        if self._probe_i >= len(self.scenario.probe_outcomes):
            return FetchOutcome(latency=0.1, ok=True)
        out = self.scenario.probe_outcomes[self._probe_i]
        self._probe_i += 1
        return out

    def fire_status(self):
        if self.checking or not self.config:
            return
        self.checking = True
        # v5.0 (FixedApp only): only paint the orange "checking" card when
        # we don't have a known state yet. Otherwise just spin the refresh
        # icon silently. Older implementations always paint "checking" —
        # the class-level flag preserves that behavior for side-by-side
        # comparison.
        if not (self.SKIP_CHECKING_PAINT_WHEN_CONFIRMED and self.has_confirmed_state):
            self.paint("checking")
        out = self._next_status()
        if out.latency is None or out.latency >= STATUS_TIMEOUT:
            self.clock.after(STATUS_TIMEOUT, lambda: self._status_done(False))
        else:
            self.clock.after(out.latency, lambda: self._status_done(out.ok))
        self.fire_probe()   # mirror app.js: probeRelay() fires synchronously inside checkStatus

    def _status_done(self, ok):
        self.checking = False
        if ok:
            self.on_status_ok()
        else:
            self.on_status_fail()

    def fire_probe(self):
        if self.relay_probing or not self.config:
            return
        self.relay_probing = True
        out = self._next_probe()
        if out.latency is None or out.latency >= PROBE_TIMEOUT:
            self.clock.after(PROBE_TIMEOUT, lambda: self._probe_done(False))
        else:
            self.clock.after(out.latency, lambda: self._probe_done(out.ok))

    def _probe_done(self, ok):
        self.relay_probing = False
        if ok:
            self.on_probe_ok()
        else:
            self.on_probe_fail()

    def on_status_ok(self):
        self.is_online = True
        self.has_confirmed_state = True
        self.paint("online")

    def on_status_fail(self):
        self.is_online = False
        self.has_confirmed_state = True
        self.paint("offline")

    def on_probe_ok(self):
        prev = self.relay_reachable
        self.relay_reachable = True
        if not prev:
            self.paint("relay-ok-flip")

    def on_probe_fail(self):
        prev = self.relay_reachable
        self.relay_reachable = False
        if prev:
            if self.is_online:
                self.paint("warn-relay")
            else:
                self.paint("offline-relay-promoted")

    def start_app(self):
        # Mirror app.js startApp(): reset isOnline/wolSent/checking/relayReachable
        # before opening the window. Without this, the simulator misses the
        # cold-launch state where the user's PWA opens with isOnline=false from
        # the start and the relay paints (warn vs promoted) are wrong.
        self.is_online = False
        self.relay_reachable = True
        self.checking = False
        self.has_confirmed_state = False
        self.just_resumed = True
        self.resume_until = self.clock.now + RESUME_WINDOW
        # v5.0 — if cache loading is enabled (FixedApp only) and the
        # scenario provides a cached state, mirror what app.js does after
        # loadCachedState(): set in-memory state + call setOnline/setOffline
        # (which paints + sets has_confirmed_state).
        if self.LOAD_CACHED_STATE and self.scenario.cached_state is not None:
            self.is_online = bool(self.scenario.cached_state.get("is_online"))
            self.relay_reachable = bool(self.scenario.cached_state.get("relay_reachable", True))
            if self.is_online:
                self.on_status_ok()
            else:
                self.on_status_fail()
        self.fire_status()
        self.check_interval_id = "tick"
        self._schedule_next_tick()

    def _schedule_next_tick(self):
        def tick():
            if self.check_interval_id != "tick":
                return
            self.fire_status()
            self._schedule_next_tick()
        self.clock.after(CHECK_INTERVAL, tick)

    def on_resume(self):
        self.checking = False
        self.relay_probing = False
        self.just_resumed = True
        self.resume_until = self.clock.now + RESUME_WINDOW
        if self.resume_retry_id:
            self.resume_retry_id = None
        self.fire_status()
        if self.check_interval_id != "tick":
            self.check_interval_id = "tick"
            self._schedule_next_tick()


class BuggyApp(App):
    """Pre-v4.3 logic — checkStatus has +5s retry, probeRelay doesn't defer."""

    def on_status_fail(self):
        self.is_online = False
        self.paint("offline")
        if self.just_resumed:
            self.just_resumed = False
            self.clock.after(RESUME_RETRY, lambda: self.fire_status())


class V43App(App):
    """v4.3 logic — both handlers defer first failure inside resumeUntil window.

    Fixes the original cold-radio-resume bug (PR #21). Still flips relay to
    false on the FIRST post-window probe failure though, which produces a
    false-positive 'Réveil indisponible' when the server is also down and the
    cold radio takes longer than 6 s to warm up.
    """

    def _in_resume_window(self):
        return self.resume_until > 0 and self.clock.now < self.resume_until

    def on_status_ok(self):
        super().on_status_ok()
        self.resume_retry_id = None

    def on_status_fail(self):
        if self._in_resume_window():
            self.clock.after(RESUME_RETRY, lambda: self.fire_status())
            return
        self.is_online = False
        self.paint("offline")

    def on_probe_fail(self):
        if self._in_resume_window() and self.relay_reachable:
            return
        prev = self.relay_reachable
        self.relay_reachable = False
        if prev:
            if self.is_online:
                self.paint("warn-relay")
            else:
                self.paint("offline-relay-promoted")


class V44App(App):
    """v4.4 logic — v4.3 window + universal 2-fail probe streak.

    Fixes the post-v4.3 relay false-positive (cold-radio probe leaks past
    the 6 s window via the checkStatus retry's probe). Still flips status
    (isOnline) on the first post-window status failure though, which
    produces a 'serveur éteint' RED false-positive when the radio is also
    cold on the status fetch.
    """

    def __init__(self, clock, scenario):
        super().__init__(clock, scenario)
        self.probe_fail_streak = 0

    def _in_resume_window(self):
        return self.resume_until > 0 and self.clock.now < self.resume_until

    def on_status_ok(self):
        super().on_status_ok()
        self.resume_retry_id = None

    def on_status_fail(self):
        if self._in_resume_window():
            self.clock.after(RESUME_RETRY, lambda: self.fire_status())
            return
        self.is_online = False
        self.paint("offline")

    def on_probe_ok(self):
        self.probe_fail_streak = 0
        super().on_probe_ok()

    def on_probe_fail(self):
        if self._in_resume_window() and self.relay_reachable:
            return
        self.probe_fail_streak += 1
        if self.relay_reachable and self.probe_fail_streak < 2:
            return
        prev = self.relay_reachable
        self.relay_reachable = False
        if prev:
            if self.is_online:
                self.paint("warn-relay")
            else:
                self.paint("offline-relay-promoted")


class V45App(App):
    """v4.5 logic — v4.4 + symmetric 2-fail status streak.

    Fixes the post-v4.4 server-up false-positive (cold-radio status leaks
    past the window + retry). Still has the 'Vérification...' UX problem
    on cold launches: up to ~30 s of orange pulsing before the next tick
    resolves the state — user complaint addressed by v5.0.
    """

    def __init__(self, clock, scenario):
        super().__init__(clock, scenario)
        self.probe_fail_streak = 0
        self.status_fail_streak = 0

    def _in_resume_window(self):
        return self.resume_until > 0 and self.clock.now < self.resume_until

    def on_status_ok(self):
        self.status_fail_streak = 0
        super().on_status_ok()
        self.resume_retry_id = None

    def on_status_fail(self):
        if self._in_resume_window():
            self.clock.after(RESUME_RETRY, lambda: self.fire_status())
            return
        self.status_fail_streak += 1
        if self.status_fail_streak < 2:
            return
        self.is_online = False
        self.has_confirmed_state = True
        self.paint("offline")

    def on_probe_ok(self):
        self.probe_fail_streak = 0
        super().on_probe_ok()

    def on_probe_fail(self):
        if self._in_resume_window() and self.relay_reachable:
            return
        self.probe_fail_streak += 1
        if self.relay_reachable and self.probe_fail_streak < 2:
            return
        prev = self.relay_reachable
        self.relay_reachable = False
        if prev:
            if self.is_online:
                self.paint("warn-relay")
            else:
                self.paint("offline-relay-promoted")


class FixedApp(V45App):
    """v5.0 logic — V45App + cached-state paint on startup + adaptive tick.

    Two changes over v4.5:

      A. On cold launch, if a recent state is cached (localStorage TTL
         5 min), paint it immediately and skip the orange "Vérification..."
         card. Background re-verify silently. The user sees the prior
         state instead of an orange flash.

      B. After a deferred status fail (streak=1), schedule a follow-up
         check at +ADAPTIVE_TICK s instead of waiting up to CHECK_INTERVAL
         for the next regular tick. With v5.1's tighter constants the
         regular tick (15 s) often beats the adaptive (+5 s) in the cold-
         radio path; the adaptive's main value is now the steady-state
         server-dies-mid-tick path where it bridges from streak=1 to
         streak=2 ~7 s before the next regular tick.
    """

    SKIP_CHECKING_PAINT_WHEN_CONFIRMED = True
    LOAD_CACHED_STATE = True

    def __init__(self, clock, scenario):
        super().__init__(clock, scenario)
        self.adaptive_tick_id = None

    def _clear_adaptive_tick(self):
        # We can't actually cancel scheduled events in our simple Clock;
        # fire_status's `if self.checking` guard prevents double-runs.
        self.adaptive_tick_id = None

    def on_status_ok(self):
        super().on_status_ok()
        self._clear_adaptive_tick()

    def on_status_fail(self):
        if self._in_resume_window():
            self.clock.after(RESUME_RETRY, lambda: self.fire_status())
            return
        self.status_fail_streak += 1
        if self.status_fail_streak < 2:
            # Adaptive tick (v5.0 B, v5.1 retuned to +5 s): schedule a
            # follow-up check ADAPTIVE_TICK seconds out instead of waiting
            # for the regular CHECK_INTERVAL tick.
            self._clear_adaptive_tick()
            self.adaptive_tick_id = "scheduled"
            self.clock.after(ADAPTIVE_TICK, lambda: self.fire_status())
            return
        self.is_online = False
        self.has_confirmed_state = True
        self.paint("offline")


class LiveApp(FixedApp):
    """v6.0 logic — FixedApp minus the cache plus radio-warm bypass.

    Two changes from FixedApp:

      A. No cached state load. The 15 min TTL could show stale green from
         yesterday when the server is now off, and the 14-16 s convergence
         to red felt broken to family users ("affiche vert alors que le
         homelab est off"). The "snappy back-to-back open" benefit is
         judged not worth the cost of misleading paints.

      B. probeRelay success closes the resume window AND marks the radio
         as confirmed warm. **During the initial cold-launch / resume
         convergence phase**, a status failure with radio_warm bypasses
         the 2-fail streak — 1 fail is enough to setOffline. The phase
         ends as soon as setOnline or setOffline fires for the first time;
         subsequent ticks fall back to FixedApp's streak protection.

    Why the gating: outside the convergence phase (steady-state ticks),
    a single transient status fail shouldn't flip the visible state — the
    streak absorbs it. Inside the convergence phase, the user is waiting
    on a definite answer and the probe-success signal is a strong enough
    correlate of network health that one status fail can be trusted.

    Trade-off: the pessimistic "server up but cold radio only fails the
    status path" scenario flashes red for ~13 s before recovering. Rare
    in practice (radio is binary — if probe-to-GCP made it through, status-
    to-home should too) and the daily "server-off morning open" case
    dominates the user's perception of correctness.
    """

    LOAD_CACHED_STATE = False  # drop the cache

    def __init__(self, clock, scenario):
        super().__init__(clock, scenario)
        self.radio_warm = False
        self.awaiting_initial_convergence = False

    def start_app(self):
        # Open the convergence phase before start_app runs (it may call
        # on_status_ok/fail synchronously via cache load — but we disabled
        # the cache, so this is mostly belt-and-braces).
        self.awaiting_initial_convergence = True
        super().start_app()

    def on_resume(self):
        self.awaiting_initial_convergence = True
        self.radio_warm = False  # re-confirm warmth on each resume
        super().on_resume()

    def on_probe_ok(self):
        # Probe to relay succeeded → mobile radio is up. Close the resume
        # window so subsequent status failures aren't auto-deferred.
        if not self.radio_warm:
            self.radio_warm = True
            self.resume_until = 0
        super().on_probe_ok()

    def on_status_ok(self):
        super().on_status_ok()
        self.awaiting_initial_convergence = False

    def on_status_fail(self):
        if self._in_resume_window():
            self.clock.after(RESUME_RETRY, lambda: self.fire_status())
            return
        if self.awaiting_initial_convergence and self.radio_warm:
            # Cold-launch / resume convergence path with confirmed warm
            # radio — 1 fail is enough. Skip the streak (it's for steady-
            # state blips, not for the user's "is the server up?" wait).
            self.is_online = False
            self.has_confirmed_state = True
            self.awaiting_initial_convergence = False
            self._clear_adaptive_tick()
            self.paint("offline")
            return
        # Steady-state OR radio not confirmed warm — keep FixedApp's
        # 2-fail streak protection.
        self.status_fail_streak += 1
        if self.status_fail_streak < 2:
            self._clear_adaptive_tick()
            self.adaptive_tick_id = "scheduled"
            self.clock.after(ADAPTIVE_TICK, lambda: self.fire_status())
            return
        self.is_online = False
        self.has_confirmed_state = True
        self.awaiting_initial_convergence = False
        self.paint("offline")


STATUS_FETCH_TIMEOUT = 5.0  # v7.1 — single relay /status fetch budget.
                            # Bumped from 3.0 to absorb cold-radio TLS handshake
                            # variance on Android 4G (family test reported a
                            # ~3 s cold open right at the v7.0 boundary).
STATUS_LOCAL_TTL = 60.0     # v7.0 — localStorage paint TTL
HOLD_RECHECK = 3.0          # v7.6 — after holding a confirmed-online state
                            # through one all-timeout cycle, re-check this
                            # soon instead of waiting the 15 s tick.
VISIBILITY_POLL_INTERVAL = 1.0  # v7.9 — fast-path resume detection. Polls
                                # document.hidden every 1 s; on a hidden→visible
                                # transition we treat it as a foreground return
                                # and re-probe immediately. Covers the Android
                                # PWA standalone case where neither focus nor
                                # visibilitychange fires reliably on app-switcher
                                # resume — the bug behind the IRL "frozen yesterday
                                # green for up to CHECK_INTERVAL (15 s)" report.


class OracleApp:
    """v7.0 — relay-as-oracle. One question per cycle, no parallel probes.

    The PWA calls GET /status on the relay and gets back {up, stale, age_s}.
    1 retry on transient failure. If the relay still fails after the retry,
    fall back to a no-cors HEAD against the home (loses the relay-up signal
    but preserves home up/down detection). On cold launch / resume, paint
    a localStorage cache value if it's < 60 s old, then refresh in the
    background.

    Compared to LiveApp (v6.0): no probe path, no resume window, no streaks,
    no adaptive tick, no radio-warm gating. The architectural change (one
    fetch instead of two) eliminates the race that v4-v6 spent ~150 lines
    of defensive code patching. See ADR `2026-05-27-pwa-plex-jqh-omv-relay-
    as-oracle`.
    """

    # v7.9 — when True, start_app installs the 1 s visibility-transition poll
    # that catches the Android PWA standalone case where no foreground event
    # fires. BuggyOracleApp flips this False to preserve the pre-fix contrast.
    ENABLE_VISIBILITY_POLL = True

    def __init__(self, clock, scenario):
        self.clock = clock
        self.scenario = scenario
        self.config = True
        self.is_online = False
        self.relay_reachable = True
        self.checking = False
        self.has_confirmed_state = False
        self._oracle_i = 0
        self._fallback_i = 0
        self.paints = []
        self.check_interval_id = None
        self.hidden = False           # v7.7 — background/foreground gating
        self.all_timeout_streak = 0   # v7.6 — see _fallback_done
        # v7.9 — fast-path resume detection state.
        self.visibility_poll_id = None
        self._was_hidden_at_poll = False

    def paint(self, kind):
        self.paints.append((round(self.clock.now, 2), kind))

    def _next_oracle(self):
        if self._oracle_i >= len(self.scenario.oracle_outcomes):
            # Default: relay says up. Lets short scenarios run out without
            # the sim hanging on missing outcomes.
            return FetchOutcome(latency=0.1, ok=True, up=True)
        out = self.scenario.oracle_outcomes[self._oracle_i]
        self._oracle_i += 1
        return out

    def _next_fallback(self):
        if self._fallback_i >= len(self.scenario.home_fallback_outcomes):
            return FetchOutcome(latency=0.1, ok=True)
        out = self.scenario.home_fallback_outcomes[self._fallback_i]
        self._fallback_i += 1
        return out

    def _settle(self, up, relay_ok):
        self.checking = False
        self.relay_reachable = relay_ok
        self.has_confirmed_state = True
        if up:
            self.is_online = True
            self.paint("online")
        else:
            self.is_online = False
            self.paint("offline")
        if not relay_ok:
            # Mirrors setFallbackState in app.js — "Réveil indisponible" surfaces
            # whether the home is up (warn) or down (offline-relay-promoted).
            self.paint("warn-relay" if up else "offline-relay-promoted")

    def fire_status(self):
        if self.checking or not self.config:
            return
        self.checking = True
        if not self.has_confirmed_state:
            self.paint("checking")
        self._try_relay(attempt=0)

    def _try_relay(self, attempt):
        out = self._next_oracle()
        if out.latency is None or out.latency >= STATUS_FETCH_TIMEOUT:
            # Timeout = transport failure (answered=False) at the budget.
            delay = STATUS_FETCH_TIMEOUT
            self.clock.after(delay, lambda: self._relay_done(attempt, ok=False, up=None, answered=False))
        else:
            self.clock.after(out.latency, lambda: self._relay_done(attempt, ok=out.ok, up=out.up, answered=out.answered))

    def _relay_done(self, attempt, ok, up, answered):
        if ok:
            self.all_timeout_streak = 0
            self._settle(up=up, relay_ok=True)
            return
        if answered:
            # v7.5 — the relay returned an HTTP response (degraded oracle, e.g.
            # 503 STATUS_TARGET_URL unset). It's alive and /wol works: skip the
            # retry (deterministic) and fall back to the direct-home check while
            # KEEPING the relay reachable (wake button stays enabled, no warn).
            self._fallback(relay_ok=True)
            return
        if attempt == 0:
            # 1 retry on a transient TRANSPORT failure — exactly what the PWA does.
            self._try_relay(attempt=1)
            return
        # 2nd transport failure → fall back to direct home, relay unreachable.
        self._fallback(relay_ok=False)

    def _fallback(self, relay_ok):
        out = self._next_fallback()
        if out.latency is None or out.latency >= STATUS_FETCH_TIMEOUT:
            self.clock.after(STATUS_FETCH_TIMEOUT, lambda: self._fallback_done(home_ok=False, relay_ok=relay_ok))
        else:
            self.clock.after(out.latency, lambda: self._fallback_done(home_ok=out.ok, relay_ok=relay_ok))

    def _fallback_done(self, home_ok, relay_ok):
        if home_ok:
            self.all_timeout_streak = 0
            self._settle(up=True, relay_ok=relay_ok)
            return
        # Home fallback failed too. relay_ok=False here means the relay didn't
        # answer (transport) — combined with a failed home check, that's an
        # all-timeout cycle. On a cold mobile radio this is indistinguishable
        # from a real outage but far more often it's just the radio warming up.
        # v7.6 held one such cycle (no red flash) ONLY when already confirmed-
        # online. v7.8 widens it to the cold-launch / no-cache case too: a fresh
        # reopen after the 60 s TTL had the same cold radio paint an instant
        # false RED ("rouge direct alors que le homelab est ON"). Hold one cycle
        # as neutral "connexion…" and re-check sooner; still capped at one held
        # cycle so a genuine outage surfaces on the next cycle.
        if (not relay_ok) and self.all_timeout_streak < 1:
            self.all_timeout_streak += 1
            self.checking = False
            self.paint("hold")   # neutral marker: not red, not warn, not checking
            self.clock.after(HOLD_RECHECK, self.fire_status)
            return
        self.all_timeout_streak = 0
        self._settle(up=False, relay_ok=relay_ok)

    def start_app(self):
        # Mirror app.js startApp(): hydrate from localStorage if recent enough.
        cache = self.scenario.oracle_cache
        if cache is not None:
            self.relay_reachable = bool(cache.get("relay_ok", True))
            if cache.get("up"):
                self.is_online = True
                self.has_confirmed_state = True
                self.paint("online")
            else:
                self.is_online = False
                self.has_confirmed_state = True
                self.paint("offline")
        self.fire_status()
        self.check_interval_id = "tick"
        self._schedule_next_tick()
        # v7.9 — fast-path visibility-transition detector. See
        # VISIBILITY_POLL_INTERVAL for rationale.
        if self.ENABLE_VISIBILITY_POLL:
            self._was_hidden_at_poll = self.hidden
            self.visibility_poll_id = "tick"
            self._schedule_visibility_poll()

    def _schedule_next_tick(self):
        # v7.7 — self-healing poll. The timer is NEVER cancelled on background;
        # its body no-ops while hidden and fires a fresh check on the first
        # tick after the app is foregrounded again. Mirrors app.js's
        # `setInterval(function(){if(!document.hidden)checkStatus();},15000)`.
        def tick():
            if self.check_interval_id != "tick":
                return
            if not self.hidden:
                self.fire_status()
            self._schedule_next_tick()   # keep the timer alive across background
        self.clock.after(CHECK_INTERVAL, tick)

    def _schedule_visibility_poll(self):
        # v7.9 — every VISIBILITY_POLL_INTERVAL we read document.hidden and
        # detect a hidden→visible transition. When one fires, we mirror the
        # onForeground fast path: reset checking, optionally reset
        # has_confirmed_state so the stale visual flips to "checking…" instead
        # of staying frozen on yesterday's green, then fire_status. This is the
        # absolute safety net for the Android PWA standalone case where neither
        # focus nor visibilitychange fires on app-switcher resume — without it
        # the user waits up to CHECK_INTERVAL (15 s) before the self-healing
        # tick catches up.
        def tick():
            if self.visibility_poll_id != "tick":
                return
            if self._was_hidden_at_poll and not self.hidden:
                self.checking = False
                # No fresh cache (or background lasted past the TTL) → drop the
                # confirmed-state flag so fire_status repaints "checking" instead
                # of leaving the stale paint visible during the re-probe.
                self.has_confirmed_state = False
                self.fire_status()
            self._was_hidden_at_poll = self.hidden
            self._schedule_visibility_poll()
        self.clock.after(VISIBILITY_POLL_INTERVAL, tick)

    def on_background(self):
        # v7.7 — going hidden only flips the flag; the self-healing interval
        # stays armed (it no-ops while hidden). The pre-fix app cleared the
        # interval here — see BuggyOracleApp.
        self.hidden = True

    def on_foreground(self):
        # v7.7 — fast-path re-probe bound to BOTH focus and visibilitychange.
        # On "none" (neither event fires — the Android quirk), the self-healing
        # interval's next tick still re-probes within CHECK_INTERVAL.
        self.hidden = False
        if self.scenario.foreground_event in ("focus", "visibilitychange"):
            self.checking = False
            self.fire_status()

    def on_resume(self):
        # The local cache covers back-to-back reopens; same flow as cold launch.
        self.checking = False
        if self.scenario.oracle_cache is not None:
            cache = self.scenario.oracle_cache
            self.relay_reachable = bool(cache.get("relay_ok", True))
            if cache.get("up"):
                self.is_online = True
                self.has_confirmed_state = True
                self.paint("online")
            else:
                self.is_online = False
                self.has_confirmed_state = True
                self.paint("offline")
        self.fire_status()
        if self.check_interval_id != "tick":
            self.check_interval_id = "tick"
            self._schedule_next_tick()


class BuggyOracleApp(OracleApp):
    """Pre-v7.7 resume logic — reproduces the IRL frozen-green bug.

    Two pre-fix behaviours combine to freeze the state:
      1. The poll interval is CLEARED on background (not just paused).
      2. The status re-probe is bound ONLY to visibilitychange, never to
         focus.

    So when the app returns to foreground without a visibilitychange event
    (focus-only, or no event at all — both observed on Android PWA standalone),
    nothing re-probes and nothing restarts the poll: the last-painted state
    (e.g. yesterday's green) stays frozen until a second app-switch finally
    fires visibilitychange. That is exactly the reported symptom.
    """

    # v7.9 — pre-fix doesn't have the visibility poll either.
    ENABLE_VISIBILITY_POLL = False

    def on_background(self):
        self.hidden = True
        self.check_interval_id = None   # pre-fix: the poll was cleared on hidden

    def on_foreground(self):
        self.hidden = False
        # Pre-fix: only visibilitychange re-probed + restarted the poll.
        if self.scenario.foreground_event == "visibilitychange":
            self.checking = False
            self.fire_status()
            if self.check_interval_id != "tick":
                self.check_interval_id = "tick"
                self._schedule_next_tick()
        # focus / none: the status re-probe was not bound here → frozen.


def run(scenario, app_class):
    clock = Clock()
    app = app_class(clock, scenario)
    clock.after(0, app.start_app)
    if scenario.resume_at > 0:
        clock.after(scenario.resume_at, app.on_resume)
    # v7.7 — drive the background/foreground transitions when the scenario
    # opts into them (only the v7 oracle apps implement these handlers).
    if scenario.background_at > 0 and hasattr(app, "on_background"):
        clock.after(scenario.background_at, app.on_background)
    if scenario.foreground_at > 0 and hasattr(app, "on_foreground"):
        clock.after(scenario.foreground_at, app.on_foreground)
    clock.run_until(scenario.horizon)
    return app


def evaluate(app):
    return {
        "paints": app.paints,
        "red": [p for p in app.paints if p[1] == "offline"],
        # Relay-degraded paints — both warn (server up) and the promoted-offline
        # link (server down) come from the same probe-fail flip. From the user's
        # POV they're both "the relay says it's off" — same false-positive risk.
        "warn": [p for p in app.paints if p[1] in ("warn-relay", "offline-relay-promoted")],
        # v5.0 — "checking" paints we want to avoid on cached-state cold launches.
        "checking": [p for p in app.paints if p[1] == "checking"],
        "final_online": app.is_online,
        "final_relay": app.relay_reachable,
    }


SCENARIOS = [
    Scenario(
        name="cold-start-radio-ok",
        status_outcomes=[FetchOutcome(0.3, True)],
        probe_outcomes=[FetchOutcome(0.4, True)],
    ),
    Scenario(
        name="cold-start-radio-times-out-then-ok",
        status_outcomes=[FetchOutcome(None, False), FetchOutcome(0.3, True)],
        probe_outcomes=[FetchOutcome(None, False), FetchOutcome(0.4, True)],
    ),
    Scenario(
        name="resume-radio-ok",
        status_outcomes=[FetchOutcome(0.2, True), FetchOutcome(0.2, True)],
        probe_outcomes=[FetchOutcome(0.3, True), FetchOutcome(0.3, True)],
        resume_at=20,
    ),
    Scenario(
        # The bug the v4.3 fix targets: post-resume fetches time out (cold
        # radio), retry succeeds. Must NOT flash red/warn during transition.
        name="resume-cold-radio-fail-then-ok",
        status_outcomes=[FetchOutcome(0.2, True), FetchOutcome(None, False), FetchOutcome(0.3, True)],
        probe_outcomes=[FetchOutcome(0.3, True), FetchOutcome(None, False), FetchOutcome(0.4, True)],
        resume_at=20,
    ),
    Scenario(
        # Real server-down on resume. v4.5's 2-fail streak adds ~30 s on
        # detection: T+65 ish instead of T+25 (v4.4 painted on first
        # post-window fail). Real outages still get caught — just slower.
        name="resume-server-down",
        status_outcomes=[FetchOutcome(0.2, True)] + [FetchOutcome(None, False)] * 10,
        probe_outcomes=[FetchOutcome(0.3, True)] * 10,
        resume_at=20,
        expect_final_online=False,
        forbid_red_flash=False,    # red IS expected here — server is really down
        horizon=75.0,
    ),
    Scenario(
        # Real relay-down on resume. v4.4's 2-fail streak adds ~15-30 s on
        # detection vs v4.3 (depending on tick rate). Real outages still
        # get caught; transient noise gets absorbed. v5.1: probe failure
        # list sized to outlast horizon=80 with the new 15 s tick rate
        # (probes fire at T≈0/15/20/30/45/60/75 = 7 attempts, so 8 fail
        # outcomes after the initial success keeps the relay reachable=False
        # for the whole horizon).
        name="resume-relay-down",
        status_outcomes=[FetchOutcome(0.2, True)] * 8,
        probe_outcomes=[FetchOutcome(0.3, True)] + [FetchOutcome(None, False)] * 8,
        resume_at=20,
        expect_final_relay_reachable=False,
        forbid_warn_flash=False,   # warn IS expected — relay is really down
        horizon=80.0,
    ),
    Scenario(
        name="resume-cold-probe-only-fails",
        status_outcomes=[FetchOutcome(0.2, True), FetchOutcome(0.2, True)],
        probe_outcomes=[FetchOutcome(0.3, True), FetchOutcome(None, False), FetchOutcome(0.4, True)],
        resume_at=20,
        horizon=60.0,
    ),
    Scenario(
        # The v4.4 fix targets this: server REALLY down, cold radio causes
        # BOTH the initial probe (T+2.5, window defers in v4.3) AND the
        # checkStatus retry's probe (T+12.5, post-window in v4.3) to fail.
        # v4.3 flips relayReachable → "Réveil indisponible" red paint
        # (false positive — relay is actually up). v4.4 streak counter
        # requires 2 post-window fails to flip when isOnline=false.
        # T+30 tick probe succeeds → relay stays reachable throughout.
        name="cold-start-server-down-with-cold-radio-probe-noise",
        status_outcomes=[FetchOutcome(None, False)] * 5,
        probe_outcomes=[
            FetchOutcome(None, False),  # T+2.5 cold-radio fail — window defers
            FetchOutcome(None, False),  # T+12.5 retry probe — post-window, v4.3 BUG
            FetchOutcome(0.4, True),    # T+30+ tick probe — radio warm, recovers
            FetchOutcome(0.4, True),
            FetchOutcome(0.4, True),
        ],
        resume_at=0,                        # cold start, not background→fg
        expect_final_online=False,          # server really down — expected
        expect_final_relay_reachable=True,  # relay actually up — must stay reachable
        forbid_red_flash=False,             # server-down RED is correct
        forbid_warn_flash=True,             # relay false-positive warn is THE BUG
        horizon=60.0,
    ),
    Scenario(
        # The v4.5 fix targets this: server actually UP, cold radio causes
        # both status #1 (window defers) AND status #2 retry (post-window,
        # v4.4 paints RED — false positive) to fail. T+30 tick succeeds
        # (radio warm). User report 2026-05-25: 'serveur éteint' shown for
        # ~1 min before recovering green on Android PWA. v4.5 streak
        # counter requires 2 post-window status fails to paint RED.
        name="cold-start-server-up-with-cold-radio-status-noise",
        status_outcomes=[
            FetchOutcome(None, False),  # T+5 cold-radio fail — window defers
            FetchOutcome(None, False),  # T+15 retry status — post-window, v4.4 BUG
            FetchOutcome(0.3, True),    # T+25 (v5.0 adaptive) or T+30 (v4.5 tick) — recovers
            FetchOutcome(0.3, True),
            FetchOutcome(0.3, True),
        ],
        probe_outcomes=[FetchOutcome(0.4, True)] * 5,
        resume_at=0,                        # cold launch
        expect_final_online=True,           # server actually up — must end green
        expect_final_relay_reachable=True,
        forbid_red_flash=True,              # RED false positive is THE BUG (v4.5)
        forbid_warn_flash=True,
        # v6.0 LiveApp trades a brief red flash here for fast convergence
        # on the realistic user scenarios — see LiveApp docstring.
        allow_live_to_fail=True,
        horizon=60.0,
    ),
    Scenario(
        # v5.0 A: cold launch with cached online state, server still up.
        # Cache loaded → "online" painted directly, no orange flash. Status
        # checks confirm — no visible change to the user.
        name="v5-cold-launch-cached-online-server-still-up",
        status_outcomes=[FetchOutcome(0.3, True)] * 5,
        probe_outcomes=[FetchOutcome(0.4, True)] * 5,
        cached_state={"is_online": True, "relay_reachable": True},
        resume_at=0,
        expect_final_online=True,
        expect_final_relay_reachable=True,
        forbid_red_flash=True,
        forbid_warn_flash=True,
        forbid_checking_paint=True,         # cached state means NO orange flash
        # v6.0 LiveApp drops the cache by design — it WILL paint checking
        # here. Trade-off documented in LiveApp docstring.
        allow_live_to_fail=True,
        horizon=40.0,
    ),
    Scenario(
        # v5.0 A: cold launch with stale cached online, server actually down.
        # Cache loaded → "online" briefly visible, but status fails reveal
        # truth. With v5.0 streak + adaptive tick, "offline" paint lands at
        # T+25 (defer at T+15, adaptive tick at T+25). RED IS expected here
        # (server is really down), but the cached state still saved the user
        # from the orange flash on opening.
        name="v5-cold-launch-cached-online-server-now-down",
        status_outcomes=[FetchOutcome(None, False)] * 5,
        probe_outcomes=[FetchOutcome(0.4, True)] * 5,
        cached_state={"is_online": True, "relay_reachable": True},
        resume_at=0,
        expect_final_online=False,          # truth wins eventually
        expect_final_relay_reachable=True,
        forbid_red_flash=False,             # red IS expected — server really down
        forbid_warn_flash=True,
        forbid_checking_paint=True,         # no orange flash thanks to cache
        # v6.0 LiveApp drops the cache — WILL paint checking. The new
        # v6-morning-open-* scenario covers the LiveApp variant of this.
        allow_live_to_fail=True,
        horizon=60.0,
    ),
    Scenario(
        # v5.0 B: cold launch without cache, server up with cold-radio
        # noise on status #1 and #2 (same as v4.5 scenario above). The
        # adaptive tick used to fire status #3 ahead of the next regular
        # tick (10 s defer vs 30 s tick). With v5.1 (5 s adaptive vs 15 s
        # tick), the regular tick actually beats the adaptive by 1 s in
        # this exact case — the adaptive doesn't help here anymore, but
        # it still matters in the steady-state server-dies-mid-tick path.
        # Same final state, just faster recovery from defer.
        name="v5-cold-launch-no-cache-adaptive-tick-faster-recovery",
        status_outcomes=[
            FetchOutcome(None, False),
            FetchOutcome(None, False),
            FetchOutcome(0.3, True),
        ],
        probe_outcomes=[FetchOutcome(0.4, True)] * 5,
        cached_state=None,                  # no cache → "Vérification..." OK
        resume_at=0,
        expect_final_online=True,
        forbid_red_flash=True,
        forbid_warn_flash=True,
        # v6.0 LiveApp paints red briefly here (probe ok → status fail #1
        # flips immediately) before recovering green at T=30. Same trade-off
        # as cold-start-server-up-with-cold-radio-status-noise — see
        # LiveApp docstring.
        allow_live_to_fail=True,
        horizon=40.0,
    ),
    Scenario(
        # v5.1 — steady-state detection bound. v5.3 retuned the timeout:
        # next regular tick at T=15 fails (timeout at T=17), adaptive
        # fires at T=22 (fail at T=24), streak=2 → setOffline at T=24.
        # horizon=24.5 fences the detection: any regression past 24.5 s
        # flips the final state and fails the scenario. User-visible
        # answer to the "25 s with status up while server is down"
        # complaint pre-v5.1.
        name="v51-steady-state-server-dies-mid-tick",
        status_outcomes=[
            FetchOutcome(0.3, True),    # T=0 cold check — server still up
            FetchOutcome(None, False),  # T=15 tick — server now down
            FetchOutcome(None, False),  # T=22 adaptive — confirms
        ],
        probe_outcomes=[FetchOutcome(0.4, True)] * 5,
        cached_state={"is_online": True, "relay_reachable": True},
        resume_at=0,
        expect_final_online=False,          # truth wins by T=24
        expect_final_relay_reachable=True,
        forbid_red_flash=False,             # red IS expected — server really down
        forbid_warn_flash=True,
        forbid_checking_paint=True,         # cached state → no orange flash
        # v6.0 LiveApp drops the cache — WILL paint checking at T=0. Its
        # convergence is actually faster here (T=17 vs T=24 for FixedApp)
        # because probe success bypasses the streak when the tick fires.
        allow_live_to_fail=True,
        horizon=24.5,                       # fence: detection must complete here
    ),
    Scenario(
        # v6.0 user-reported scenario 1: morning open after server has been
        # off all night. Cold launch with stale cached "online" from
        # yesterday — FixedApp paints misleading green, takes 14-16 s to
        # converge to red ("affiche vert alors que le homelab est off").
        # LiveApp doesn't load the cache (paints orange briefly) AND uses
        # probe-success to skip the streak — converges in ~2 s to red.
        # horizon=3.5 fences LiveApp's convergence; FixedApp will fail
        # this scenario (still showing cached green at horizon).
        name="v6-morning-open-stale-cache-server-off-converge-fast",
        status_outcomes=[FetchOutcome(None, False)] * 5,
        probe_outcomes=[FetchOutcome(0.4, True)] * 5,
        cached_state={"is_online": True, "relay_reachable": True},
        resume_at=0,
        expect_final_online=False,          # truth wins fast for LiveApp
        expect_final_relay_reachable=True,
        forbid_red_flash=False,             # red IS expected — server really off
        forbid_warn_flash=True,
        horizon=3.5,                        # fence: LiveApp must converge here
    ),
    Scenario(
        # v6.0 user-reported scenario 2: re-open 30 min later, cache TTL
        # expired (15 min). FixedApp paints orange "Vérification…" for
        # ~14 s before flipping to red. LiveApp paints orange but converges
        # in ~2 s thanks to probe-success + streak bypass.
        name="v6-thirtymin-reopen-no-cache-server-off-converge-fast",
        status_outcomes=[FetchOutcome(None, False)] * 5,
        probe_outcomes=[FetchOutcome(0.4, True)] * 5,
        cached_state=None,                  # cache expired or absent
        resume_at=0,
        expect_final_online=False,          # truth wins fast for LiveApp
        expect_final_relay_reachable=True,
        forbid_red_flash=False,             # red IS expected — server really off
        forbid_warn_flash=True,
        horizon=3.5,                        # fence: LiveApp must converge here
    ),

    # ============================================================
    # v7.0 — OracleApp scenarios.
    #
    # Each one populates `oracle_outcomes` (and optionally
    # `home_fallback_outcomes` / `oracle_cache`). Pre-v7 apps don't run
    # on these — see main(). The criteria mirror the ADR's "Critères
    # d'acceptance Phase 2".
    # ============================================================
    Scenario(
        # Happy path: relay says home is up; settle green in <500 ms.
        name="v7-cold-launch-server-up-fast",
        oracle_outcomes=[FetchOutcome(0.3, True, up=True)],
        resume_at=0,
        expect_final_online=True,
        expect_final_relay_reachable=True,
        forbid_red_flash=True,
        forbid_warn_flash=True,
        horizon=1.0,
    ),
    Scenario(
        # Relay says home is down; settle red in <500 ms (RED is expected).
        name="v7-cold-launch-server-off-fast",
        oracle_outcomes=[FetchOutcome(0.3, True, up=False)],
        resume_at=0,
        expect_final_online=False,
        expect_final_relay_reachable=True,
        forbid_red_flash=False,             # red IS expected
        forbid_warn_flash=True,
        horizon=1.0,
    ),
    Scenario(
        # Relay timeout twice → fallback to direct home (which succeeds) →
        # green with relay-down warn banner. Detection survives a GCP outage.
        # Worst-case latency: STATUS_FETCH_TIMEOUT × 2 (relay attempts) +
        # home response. With v7.1's 5 s timeout that's ~10.1 s.
        name="v7-relay-timeout-fallback-home-up",
        oracle_outcomes=[FetchOutcome(None, False), FetchOutcome(None, False)],
        home_fallback_outcomes=[FetchOutcome(0.3, True)],
        resume_at=0,
        expect_final_online=True,
        expect_final_relay_reachable=False,   # relay is down — warn banner expected
        forbid_red_flash=True,                # home is up, no red
        forbid_warn_flash=False,              # warn IS expected (relay down)
        horizon=12.0,
    ),
    Scenario(
        # Both relay and home down → red + warn. Full outage from the PWA's POV;
        # both paints expected. v7.8: the all-timeout HOLD now also covers the
        # cold-launch case, so a genuine full outage holds ONE neutral cycle
        # before painting red — two all-timeout cycles instead of one. Worst-case
        # when everything truly *times out* (5 s each): cycle1 3×5 s = 15 s →
        # hold 3 s → cycle2 3×5 s ends at ~33 s. (Real GCP/home-down usually
        # fails fast rather than timing out, so this ceiling is rarely hit.)
        name="v7-relay-timeout-fallback-home-down",
        oracle_outcomes=[FetchOutcome(None, False), FetchOutcome(None, False),
                         FetchOutcome(None, False), FetchOutcome(None, False)],
        home_fallback_outcomes=[FetchOutcome(None, False), FetchOutcome(None, False)],
        resume_at=0,
        expect_final_online=False,
        expect_final_relay_reachable=False,
        forbid_red_flash=False,               # home really down — red expected
        forbid_warn_flash=False,              # relay also down — warn expected
        horizon=35.0,
    ),
    Scenario(
        # localStorage cache <60 s + server still up → instant green paint,
        # then background refresh confirms. No orange flash.
        name="v7-stale-cache-paint-then-refresh",
        oracle_outcomes=[FetchOutcome(0.3, True, up=True)],
        oracle_cache={"up": True, "relay_ok": True},
        resume_at=0,
        expect_final_online=True,
        expect_final_relay_reachable=True,
        forbid_red_flash=True,
        forbid_warn_flash=True,
        forbid_checking_paint=True,           # cache pre-paint → no orange
        horizon=1.0,
    ),
    Scenario(
        # First /status fetch fails, retry succeeds → green, no red flash
        # mid-transition.
        name="v7-status-with-1-retry",
        oracle_outcomes=[FetchOutcome(None, False), FetchOutcome(0.3, True, up=True)],
        resume_at=0,
        expect_final_online=True,
        expect_final_relay_reachable=True,
        forbid_red_flash=True,
        forbid_warn_flash=True,
        horizon=6.0,
    ),
    Scenario(
        # v7.5 A1 fix — the relay ANSWERS /status with a degraded verdict
        # (503 when STATUS_TARGET_URL is unset, 404 on a legacy relay, …).
        # The relay is alive and /wol still works, so it must NOT be marked
        # unreachable: no "Réveil indisponible"/warn banner, wake button stays
        # enabled. Server is up → the direct-home fallback confirms green.
        name="v7-relay-answered-degraded-server-up",
        oracle_outcomes=[FetchOutcome(0.3, ok=False, answered=True),
                         FetchOutcome(0.3, ok=False, answered=True)],
        home_fallback_outcomes=[FetchOutcome(0.3, True)],
        resume_at=0,
        expect_final_online=True,
        expect_final_relay_reachable=True,   # KEY: relay answered → still reachable
        forbid_red_flash=True,
        forbid_warn_flash=True,              # KEY: no false relay-down warn
        horizon=2.0,
    ),
    Scenario(
        # v7.5 A1 fix — same degraded /status, but the home server is actually
        # down. The wake button must stay ENABLED (relay_reachable=True) so the
        # user can still fire a WoL; red is expected (server down) but NOT the
        # warn banner (the relay isn't down, only its oracle is). This is the
        # case the author hit IRL: "red relay + WoL unavailable while it was
        # actually fine".
        name="v7-relay-answered-degraded-server-down",
        oracle_outcomes=[FetchOutcome(0.3, ok=False, answered=True),
                         FetchOutcome(0.3, ok=False, answered=True)],
        home_fallback_outcomes=[FetchOutcome(None, False)],
        resume_at=0,
        expect_final_online=False,
        expect_final_relay_reachable=True,   # KEY: relay alive → button stays enabled
        forbid_red_flash=False,              # server really down → red expected
        forbid_warn_flash=True,              # KEY: relay isn't down → no warn
        horizon=7.0,
    ),
    Scenario(
        # v7.6 — all-timeout cycle on a healthy green server (cold-radio blip):
        # both relay attempts time out AND the direct-home fallback times out.
        # Must NOT flash red — hold the confirmed-online state for one cycle,
        # then the held re-check (HOLD_RECHECK) sees the relay up → stays green.
        # This is the IRL false-positive (green → red+relay → force-refresh →
        # green) that v7.6 kills.
        name="v7-all-timeout-while-green-holds-not-red",
        oracle_cache={"up": True, "relay_ok": True},   # start confirmed-green
        oracle_outcomes=[FetchOutcome(None, False), FetchOutcome(None, False),  # cycle: relay ✕✕
                         FetchOutcome(0.3, True, up=True)],                     # held re-check: up
        home_fallback_outcomes=[FetchOutcome(None, False)],                    # cycle: home ✕ → hold
        resume_at=0,
        expect_final_online=True,            # held green, re-check confirms up
        expect_final_relay_reachable=True,
        forbid_red_flash=True,               # KEY: no false red on the blip
        forbid_warn_flash=True,
        forbid_checking_paint=True,          # cache pre-paint → no orange
        horizon=20.0,
    ),
    Scenario(
        # v7.7 — the reported bug. A confirmed-green PWA is backgrounded; the
        # home server goes OFF during the background window; the app returns to
        # foreground via FOCUS only (no visibilitychange — the Android PWA
        # standalone quirk). Pre-fix (BuggyOracleApp): the poll was cleared on
        # background and the status re-probe was bound only to visibilitychange,
        # so nothing re-probes → the screen stays FROZEN GREEN (final_online
        # stays True → FAIL). v7.7 (OracleApp): re-probe is also bound to focus
        # AND the poll is self-healing → converges to red (final_online=False).
        # background_at=10 lands before the first 15 s tick so no tick consumes
        # an outcome while the server is still up.
        name="v7.7-resume-focus-only-no-visibilitychange-converges-to-red",
        oracle_cache={"up": True, "relay_ok": True},   # confirmed-green before bg
        oracle_outcomes=[
            FetchOutcome(0.3, True, up=True),    # T=0 cold check — server up
            FetchOutcome(0.3, True, up=False),   # post-foreground re-probe — now off
            FetchOutcome(0.3, True, up=False),
            FetchOutcome(0.3, True, up=False),
        ],
        resume_at=0,
        background_at=10,
        foreground_at=40,
        foreground_event="focus",
        expect_final_online=False,           # fixed app converges to red
        expect_final_relay_reachable=True,
        forbid_red_flash=False,              # red IS correct — server really off
        forbid_warn_flash=True,
        horizon=60.0,
    ),
    Scenario(
        # v7.7 — same bug, harsher trigger: the foreground return carries NO
        # event at all (neither focus nor visibilitychange). Pre-fix: frozen
        # green forever (FAIL). v7.7: the self-healing interval's first tick
        # after foreground (T=45) re-probes on its own → red. Proves the safety
        # net works without ANY foreground event.
        name="v7.7-resume-no-event-self-healing-interval-converges-to-red",
        oracle_cache={"up": True, "relay_ok": True},
        oracle_outcomes=[
            FetchOutcome(0.3, True, up=True),    # T=0 — server up
            FetchOutcome(0.3, True, up=False),   # T=45 self-healing tick — now off
            FetchOutcome(0.3, True, up=False),
            FetchOutcome(0.3, True, up=False),
        ],
        resume_at=0,
        background_at=10,
        foreground_at=40,
        foreground_event="none",
        expect_final_online=False,
        expect_final_relay_reachable=True,
        forbid_red_flash=False,
        forbid_warn_flash=True,
        horizon=60.0,
    ),
    Scenario(
        # v7.9 — same shape as the no-event scenario above but the horizon is
        # tightened to 43 s. Without the v7.9 visibility-transition poll, the
        # self-healing interval only fires its first post-foreground tick at
        # T=45 (CHECK_INTERVAL after the last background-elided tick), so the
        # status would stay frozen on yesterday's green inside the horizon and
        # final_online=True → FAIL. WITH the poll (sub-second hidden→visible
        # detection), the re-probe fires at ~T=41 and offline lands at ~T=41.3
        # → PASS. This is the regression test for the IRL family report
        # "il faut attendre au moins 15 s pour voir le statut passer à rouge".
        name="v7.9-resume-no-event-fast-path-converges-within-3s",
        oracle_cache={"up": True, "relay_ok": True},
        oracle_outcomes=[
            FetchOutcome(0.3, True, up=True),    # T=0 cold check — server up
            FetchOutcome(0.3, True, up=False),   # post-foreground fast-path — now off
            FetchOutcome(0.3, True, up=False),
        ],
        resume_at=0,
        background_at=10,
        foreground_at=40,
        foreground_event="none",
        expect_final_online=False,
        expect_final_relay_reachable=True,
        forbid_red_flash=False,              # red IS correct — server really off
        forbid_warn_flash=True,
        horizon=43.0,                        # tight — only the v7.9 poll meets it
    ),
    Scenario(
        # v7.8 — the reported bug. COLD reopen after the 60 s localStorage TTL
        # expired (app killed, reopened minutes later), home server actually UP,
        # but the first cycle all-times-out on a cold mobile radio (both relay
        # attempts + the direct-home fallback). Pre-v7.8 the all-timeout HOLD
        # guard required is_online && has_confirmed_state (only the already-green
        # case), so a fresh launch with no cache fell straight through to RED —
        # then recovered green on the next tick (~15-25 s). That's the "rouge
        # direct alors que le homelab est ON, ~25 s pour le vert" report. The fix
        # widens the hold to cover the no-confirmed-state cold launch: hold one
        # cycle (neutral "connexion…") and re-check sooner instead of flashing red.
        name="v7.8-cold-reopen-no-cache-all-timeout-server-up-no-red-flash",
        oracle_cache=None,                   # TTL expired → no pre-paint
        oracle_outcomes=[
            FetchOutcome(None, False),       # cycle 1: relay attempt 0 — timeout
            FetchOutcome(None, False),       # cycle 1: relay attempt 1 — timeout
            FetchOutcome(0.3, True, up=True),# held re-check: relay up (radio warm)
        ],
        home_fallback_outcomes=[FetchOutcome(None, False)],  # cycle 1: home fallback — timeout
        resume_at=0,
        expect_final_online=True,            # server is up — must end green
        expect_final_relay_reachable=True,
        forbid_red_flash=True,               # KEY: no false red on cold reopen
        forbid_warn_flash=True,
        horizon=25.0,
    ),
]


def fmt_paints(paints):
    return ", ".join(f"{t}s->{k}" for t, k in paints) or "(no transitions)"


def main():
    print("=" * 72)
    print("PWA state-machine simulation — Buggy/V43/V44/V45/Fixed(v5)/Live(v6)/Oracle(v7)")
    print("=" * 72)
    live_pass = True
    oracle_pass = True
    contrast_ok = True
    for scenario in SCENARIOS:
        print(f"\n## {scenario.name}")
        verdicts = {}
        # Pre-v7 apps run when the scenario defines status_outcomes; OracleApp
        # runs when oracle_outcomes is defined. Some legacy scenarios cover
        # both columns (rare); the split here keeps each class on the data it
        # understands.
        classes = []
        if scenario.status_outcomes:
            classes += [BuggyApp, V43App, V44App, V45App, FixedApp, LiveApp]
        if scenario.oracle_outcomes:
            # On resume scenarios, run the pre-fix app too so the output shows
            # the contrast (BuggyOracleApp freezes, OracleApp converges).
            if scenario.background_at > 0:
                classes.append(BuggyOracleApp)
            classes.append(OracleApp)
        for cls in classes:
            res = evaluate(run(scenario, cls))
            ok_red = not res["red"] if scenario.forbid_red_flash else True
            ok_warn = not res["warn"] if scenario.forbid_warn_flash else True
            ok_checking = not res["checking"] if scenario.forbid_checking_paint else True
            ok_online = res["final_online"] == scenario.expect_final_online
            ok_relay = res["final_relay"] == scenario.expect_final_relay_reachable
            verdict = "PASS" if (ok_red and ok_warn and ok_checking and ok_online and ok_relay) else "FAIL"
            # Track the current targets (LiveApp v6.0 + OracleApp v7.0)
            # unless the scenario explicitly opts out for the legacy column.
            if cls is LiveApp and verdict != "PASS" and not scenario.allow_live_to_fail:
                live_pass = False
            if cls is OracleApp and verdict != "PASS":
                oracle_pass = False
            verdicts[cls.__name__] = verdict
            issues = []
            if not ok_red:
                issues.append(f"unexpected RED at {[p[0] for p in res['red']]}")
            if not ok_warn:
                issues.append(f"unexpected WARN at {[p[0] for p in res['warn']]}")
            if not ok_checking:
                issues.append(f"unexpected CHECKING paint at {[p[0] for p in res['checking']]}")
            if not ok_online:
                issues.append(f"final online={res['final_online']} vs expected {scenario.expect_final_online}")
            if not ok_relay:
                issues.append(f"final relay={res['final_relay']} vs expected {scenario.expect_final_relay_reachable}")
            print(f"  [{cls.__name__:13}] {verdict}  paints: {fmt_paints(res['paints'])}")
            if issues:
                print(f"                   issues: {'; '.join(issues)}")
        # Regression contrast (v7.7): a resume scenario only proves the fix if
        # it FAILS the pre-fix app and PASSES the fixed one. If the pre-fix app
        # accidentally passes, the scenario isn't exercising the bug.
        if scenario.background_at > 0:
            if verdicts.get("BuggyOracleApp") != "FAIL" or verdicts.get("OracleApp") != "PASS":
                contrast_ok = False
                print(f"  [contrast]      FAIL  expected BuggyOracleApp=FAIL & OracleApp=PASS, "
                      f"got Buggy={verdicts.get('BuggyOracleApp')} Oracle={verdicts.get('OracleApp')}")
    print("\n" + "=" * 72)
    print(f"LiveApp   (v6.0): {'all required scenarios PASS' if live_pass else 'AT LEAST ONE REQUIRED SCENARIO FAILED'}")
    print(f"OracleApp (v7.0): {'all required scenarios PASS' if oracle_pass else 'AT LEAST ONE REQUIRED SCENARIO FAILED'}")
    print(f"Resume contrast (v7.7): {'pre-fix FAILS & fixed PASSES on every resume scenario' if contrast_ok else 'CONTRAST BROKEN — see [contrast] lines above'}")
    return 0 if (live_pass and oracle_pass and contrast_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
