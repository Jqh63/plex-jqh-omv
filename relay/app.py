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
import hmac
import logging
import os
import socket
import threading
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

ALLOWED_MAC = os.environ["ALLOWED_MAC"].lower()
SHARED_TOKEN = os.environ["WOL_TOKEN"]
TARGET_HOST = os.environ["TARGET_HOST"]
TARGET_PORT = int(os.environ.get("TARGET_PORT", "9"))

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
    # Verifies the things /wol actually needs at runtime: DNS resolution of
    # TARGET_HOST and the ability to open a broadcast-capable UDP socket.
    # The /wol path itself is unauthenticated until the X-Token check, so we
    # keep this endpoint anonymous too — it never reveals MAC/token values,
    # only ok/fail per check.
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

    return JSONResponse(
        content={"status": "ok" if overall else "degraded", "checks": checks},
        status_code=200 if overall else 503,
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
