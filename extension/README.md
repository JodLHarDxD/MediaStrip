# MediaStrip Catcher — Browser Extension

IDM-style media catcher for Chrome/Edge/Brave. Shows a floating **MediaStrip** download
button over any video or large image, sniffs streaming manifests (m3u8/DASH) from
network traffic, and sends everything to your local MediaStrip server.

## Install (Developer Mode)

1. Start the MediaStrip server:
   ```
   python -m uvicorn main:app --port 8000
   ```
2. Open `chrome://extensions` (or `edge://extensions`)
3. Enable **Developer mode** (top-right toggle)
4. Click **Load unpacked** → select this `extension/` folder
5. Pin "MediaStrip Catcher" to the toolbar

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

## Permissions explained

- `webRequest` + `<all_urls>` — observe (not block) responses to sniff media content-types
- `storage` — remember server URL; per-tab catch lists live in session storage
- `tabs` — read active tab URL for page-level extraction
