"""Static-IP fallback when the dynamic-DNS name stops resolving.

TARGET_HOST is a dynamic-DNS name, so a PROVIDER outage — with the home
perfectly capable of waking — used to fail every wake with 502
dns_resolution_failed. These tests pin the three states the resolver can be
in, and that the /wol endpoint inherits them.
"""
import socket

import pytest
from fastapi.testclient import TestClient

import app as relay


@pytest.fixture
def no_dns(monkeypatch):
    """Every gethostbyname raises, as during a dyn-DNS provider outage."""
    def boom(_host):
        raise socket.gaierror("simulated dyn-dns outage")
    monkeypatch.setattr(relay.socket, "gethostbyname", boom)


def test_resolves_by_name_when_dns_works(monkeypatch):
    monkeypatch.setattr(relay.socket, "gethostbyname", lambda h: "198.51.100.7")
    # The static fallback must NOT shadow a working DNS answer: a stale
    # TARGET_IP after an ISP address change would otherwise send every packet
    # to the old address forever, and nothing would report it.
    monkeypatch.setattr(relay, "TARGET_IP", "203.0.113.10")
    assert relay.resolve_target() == "198.51.100.7"


def test_falls_back_to_static_ip_when_dns_fails(no_dns, monkeypatch):
    monkeypatch.setattr(relay, "TARGET_IP", "203.0.113.10")
    assert relay.resolve_target() == "203.0.113.10"


def test_returns_none_when_dns_fails_and_no_fallback(no_dns, monkeypatch):
    # Control: without the fallback configured the failure must still surface.
    # Without this case the two tests above would pass on an implementation
    # that always returned the static IP.
    monkeypatch.setattr(relay, "TARGET_IP", "")
    assert relay.resolve_target() is None


def test_wol_succeeds_through_the_fallback(no_dns, monkeypatch):
    sent = {}
    monkeypatch.setattr(relay, "TARGET_IP", "203.0.113.10")
    monkeypatch.setattr(relay, "_send_packets",
                        lambda ip, pkt: sent.update(ip=ip, size=len(pkt)))
    # TestClient as a CONTEXT MANAGER on purpose: /wol arms the wake campaign
    # via _loop.call_soon_threadsafe, and _loop is a module global captured at
    # startup. Without the `with`, startup never runs and the handler reaches
    # for a previous test module's already-closed loop ("Event loop is closed"
    # — a failure that only appears in a full-suite run, never in isolation).
    with TestClient(relay.app) as client:
        r = client.post(
            "/wol", json={"mac": "AA:BB:CC:DD:EE:FF"},
            headers={"X-Token": "test-token", "X-Real-IP": "198.51.100.21"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["to"] == "203.0.113.10"
    assert sent["ip"] == "203.0.113.10"


def test_wol_still_502s_without_a_fallback(no_dns, monkeypatch):
    monkeypatch.setattr(relay, "TARGET_IP", "")
    monkeypatch.setattr(relay, "_send_packets",
                        lambda ip, pkt: pytest.fail("no packet may be sent"))
    with TestClient(relay.app) as client:
        r = client.post(
            "/wol", json={"mac": "AA:BB:CC:DD:EE:FF"},
            headers={"X-Token": "test-token", "X-Real-IP": "198.51.100.22"},
        )
    assert r.status_code == 502


def test_health_deep_distinguishes_fallback_from_dead(no_dns, monkeypatch):
    monkeypatch.setattr(relay, "TARGET_IP", "203.0.113.10")
    body = TestClient(relay.app).get("/health/deep").json()
    assert body["checks"]["dns"] == "fallback_ip"
    monkeypatch.setattr(relay, "TARGET_IP", "")
    body = TestClient(relay.app).get("/health/deep").json()
    assert body["checks"]["dns"] == "fail"


def test_health_deep_shows_whether_the_fallback_is_armed(monkeypatch):
    """The only way for an operator to confirm a freshly-added TARGET_IP took
    effect: the env file is unreadable off-VM and the fallback is invisible
    while DNS works."""
    monkeypatch.setattr(relay.socket, "gethostbyname", lambda h: "198.51.100.7")
    monkeypatch.setattr(relay, "TARGET_IP", "203.0.113.10")
    body = TestClient(relay.app).get("/health/deep").json()
    assert body["checks"]["target_ip_fallback"] == "ok"
    assert body["status"] == "ok"

    # Unset: the key is ABSENT, not "fail". An optional feature must never land
    # in the degraded branch's failed-checks list that "Tester le relais" shows.
    monkeypatch.setattr(relay, "TARGET_IP", "")
    body = TestClient(relay.app).get("/health/deep").json()
    assert "target_ip_fallback" not in body["checks"]
