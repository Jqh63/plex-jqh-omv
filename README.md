# Plex jqh omv

A lightweight PWA to wake and monitor a Plex media server via Wake-on-LAN.

## Features

- **Server status** — checks every 30s via HTTPS fetch
- **Wake-on-LAN** — sends magic packet through [depicus.com](https://www.depicus.com) (hidden iframe)
- **Auto-retry** — rechecks at 1, 2, 3 min after WoL. Retry available after 4 min
- **Quick links** — dynamic Seerr and Plex links based on configured host
- **Installable PWA** — works on PC, Android (Chrome) and iOS (Safari)
- **Auto-update** — new service worker triggers automatic page reload

## Setup

### Option 1 — URL parameters (recommended)

Share this link with your users:

```
https://<your-github>.github.io/plex-jqh-omv/?mac=AABBCCDDEEFF&host=myserver.example.com&port=9
```

Parameters are read on first visit and stored in localStorage. The app works immediately.

### Option 2 — Manual configuration

Open the app without parameters. A settings form will appear to enter MAC address, hostname and WoL port.

## Files

| File | Description |
|---|---|
| `index.html` | App (HTML + CSS + JS, single file) |
| `manifest.json` | PWA manifest (no `start_url`, preserves URL params) |
| `sw.js` | Service worker (cache with `ignoreSearch`, auto-update) |
| `icon-192.png` | App icon 192x192 (Android + iOS home screen) |
| `icon-512.png` | App icon 512x512 (Android splash screen) |
| `icon.svg` | Source icon (SVG) |

## Deployment

1. Create a public GitHub repository
2. Upload all files (`index.html`, `manifest.json`, `sw.js`, `icon-192.png`, `icon-512.png`, `icon.svg`, `README.md`)
3. Enable GitHub Pages (Settings > Pages > Deploy from branch `main`)
4. Share the URL with parameters to your users

## Security

- **Zero personal data in source code** — MAC, host and port are only in URL parameters and localStorage
- No `innerHTML` — `textContent` only (XSS safe)
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
