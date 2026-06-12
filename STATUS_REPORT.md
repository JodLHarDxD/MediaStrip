# MediaStrip — Software Status Report
**Generated:** 2026-06-12  
**Repo:** `d:\side_project\EditerX`  
**Deployed:** `https://mediastrip-jodl.up.railway.app`  
**Built by:** JodLHarDxD (`jodl.hrs03@gmail.com`)

---

## 0. Tech Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| **Language** | Python 3.11 | Backend only |
| **Web Framework** | FastAPI | ASGI, async-native |
| **ASGI Server** | Uvicorn (standard) | Runs on Railway via `${PORT}` env |
| **Frontend** | Vanilla HTML + CSS + JS | No React/Vue — single `index.html` |
| **Streaming** | SSE (Server-Sent Events) | `EventSource` in browser → FastAPI async generator |
| **Video Downloader** | yt-dlp | 1000+ sites, format selector: `bestvideo+bestaudio→mp4`, 8 concurrent fragments |
| **Direct Downloader** | Custom (`segmented.py`) | IDM-style: 8 parallel HTTP Range connections, auto-fallback to single stream |
| **Browser Extension** | Chrome MV3 (`extension/`) | Chrome/Edge/Brave. Media sniffing via observational webRequest + floating download button + cookie forwarding for login-gated streams |
| **Video Processing** | ffmpeg | Audio merge, stream copy (no re-encode), frame output |
| **Frame Analysis** | OpenCV headless | Temporal variance watermark detection |
| **AI Inpainting** | LaMa (`simple-lama-inpainting`) + PyTorch | GPU (CUDA) — local only, not on Railway |
| **CPU Inpainting Fallback** | OpenCV TELEA | Active on Railway (no GPU) |
| **Image I/O** | Pillow | Read/write frames for LaMa pipeline |
| **Anime Resolver** | Custom (`anime_extractor.py`) | hianime/aniwatch → myani.cfd API → megaplay embed → m3u8 |
| **Cloudflare Bypass** | curl_cffi | Browser impersonation for m3u8 CDN streams |
| **Async HTTP** | httpx | Anime resolver API calls |
| **HTML Parsing** | BeautifulSoup4 | Megaplay embed page scraping |
| **Instagram Fallback** | requests + Instagram embed API | Fetches carousel images yt-dlp silently skips |
| **Rate Limiting** | slowapi | 10/min download · 5/min watermark |
| **Containerization** | Docker (multi-stage, python:3.11-slim) | Builder + Runtime stages |
| **Deployment** | Railway | Auto-deploy from Git, `PORT` env injected |
| **Fonts** | Clash Display · DM Sans · JetBrains Mono | CDN-loaded, no local font files |
| **Design** | Glassmorphism · dark luxury (#080C10 bg, #00E5FF accent) | Custom CSS, no UI framework |

---

## 1. What This App Is

**MediaStrip** is a local-first web app with two tools:
1. **Content Downloader** — paste any URL, get best-quality video/audio via yt-dlp
2. **Watermark Remover** — upload video or image, get AI-inpainted clean version

Dark luxury UI aesthetic (glassmorphism, Clash Display font, electric cyan accent).  
Stack: FastAPI (Python) backend + vanilla HTML/CSS/JS frontend. No framework.

---

## 2. File Map

```
main.py              FastAPI server — all routes, SSE streaming, cleanup loop
downloader.py        yt-dlp wrapper — progress parsing, Instagram embed fallback, anime routing
segmented.py         IDM-style multi-connection Range downloader (8 parallel segments)
extension/           Chrome MV3 extension — media sniffer + floating download button + popup
watermark.py         CV2 + LaMa AI inpainting — video frame-by-frame + image processing
anime_extractor.py   Anime module — resolves hianime/aniwatch URLs → m3u8 via myani.cfd API
static/
  index.html         Full UI (single-page, 759-line vanilla JS in app.js)
  style.css          Design system — glassmorphism, dark palette, animations
  app.js             All interactivity — SSE consumption, drag-drop, catalog, toasts
  assets/            atlas.mp4 (hero bg video), favicon SVG
  blog/              2 blog posts (HTML + meta JSON) for SEO
  og-image.png       Social share image
Dockerfile           Multi-stage build → Railway deployment
requirements.txt     Python deps
robots.txt           Crawl rules
sitemap.xml          SEO sitemap
```

---

## 3. Architecture — How It Works

### 3.1 Download Flow

```
User pastes URL → POST /download
  → main.py: validates URL (SSRF guard), creates job_id + asyncio.Queue
  → background task: downloader.download_video()
    → if anime URL: anime_extractor.resolve_stream() → m3u8 → yt-dlp
    → if Instagram /p/: yt-dlp + Instagram embed API fallback for images
    → else: yt-dlp with format selector (bestvideo+bestaudio → mp4)
  → client polls GET /stream/{job_id} (SSE)
    → server pushes: filename / progress % / speed / ETA / log lines
    → on done: serializes artifact list (URL, download_url, poster_url, size)
  → UI renders catalog card with video/image preview
```

### 3.2 Watermark Removal Flow

```
User drops file → POST /remove-watermark (multipart, max 500MB)
  → main.py: saves to uploads/, creates job_id + asyncio.Queue
  → background task: watermark.remove_watermark()
    → image: preset mask → LaMa AI inpainting (GPU) or TELEA fallback (CPU)
    → video:
        1. Sample 30 frames, compute per-pixel temporal std deviation
        2. Low-variance pixels → static overlay → watermark candidate mask
        3. Morphological cleanup (close + dilate, resolution-scaled kernels)
        4. If mask passes sanity check (0.01%-20% coverage) → use it
        5. Else → use platform preset (TikTok/Instagram/YouTube coordinates)
        6. LaMa AI inpainting each frame → temp.mp4
        7. ffmpeg merges original audio into final output
  → SSE stream sends progress, done event with output file artifact
```

### 3.3 Anime Download Flow

```
URL matches hianime/aniwatch/kaido domains → anime_extractor.resolve_stream()
  Step 1: slug extracted from URL
  Step 2: GET myani.cfd/api/episode/{slug} → episode metadata + embed URL
  Step 3: GET megaplay.buzz embed page → extract data-id (player ID)
  Step 4: GET megaplay.buzz/stream/getSources?id={player_id} → m3u8 URL
  Step 5: m3u8 handed to yt-dlp with --extractor-args generic:impersonate
         (bypasses Cloudflare on CDN) + Referer header support
```

### 3.4 SSE / Job State

- Jobs stored in `job_queues: dict[str, asyncio.Queue]` — **in-memory only**
- SSE endpoint sends JSON events: `filename`, `progress`, `log`, `ping`, `done`, `error`
- Queue cleaned up after `done` or `error` event
- 30s timeout per SSE read → sends `ping` to keep connection alive
- **Limitation:** App restart loses all in-flight jobs (no Redis/DB persistence)

### 3.5 File Storage

| Directory | Purpose | Cleanup |
|-----------|---------|---------|
| `downloads/{job_id}/` | yt-dlp output (video + thumbnail) | 24h auto-cleanup |
| `uploads/` | Watermark input files | 24h auto-cleanup |
| `output/{job_id}/` | Watermark processed output | 24h auto-cleanup |

Cleanup runs every hour via `_cleanup_loop()` background task.

---

## 4. API Routes

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/` | Serve index.html |
| POST | `/download` | Start yt-dlp download (rate: 10/min) |
| POST | `/remove-watermark` | Start watermark removal (rate: 5/min) |
| GET | `/stream/{job_id}` | SSE progress stream |
| GET | `/media/{bucket}/{path}` | Serve generated media inline |
| GET | `/media-download/{bucket}/{path}` | Download generated media (with filename header) |
| GET | `/blog/{slug}` | Rendered blog post (SEO) |
| GET | `/favicon.ico` | SVG favicon |
| GET | `/robots.txt` | Crawl rules |
| GET | `/sitemap.xml` | SEO sitemap |
| GET | `/api/extension/ping` | Extension connectivity check |
| POST | `/api/extension/download` | Extension-triggered download — routes to segmented/yt-dlp engine (rate: 20/min) |
| GET | `/anime/resolve` | Resolve anime URL → stream info |
| GET | `/anime/download` | Download anime episode (sync, returns file) |
| GET | `/anime/download/async` | Start anime download, return job_id |
| GET | `/anime/status/{job_id}` | Poll async anime job status |
| GET | `/anime/file/{job_id}` | Serve completed anime file |

---

## 5. Python Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` | Web framework |
| `uvicorn[standard]` | ASGI server |
| `yt-dlp` | Core downloader (1000+ sites) |
| `opencv-python-headless` | Frame processing, temporal variance detection |
| `Pillow` | Image I/O for LaMa |
| `python-multipart` | File upload parsing |
| `requests` | Instagram embed HTTP calls |
| `slowapi` | Rate limiting |
| `httpx` | Async HTTP for anime resolver |
| `beautifulsoup4` | HTML parsing for anime embed pages |
| `curl_cffi` | Cloudflare bypass (yt-dlp impersonation on m3u8 CDNs) |
| `torch` + `simple-lama-inpainting` | **Optional** — GPU AI inpainting (not in requirements.txt, manual install) |

---

## 6. Frontend — How It Works

**Single-page app** (no framework). `index.html` loads once. JS shows/hides sections.

Key JS state:
- `selectedPlatform` — tiktok/instagram/youtube for watermark presets
- `selectedFile` — File object from drag-drop or file picker
- `activeEventSources` — tracks open SSE connections (closes old on new job)

UI sections:
1. **Hero** — fullscreen `atlas.mp4` background video, two CTAs
2. **Download** — URL input → progress log → artifact catalog
3. **Watermark** — drag-drop zone → platform selector → progress → catalog
4. **Result Catalog** — grid of all downloaded/processed files, lazy-loads video thumbnails

---

## 7. Security

| Measure | Implementation |
|---------|---------------|
| SSRF guard | `_validate_download_url()` — blocks localhost, RFC1918, non-http(s) schemes |
| Upload limit | 500MB max enforced in-process before writing to disk |
| Path traversal | `_resolve_media_path()` checks `base in target.parents` before serving |
| Filename sanitization | `_safe_filename()` strips path components, uses basename only |
| Rate limiting | 10/min on download, 5/min on watermark via slowapi |
| Security headers | X-Content-Type-Options, X-Frame-Options, CSP, Referrer-Policy, Permissions-Policy |
| CORS | Allow-all origins (intentional — public tool, no auth) |

---

## 8. Deployment

**Platform:** Railway  
**Container:** Docker multi-stage build (python:3.11-slim)  
**Port:** `${PORT:-8080}` (Railway injects PORT env var)

Build installs:
- System: `ffmpeg`, `libgl1`, `libglib2.0-0`, `libsm6`, `libxext6`
- Python: all requirements.txt deps + `curl_cffi` (installed separately in runtime stage to avoid wheel issues)

**LaMa model NOT installed in Docker** — requirements.txt comments it out. On Railway, watermark falls back to OpenCV TELEA inpainting (CPU). GPU LaMa only works in local dev with CUDA.

---

## 9. Known Gaps / Issues

| # | Issue | Severity |
|---|-------|----------|
| 1 | **LaMa not in Docker** — Railway deployments use TELEA fallback, not GPU AI | Medium |
| 2 | **Anime job state not surfaced in main UI** — `/anime/download` is a separate endpoint not wired to the SSE progress UI | Medium |
| 3 | **In-memory job state** — server restart kills all in-flight jobs, no recovery | Low (Railway rarely restarts mid-job) |
| 4 | **No auth** — rate limits only, anyone can trigger downloads/watermark jobs | Low (public tool, intentional) |
| 5 | **`curl_cffi` separate pip install in Dockerfile** — fragile, should be in requirements.txt | Low |
| 6 | **Anime `_job_results` dict** — grows unbounded (no TTL/eviction), memory leak on long runs | Low |
| 7 | **`verify=False` in anime HTTP calls** — skips SSL cert validation | Low |
| 8 | **CORS allow-all** — `allow_origins=["*"]` lets any site make requests | Info |

---

## 10. What's Working (Confirmed by git history)

- yt-dlp download with progress streaming — working
- Instagram carousel (mixed video+image) — working (embed fallback added)
- Anime hianime/aniwatch URL resolution → m3u8 → yt-dlp — working (Cloudflare bypass via curl_cffi)
- Watermark removal (image + video) with TELEA fallback — working
- Blog posts + SEO metadata — working
- Favicon SVG + sitemap + robots — working
- File cleanup (24h TTL) — working
- Rate limiting — working
- Security headers — working
