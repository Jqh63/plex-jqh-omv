# Regression: the confirm-before-DOWN hold must never RESURRECT a stale verdict.
#
# IRL 2026-07-18 04:19 UTC: first poll after a whole night of silence (keepalive
# paused outside the window, heartbeat stale since the 22:47 last-gasp). The
# cache still held the previous evening's UP. The poll failed (home booting),
# and the hold branch kept that ~6 h-old UP *and refreshed last_success_at* —
# so /status served a fresh-looking green while the wake was in progress, and
# _home_up_fresh() killed the wake campaign at t+15 s.
#
# The hold exists to absorb ONE flaky probe on a *warm* leg (recent successes).
# A verdict already past STATUS_CACHE_STALE_S carries no information worth
# protecting: the first failure must commit DOWN.
import time

import pytest

import app as relay

ST = {"X-Token": "test-token"}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    relay._hb_last_at, relay._hb_up, relay._hb_degraded = 0.0, False, False
    relay._campaign_task = None
    relay._wake_pending = False
    relay._last_wol_at = 0.0
    relay._status_cache = relay._StatusCache()
    relay._consecutive_poll_failures = 0

    async def fail_poll(background=False):
        return False, False

    monkeypatch.setattr(relay, "_poll_home", fail_poll)
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    with TestClient(relay.app) as c:
        yield c


def _seed_cache(state: bool, success_age_s: float) -> None:
    now = time.monotonic()
    relay._status_cache.last_state = state
    relay._status_cache.last_success_at = now - success_age_s
    relay._status_cache.last_poll_at = now - success_age_s


def test_first_failure_after_long_silence_commits_down(client):
    # UP verdict from "yesterday evening", hours past the stale ceiling.
    _seed_cache(True, 6 * 3600)
    r = client.get("/status", headers=ST)
    assert r.status_code == 200
    body = r.json()
    # The buggy hold served up=true here (green during a boot, IRL 2026-07-18).
    assert body["up"] is False
    # And the campaign guard must not see a "fresh UP" either — the resurrected
    # freshness is what stopped the wake campaign at t+15 s.
    assert relay._home_up_fresh() is False


def test_single_failure_on_warm_leg_still_holds():
    # The hold's legitimate case: recent success, one flaky probe → keep UP.
    import asyncio
    _seed_cache(True, 30)
    asyncio.run(relay._poll_home_and_update())
    assert relay._status_cache.last_state is True
    assert relay._consecutive_poll_failures == 1
    # Second agreeing failure commits DOWN (unchanged behaviour).
    asyncio.run(relay._poll_home_and_update())
    assert relay._status_cache.last_state is False


def test_flip_is_logged_only_when_committed(caplog):
    # The hold used to log "verdict flip: UP → DOWN" while *keeping* UP — a log
    # that lies. Warm leg, single failure: verdict held, no flip line.
    import asyncio
    _seed_cache(True, 30)
    with caplog.at_level("INFO"):
        asyncio.run(relay._poll_home_and_update())
    assert relay._status_cache.last_state is True
    assert not any("verdict flip" in r.message for r in caplog.records)
