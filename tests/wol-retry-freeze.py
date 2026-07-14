#!/usr/bin/env python3
"""Deterministic test — a WoL retry must never survive a background freeze.

## The bug (reported 2026-07-14, fixed in v8.32)

Yann saw a boot countdown on a freshly-opened PWA, on a home that was OFF, and the
wake it referred to lined up with an AM5 boot from *the previous day*. The relay's
`waking` flag is bounded to WAKE_SIGNAL_TTL_S (150 s), so it CANNOT advertise a
day-old wake: something had genuinely POSTed /wol seconds before the app opened.

That something was the PWA itself. sendWol() schedules four retry POSTs (15/30/60/
90 s) to drown out UDP loss. Android FREEZES a backgrounded PWA: pending setTimeouts
do not run — they queue, and they all fire at once when the page is resumed. The
retry guard was `if (!wolSent || isOnline) return;` — flags only. But a page frozen
mid-wake freezes `wolSent = true` right alongside the timers, so on resume (hours
later, at the next open) every pending retry passed the guard and POSTed a magic
packet nobody had asked for. Worse than the stray packet: it re-armed the relay's
`waking` signal, so EVERY open PWA painted a boot countdown for a phantom wake.

The relay log caught the thaw red-handed — three retries POSTed inside the same
0.11 s:

    Jul 14 05:24:00,169 INFO wol-relay wol ip=… device=android cid=1b439b19…
    Jul 14 05:24:00,280 INFO wol-relay wol ip=… device=android cid=1b439b19…
    Jul 14 05:24:00,280 INFO wol-relay wol ip=… device=android cid=1b439b19…

## The fix

Flags survive a freeze; the wall clock does not lie. A retry is only fired inside
the window it was scheduled for (WOL_RETRY_MAX_AGE_MS = last delay + 30 s grace).

Run: python3 tests/wol-retry-freeze.py    # expect: ALL PASS
"""

import sys

# Mirrors app.js.
WOL_RETRY_DELAYS_MS = [15000, 30000, 60000, 90000]
WOL_RETRY_MAX_AGE_MS = WOL_RETRY_DELAYS_MS[-1] + 30000  # 120 s


class Pwa:
    """Models sendWol()'s retry scheduling under a freeze/thaw cycle.

    `wall_clock_guard=False` reproduces the shipped (buggy) flag-only guard.
    """

    def __init__(self, wall_clock_guard):
        self.wall_clock_guard = wall_clock_guard
        self.wol_sent = False
        self.is_online = False
        self.wol_start_time = 0
        self.pending = []   # (scheduled_delay, ) — timers not yet fired
        self.posts = []     # wall-clock ms of each POST /wol actually sent

    def send_wol(self, now):
        self.wol_sent = True
        self.wol_start_time = now
        self.posts.append(now)                       # the initial POST
        self.pending = list(WOL_RETRY_DELAYS_MS)     # the four retries

    def _retry_fires(self, now):
        # The guard as it runs inside the setTimeout callback.
        if not self.wol_sent or self.is_online:
            return False
        if self.wall_clock_guard and (now - self.wol_start_time) > WOL_RETRY_MAX_AGE_MS:
            return False
        return True

    def advance_to(self, now):
        """Run every timer whose deadline has passed — the NORMAL, unfrozen case."""
        still = []
        for delay in self.pending:
            if self.wol_start_time + delay <= now:
                if self._retry_fires(self.wol_start_time + delay):
                    self.posts.append(self.wol_start_time + delay)
            else:
                still.append(delay)
        self.pending = still

    def thaw_at(self, now):
        """Android resume: EVERY pending timer fires at once, at `now` — however
        long the page was frozen. This is the behaviour the flag-only guard missed."""
        fired = self.pending
        self.pending = []
        for _ in fired:
            if self._retry_fires(now):
                self.posts.append(now)


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return cond


def main():
    ok = True
    print("WoL retry freeze/thaw — flag-only guard (shipped) vs wall-clock guard (v8.32)")

    print("\n## nominal: no freeze — all 4 retries fire on schedule (fix must not regress)")
    p = Pwa(wall_clock_guard=True)
    p.send_wol(0)
    p.advance_to(90000)
    ok &= check("5 POSTs (1 initial + 4 retries)", len(p.posts) == 5, f"posts={p.posts}")

    print("\n## the IRL bug: tap at 20:00, phone freezes, PWA reopened ~24 h later")
    FROZEN_AT = 5000            # frozen 5 s in, mid-wake — before any retry ran
    THAW_AT = 24 * 3600 * 1000  # reopened the next morning

    buggy = Pwa(wall_clock_guard=False)
    buggy.send_wol(0)
    buggy.advance_to(FROZEN_AT)   # nothing due yet
    buggy.thaw_at(THAW_AT)        # ...and the whole batch thaws at once
    phantom_buggy = [t for t in buggy.posts if t >= THAW_AT]
    ok &= check("shipped guard fires PHANTOM POSTs on thaw (reproduces the bug)",
                len(phantom_buggy) == 4,
                f"{len(phantom_buggy)} phantom POSTs ~24 h after the tap → relay "
                f"re-arms `waking` → every PWA paints a false countdown")

    fixed = Pwa(wall_clock_guard=True)
    fixed.send_wol(0)
    fixed.advance_to(FROZEN_AT)
    fixed.thaw_at(THAW_AT)
    phantom_fixed = [t for t in fixed.posts if t >= THAW_AT]
    ok &= check("wall-clock guard fires NO phantom POST on thaw",
                not phantom_fixed, f"posts={fixed.posts} (initial only)")

    print("\n## a SHORT freeze still retries — the guard must not break UDP-loss cover")
    # Frozen 20 s, resumed at T+40 s: still inside the retry window, so the
    # thawed retries are legitimate and must fire (this is the whole point of them).
    short = Pwa(wall_clock_guard=True)
    short.send_wol(0)
    short.thaw_at(40000)
    ok &= check("retries thawed inside the window still fire",
                len(short.posts) == 5, f"posts={short.posts}")

    print("\n" + "=" * 72)
    print("ALL PASS" if ok else "FAILURES — see above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
