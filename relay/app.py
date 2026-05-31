"""
HTTP→UDP relay for Wake-on-LAN, deployed on a free-tier GCP e2-micro
in us-east1. Browsers can't open raw UDP sockets, so the PWA POSTs
here and this process fans out the magic packet.

Security model (defense in depth):
- Shared X-Token header (anti-scan)
- MAC allowlist (a leaked token can only wake the listed MAC)
- TARGET_HOST resolved server-side (clients cannot redirect packets
  to arbitrary hosts)
- Sliding-window rate limit per source IP (limits scan / brute force
  velocity on the /wol endpoint, applied before any other check)
- Audit log of every /wol attempt (status + client IP, never token or
  MAC) — visible via `journalctl -u wol-relay`
- CORS restricted to the GitHub Pages origin
- Runs as a non-privileged systemd user, ProtectSystem=strict

Cf. relay/README.md for the full deploy / hardening procedure.
"""
import asyncio
import hmac
import logging
import os
import socket
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

ALLOWED_MAC = os.environ["ALLOWED_MAC"].lower()
SHARED_TOKEN = os.environ["WOL_TOKEN"]
TARGET_HOST = os.environ["TARGET_HOST"]
TARGET_PORT = int(os.environ.get("TARGET_PORT", "9"))

# Status oracle (v7.0). The relay also answers "is the home server up?" so
# the PWA needs only a single fetch (vs. v6.0's 2 concurrent probes). When
# STATUS_TARGET_URL is unset, /status returns 503 — the PWA fallback path
# (direct HEAD to the home host) keeps up/down detection working.
# Cf. ADR `2026-05-27-pwa-plex-jqh-omv-relay-as-oracle`.
STATUS_TARGET_URL = os.environ.get("STATUS_TARGET_URL")
# Two timeouts because the first poll after long idle (hours) needs more
# budget than warm polls: the persistent httpx client's TLS session dies
# past kernel keepalive (~minutes), and the next /status request must
# re-handshake — typically 1-2 s GCP-to-home cold. A flat 1.5 s left this
# path failing as a false-negative "down" on PWA reopen after a long
# background spell (incident 2026-05-30, ~9 h gap → first attempt timed
# out, retry too tight to recover within the warmed socket window).
#
#   FIRST: 3.0 s — fits a full TLS handshake + HEAD response on a cold
#   socket, while still falling short of the PWA's 5 s STATUS_FETCH_TIMEOUT_MS.
#
#   RETRY: 1.5 s — once a connection is up, polls are ~50-150 ms (RTT to
#   home), so 1.5 s is 10× headroom. Keeps the "home off" verdict snappy
#   (~4.5 s worst case = FIRST + RETRY) rather than 6 s with a flat 3 s.
#
# Override either via env var on links with unusual latencies.
STATUS_POLL_FIRST_TIMEOUT_S = float(os.environ.get("STATUS_POLL_FIRST_TIMEOUT_S", "3.0"))
STATUS_POLL_RETRY_TIMEOUT_S = float(os.environ.get("STATUS_POLL_RETRY_TIMEOUT_S", "1.5"))
STATUS_CACHE_FRESH_S = int(os.environ.get("STATUS_CACHE_FRESH_S", "5"))
STATUS_CACHE_STALE_S = int(os.environ.get("STATUS_CACHE_STALE_S", "60"))

# Send the magic packet multiple times to compensate for UDP drop. Each
# packet is ~100 bytes so 3 sends = ~300 bytes total, negligible. The
# 500 ms gap leaves room for transient network blips without piling up
# packets back-to-back.
PACKET_REPEATS = 3
PACKET_GAP_S = 0.5

# Rate limit per source IP on /wol. Sliding window, in-memory: a leaked
# token can't be brute-bursted, and a scanner hitting /wol gets capped
# fast without ever reaching the token comparison. uvicorn runs as a
# single worker (see wol-relay.service), so a process-local dict is
# coherent; threading.Lock guards concurrent requests within that worker.
RATE_LIMIT_WINDOW_S = 60
RATE_LIMIT_MAX_REQ = 10
_rate_lock = threading.Lock()
_rate_state: dict[str, deque] = defaultdict(deque)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("wol-relay")

# CORS is handled exclusively at the Caddy layer (see Caddyfile). Caddy
# is the only public ingress, so injecting headers there covers both
# success responses and Caddy-generated errors (502 when this process
# is down). A redundant FastAPI CORSMiddleware was emitting duplicate
# Access-Control-Allow-Origin headers (RFC 6454 violation, tolerated by
# major browsers but ugly) — removed.
app = FastAPI(title="WoL Relay", docs_url=None, redoc_url=None, openapi_url=None)


class WolReq(BaseModel):
    mac: str = Field(..., pattern=r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def magic_packet(mac: str) -> bytes:
    clean = mac.replace(":", "").replace("-", "")
    payload = bytes.fromhex(clean)
    return b"\xff" * 6 + payload * 16


def client_ip(request: Request) -> str:
    # uvicorn binds 127.0.0.1 only, Caddy is the sole ingress and sets
    # X-Forwarded-For by default — so the header is trustworthy here.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limited(ip: str) -> bool:
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_S
    with _rate_lock:
        dq = _rate_state[ip]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if not dq:
            # Drop empty entries to bound dict growth on scan traffic.
            _rate_state.pop(ip, None)
            dq = _rate_state[ip]
        if len(dq) >= RATE_LIMIT_MAX_REQ:
            return True
        dq.append(now)
    return False


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/deep")
def health_deep():
    # Verifies what the relay needs at runtime: for /wol, DNS resolution of
    # TARGET_HOST and a broadcast-capable UDP socket; for the /status oracle,
    # that STATUS_TARGET_URL is configured at all. The /wol path itself is
    # unauthenticated until the X-Token check, so we keep this endpoint
    # anonymous too — it never reveals MAC/token values, only ok/fail per check.
    checks = {"uvicorn": "ok"}
    overall = True

    try:
        socket.gethostbyname(TARGET_HOST)
        checks["dns"] = "ok"
    except socket.gaierror:
        checks["dns"] = "fail"
        overall = False

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.close()
        checks["udp"] = "ok"
    except OSError:
        checks["udp"] = "fail"
        overall = False

    # status_target is what the /status oracle needs, not /wol. Reporting it
    # here means "Tester le relais" (and a manual curl /health/deep) surfaces a
    # missing STATUS_TARGET_URL loudly, instead of /status silently returning
    # 503 — which the PWA reads as a degraded oracle and falls back on. Unset =>
    # degraded, so an accidental drop of the env var on redeploy is caught.
    if STATUS_TARGET_URL:
        checks["status_target"] = "ok"
    else:
        checks["status_target"] = "not_configured"
        overall = False

    return JSONResponse(
        content={"status": "ok" if overall else "degraded", "checks": checks},
        status_code=200 if overall else 503,
    )


# Persistent HTTP/2 client for /status polls. Keeping the same
# AsyncClient across requests preserves the TCP+TLS session, so a poll
# after a few minutes of idle still completes in ~50-100 ms (session
# resumption) instead of a full TLS handshake (1+ s from us-east1 to
# the home server).
_http_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _status_startup() -> None:
    global _http_client
    # The retry timeout is the client default (warm-path budget); _poll_home()
    # overrides per call to give the first attempt the cold-handshake budget.
    _http_client = httpx.AsyncClient(http2=True, timeout=STATUS_POLL_RETRY_TIMEOUT_S)


@app.on_event("shutdown")
async def _status_shutdown() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


@dataclass
class _StatusCache:
    last_state: bool | None = None
    # monotonic timestamp of the latest poll *attempt* (success or fail)
    last_poll_at: float = 0.0
    # monotonic timestamp of the latest *successful* poll. Tracked
    # separately so a streak of failures eventually expires the cached
    # "up" state past STATUS_CACHE_STALE_S.
    last_success_at: float = 0.0


_status_cache = _StatusCache()
_status_poll_lock = asyncio.Lock()


async def _poll_home() -> bool:
    # HEAD + 1 retry. Anything <500 counts as "the host is serving" — the
    # exact status (200, 302, 401, 444) doesn't matter for liveness.
    # First attempt gets the cold-handshake budget; retry uses the warm
    # one (by then the TCP+TLS session is up even if the first response
    # was aborted — the handshake bytes hit the wire before the cancel).
    timeouts = (STATUS_POLL_FIRST_TIMEOUT_S, STATUS_POLL_RETRY_TIMEOUT_S)
    for attempt, timeout in enumerate(timeouts):
        try:
            r = await _http_client.head(STATUS_TARGET_URL, timeout=timeout)
            return r.status_code < 500
        except httpx.HTTPError:
            if attempt == len(timeouts) - 1:
                return False
    return False


async def _poll_home_and_update() -> None:
    # Run one poll and fold the verdict into the cache. Shared by the blocking
    # path (cold/expired cache) and the background SWR refresh. Caller holds
    # _status_poll_lock.
    ok = await _poll_home()
    polled_at = time.monotonic()
    _status_cache.last_poll_at = polled_at
    if ok:
        _status_cache.last_state = True
        _status_cache.last_success_at = polled_at
    elif _status_cache.last_state is None:
        # First-ever poll failed → bootstrap with a real verdict so the cache
        # isn't stuck on "no opinion".
        _status_cache.last_state = False
        _status_cache.last_success_at = polled_at


async def _background_refresh() -> None:
    # SWR refresh: poll under the lock, swallow everything (a failed poll is
    # already absorbed by _poll_home returning False; we never want a refresh
    # error to surface as an unhandled task exception).
    try:
        async with _status_poll_lock:
            if (time.monotonic() - _status_cache.last_poll_at) >= STATUS_CACHE_FRESH_S:
                await _poll_home_and_update()
    except Exception:
        logger.exception("status background refresh failed")


_bg_refresh_task: "asyncio.Task | None" = None


def _maybe_background_refresh() -> None:
    # Fire-and-forget a single background refresh. Holding the reference keeps
    # the task alive (an un-referenced task can be GC'd mid-flight) and the
    # done()/locked() guards stop a second one from stacking under load.
    global _bg_refresh_task
    if _bg_refresh_task is not None and not _bg_refresh_task.done():
        return
    if _status_poll_lock.locked():
        return
    _bg_refresh_task = asyncio.create_task(_background_refresh())


@app.get("/status")
async def status():
    if not STATUS_TARGET_URL:
        # Config-missing → degraded mode, surface as 503 so the PWA falls
        # back to its direct-home check path.
        raise HTTPException(status_code=503, detail="status target not configured")

    now = time.monotonic()
    have_value = _status_cache.last_state is not None
    success_age = (now - _status_cache.last_success_at) if have_value else None
    # "Usable" = a value we can serve without lying: present and not past the
    # stale ceiling. Past STATUS_CACHE_STALE_S we have no trustworthy value.
    usable = have_value and success_age is not None and success_age <= STATUS_CACHE_STALE_S

    if (now - _status_cache.last_poll_at) >= STATUS_CACHE_FRESH_S:
        if usable:
            # Stale-while-revalidate: serve the (slightly stale) cached verdict
            # NOW and refresh in the background. The PWA never waits behind the
            # home poll — which on a cold relay→home leg can take up to
            # STATUS_POLL_FIRST_TIMEOUT_S + RETRY (~4.5 s) and push the PWA's own
            # 5 s fetch into a false-negative timeout (red flash on a server
            # that's actually up). See ADR 2026-05-27 addendum (2026-05-31).
            _maybe_background_refresh()
        else:
            # Nothing trustworthy to serve (cold start, or contact lost past the
            # stale ceiling) → block on a fresh poll, coalesced under the lock.
            async with _status_poll_lock:
                if (time.monotonic() - _status_cache.last_poll_at) >= STATUS_CACHE_FRESH_S:
                    await _poll_home_and_update()

    if _status_cache.last_state is None:
        body = {"up": False, "stale": False, "age_s": None}
    else:
        success_age = time.monotonic() - _status_cache.last_success_at
        if success_age > STATUS_CACHE_STALE_S:
            # Lost contact for too long — the cached "up" can't be trusted.
            body = {"up": False, "stale": False, "age_s": None}
        else:
            body = {
                "up": _status_cache.last_state,
                "stale": success_age > STATUS_CACHE_FRESH_S,
                "age_s": int(success_age),
            }
    return JSONResponse(
        content=body,
        headers={"Cache-Control": "public, max-age=5"},
    )


@app.post("/wol")
def wol(req: WolReq, request: Request, x_token: str = Header(...)):
    ip = client_ip(request)
    if rate_limited(ip):
        logger.warning("wol ip=%s status=429 reason=rate_limit", ip)
        raise HTTPException(status_code=429, detail="rate limited")
    # Constant-time comparison defeats timing-based brute-force on the
    # token (a regular `!=` short-circuits on the first byte mismatch
    # and leaks length / prefix information through response timing).
    if not hmac.compare_digest(x_token, SHARED_TOKEN):
        logger.warning("wol ip=%s status=401 reason=bad_token", ip)
        raise HTTPException(status_code=401, detail="bad token")
    if req.mac.lower() != ALLOWED_MAC:
        logger.warning("wol ip=%s status=403 reason=mac_not_allowed", ip)
        raise HTTPException(status_code=403, detail="mac not allowed")
    try:
        target_ip = socket.gethostbyname(TARGET_HOST)
    except socket.gaierror:
        logger.error("wol ip=%s status=502 reason=dns_resolution_failed", ip)
        raise HTTPException(status_code=502, detail="dns resolution failed")
    pkt = magic_packet(req.mac)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for i in range(PACKET_REPEATS):
            s.sendto(pkt, (target_ip, TARGET_PORT))
            if i < PACKET_REPEATS - 1:
                time.sleep(PACKET_GAP_S)
    finally:
        s.close()
    logger.info("wol ip=%s status=200", ip)
    return {"sent": True, "to": target_ip, "port": TARGET_PORT, "repeats": PACKET_REPEATS}
