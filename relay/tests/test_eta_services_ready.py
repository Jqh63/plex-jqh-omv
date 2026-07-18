# Boot-ETA gated on services-ready (v8.49). The first post-WoL up signal fires
# when the network is up, ~20 s before Seerr serves — the shared countdown ended
# early on every wake (IRL 2026-07-18). The sample must land on the first
# NON-degraded up signal, on both the heartbeat and the pull path.
import time

import pytest
from fastapi.testclient import TestClient

import app as relay

HB = {"X-Token": "hb-test-token"}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    relay._hb_last_at, relay._hb_up, relay._hb_degraded = 0.0, False, False
    relay._hb_times.clear()
    relay._campaign_task = None
    relay._wake_pending = False
    relay._last_wol_at = 0.0
    relay._boot_history.clear()
    relay._status_cache = relay._StatusCache()
    yield


@pytest.fixture()
def client():
    with TestClient(relay.app) as c:
        yield c


def test_degraded_beat_keeps_wake_pending_no_sample(client):
    relay._wake_pending = True
    relay._last_wol_at = time.monotonic() - 40
    client.post("/heartbeat", json={"up": True, "degraded": True}, headers=HB)
    # Host up (campaign stop condition holds) but services not ready: no ETA
    # sample yet, wake still pending for the ready instant.
    assert relay._home_up_fresh() is True
    assert relay._wake_pending is True
    assert len(relay._boot_history) == 0


def test_sample_lands_on_first_non_degraded_beat(client):
    relay._wake_pending = True
    relay._last_wol_at = time.monotonic() - 40
    client.post("/heartbeat", json={"up": True, "degraded": True}, headers=HB)
    relay._last_wol_at = time.monotonic() - 60  # 20 s later, apps now serve
    client.post("/heartbeat", json={"up": True, "degraded": False}, headers=HB)
    assert relay._wake_pending is False
    assert len(relay._boot_history) == 1
    assert 59000 <= relay._boot_history[0] <= 61000


def test_never_ready_wake_dropped_past_ceiling(client):
    relay._wake_pending = True
    relay._last_wol_at = time.monotonic() - (relay.BOOT_MAX_MS / 1000.0) - 5
    client.post("/heartbeat", json={"up": True, "degraded": True}, headers=HB)
    assert relay._wake_pending is False
    assert len(relay._boot_history) == 0


@pytest.mark.anyio
async def test_pull_path_degraded_keeps_pending(monkeypatch):
    relay._wake_pending = True
    relay._last_wol_at = time.monotonic() - 40
    relay._status_cache.last_poll_at = time.monotonic() - 5  # tight polling
    async def pull_degraded():
        return True, True
    monkeypatch.setattr(relay, "_poll_home", pull_degraded)
    await relay._poll_home_and_update()
    assert relay._wake_pending is True
    assert len(relay._boot_history) == 0
    async def pull_ready():
        return True, False
    monkeypatch.setattr(relay, "_poll_home", pull_ready)
    await relay._poll_home_and_update()
    assert relay._wake_pending is False
    assert len(relay._boot_history) == 1


@pytest.fixture
def anyio_backend():
    return "asyncio"
