# /status carries the same shared token as /wol, but had NO rate limit at all
# (found 2026-08-23 auditing the relay surface). The asymmetry cut two ways:
# the token could be tried without limit, and every rejection wrote a warning
# line — so an anonymous scanner could inflate the wol-relay journal, eroding
# the retention of the very journal used to diagnose this service.
#
# The cap counts REJECTED attempts only. That distinction is the whole design:
# a legitimate poll runs every ~8 s (~7.5/min per device) and several family
# devices share one NAT egress IP, so capping *all* /status calls — the obvious
# reading of "add rate limiting" — would throttle real users. test_authenticated
# _polls_are_never_throttled is the control that keeps that door shut.
import logging

import pytest

import app as relay

# Read through getattr so this bench also RUNS against the pre-fix app.py —
# and fails there on its assertions, not on an import error. A negative control
# that dies in the fixture proves nothing about behaviour.
MAX_FAILS = getattr(relay, "STATUS_AUTH_MAX_FAILS", 10)

GOOD = {"X-Token": "test-token", "X-Client-Id": "cid-test"}
BAD = {"X-Token": "wrong-token", "X-Client-Id": "cid-test"}


@pytest.fixture(autouse=True)
def clean_state():
    # getattr: the state does not exist in the pre-fix module (see MAX_FAILS).
    getattr(relay, "_status_auth_state", {}).clear()
    relay._rate_state.clear()
    relay._hb_last_at, relay._hb_up, relay._hb_degraded = 0.0, False, False
    relay._status_cache = relay._StatusCache()
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    with TestClient(relay.app) as c:
        yield c


def test_rejected_attempts_are_capped(client):
    # The first STATUS_AUTH_MAX_FAILS rejections answer 401; past that, 429.
    codes = [
        client.get("/status", headers=BAD).status_code
        for _ in range(MAX_FAILS + 3)
    ]
    assert codes[: MAX_FAILS] == [401] * MAX_FAILS
    assert codes[MAX_FAILS :] == [429, 429, 429]


def test_capped_attempts_stop_writing_to_the_journal(client, caplog):
    # Half the point: logging the flood we are capping would defeat the purpose.
    # The line budget per IP per window must equal the request budget, not grow
    # with the attack.
    with caplog.at_level(logging.WARNING, logger=relay.logger.name):
        for _ in range(MAX_FAILS + 25):
            client.get("/status", headers=BAD)
    lines = [r for r in caplog.records if "reason=bad_token" in r.getMessage()]
    assert len(lines) == MAX_FAILS


def test_authenticated_polls_are_never_throttled(client, monkeypatch):
    # CONTROL: the family case. Far more calls than the cap, all authenticated,
    # from one IP (a household behind one NAT egress). Not one must be refused —
    # this is the assertion that fails if someone "simplifies" the limiter into
    # the /wol one.
    monkeypatch.setattr(relay, "STATUS_TARGET_URL", "")  # 503, not a 429
    codes = {
        client.get("/status", headers=GOOD).status_code
        for _ in range(MAX_FAILS * 5)
    }
    assert 429 not in codes


def test_a_rejected_ip_does_not_lock_out_the_authenticated_path(client, monkeypatch):
    # Rejections and authenticated polls share an IP but must not share a
    # budget: a scanner on the family's egress IP would otherwise take the
    # household's oracle down with it.
    monkeypatch.setattr(relay, "STATUS_TARGET_URL", "")
    for _ in range(MAX_FAILS + 5):
        client.get("/status", headers=BAD)
    assert client.get("/status", headers=GOOD).status_code == 503


def test_wol_budget_is_untouched(client):
    # The two limiters must not share state either: burning the /status
    # rejection budget must leave /wol's own budget intact.
    for _ in range(MAX_FAILS + 5):
        client.get("/status", headers=BAD)
    r = client.post("/wol", headers={"X-Token": "wrong"}, json={})
    assert r.status_code != 429
