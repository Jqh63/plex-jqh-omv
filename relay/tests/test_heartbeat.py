# Stub tests for POST /heartbeat + heartbeat-primary /status verdict
# (KB ADR 2026-07-16, step 1). Real app.py, network stubbed.
import time

import pytest
from fastapi.testclient import TestClient

import app as relay

HB = {"X-Token": "hb-test-token"}
ST = {"X-Token": "test-token"}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    relay._hb_last_at, relay._hb_up, relay._hb_degraded = 0.0, False, False
    relay._hb_times.clear()
    relay._campaign_task = None
    relay._wake_pending = False
    relay._last_wol_at = 0.0
    relay._boot_history.clear()
    relay._status_cache = relay._StatusCache()
    # Any accidental real poll must be loud, not a network call.
    async def boom(background=False):
        raise AssertionError("unexpected pull while heartbeat fresh")
    monkeypatch.setattr(relay, "_poll_home", boom)
    yield


@pytest.fixture()
def client():
    with TestClient(relay.app) as c:
        yield c


def test_rejects_bad_token(client):
    r = client.post("/heartbeat", json={"up": True}, headers={"X-Token": "wrong"})
    assert r.status_code == 401
    assert not relay._hb_fresh()


def test_fresh_beat_drives_status_without_pull(client):
    assert client.post("/heartbeat", json={"up": True}, headers=HB).status_code == 200
    r = client.get("/status", headers=ST)
    assert r.status_code == 200
    body = r.json()
    assert body["up"] is True and body["source"] == "heartbeat"
    assert body["stale"] is False and body["age_s"] == 0
    assert "degraded" not in body


def test_degraded_is_home_measured(client):
    client.post("/heartbeat", json={"up": True, "degraded": True}, headers=HB)
    assert client.get("/status", headers=ST).json().get("degraded") is True


def test_last_gasp_turns_verdict_down_instantly(client):
    client.post("/heartbeat", json={"up": True}, headers=HB)
    client.post("/heartbeat", json={"up": False}, headers=HB)
    body = client.get("/status", headers=ST).json()
    assert body["up"] is False and body["source"] == "heartbeat"


def test_stale_heartbeat_falls_back_to_pull(client, monkeypatch):
    # Beat received, then expired → the pull path must take over exactly.
    client.post("/heartbeat", json={"up": True}, headers=HB)
    relay._hb_last_at = time.monotonic() - relay.HEARTBEAT_TTL_S - 1
    async def pull_up(background=False):
        return True, False
    monkeypatch.setattr(relay, "_poll_home", pull_up)
    body = client.get("/status", headers=ST).json()
    assert body["up"] is True and body.get("source") != "heartbeat"


def test_first_beat_ends_wake_campaign_and_measures_eta(client):
    relay._wake_pending = True
    relay._last_wol_at = time.monotonic() - 40  # 40 s boot, within bounds
    client.post("/heartbeat", json={"up": True}, headers=HB)
    assert relay._home_up_fresh() is True      # campaign stop condition
    assert relay._wake_pending is False
    assert len(relay._boot_history) == 1
    assert 39000 <= relay._boot_history[0] <= 41000


def test_rate_limit_burst_tolerant_and_never_expires_faster(client):
    for _ in range(relay.HEARTBEAT_RATE_MAX_PER_MIN):
        assert client.post("/heartbeat", json={"up": True}, headers=HB).status_code == 200
    r = client.post("/heartbeat", json={"up": True}, headers=HB)
    assert r.status_code == 429
    # A rejected beat must not make the verdict stale faster.
    assert relay._hb_fresh() is True


def test_unconfigured_token_disables_endpoint(client, monkeypatch):
    monkeypatch.setattr(relay, "HEARTBEAT_TOKEN", "")
    assert client.post("/heartbeat", json={"up": True}, headers=HB).status_code == 503
