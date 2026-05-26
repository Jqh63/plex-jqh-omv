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

- **Server status** — checks every 15s via HTTPS fetch (steady-state offline detection within ~24s of the actual outage)
- **Wake-on-LAN** — sends magic packet via a self-hosted HTTP→UDP relay (browsers cannot send UDP directly — see [Relay](#relay-required-for-wol) below)
- **Auto-retry** — re-sends the WoL POST at +15/30/60/90s and polls every 5s post-WoL until the server answers or 5 min timeout
- **Dynamic app links** — configurable via URL parameter, built from a catalog of known apps
- **Read-only mode** — works without a MAC address (status monitoring only, no WoL button)
- **Installable PWA** — works on PC, Android (Chrome) and iOS (Safari)
- **Auto-update** — new service worker triggers a toast notification

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

Open the app without parameters. A settings form will appear to enter title, domain, MAC address, WoL port, relay URL, shared token, and apps.

## Status check logic

The app pings `https://{statusHost}` with `mode: 'no-cors'`. Any HTTP response = server up, network error = server down.

`statusHost` resolves as: explicit `?status=` param → first subdomain app from the apps list → base `host`.

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
GET /health
  → 200 {"status":"ok"}        (used by the PWA reachability probe)

POST /wol
  Headers: Content-Type: application/json
           X-Token: <shared-token>
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

The app probes `GET <relay>/health` on load and at every status tick (every 15 s). When the probe fails, the power button is visually disabled and labelled "Réveil indisponible — utilise le réveil manuel ↓" so users get explicit feedback instead of a silent click. A small permanent "Réveil manuel" link sits under the power button — it covers cases the probe can miss (relay up but `/wol` broken, network blip mid-dispatch) and the residual SPOF inherent to any single relay.

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
- Relay reachability probed with `fetch GET /health` — true status code read (no `no-cors` blind spot)
- Status check on the media server itself uses `fetch` with `mode: 'no-cors'` — opaque response = server up, network error = server down (no CORS needed since we only care about reachability)

## License

MIT
