# F7 (claude-security scan 2026-09-27): hmac.compare_digest(str, str) raises
# TypeError when either side holds a non-ASCII character. A crafted X-Token
# therefore produced a 500 + traceback on /status, /wol and /heartbeat, before
# the /status rejection limiter — an anonymous way to flood the VM journal.
# Each endpoint must answer an ordinary rejection instead.
import pytest

import app as relay

# Latin-1 bytes: HTTP header values are bytes on the wire; Starlette decodes
# them as latin-1, which yields a non-ASCII str on the server side.
BAD = {"X-Token": "t\xe9st-token".encode("latin-1"), "X-Client-Id": b"cid-test"}


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    getattr(relay, "_status_auth_state", {}).clear()
    relay._rate_state.clear()
    return TestClient(relay.app, raise_server_exceptions=False)


@pytest.mark.parametrize("method,path,body", [
    ("get", "/status", None),
    ("post", "/wol", {"mac": "aa:bb:cc:dd:ee:ff"}),
    ("post", "/heartbeat", {"up": True}),
])
def test_non_ascii_token_is_rejected_not_crashed(client, method, path, body):
    r = getattr(client, method)(path, headers=BAD, **({"json": body} if body else {}))
    assert r.status_code == 401, (path, r.status_code)


def test_good_token_still_accepted(client):
    # Positive control: the fix must not reject the legitimate token.
    r = client.get("/status", headers={"X-Token": "test-token", "X-Client-Id": "cid"})
    assert r.status_code != 401
