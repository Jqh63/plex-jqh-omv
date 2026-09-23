# The uptime window is accepted in two spellings — "13:50-00:10" and
# "13h50-00h10" — by _WINDOW_RE, the push-window route and the env example.
# _in_uptime_window() used to split on ":" only, so the "h" spelling raised,
# was swallowed as "cannot parse", and read as OUTSIDE the window: a wake
# campaign armed just before the scheduled shutdown then never saw the window
# close, and re-woke the home after it. Found in the KB PWA/relay audit,
# 2026-09-23 (the two other consumers, home-watch and the PWA, already
# normalised "h").
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import app as relay


def _span(sep: str, before_min: int, after_min: int) -> str:
    now = datetime.now(ZoneInfo(relay.KEEPALIVE_TZ))
    a, b = now + timedelta(minutes=before_min), now + timedelta(minutes=after_min)
    return f"{a:%H}{sep}{a:%M}-{b:%H}{sep}{b:%M}"


@pytest.mark.parametrize("sep", [":", "h"])
def test_open_window_reads_inside(monkeypatch, sep):
    monkeypatch.setattr(relay, "current_window", lambda: _span(sep, -60, 60))
    assert relay._in_uptime_window() is True


@pytest.mark.parametrize("sep", [":", "h"])
def test_closed_window_reads_outside(monkeypatch, sep):
    monkeypatch.setattr(relay, "current_window", lambda: _span(sep, -3, -2))
    assert relay._in_uptime_window() is False
