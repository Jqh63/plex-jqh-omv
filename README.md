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
| `chat` | `chat.{host}` | Assistant IA |
| `librechat` | `librechat.{host}` | Assistant IA |
| `jellyfin` | `jellyfin.{host}` | Regarder sur Jellyfin |
| `plexweb` | `https://app.plex.tv` | Regarder sur Plex |

Unknown keys create a generic link to `{key}.{host}`.

#### Examples

Existing setup (unchanged — no URL update needed for current users):
```
?mac=7085c2fb2992&host=jqh.duckdns.org&port=9
```
→ title "Plex jqh omv", Seerr + Plex buttons, status on `seerr.jqh.duckdns.org`

Add LibreChat to existing setup:
```
?mac=7085c2fb2992&host=jqh.duckdns.org&port=9&apps=seerr,chat,plexweb
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

Example: `host=jqh.duckdns.org` + `apps=seerr,plexweb` → pings `seerr.jqh.duckdns.org` (DuckDNS wildcard cert covers subdomains but not the root domain).

## Files

| File | Description |
|---|---|
| `index.html` | App (HTML + CSS + JS, single file) |
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

## Technical notes

- URL params are never cleaned (required for iOS standalone — separate localStorage)
- Service worker uses `ignoreSearch: true` to match cache with query params
- `skipWaiting` + `clients.claim` + `controllerchange` listener for seamless updates
- WoL sent via hidden iframe to depicus.com (browsers cannot send UDP directly)
- Status check uses `fetch` with `mode: 'no-cors'` — opaque response = server up, network error = server down

## License

MIT
