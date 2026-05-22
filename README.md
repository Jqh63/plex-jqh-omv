# Plex jqh omv

A lightweight PWA to wake and monitor a media server via Wake-on-LAN.

## Features

- **Server status** — checks every 30s via HTTPS fetch
- **Wake-on-LAN** — sends magic packet through [depicus.com](https://www.depicus.com) (hidden iframe)
- **Auto-retry** — rechecks at 1, 2, and 3 min after WoL; manual retry available after 4 min
- **Dynamic app links** — configurable via URL parameter, built from a catalog of known apps
- **Read-only mode** — works without a MAC address (status monitoring only, no WoL button)
- **Installable PWA** — works on PC, Android (Chrome) and iOS (Safari)
- **Auto-update** — new service worker triggers a toast notification

## Setup

### Option 1 — URL parameters (recommended)

Share this link with your users:

```
https://<your-github>.github.io/plex-jqh-omv/?mac=AABBCCDDEEFF&host=myserver.example.com&port=9
```

Parameters are read on first visit and stored in localStorage. The app works immediately.

#### All URL parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `host` | yes | — | Base domain (used for WoL and deriving app URLs) |
| `mac` | no | — | MAC address for Wake-on-LAN (omit for read-only mode) |
| `port` | no | `9` | WoL UDP port |
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

Plex + Seerr setup (WoL + Seerr link + Plex link):
```
?mac=AABBCCDDEEFF&host=myserver.example.com&port=9
```

Customise the apps shown (override the default list):
```
?mac=AABBCCDDEEFF&host=myserver.example.com&port=9&apps=overseerr,jellyfin,plexweb
```

Another user with Jellyfin, no WoL:
```
?host=mon.serveur.com&apps=jellyseerr,jellyfin&title=Mon+Media
```

### Option 2 — Manual configuration

Open the app without parameters. A settings form will appear to enter title, domain, MAC address, WoL port, and apps.

## Status check logic

The app pings `https://{statusHost}` with `mode: 'no-cors'`. Any HTTP response = server up, network error = server down.

`statusHost` resolves as: explicit `?status=` param → first subdomain app from the apps list → base `host`.

Example: `host=myserver.example.com` + `apps=seerr,plexweb` → pings `seerr.myserver.example.com`. Useful when the root domain lacks a valid SSL cert (e.g. DuckDNS wildcard certs cover `*.domain` but not the bare root).

## Files

| File | Description |
|---|---|
| `index.html` | App (HTML + CSS + JS, single file) |
| `fallback.html` | French manual-WoL fallback page (single file, opened from the PWA when the depicus probe fails or as a permanent safety net) |
| `manifest.json` | PWA manifest |
| `sw.js` | Service worker (cache with `ignoreSearch`, auto-update) |
| `icon-192.png` | App icon 192x192 |
| `icon-512.png` | App icon 512x512 |
| `icon.svg` | Source icon (SVG) |

## Deployment

1. Create a public GitHub repository
2. Upload all files
3. Enable GitHub Pages (Settings > Pages > Deploy from branch `main`)
4. Share the URL with parameters to your users

## Security

- **Zero personal data in source code** — MAC, host and port are only in URL parameters and localStorage
- No `innerHTML` for user data — DOM API only (XSS safe)
- `rel="noopener"` on all external links
- No API keys, tokens or passwords
- WoL risk: someone with the URL can only power on the server — no access to services

## Compatibility

| Platform | Browser | Installed PWA |
|---|---|---|
| PC | Chrome / Firefox / Edge / Safari | — |
| Android | Chrome | ✅ |
| iOS | Safari | ✅ |

> iOS PWA must be installed from Safari. Chrome on iOS does not support Add to Home Screen.

## Fallback — manual Wake-on-LAN if the power button fails

This app sends the magic packet via [depicus.com](https://www.depicus.com), a third-party HTTP→UDP relay (browsers cannot send UDP directly). If depicus is unreachable, the power button silently fails — the iframe still loads but no packet is emitted.

Since **v2.13**, the app probes depicus reachability on load and every 30 s. When the probe fails, the power button is visually disabled and labelled "Service WoL externe injoignable" so users get explicit feedback instead of a silent click. The probe uses `fetch` with `mode: 'no-cors'`, which detects DNS / TLS / total network outage but **cannot** detect a Cloudflare 522 from depicus (the edge still serves a valid HTTP response). A small permanent "Réveil manuel" link sits under the power button — it covers the residual case and the deeper SPOF until a self-hosted relay replaces depicus.

Since **v2.14**, that link points to a dedicated **French user-friendly fallback page** served from this same repo (`fallback.html`), with the user's MAC, host and port pre-filled (read from URL query parameters). The page gives a per-OS method (Android: WolOn, iOS: Mocha WOL, Windows: PowerShell), with click-to-copy on the parameter values. The section below remains the canonical developer reference.

Until that single point of failure is resolved, users can wake the server manually using OS-native tools. Keep the same values handy as your URL parameters: server MAC address, host (e.g. `myserver.example.com`), and UDP port (default `9`).

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

> All these tools send the same magic packet that depicus sends. They work whether the server is on your LAN (direct broadcast) or remote (provided your router/NAT forwards UDP port 9 to the server's broadcast address). If WoL succeeded with this app before, it will succeed with these tools using the same parameters.

## Technical notes

- URL params are never cleaned (required for iOS standalone — separate localStorage)
- Service worker uses `ignoreSearch: true` to match cache with query params
- `skipWaiting` + `clients.claim` + `controllerchange` listener for seamless updates
- WoL sent via hidden iframe to depicus.com (browsers cannot send UDP directly)
- Status check uses `fetch` with `mode: 'no-cors'` — opaque response = server up, network error = server down

## License

MIT
