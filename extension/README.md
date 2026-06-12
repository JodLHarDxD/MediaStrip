# MediaStrip Catcher — Browser Extension

IDM-style media catcher for Chrome / Edge / Brave. Shows a floating **MediaStrip**
download button over any video or large image, sniffs streaming manifests (m3u8/DASH)
from network traffic, forwards your session cookies for login-gated streams, and sends
everything to your local MediaStrip server.

## Install (Developer Mode)

1. Start the MediaStrip server:
   ```
   python -m uvicorn main:app --port 8000
   ```
2. Open the extensions page:
   - **Chrome** → `chrome://extensions`
   - **Edge** → `edge://extensions`
   - **Brave** → `brave://extensions`
3. Enable **Developer mode** (top-right toggle in Chrome/Edge; left sidebar in Brave)
4. Click **Load unpacked** → select this `extension/` folder
5. Pin "MediaStrip Catcher" to the toolbar

> **Brave note:** Brave Shields does not block your own extensions — the catcher
> works on Shields-up sites. If a site's video won't sniff, lower Shields for that
> site once and reload; aggressive fingerprint blocking can stall some players.

## How it works

| Surface | What happens |
|---------|--------------|
| Hover a `<video>` or large image | Floating ⬇ MediaStrip button appears — click to download |
| Video uses blob:/MSE (YouTube etc.) | Page URL is sent instead — server-side yt-dlp extracts the real stream |
| Direct file (mp4/jpg/zip…) | Server downloads with **8 parallel Range connections** (IDM-style) |
| HLS/DASH manifest sniffed | Toolbar badge counts it — download from the popup, Referer auto-attached |
| Toolbar popup | Lists everything caught on the tab + "Download media on this page" button |

## Settings

Popup → server URL field. Default `http://localhost:8000`.
Point it at a remote MediaStrip instance if you run one.

## Login-gated streams (cookies)

Some sites only serve the video to a logged-in session. The extension reads the
cookies **for that media domain only** (via `chrome.cookies.getAll`) and forwards
them with the download request, so the server fetches the stream exactly as your
browser would. Cookies are scoped to the target domain — no blanket cookie dump.

> **Privacy:** cookies are sent to whatever server URL is set in the popup
> (default `http://localhost:8000`, your own machine). Only point the extension at
> a remote server you trust — that server receives session cookies for gated downloads.

## What it can and cannot download

| Works | Won't work |
|-------|-----------|
| yt-dlp-supported sites (1000+) | DRM-protected (Netflix, Prime, Disney+, Crunchyroll premium) — Widevine-encrypted, no tool can |
| Anime sites (hianime/aniwatch/kaido) via resolver | — |
| Obfuscated players — sniffed at network level | — |
| Login-gated streams — cookies forwarded | — |
| Direct files — 8-connection accelerated | — |

## Permissions explained

- `webRequest` + `<all_urls>` — observe (not block) responses to sniff media content-types
- `cookies` — read cookies for a media domain to download login-gated streams
- `storage` — remember server URL; per-tab catch lists live in session storage
- `tabs` — read active tab URL for page-level extraction
