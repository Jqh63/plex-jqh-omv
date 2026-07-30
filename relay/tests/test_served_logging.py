# The relay must log what it ANSWERED, not only its own internal transitions.
#
# IRL 2026-07-30: a PWA showed "Éteint (prévu)" → GREEN → éteint on a home that
# had declared a clean shutdown 9 minutes earlier. Every line in the journal
# agreed the verdict was down, but none of them recorded a *served body*, so the
# relay could not be cleared as the source of the green — the investigation ran
# on code reading alone. These pins are the observability that makes the next
# report replayable.
#
# Two properties, and the second is what keeps the first affordable: a served
# verdict is logged when it CHANGES, and an unchanged one is silent (one open PWA
# polls every 8 s; per-request lines would bury the journal).
import logging

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


def _served(caplog):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("status served")]


def test_served_verdict_is_logged_with_its_source(client, caplog):
    caplog.set_level(logging.INFO, logger="wol-relay")
    assert client.post("/heartbeat", headers=HB, json={"up": True}).status_code == 200
    assert client.get("/status", headers=ST).json()["up"] is True
    lines = _served(caplog)
    assert len(lines) == 1, lines
    assert "up=True" in lines[0] and "source=heartbeat" in lines[0]
    assert "cid=cid-test" in lines[0]


def test_an_unchanged_verdict_stays_silent_then_a_flip_speaks(client, caplog):
    caplog.set_level(logging.INFO, logger="wol-relay")
    client.post("/heartbeat", headers=HB, json={"up": True})
    for _ in range(4):
        client.get("/status", headers=ST)
    assert len(_served(caplog)) == 1, "an unchanged served verdict must not re-log"

    # The last-gasp: same endpoint, opposite verdict. THIS is the transition a
    # flapping card is made of, so it must appear.
    client.post("/heartbeat", headers=HB, json={"up": False})
    client.get("/status", headers=ST)
    lines = _served(caplog)
    assert len(lines) == 2, lines
    assert "up=False" in lines[1] and "source=heartbeat" in lines[1]


def test_the_waking_flag_counts_as_its_own_verdict(client, caplog):
    # up=False + waking is a different card (countdown, not "Éteint"), so it is a
    # distinct served state — otherwise a wake would be invisible in the journal
    # between two identical up=False lines.
    caplog.set_level(logging.INFO, logger="wol-relay")
    client.post("/heartbeat", headers=HB, json={"up": False})
    client.get("/status", headers=ST)
    relay._last_wol_at = relay.time.monotonic()
    client.get("/status", headers=ST)
    lines = _served(caplog)
    assert len(lines) == 2, lines
    assert "waking" in lines[1]
