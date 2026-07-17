# Stub tests for the server-side wake campaign (KB ADR 2026-07-16, step 5).
# Exercises the REAL relay/app.py — network I/O (magic packets, DNS) is
# stubbed, the campaign logic and its stop conditions are not.
#
# Run: /tmp/relay-venv/bin/python -m pytest relay/tests/ -v   (any venv with
# httpx + fastapi + pytest works; no pytest-asyncio needed — asyncio.run).
import asyncio
import os
import sys
import time

import pytest

os.environ.setdefault("ALLOWED_MAC", "aa:bb:cc:dd:ee:ff")
os.environ.setdefault("WOL_TOKEN", "test-token")
os.environ.setdefault("TARGET_HOST", "home.example.com")
# Compressed offsets so a full campaign runs in ~0.2 s of wall clock.
os.environ.setdefault("WOL_CAMPAIGN_DELAYS_S", "0.05,0.1,0.15,0.2")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app as relay  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    # Every test starts from a cold relay with the home DOWN and no campaign.
    relay._campaign_task = None
    relay._status_cache = relay._StatusCache()
    relay._status_cache.last_state = False
    relay._status_cache.last_success_at = time.monotonic()
    sent = []
    monkeypatch.setattr(relay, "_resolve_and_send", lambda: sent.append(1) or True)
    yield sent


def set_home_up():
    relay._status_cache.last_state = True
    relay._status_cache.last_success_at = time.monotonic()


def test_exhaustion_fires_every_burst(clean_state):
    asyncio.run(relay._wake_campaign())
    assert len(clean_state) == len(relay.WOL_CAMPAIGN_DELAYS_S)


def test_stops_at_first_up_verdict(clean_state):
    async def run():
        task = asyncio.ensure_future(relay._wake_campaign())
        await asyncio.sleep(0.07)  # after burst 1, before burst 2
        set_home_up()
        await task
    asyncio.run(run())
    assert len(clean_state) == 1


def test_stale_up_verdict_does_not_stop(clean_state):
    # An UP older than the stale ceiling is not a "home answered" signal.
    relay._status_cache.last_state = True
    relay._status_cache.last_success_at = time.monotonic() - relay.STATUS_CACHE_STALE_S - 1
    asyncio.run(relay._wake_campaign())
    assert len(clean_state) == len(relay.WOL_CAMPAIGN_DELAYS_S)


def test_window_close_stops_campaign(clean_state, monkeypatch):
    # Armed inside the uptime window, window closes before the first burst:
    # no re-wake after a scheduled shutdown (yo-yo guard).
    calls = iter([True] + [False] * 10)
    monkeypatch.setattr(relay, "_in_uptime_window", lambda: next(calls))
    asyncio.run(relay._wake_campaign())
    assert len(clean_state) == 0


def test_armed_outside_window_still_runs(clean_state, monkeypatch):
    # Manual wake outside the window (S5): the window guard must not apply.
    monkeypatch.setattr(relay, "_in_uptime_window", lambda: False)
    asyncio.run(relay._wake_campaign())
    assert len(clean_state) == len(relay.WOL_CAMPAIGN_DELAYS_S)


def test_single_campaign_for_concurrent_triggers(clean_state):
    async def run():
        relay._arm_campaign()
        first = relay._campaign_task
        relay._arm_campaign()  # second trigger attaches, no new task
        assert relay._campaign_task is first
        await first
    asyncio.run(run())
    assert len(clean_state) == len(relay.WOL_CAMPAIGN_DELAYS_S)


def test_no_campaign_on_fresh_up_home(clean_state):
    set_home_up()

    async def run():
        relay._arm_campaign()
        assert relay._campaign_task is None
    asyncio.run(run())


def test_wol_endpoint_arms_campaign(clean_state, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(relay.socket, "gethostbyname", lambda h: "192.0.2.1")
    monkeypatch.setattr(relay, "_send_packets", lambda ip, pkt: None)
    with TestClient(relay.app) as client:
        r = client.post("/wol", json={"mac": os.environ["ALLOWED_MAC"]},
                        headers={"X-Token": os.environ["WOL_TOKEN"]})
        assert r.status_code == 200
        # call_soon_threadsafe lands on the app loop; give it a beat.
        deadline = time.monotonic() + 2
        while relay._campaign_task is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert relay._campaign_task is not None
