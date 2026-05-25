"""
HTTP→UDP relay for Wake-on-LAN, deployed on a free-tier GCP e2-micro
in us-east1. Browsers can't open raw UDP sockets, so the PWA POSTs
here and this process fans out the magic packet.

Security model (defense in depth):
- Shared X-Token header (anti-scan)
- MAC allowlist (a leaked token can only wake the listed MAC)
- TARGET_HOST resolved server-side (clients cannot redirect packets
  to arbitrary hosts)
- CORS restricted to the GitHub Pages origin
- Runs as a non-privileged systemd user, ProtectSystem=strict

Cf. relay/README.md for the full deploy / hardening procedure.
"""
import os
import socket
import time

from fastapi import FastAPI, Header, HTTPException
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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/wol")
def wol(req: WolReq, x_token: str = Header(...)):
    if x_token != SHARED_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")
    if req.mac.lower() != ALLOWED_MAC:
        raise HTTPException(status_code=403, detail="mac not allowed")
    try:
        target_ip = socket.gethostbyname(TARGET_HOST)
    except socket.gaierror:
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
    return {"sent": True, "to": target_ip, "port": TARGET_PORT, "repeats": PACKET_REPEATS}
