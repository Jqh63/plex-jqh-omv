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
    relay._hb_declared_down, relay._declared_revalidate_at = False, 0.0
    relay._bg_refresh_task = None
    relay._consecutive_poll_failures = 0
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


def test_declared_down_survives_the_beat_ttl(client, monkeypatch):
    """A last-gasp is a DECLARATION, not a measurement that decays.

    Before 2026-07-29 the TTL erased it after 45 s and every subsequent /status
    blocked on a full relay→home pull (up to STATUS_POLL_FIRST + RETRY = 7 s) of
    a machine known to be off — per family open, all night, on an e2-micro. This
    test counts those polls: it reports 4 against the old code, and the body also
    lost `source: heartbeat`, which is what let the PWA commit the down without
    its own orange re-check.
    """
    polls = {"n": 0}

    async def pull_fail(background=False):
        polls["n"] += 1
        return False, False

    monkeypatch.setattr(relay, "_poll_home", pull_fail)
    client.post("/heartbeat", json={"up": True}, headers=HB)
    client.post("/heartbeat", json={"up": False}, headers=HB)   # last-gasp
    relay._hb_last_at = time.monotonic() - relay.HEARTBEAT_TTL_S - 1

    for _ in range(4):      # a night of family opens, cache long expired
        relay._status_cache.last_poll_at -= relay.STATUS_CACHE_FRESH_S + 1
        relay._status_cache.last_success_at -= relay.STATUS_CACHE_STALE_S + 1
        body = client.get("/status", headers=ST).json()
        assert body["up"] is False
        assert body["source"] == "heartbeat"    # still the home's own words

    assert polls["n"] == 0, "no reader may pay a pull to re-learn a declared stop"


def test_declared_down_is_revalidated_on_the_slow_clock(client, monkeypatch):
    """The safety net: the home is UP, its heartbeat sender is not.

    Sticking to the declaration forever would strand the family on a false
    "éteint" with no way out, so a pull still second-guesses it — on
    DECLARED_REVALIDATE_S, in the background, never in a reader's critical path.
    """
    async def pull_up(background=False):
        return True, False

    monkeypatch.setattr(relay, "_poll_home", pull_up)
    client.post("/heartbeat", json={"up": False}, headers=HB)
    relay._hb_last_at = time.monotonic() - relay.HEARTBEAT_TTL_S - 1
    assert client.get("/status", headers=ST).json()["up"] is False

    relay._declared_revalidate_at = 0.0          # the interval has elapsed
    relay._status_cache.last_poll_at -= relay.STATUS_CACHE_FRESH_S + 1
    client.get("/status", headers=ST)            # fires the background refresh
    assert relay._hb_declared_down is False, "a home that answers un-says its last-gasp"
    assert client.get("/status", headers=ST).json()["up"] is True


def test_any_beat_clears_the_declaration(client):
    client.post("/heartbeat", json={"up": False}, headers=HB)
    assert relay._hb_declared_down is True
    client.post("/heartbeat", json={"up": True}, headers=HB)
    assert relay._hb_declared_down is False


def test_stale_heartbeat_falls_back_to_pull(client, monkeypatch):
    # Beat received, then expired → the pull path must take over exactly. Note
    # the beat here is an UP one: only a last-gasp is sticky (see above), a stale
    # "up" carries no claim about the present and must yield to the pull.
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
