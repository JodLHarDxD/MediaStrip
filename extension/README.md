# MediaStrip Catcher — Browser Extension

IDM-style integrated media catcher for Chrome / Edge / Brave. Goes *inside* the page:
scans the DOM for media, sniffs the HLS/DASH streams the player loads (even inside
iframes), lists everything in a floating panel, forwards your session cookies for
login-gated streams, and sends downloads to MediaStrip — no URL pasting.

## Install (Developer Mode)

1. Pick your backend (set later in the popup's server field):
   - **Default — the live site** `https://mediastrip.jodlx.in` (works out of the box;
     the server downloads the media, then you grab the file from the catalog).
   - **Local — closest to IDM** run `python -m uvicorn main:app --port 8000` and set the
     server to `http://localhost:8000` so files land on your own machine.
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

Integrated, IDM-style — it goes *inside* the page, you never paste a URL:

| Surface | What happens |
|---------|--------------|
| **Floating launcher** (bottom-right) | Appears whenever media is found on the page; the badge shows how many items |
| **Panel** (click the launcher) | Lists everything found: page `<video>`/`<audio>`/`<img>`, `<source>` tags, og:video meta, **and** the HLS/DASH streams the player loaded (network-sniffed). One ⬇ per item |
| Video uses blob:/MSE (YouTube etc.) | "Download whole page (auto-extract)" sends the page URL → server-side yt-dlp pulls the real stream |
| Direct file (mp4/jpg/zip…) | Server downloads with **8 parallel Range connections** (IDM-style) |
| HLS/DASH manifest | Caught at network level even inside iframes/obfuscated players; Referer + cookies auto-attached |
| Toolbar popup | Same catch list + server URL setting + "Download media on this page" |

The page is re-scanned on DOM changes and on `play`, so lazily-loaded players
and single-page-app navigation are picked up automatically.

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
