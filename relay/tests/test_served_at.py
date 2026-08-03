# /status must stamp WHEN it built the body, on the wall clock.
#
# `age_s` is a duration measured against the last beat, so a body that is
# replayed hours later still carries a perfectly plausible number — that is
# exactly how a false red survived three investigations (2026-07-31 age=578s
# then age=6s a second apart; 2026-08-03 age=21447s built at 07:01 and painted
# at 20:23 with rt=805ms, i.e. an OLD body over a FAST transport).
#
# An absolute timestamp cannot do that: replayed, it stays in the past while the
# client's clock moves on, so the client can name the defect from one paint
# instead of eliminating layers by code reading.
import pytest

import app as relay

ST = {"X-Token": "test-token", "X-Client-Id": "cid-test"}
HB = {"X-Token": "hb-test-token"}


@pytest.fixture(autouse=True)
def clean_state():
    relay._hb_last_at, relay._hb_up, relay._hb_degraded = 0.0, False, False
    relay._hb_declared_down, relay._declared_revalidate_at = False, 0.0
    relay._status_cache = relay._StatusCache()
    relay._last_wol_at = 0.0
    relay._wake_failed_at = 0.0
    relay._last_served = None
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    with TestClient(relay.app) as c:
        yield c


def test_status_stamps_the_wall_clock_moment_it_built_the_body(client, monkeypatch):
    # Pinned to a fixed wall clock so this asserts "the stamp is time.time() at
    # build", not "the stamp is roughly now" — which a replayed body would also
    # satisfy for a while.
    monkeypatch.setattr(relay.time, "time", lambda: 1785000000)
    client.post("/heartbeat", headers=HB, json={"up": True})
    body = client.get("/status", headers=ST).json()
    assert body["served_at"] == 1785000000


def test_the_stamp_moves_with_the_clock_even_when_the_verdict_does_not(
        client, monkeypatch):
    # The property that makes a replay visible: the verdict and its age may be
    # identical between two answers, but the stamp must still advance. A body
    # held and re-delivered keeps the OLD stamp — that is the signature.
    now = [1785000000]
    monkeypatch.setattr(relay.time, "time", lambda: now[0])
    client.post("/heartbeat", headers=HB, json={"up": True})

    first = client.get("/status", headers=ST).json()
    now[0] += 3600
    second = client.get("/status", headers=ST).json()

    assert first["up"] == second["up"], "precondition: same verdict"
    assert second["served_at"] == first["served_at"] + 3600
