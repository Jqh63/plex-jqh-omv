# Plex jqh omv

A lightweight PWA to wake and monitor a media server via Wake-on-LAN.

This repo bundles **two components**:

- The **PWA** itself (`index.html`, `sw.js`, `manifest.json`, …) — pure
  HTML/JS/CSS, hosted on GitHub Pages.
- A **reference HTTP→UDP relay** in [`relay/`](relay/) — the small
  server-side process the PWA `POST`s to in order to dispatch the
  magic packet (browsers cannot send raw UDP). Use this implementation
  as-is, or substitute any compatible relay; the wire contract is
  documented under [API contract](#api-contract) below.

## Features

- **Server status** — checks every 15s by fetching the relay's `GET /status` oracle (steady-state offline detection within ~24s of the actual outage)
- **Wake-on-LAN** — sends magic packet via a self-hosted HTTP→UDP relay (browsers cannot send UDP directly — see [Relay](#relay-required-for-wol) below)
- **Auto-retry** — re-sends the WoL POST at +15/30/60/90s and polls every 5s post-WoL until the server answers or 5 min timeout
- **Dynamic app links** — configurable via URL parameter, built from a catalog of known apps
- **Read-only mode** — works without a MAC address (status monitoring only, no WoL button)
- **Installable PWA** — works on PC, Android (Chrome) and iOS (Safari)
- **Auto-update** — a new service worker is detected (on focus / visibility / periodic poll) and the page silently auto-reloads to pick it up

## Setup

### Option 1 — URL parameters (recommended)

Share this link with your users:

```
https://<your-github>.github.io/plex-jqh-omv/?mac=AABBCCDDEEFF&host=myserver.example.com&port=9&relay=https://wol.example.com&token=<your-shared-token>
```

Parameters are read on first visit and stored in localStorage. The app works immediately.

#### All URL parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `host` | yes | — | Base domain (used for status check target and deriving app URLs) |
| `mac` | no | — | MAC address for Wake-on-LAN (omit for read-only mode — power button hidden) |
| `port` | no | `9` | WoL UDP port (forwarded to relay) |
| `relay` | no | — | HTTPS URL of your self-hosted relay (e.g. `https://wol.example.com`). Required together with `token` to enable the power button |
| `token` | no | — | Shared secret sent as `X-Token` header to the relay. Required together with `relay` |
| `title` | no | `Plex jqh omv` | App title shown in the header |
| `apps` | no | `seerr,plexweb` | Comma-separated list of app keys (see catalog below) |
| `status` | no | first subdomain app | Override the host used for the status check |
| `ip` | no | — | IPv4 address of the server, shown on the manual fallback page as an alternative to the domain. When present, the ready-to-paste PowerShell / `wakeonlan` commands use the IP instead of `host` so they keep working during a DNS outage on the domain. |
| `window` | no | — | Scheduled-uptime window, `13h50-00h10` or `13:50-00:10` (may wrap past midnight). Purely informative: outside the window a red status becomes a calm blue "Éteint (prévu)" card with the auto-wake time instead of the alarming red "Hors ligne", so a deliberate nightly shutdown doesn't look like an outage. Never gates the power button. Since v8.12 the relay can also serve this value (`UPTIME_WINDOW` env → `window` field in `/status`); a relay-served window is adopted and persisted automatically, so existing users get it without a new URL — and the relay value wins over the local one. |

#### App catalog

| Key | URL | Label |
|---|---|---|
| `seerr` | `seerr.{host}` | Demander un film / une série |
| `overseerr` | `overseerr.{host}` | Demander un film / une série |
| `jellyseerr` | `jellyseerr.{host}` | Demander un film / une série |
| `jellyfin` | `jellyfin.{host}` | Regarder sur Jellyfin |
| `plexweb` | `https://app.plex.tv` | Regarder sur Plex |

Unknown keys create a generic link to `{key}.{host}`.

#### Examples

Plex + Seerr setup (full WoL + Seerr link + Plex link):
```
?mac=AABBCCDDEEFF&host=myserver.example.com&port=9&relay=https://wol.example.com&token=<shared-token>
```

Customise the apps shown (override the default list):
```
?mac=AABBCCDDEEFF&host=myserver.example.com&port=9&relay=https://wol.example.com&token=<shared-token>&apps=overseerr,jellyfin,plexweb
```

Another user with Jellyfin, no WoL (read-only mode):
```
?host=mon.serveur.com&apps=jellyseerr,jellyfin&title=Mon+Media
```

### Option 2 — Manual configuration

Open the app without parameters. A settings form will appear to enter title, domain, MAC address, WoL port, server IP (optional, used on the manual fallback page when the domain is unreachable), relay URL, shared token, and apps.

## Status check logic

Since **v7.0 (relay-as-oracle)** the primary status check is a single `fetch GET {relay}/status` — done once on open and at every 15s tick. The relay polls the home server itself (HEAD on a configurable `STATUS_TARGET_URL`, 5s fresh / 60s stale cache) and returns `{up, stale, age_s}`, so one fetch answers both *is the home server up* and *is the relay reachable*. See [Relay](#relay-required-for-wol).

**v8.0 (single-probe model)** keeps that contract but rewrites the client state machine. `checkStatus()` runs one probe that resolves exactly once to `{up, relayReachable}`: one relay `/status` fetch with a generous 8s timeout (so a cold mobile radio warms *inside* the attempt) and, on its failure, one direct-home fallback — no retry chain, no fail-streaks, no all-timeout hold. A `probeGen` counter drops a stale in-flight probe that resolves after a resume. This replaced a v4→v7 pile of cold-radio defences that could hold the orange "Vérification…" card for ~33s on reopen; v8's worst case is one PROBE+HOME (~13s, genuine outage only), commonly <3s.

**v8.2** hardens two failure modes that slipped past v8.1:

- **A `checking` watchdog.** `checkStatus()` early-returns while a check is in flight (`if (checking) return`) so concurrent probes don't pile up. But the Android suspend-mid-fetch race can tear down the socket and freeze the probe's abort timer with it, so the probe never resolves and never clears `checking` — and *every* subsequent re-probe then early-returns forever, freezing the app on a stale status until it's force-killed. v8.2 stamps each check's start time and, past a `PROBE+HOME+slack` budget (~14s, under the 15s tick), lets the next re-probe trigger reclaim the wedged flag and start fresh (the `probeGen` bump drops the zombie probe if it ever resolves late). The 15s self-healing tick is now genuinely guaranteed-eventually — it can no longer be blocked by a stuck flag.
- **An N-consecutive-miss relay-down debounce** (was 1-tick in v8.1). A cold e2-micro can miss `/status` across *more than one* 15s tick, and v8.1's 2-miss confirm painted a false "Relais injoignable" on cold open (~15s in, cleared ~30s in). v8.2 waits for `RELAY_DOWN_MISSES` (3) misses in a row before the advisory cosmetic hardens; any answered/up probe resets the streak. The relay-down indicator is purely advisory — a real relay failure the user hits via WoL still surfaces instantly (the `POST /wol` failure path flips the state directly) — so it can afford the patience.

**Fallback** — if no relay is configured, or if `/status` is unusable (timeout / non-200 / unexpected shape), the app falls back to a direct `fetch https://{statusHost}` with `mode: 'no-cors'`: any fulfilled response = server up, network error = server down.

`statusHost` (used by the fallback) resolves as: explicit `?status=` param → first subdomain app from the apps list → base `host`.

Example: `host=myserver.example.com` + `apps=seerr,plexweb` → pings `seerr.myserver.example.com`. Useful when the root domain lacks a valid SSL cert (e.g. DuckDNS wildcard certs cover `*.domain` but not the bare root).

## Files

### PWA (repo root)

| File | Description |
|---|---|
| `index.html` | App markup + inline `<style>` |
| `app.js` | App logic (extracted from `index.html` so the CSP can drop `'unsafe-inline'` from `script-src`) |
| `fallback.html` | French manual-WoL fallback page markup, opened from the PWA when the relay probe fails or as a permanent safety net |
| `fallback.js` | Logic for `fallback.html` (same CSP rationale as `app.js`) |
| `debug.html` | Debug snapshot page (long-press the app title for 2s to open) |
| `debug.js` | Logic for `debug.html` |
| `manifest.json` | PWA manifest |
| `sw.js` | Service worker (cache with `ignoreSearch`, auto-update) |
| `icon-192.png` | App icon 192x192 |
| `icon-512.png` | App icon 512x512 |
| `icon.svg` | Source icon (SVG) |

### Relay ([`relay/`](relay/))

See [`relay/README.md`](relay/README.md) for the full file inventory
(FastAPI app, Caddyfile, systemd unit, env templates, bootstrap
scripts, GitOps deploy channel). Use it as-is, fork it, or replace
with any backend that honours the [API contract](#api-contract).

## Deployment

### PWA (GitHub Pages)

1. Create a public GitHub repository
2. Upload all files
3. Enable GitHub Pages (Settings > Pages > Deploy from branch `main`)
4. Share the URL with parameters to your users

### Relay (your own small VM)

Follow [`relay/README.md`](relay/README.md). The relay needs a public
HTTPS endpoint and UDP egress — any always-free VM works (GCP
e2-micro, Oracle Cloud, etc.).

## Security

- **Zero personal data in source code** — MAC, host, port, relay URL and token are only in URL parameters and localStorage
- No `innerHTML` for user data — DOM API only (XSS safe)
- `rel="noopener"` on all external links
- No API keys, tokens or passwords baked into the source — the shared `token` lives only in the per-user URL (treated as a sensitive bookmark)
- WoL risk: someone with the URL can only power on the server — no access to services. Relay-side MAC allowlist (recommended) further restricts what targets the token can wake

## Compatibility

| Platform | Browser | Installed PWA |
|---|---|---|
| PC | Chrome / Firefox / Edge / Safari | — |
| Android | Chrome | ✅ |
| iOS | Safari | ✅ |

> iOS PWA must be installed from Safari. Chrome on iOS does not support Add to Home Screen.

## Relay (required for WoL)

Browsers cannot send raw UDP, so the magic packet must be dispatched by a small HTTP→UDP relay you host yourself. The PWA `POST`s a JSON payload over HTTPS; the relay validates and forwards the magic packet to the target.

**A reference implementation ships in [`relay/`](relay/)** — FastAPI +
Caddy auto-HTTPS, ~80 lines of Python, sandboxed systemd unit, GitOps
deploy channel. The contract below is the source of truth; any
backend that honours it is interchangeable with the reference.

### API contract

```
GET /status
  Headers: X-Token: <shared-token>, X-Client-Id: <opaque-uuid> (optional)
  → 200 {"up":bool, "stale":bool, "age_s":int}   (the PWA's steady-state oracle —
        relay polls the home server itself and caches the verdict; one fetch tells
        the app both "home up?" and "relay reachable?")
        + "window":str   if the relay has UPTIME_WINDOW set (adopted by the PWA)
        + "waking":true, "wake_age_s":int   while a recent POST /wol is booting the
          home and it's not up yet — lets any open PWA show the wake countdown
        + "eta_s":int   canonical boot ETA (median of relay-measured boot times) —
          every open PWA seeds its wake countdown from it, so the timer is synced
          across devices
  → 503 if the relay has no STATUS_TARGET_URL configured

GET /health
  → 200 {"status":"ok"}        (relay liveness; hit only by the settings
        "Tester le relais" button — /health/deep with /health legacy fallback)

POST /wol
  Headers: Content-Type: application/json
           X-Token: <shared-token>
           X-Client-Id: <opaque-uuid> (optional — device telemetry, logged not stored)
  Body:    {"mac":"AA:BB:CC:DD:EE:FF"}
  → 200 on success, 401 on bad token, 403 on disallowed MAC,
    422 on malformed body, 4xx/5xx otherwise.

CORS: must allow your GitHub Pages origin
  (e.g. https://<your-github>.github.io) for POST + GET with header X-Token.
```

### Recommended hardening

- **MAC allowlist on the relay side** — only the MAC(s) you own can be woken. A leaked token then only powers on hardware you control.
- **TLS via Let's Encrypt** (Caddy auto-HTTPS or equivalent) — the token transits in a header, must be encrypted.
- **Resolve the target host server-side** — do not accept an IP from the client; resolve a fixed FQDN like `myserver.example.com` instead so a leaked token cannot redirect packets to arbitrary hosts.
- **Drop privileges** — run the relay as a non-root user, disable PrivateTmp / write paths in systemd.

### Reference implementation

Full working code (FastAPI + Caddy + systemd unit + deploy scripts)
lives in [`relay/`](relay/). It implements every item under
*Recommended hardening* above and adds a GitOps deploy channel for
safe remote updates. See [`relay/README.md`](relay/README.md) for the
full procedure.

### Hosting suggestions

Any small VM with UDP egress and a public HTTPS endpoint works. Some always-free options as of 2026: **GCP Compute Engine e2-micro** (us-west1/central1/east1), **Oracle Cloud Always Free** (broader regions). Avoid serverless platforms that can't open raw UDP sockets (Cloudflare Workers, Vercel Edge, Deno Deploy).

## Fallback — manual Wake-on-LAN if the relay fails

The app probes `GET <relay>/status` on load and at every status tick (every 15 s); relay reachability is derived from that same fetch. When the fetch fails at transport level (timeout / network / DNS), the relay is treated as unreachable, the power button is visually disabled and labelled "Réveil indisponible — utilise le réveil manuel ↓" so users get explicit feedback instead of a silent click. Since v8.2 this hard "unreachable" state is debounced by three consecutive misses — a slow-but-alive relay (a cold e2-micro can miss across more than one tick) stays optimistic and the disabled state only paints once `RELAY_DOWN_MISSES` probes in a row miss (see [Status check](#status-check) above), so it no longer false-alarms on cold open. (An *answered* but degraded `/status` keeps the button enabled, since `POST /wol` still works.) A small permanent "Réveil manuel" link sits under the power button — it covers cases the probe can miss (relay up but `/wol` broken, network blip mid-dispatch) and the residual SPOF inherent to any single relay.

That link points to a dedicated **French user-friendly fallback page** served from this same repo (`fallback.html`), with the user's MAC, host and port pre-filled (read from URL query parameters). The page gives a per-OS method (Android: WolOn, iOS: Mocha WOL, Windows: PowerShell), with click-to-copy on the parameter values. The section below remains the canonical developer reference.

If the relay is down for a while, users can wake the server manually using OS-native tools. Keep the same values handy as your URL parameters: server MAC address, host (e.g. `myserver.example.com`), and UDP port (default `9`).

### Android

**WolOn** (Play Store) — enter MAC, host, port. Sends from mobile data or Wi-Fi.

### iOS

- **Mocha WOL** (App Store)
- **Wake on LAN** by Hjørnet (App Store)

Same config: MAC, host, port.

### Windows

**PowerShell one-liner** — no install, PowerShell ships with Windows. Replace `AABBCCDDEEFF` with your MAC (no separators) and `myserver.example.com` with your host:

```powershell
$mac=[byte[]]-split('AABBCCDDEEFF' -replace '..','0x$0 ');$u=New-Object Net.Sockets.UdpClient;$u.Connect('myserver.example.com',9);$u.Send(([byte[]](,0xFF*6)+($mac*16)),102)|Out-Null
```

### Linux / macOS

- **`wakeonlan`** package — `sudo apt install wakeonlan` (Debian/Ubuntu) or `brew install wakeonlan` (macOS), then:

  ```bash
  wakeonlan -i myserver.example.com -p 9 AA:BB:CC:DD:EE:FF
  ```

- **`etherwake`** (LAN-only, no host argument): `sudo etherwake AA:BB:CC:DD:EE:FF`

> All these tools send the same magic packet the relay sends. They work whether the server is on your LAN (direct broadcast) or remote (provided your router/NAT forwards UDP port 9 to the server's broadcast address). If WoL succeeded with this app before, it will succeed with these tools using the same parameters.

## Technical notes

- URL params are never cleaned (required for iOS standalone — separate localStorage)
- Service worker uses `ignoreSearch: true` to match cache with query params
- `skipWaiting` + `clients.claim` + `controllerchange` listener for seamless updates
- WoL sent via `fetch POST` to your self-hosted relay (browsers cannot send UDP directly — see [Relay](#relay-required-for-wol))
- Primary status check is `fetch GET <relay>/status` — true JSON verdict `{up,stale,age_s}` read (no `no-cors` blind spot); relay reachability derived from the same fetch (answered vs transport failure). `GET /health` / `/health/deep` are hit only by the settings "Tester le relais" button
- Fallback status check on the media server itself uses `fetch` with `mode: 'no-cors'` — opaque response = server up, network error = server down (used only when `/status` is unavailable; no CORS needed since we only care about reachability)

## License

MIT
