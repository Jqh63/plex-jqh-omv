#!/usr/bin/env python3
"""
Deterministic state-machine simulator of app.js status / probe timing.

Replays the relevant timer / fetch / visibilitychange logic on a
synthetic clock so we can verify the cold-radio resume race without
spinning up a browser. Faster than the E2E (~50 ms vs ~2 min), so
useful for tight iteration on timing logic. The browser E2E
(`cold-radio-e2e.py`) is the source of truth — this sim is a
lightweight first line of defence.

Two implementations side-by-side:
  - BuggyApp: pre-v4.3 logic (justResumed flag, checkStatus retry only)
  - FixedApp: v4.3 logic (resumeUntil window, both handlers defer once)

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

STATUS_TIMEOUT = 5.0
PROBE_TIMEOUT = 2.5
CHECK_INTERVAL = 30.0
RESUME_RETRY = 5.0
RESUME_WINDOW = 6.0  # seconds — must match RESUME_GRACE_MS / 1000 in app.js


@dataclass
class FetchOutcome:
    """latency=None means the fetch times out at the caller-defined timeout."""
    latency: Optional[float]
    ok: bool = True


@dataclass
class Scenario:
    name: str
    status_outcomes: List[FetchOutcome] = field(default_factory=list)
    probe_outcomes: List[FetchOutcome] = field(default_factory=list)
    resume_at: float = 0.0   # 0 = cold start, >0 = background→foreground at that time
    expect_final_online: bool = True
    expect_final_relay_reachable: bool = True
    forbid_red_flash: bool = True
    forbid_warn_flash: bool = True
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
        self.resume_until = 0.0          # FixedApp timestamp
        self.resume_retry_id = None
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
        self.paint("online")

    def on_status_fail(self):
        self.is_online = False
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
        self.just_resumed = True
        self.resume_until = self.clock.now + RESUME_WINDOW
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


class FixedApp(App):
    """v4.3 logic — both handlers defer first failure inside resumeUntil window."""

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


def run(scenario, app_class):
    clock = Clock()
    app = app_class(clock, scenario)
    clock.after(0, app.start_app)
    if scenario.resume_at > 0:
        clock.after(scenario.resume_at, app.on_resume)
    clock.run_until(scenario.horizon)
    return app


def evaluate(app):
    return {
        "paints": app.paints,
        "red": [p for p in app.paints if p[1] == "offline"],
        "warn": [p for p in app.paints if p[1].startswith("warn")],
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
        name="resume-server-down",
        status_outcomes=[FetchOutcome(0.2, True)] + [FetchOutcome(None, False)] * 3,
        probe_outcomes=[FetchOutcome(0.3, True)] * 4,
        resume_at=20,
        expect_final_online=False,
        forbid_red_flash=False,    # red IS expected here — server is really down
        horizon=60.0,
    ),
    Scenario(
        name="resume-relay-down",
        status_outcomes=[FetchOutcome(0.2, True)] * 3,
        probe_outcomes=[FetchOutcome(0.3, True), FetchOutcome(None, False), FetchOutcome(None, False)],
        resume_at=20,
        expect_final_relay_reachable=False,
        forbid_warn_flash=False,   # warn IS expected here — relay is really down
        horizon=60.0,
    ),
    Scenario(
        name="resume-cold-probe-only-fails",
        status_outcomes=[FetchOutcome(0.2, True), FetchOutcome(0.2, True)],
        probe_outcomes=[FetchOutcome(0.3, True), FetchOutcome(None, False), FetchOutcome(0.4, True)],
        resume_at=20,
        horizon=60.0,
    ),
]


def fmt_paints(paints):
    return ", ".join(f"{t}s->{k}" for t, k in paints) or "(no transitions)"


def main():
    print("=" * 72)
    print("PWA state-machine simulation — Buggy (pre-v4.3) vs Fixed (v4.3+)")
    print("=" * 72)
    overall_pass = True
    for scenario in SCENARIOS:
        print(f"\n## {scenario.name}")
        for cls in (BuggyApp, FixedApp):
            res = evaluate(run(scenario, cls))
            ok_red = not res["red"] if scenario.forbid_red_flash else True
            ok_warn = not res["warn"] if scenario.forbid_warn_flash else True
            ok_online = res["final_online"] == scenario.expect_final_online
            ok_relay = res["final_relay"] == scenario.expect_final_relay_reachable
            verdict = "PASS" if (ok_red and ok_warn and ok_online and ok_relay) else "FAIL"
            if cls is FixedApp and verdict != "PASS":
                overall_pass = False
            issues = []
            if not ok_red:
                issues.append(f"unexpected RED at {[p[0] for p in res['red']]}")
            if not ok_warn:
                issues.append(f"unexpected WARN at {[p[0] for p in res['warn']]}")
            if not ok_online:
                issues.append(f"final online={res['final_online']} vs expected {scenario.expect_final_online}")
            if not ok_relay:
                issues.append(f"final relay={res['final_relay']} vs expected {scenario.expect_final_relay_reachable}")
            print(f"  [{cls.__name__:9}] {verdict}  paints: {fmt_paints(res['paints'])}")
            if issues:
                print(f"               issues: {'; '.join(issues)}")
    print("\n" + "=" * 72)
    print(f"FixedApp (v4.3+): {'all scenarios PASS' if overall_pass else 'AT LEAST ONE FAIL'}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
