# MediaStrip Catcher — Browser Extension

IDM-style media catcher for Chrome / Edge / Brave. A **Download button appears on
the video itself** (top-right corner of the player, on hover) — exactly like IDM.
Click it → a small panel anchored to the video shows the actual downloadable
source(s) → pick one → live progress bar right there in the page. Session cookies
ride along for login-gated streams. No URL pasting, no screen-corner clutter,
no junk thumbnails.

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

| Surface | What happens |
|---------|--------------|
| **⬇ Download pill on the video** | Pinned to the top-right corner of every real player (≥240×120 — preview thumbs and ad slivers are skipped). Appears on hover; ✕ hides it for that video |
| **Source panel** (click the pill) | Anchored to the video. Shows what would actually be downloaded: the direct file, the sniffed HLS/DASH stream(s) the player loaded, or full-quality page extraction. Pick → Download |
| **Live progress** | Progress bar + speed + ETA inside the panel (server's SSE stream relayed through the extension); "Saved ✓" when done, link to the file in MediaStrip |
| YouTube | One option only: full video at best quality via the server extractor — no thumbnail junk, no partial chunks |
| Embedded players (movie sites, iframes) | Content script runs in every frame — the button appears on iframe videos too; sniffed streams are matched in |
| Direct file (mp4 …) | Server downloads with **8 parallel Range connections** (IDM-style) |
| Toolbar popup | Backup catch list + server URL setting + "Download media on this page" |

The page is re-scanned on DOM changes and on `play`, so lazily-loaded players
and single-page-app navigation are picked up automatically. Images are
deliberately ignored — the catcher targets the actual media, not page chrome.

## Settings

Popup → server URL field. Default `https://mediastrip.jodlx.in` (the live site).
Point it at `http://localhost:8000` to download straight to your own machine.

## Login-gated streams (cookies)

Some sites only serve the video to a logged-in session. The extension reads the
cookies **for that media domain only** (via `chrome.cookies.getAll`) and forwards
them with the download request, so the server fetches the stream exactly as your
browser would. Cookies are scoped to the target domain — no blanket cookie dump.

> **Privacy:** cookies are sent to whatever server URL is set in the popup
> (default `https://mediastrip.jodlx.in`). Only point the extension at
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
- `scripting` — re-inject the catcher into already-open tabs after the extension
  is installed, updated, or reloaded (otherwise those tabs need a manual refresh)

## Troubleshooting

**"Extension context invalidated" in the console / dead ⬇ buttons** — fixed in
v1.3.0. This happened when the extension was reloaded while pages were open: the
old in-page script kept running against a dead extension. Now the old script
silently removes itself and a fresh catcher is auto-injected into every open tab.

**Dev loop:** `python test_extension.py` (needs `pip install playwright` +
`playwright install chromium`) loads the extension in a scratch Chromium profile
and verifies sniffing, the panel, double-injection guarding, and silent teardown.
