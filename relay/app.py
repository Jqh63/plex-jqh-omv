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
import re
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
#
# 2026-07-14 — RETRY raised 1.5 s → 4.0 s, on measurement. The comment above assumed the
# retry runs "once a connection is up" (~50-150 ms). That assumption is FALSE here: the
# instrumentation shows attempt=1 failing on nearly every poll (the pooled HTTP/2
# connection to the home dies between polls), so the retry is precisely the attempt that
# must pay a FULL cold handshake — measured at 3.37 s. It was being given 1.5 s. It could
# not succeed, and the relay then declared the home OFF to the whole family. Total worst
# case is now 3.0 + 4.0 = 7 s, still inside the PWA's 8 s probe budget (PROBE_TIMEOUT_MS),
# which is the real constraint on this path.
STATUS_POLL_FIRST_TIMEOUT_S = float(os.environ.get("STATUS_POLL_FIRST_TIMEOUT_S", "3.0"))
STATUS_POLL_RETRY_TIMEOUT_S = float(os.environ.get("STATUS_POLL_RETRY_TIMEOUT_S", "4.0"))
STATUS_CACHE_FRESH_S = int(os.environ.get("STATUS_CACHE_FRESH_S", "5"))
STATUS_CACHE_STALE_S = int(os.environ.get("STATUS_CACHE_STALE_S", "60"))

# Keepalive poll (2026-07-14) — the fix for the false "Éteint (prévu)" on a home that
# was UP. Root cause: the cache lapses after STATUS_CACHE_STALE_S and the household is
# the relay's ONLY caller, so essentially EVERY open paid a full cold TLS handshake to
# the home. Measured on a cold relay: 1.51 s of a 3.0 s budget — half of it, on a good
# day. On a worse one the budget blows, _poll_home returns False, and the relay tells
# every PWA the home is off while it is running fine.
#
# Widening the budget was the wrong lever: the PWA only grants its own probe 8 s, so a
# relay that blocks longer just pushes the client onto its fallback path. Instead, keep
# the leg WARM so it is never cold at the moment it matters: a background poll holds the
# HTTP/2 connection open and the cache fresh, and /status is then served from cache
# instantly, without ever blocking.
#
# Adaptive cadence, so this is not a tap:
#   home UP   → 30 s. There IS a connection to keep warm, and the cache stays fresh.
#   home DOWN → 60 s. Nothing to keep warm (the box is off — the nightly window is ~14 h
#               of guaranteed failures), but a DOWN is the verdict that can be WRONG, so
#               it is never the one we re-check lazily. A wake does not depend on this
#               loop: the PWA polls hard on its own during a boot.
# Cost: ~2 HEADs/min while up, 1/min while down, on a service whose only job is this.
# The home sees a HEAD on SWAG, which has access_log off — no noise there either.
STATUS_KEEPALIVE_UP_S = int(os.environ.get("STATUS_KEEPALIVE_UP_S", "30"))
# 60 s, not 300 s. A DOWN verdict is the one that LIES to the family, so it must never
# be the one we re-check lazily: the first version of this loop used 300 s, its cold
# start poll failed, and the relay then sat on a confidently-served WRONG "down" for
# five minutes (2026-07-14, "verdict flip: DOWN → UP" 11 s after an open). Re-checking
# a down home every 60 s is free and bounds that damage.
STATUS_KEEPALIVE_DOWN_S = int(os.environ.get("STATUS_KEEPALIVE_DOWN_S", "60"))
# The keepalive runs in the BACKGROUND, with no client waiting on it — so it must NOT
# use the foreground budget. That was the design error behind the regression above:
# 3 s is tuned to fit inside the PWA's own 8 s probe, a constraint that simply does not
# apply here. A cold TLS handshake from this (slow) e2-micro can exceed 3 s, the
# background poll then failed, and a WRONG "down" got cached and served instantly.
# Give the loop room to actually complete the handshake it exists to warm up.
STATUS_KEEPALIVE_FIRST_TIMEOUT_S = float(os.environ.get("STATUS_KEEPALIVE_FIRST_TIMEOUT_S", "10.0"))
STATUS_KEEPALIVE_RETRY_TIMEOUT_S = float(os.environ.get("STATUS_KEEPALIVE_RETRY_TIMEOUT_S", "5.0"))

# Confirm-before-DOWN (2026-07-14) — the real fix, after two wrong ones.
#
# The relay→home leg is simply UNRELIABLE: the instrumentation shows attempt=1 failing
# on essentially every poll (the pooled HTTP/2 connection dies between polls, 30 s
# apart) and attempt=2 paying a fresh ~3.4 s handshake. Chasing that with bigger budgets
# and a keepalive was treating the symptom — some polls will fail, always, and no timeout
# makes a flaky leg reliable.
#
# The actual defect is that ONE failed poll was enough for the relay to declare the home
# OFF — to every PWA in the house, on a home that is running fine. The PWA has never been
# that naive: it demands DOWN_CONFIRM consecutive agreeing verdicts before it commits a
# red. The relay, which is the ORACLE the whole family trusts, had no such guard.
#
# So: an UP verdict commits instantly (optimistic — a home that answers IS up), while a
# DOWN needs STATUS_DOWN_CONFIRM_POLLS consecutive failures before it is believed. Until
# then the last known UP keeps being served. A genuinely-off home simply takes one extra
# poll to be reported — nothing depends on that latency (the nightly shutdown is not a
# race), whereas a false "éteint" is seen by everyone.
STATUS_DOWN_CONFIRM_POLLS = int(os.environ.get("STATUS_DOWN_CONFIRM_POLLS", "2"))
_consecutive_poll_failures: int = 0

# Wake-in-progress signal (shared wake-state). After a /wol POST, /status
# advertises `waking: true` for this long while the home is still down, so ANY
# open PWA — not just the device that fired the wake — can show the boot
# countdown. Cleared implicitly once the home answers (an `up` verdict wins).
# Sized a bit above a typical J5005 cold boot (~80 s) plus slack.
WAKE_SIGNAL_TTL_S = int(os.environ.get("WAKE_SIGNAL_TTL_S", "150"))
# Shared boot-ETA (multi-device timer sync). The relay measures the wall-clock
# from a /wol to the next observed "up" flip and keeps a small ring of the last
# few, serving their median as `eta_s` in every /status. Every open PWA seeds its
# wake countdown from that single value, so the timer is identical across devices
# instead of each running its own local boot-history median. In-memory (ephemeral
# on relay restart → falls back to ETA_FALLBACK_S until a few wakes reconverge),
# mirroring the PWA's own client-side history bounds.
BOOT_MIN_MS = 10_000
BOOT_MAX_MS = 300_000
BOOT_HISTORY_MAX = 10
ETA_FALLBACK_S = int(os.environ.get("ETA_FALLBACK_S", "80"))
# A boot sample is only trusted when the up-flip was observed by tight polling
# (an open PWA polls /status every ~8 s). Wider gap ⇒ the flip timestamp mostly
# measures how long the relay went unpolled, not the boot — drop the sample.
BOOT_SAMPLE_MAX_POLL_GAP_S = int(os.environ.get("BOOT_SAMPLE_MAX_POLL_GAP_S", "30"))
_boot_history: deque = deque(maxlen=BOOT_HISTORY_MAX)
# True from a /wol until the next "up" flip consumes it for a boot measurement,
# so a steady-state up (no wake) is never mis-recorded as a boot.
_wake_pending: bool = False

# Usage-log dedupe window: a given client-id's /status poll is logged at most
# once per this interval, turning the 8 s self-healing poll of every open PWA
# into a coarse "PWA was open around time T on device X" signal without flooding
# journalctl. In-memory, bounded like the rate limiter.
USAGE_LOG_DEDUPE_S = int(os.environ.get("USAGE_LOG_DEDUPE_S", "600"))

# Optional scheduled-uptime window, e.g. "13h50-00h10" (also accepts
# "13:50-00:10"; may wrap past midnight). When set, it's echoed verbatim in
# every /status response as "window" and the PWA adopts it automatically —
# the relay acts as the admin-controlled config channel, so installed
# clients pick up the window on their next poll without re-provisioning.
# The relay itself never interprets it (purely client-side display logic).
_WINDOW_RE = re.compile(r"^([01]?\d|2[0-3])[h:]([0-5]\d)\s*-\s*([01]?\d|2[0-3])[h:]([0-5]\d)$")
UPTIME_WINDOW = os.environ.get("UPTIME_WINDOW", "").strip() or None
if UPTIME_WINDOW and not _WINDOW_RE.match(UPTIME_WINDOW):
    raise RuntimeError(f"UPTIME_WINDOW malformed (want HH:MM-HH:MM / HHhMM-HHhMM): {UPTIME_WINDOW!r}")

# Deployable window file — pushed through the GitOps channel (dispatch.sh
# `push-window` / `apply-window`), sourced at home from the versioned
# autoshutdown config: one source of truth for every consumer (PWA clients
# via /status, home-watch window, auto-WoL trigger). Takes precedence over
# the UPTIME_WINDOW env fallback, and is re-read on mtime change so an
# apply-window is live on the next /status poll without a service restart.
WINDOW_FILE = os.environ.get("WINDOW_FILE", "/opt/wol-relay/window").strip()
_window_file_cache: tuple[float, str | None] = (0.0, None)


def current_window() -> str | None:
    global _window_file_cache
    try:
        mtime = os.stat(WINDOW_FILE).st_mtime
    except OSError:
        return UPTIME_WINDOW
    if mtime != _window_file_cache[0]:
        try:
            raw = open(WINDOW_FILE, encoding="utf-8").read().strip()
        except OSError:
            return UPTIME_WINDOW
        val = raw if raw and _WINDOW_RE.match(raw) else None
        if raw and val is None:
            logger.warning("window file %s malformed (%r) — ignored", WINDOW_FILE, raw)
        _window_file_cache = (mtime, val)
    return _window_file_cache[1] or UPTIME_WINDOW

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
# Hard cap on tracked source IPs. With X-Real-IP (Caddy-set, unforgeable) the
# key space is bounded by real clients, but a one-shot visitor still leaves an
# entry that's never revisited (the per-call prune only fires when the SAME ip
# returns). Cap the dict and sweep fully-drained entries when exceeded so memory
# can't creep over a long uptime. Defense-in-depth tied to the XFF fix.
MAX_TRACKED_IPS = 4096
_rate_lock = threading.Lock()
_rate_state: dict[str, deque] = defaultdict(deque)

# Wake-state shared between POST /wol (writer) and GET /status (reader).
# uvicorn runs a single worker (see wol-relay.service) so a module global is
# coherent without locking — a stale read at worst flips `waking` one poll late.
_last_wol_at: float = 0.0
# client-id -> monotonic ts of its last logged /status open (usage dedupe).
_usage_seen: dict[str, float] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# httpx logs every request at INFO. That was tolerable when the home was only polled on
# client traffic; with the keepalive loop it is one line every 30 s — ~2 900/day of
# "HEAD … 405", drowning the few lines that actually mean something (a slow poll, a
# verdict flip, a /wol). Silence the routine chatter; a real transport problem still
# surfaces as a WARNING from httpx and, above all, through our own poll logging.
logging.getLogger("httpx").setLevel(logging.WARNING)
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
    # Caddy (the sole ingress, on localhost) strips any client-supplied
    # X-Forwarded-For and sets X-Real-IP to the real connecting peer it sees
    # ({remote_host} — see Caddyfile). That header is the only IP value an
    # external client cannot forge, so the rate limiter + audit log key on it.
    # NB: do NOT read X-Forwarded-For here — Caddy *appends* to it, so its
    # leftmost element is attacker-controlled (rate-limit bypass + log poison).
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else "unknown"


_UA_RULES = (
    (("iphone", "ipad", "ipod"), "ios"),
    (("android",), "android"),
    (("windows",), "windows"),
    (("macintosh", "mac os"), "mac"),
    (("linux",), "linux"),
)


def device_class(ua: str | None) -> str:
    # Coarse device bucket from the User-Agent, for the usage / "who woke it"
    # audit log. Never parsed for any logic — purely an admin-facing hint.
    ua = (ua or "").lower()
    for needles, name in _UA_RULES:
        if any(n in ua for n in needles):
            return name
    return "other"


def clean_cid(raw: str | None) -> str:
    # The X-Client-Id is an opaque random UUID the PWA persists in localStorage.
    # Not a secret and carries no PII, but it IS client-controlled and lands in
    # logs, so constrain it to a safe charset + length (anti log-injection).
    return re.sub(r"[^A-Za-z0-9-]", "", (raw or "")[:36]) or "-"


def note_usage(cid: str, ua: str | None, ip: str) -> None:
    # Coarse usage telemetry: log a client's /status open at most once per
    # USAGE_LOG_DEDUPE_S so "PWA open" hours show in journalctl without a line
    # every 8 s. In-memory, bounded; only token-authenticated callers reach this
    # (the /status token check runs first), so the key space is the family's
    # devices, not the open internet.
    if cid == "-":
        return
    now = time.monotonic()
    if now - _usage_seen.get(cid, 0.0) < USAGE_LOG_DEDUPE_S:
        return
    _usage_seen[cid] = now
    if len(_usage_seen) > MAX_TRACKED_IPS:
        cutoff = now - USAGE_LOG_DEDUPE_S
        for k in [k for k, t in _usage_seen.items() if t < cutoff]:
            _usage_seen.pop(k, None)
    logger.info("open ip=%s device=%s cid=%s", ip, device_class(ua), cid)


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
        if len(_rate_state) > MAX_TRACKED_IPS:
            # Bound memory: drop entries whose window has fully drained.
            stale = [k for k, d in _rate_state.items() if not d or d[-1] < cutoff]
            for k in stale:
                _rate_state.pop(k, None)
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
_keepalive_task: "asyncio.Task | None" = None


@app.on_event("startup")
async def _status_startup() -> None:
    global _http_client, _keepalive_task
    # The retry timeout is the client default (warm-path budget); _poll_home()
    # overrides per call to give the first attempt the cold-handshake budget.
    _http_client = httpx.AsyncClient(http2=True, timeout=STATUS_POLL_RETRY_TIMEOUT_S)
    _keepalive_task = asyncio.create_task(_keepalive_loop())


@app.on_event("shutdown")
async def _status_shutdown() -> None:
    global _http_client, _keepalive_task
    if _keepalive_task is not None:
        _keepalive_task.cancel()
        try:
            await _keepalive_task
        except (asyncio.CancelledError, Exception):
            pass
        _keepalive_task = None
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
    # Host reachable but STATUS_TARGET_URL answered 5xx: the box is awake and
    # its reverse proxy is serving, the probed app behind it is not. Orthogonal
    # to last_state — a degraded home is an UP home.
    last_degraded: bool = False


_status_cache = _StatusCache()
_status_poll_lock = asyncio.Lock()


async def _poll_home(background: bool = False) -> tuple[bool, bool]:
    # HEAD + 1 retry. Returns (up, degraded). `background` = the keepalive loop, which
    # has NO client waiting and therefore gets a budget generous enough to actually
    # complete a cold TLS handshake (see STATUS_KEEPALIVE_FIRST_TIMEOUT_S).
    #
    # ANY HTTP response means the host is serving — it answered the TCP+TLS
    # handshake and spoke HTTP, which is the only thing WoL cares about. The
    # status code says nothing about the *host*, only about the app behind the
    # reverse proxy. A 5xx (typically SWAG's 502 while the probed container is
    # stopped or still booting) therefore means up=True, degraded=True: don't
    # send a magic packet to an awake machine, but don't pretend all is well.
    # Only a transport failure (timeout, DNS, refused) means the home is down.
    #
    # First attempt gets the cold-handshake budget; retry uses the warm
    # one (by then the TCP+TLS session is up even if the first response
    # was aborted — the handshake bytes hit the wire before the cancel).
    # Observability (2026-07-14), deliberately QUIET. A "down" verdict here paints the
    # home as off in EVERY PWA, so a wrong one (home up, probe merely slow) lies to the
    # whole family — and until now a timed-out poll logged NOTHING, which made a false
    # "éteint" impossible to attribute after the fact. But this now runs on a keepalive
    # every 30 s, so logging each poll would be a tap, not a signal. Two lines only:
    # a success that burned more than half its budget (the early warning that the leg
    # is going cold), and the verdict FLIPS (logged by the caller). Steady state is
    # silent.
    timeouts = ((STATUS_KEEPALIVE_FIRST_TIMEOUT_S, STATUS_KEEPALIVE_RETRY_TIMEOUT_S)
                if background
                else (STATUS_POLL_FIRST_TIMEOUT_S, STATUS_POLL_RETRY_TIMEOUT_S))
    for attempt, timeout in enumerate(timeouts):
        started = time.monotonic()
        try:
            r = await _http_client.head(STATUS_TARGET_URL, timeout=timeout)
            elapsed = time.monotonic() - started
            if elapsed > timeout / 2:
                logger.warning(
                    "poll slow: attempt=%d answered %s in %.2fs of a %.1fs budget "
                    "(a cold TLS leg from this VM is what produces a false DOWN)",
                    attempt + 1, r.status_code, elapsed, timeout,
                )
            degraded = r.status_code >= 500
            if degraded:
                logger.warning(
                    "status target degraded: %s returned %s (host is up)",
                    STATUS_TARGET_URL, r.status_code,
                )
            return True, degraded
        except httpx.HTTPError:
            if attempt == len(timeouts) - 1:
                return False, False
    return False, False


def _current_eta_s() -> int:
    # Median of the observed boot durations, in seconds, for the shared wake
    # countdown. Empty ring (cold relay) → fallback. Mirrors the PWA's getEta().
    if not _boot_history:
        return ETA_FALLBACK_S
    s = sorted(_boot_history)
    n = len(s)
    mid = n // 2
    med = (s[mid - 1] + s[mid]) / 2 if n % 2 == 0 else s[mid]
    return max(1, round(med / 1000))


async def _poll_home_and_update(background: bool = False) -> None:
    # Run one poll and fold the verdict into the cache. Shared by the blocking
    # path (cold/expired cache) and the background SWR refresh. Caller holds
    # _status_poll_lock.
    global _wake_pending, _consecutive_poll_failures
    prev_poll_at = _status_cache.last_poll_at
    prev_state = _status_cache.last_state
    ok, degraded = await _poll_home(background=background)
    polled_at = time.monotonic()
    # Verdict flips only — the one line that matters, and the one that makes a false
    # negative self-evident afterwards ("home DOWN" while the box was demonstrably up)
    # instead of something to be reconstructed from silence. Steady state logs nothing.
    if prev_state is not None and ok != prev_state:
        logger.info("verdict flip: %s → %s", "UP" if prev_state else "DOWN",
                    "UP" if ok else "DOWN")
    _status_cache.last_poll_at = polled_at
    _status_cache.last_degraded = degraded
    if ok:
        _consecutive_poll_failures = 0
        _status_cache.last_state = True
        _status_cache.last_success_at = polled_at
        # Shared-ETA measurement: a wake was pending and the home just answered →
        # record the boot duration (bounded like the PWA's own history; a wake
        # fired against an already-up server measures <BOOT_MIN_MS and is dropped).
        if _wake_pending and _last_wol_at:
            boot_ms = (polled_at - _last_wol_at) * 1000.0
            # Polls only happen on /status traffic (SWR, no periodic loop), so a
            # wake with no PWA open (e.g. home-watch auto-WoL) sees its up-flip
            # only at the NEXT poll, minutes later — that measures poll latency,
            # not the boot. Only record when the previous poll was recent enough
            # for the flip timestamp to be trustworthy; still consume the pending
            # wake either way so a later poll can't record an even worse sample.
            tight_polling = (polled_at - prev_poll_at) <= BOOT_SAMPLE_MAX_POLL_GAP_S
            if tight_polling and BOOT_MIN_MS <= boot_ms <= BOOT_MAX_MS:
                _boot_history.append(boot_ms)
            elif not tight_polling:
                logger.info(
                    "boot sample dropped: poll gap %.0fs > %ss (no client polling during wake)",
                    polled_at - prev_poll_at, BOOT_SAMPLE_MAX_POLL_GAP_S,
                )
            _wake_pending = False
    else:
        _consecutive_poll_failures += 1
        if _status_cache.last_state is None:
            # First-ever poll failed → bootstrap with a real verdict so the cache isn't
            # stuck on "no opinion". (A cold-start poll is the least reliable one there
            # is, but with nothing to compare against we have no better answer.)
            _status_cache.last_state = False
            _status_cache.last_success_at = polled_at
        elif _consecutive_poll_failures < STATUS_DOWN_CONFIRM_POLLS:
            # Confirm-before-DOWN: a SINGLE failed poll must not turn the home off for
            # the whole family. The relay→home leg is flaky by nature (attempt=1 fails on
            # nearly every poll — the pooled HTTP/2 connection dies between polls), so an
            # isolated failure carries no information. Keep serving the last known
            # verdict, and hold the freshness clock with it: otherwise last_success_at
            # ages past STATUS_CACHE_STALE_S and /status demotes to "down" anyway — the
            # staleness backstop would silently defeat the confirmation.
            _status_cache.last_success_at = polled_at
            logger.info(
                "poll failed (%d/%d) — holding the last verdict (%s) rather than "
                "flipping the home off on one flaky probe",
                _consecutive_poll_failures, STATUS_DOWN_CONFIRM_POLLS,
                "UP" if _status_cache.last_state else "DOWN",
            )
        else:
            # Confirmed: consecutive failures agree. Now we believe it.
            _status_cache.last_state = False


async def _keepalive_loop() -> None:
    # See STATUS_KEEPALIVE_UP_S. Holds the relay→home leg warm so /status is always
    # served from a fresh cache instead of blocking on a cold handshake. Never raises:
    # a failed poll is already absorbed by _poll_home returning False, and this task
    # must outlive every transient (a crash here would silently restore the old cold
    # behaviour — the exact bug it exists to prevent).
    while True:
        try:
            async with _status_poll_lock:
                await _poll_home_and_update(background=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("keepalive poll failed")
        delay = (STATUS_KEEPALIVE_UP_S if _status_cache.last_state
                 else STATUS_KEEPALIVE_DOWN_S)
        await asyncio.sleep(delay)


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
async def status(request: Request, x_token: str | None = Header(None)):
    # Same shared token as /wol (the PWA already holds it). Closes the
    # anonymous "is the home up?" info disclosure: before this, anyone who
    # knew the relay domain could read the home's up/down state. Checked
    # BEFORE the config-state branch so an unauthenticated caller learns
    # nothing (not even whether STATUS_TARGET_URL is configured). Clients
    # without a token fall back to their direct-home probe (the 401 is an
    # "answered" rejection on the PWA side — relay alive, oracle denied).
    if x_token is None or not hmac.compare_digest(x_token, SHARED_TOKEN):
        logger.warning("status ip=%s status=401 reason=bad_token", client_ip(request))
        raise HTTPException(status_code=401, detail="bad token")
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
            # Only meaningful alongside up=True: the box answers, the probed
            # app doesn't. The PWA stays green (no pointless WoL) and warns.
            if _status_cache.last_state and _status_cache.last_degraded:
                body["degraded"] = True
    # Wake-in-progress: advertise a recently-fired /wol so every open PWA can
    # show the boot countdown, not just the device that initiated it. Only while
    # still down — once up, the green verdict drives the UI and the signal is moot.
    if not body["up"] and _last_wol_at:
        wake_age = time.monotonic() - _last_wol_at
        if wake_age < WAKE_SIGNAL_TTL_S:
            body["waking"] = True
            body["wake_age_s"] = int(wake_age)
    window = current_window()
    if window:
        body["window"] = window
    # Canonical shared boot ETA — served on every /status (cheap) so every open
    # PWA seeds its wake countdown from the same value, synced across devices.
    body["eta_s"] = _current_eta_s()
    note_usage(clean_cid(request.headers.get("x-client-id")),
               request.headers.get("user-agent"), client_ip(request))
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
    global _last_wol_at, _wake_pending
    # Anchor the boot-time measurement to the FIRST POST of a wake cycle. The
    # PWA fires ~4 retry POSTs over ~60 s to cover UDP loss; if each reset the
    # anchor, boot_ms (_poll_home_and_update) would measure only the
    # last-retry->up gap (~14 s) instead of the true ~75 s boot, dragging the
    # shared eta_s far below reality. Reset only for a genuinely new cycle:
    # none pending, or the pending one is older than the wake-signal TTL (a
    # stale/failed wake). Also keeps wake_age_s anchored for cross-device adopt.
    now = time.monotonic()
    if not _wake_pending or (now - _last_wol_at) > WAKE_SIGNAL_TTL_S:
        _last_wol_at = now
    _wake_pending = True
    logger.info("wol ip=%s device=%s cid=%s status=200", ip,
                device_class(request.headers.get("user-agent")),
                clean_cid(request.headers.get("x-client-id")))
    return {"sent": True, "to": target_ip, "port": TARGET_PORT, "repeats": PACKET_REPEATS}
