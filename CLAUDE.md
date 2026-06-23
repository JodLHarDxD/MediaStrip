# PROJECT: MediaStrip — Content Downloader + Watermark Remover

## Vision
Premium, deployable web app. Dark luxury aesthetic.
Think: tool used by professionals, not a hobbyist script wrapper.
Every interaction should feel deliberate and high-end.

## Design System

### Typography
- Display/headings: "Clash Display" (CDN: fonts.cdnfonts.com/css/clash-display)
- Body/UI: "DM Sans" (Google Fonts)
- Mono (progress/logs): "JetBrains Mono" (Google Fonts)

### Color Palette
- Background: #080C10 (near-black, not pure black)
- Surface: #0E1318
- Surface elevated: #141B22
- Border: #1E2730 (subtle), #2A3540 (hover)
- Accent primary: #00E5FF (electric cyan)
- Accent secondary: #7B61FF (violet)
- Accent glow: rgba(0, 229, 255, 0.15)
- Text primary: #F0F4F8
- Text secondary: #7A8FA6
- Text muted: #3D5166
- Success: #00D97E
- Error: #FF4D6A
- Warning: #FFB830

### Design Language
- Glassmorphism panels: backdrop-filter blur + subtle border
- Thin glowing borders on active elements (1px cyan glow)
- Subtle grain texture overlay on backgrounds (SVG noise filter)
- Animated gradient mesh background (slow-moving, dark)
- Custom scrollbar (thin, cyan accent)
- Smooth transitions everywhere (cubic-bezier, 300ms)
- Progress bars with animated shimmer
- Micro-interactions on all interactive elements

### Layout
- Full viewport height app shell
- Left sidebar: branding + nav (240px)
- Right: main content area
- Mobile: sidebar collapses to top nav bar

## UI Structure

### Sidebar
- Logo: "MediaStrip" in Clash Display, cyan accent on "Strip"
- Subtitle: "Content Tools" in muted text
- Nav items: Download, Watermark, History, Settings
- Active state: cyan left border + subtle background glow
- Bottom: GPU status indicator (green dot + "GPU Active")

### Page: Download
- Hero heading: "Download Anything." (large, Clash Display)
- Subtext: "YouTube · Instagram · TikTok · Twitter · 1000+ sites"
- URL input: large, full-width, glassmorphism — placeholder "Paste URL here..."
  - On focus: cyan border glow animation
  - Paste button on right side of input
- Output folder row: folder icon + path display + "Browse" button
- Quality badge row: auto-selected "4K / Best Available" badge (cyan pill)
- Download button: full-width, gradient (cyan → violet), with arrow icon
  - Hover: slight scale up + glow intensifies
- Progress section (appears after click):
  - Filename detected (fade in)
  - Animated progress bar with shimmer
  - Live log output in JetBrains Mono (scrollable, dark terminal aesthetic)
  - Speed + ETA stats row
  - Success state: green checkmark + file size + "Open Folder" button

### Page: Watermark Remover
- Heading: "Remove Watermarks."
- Drag-drop zone: dashed border (animated dash stroke), centered icon
  - States: idle / hover (cyan border) / file loaded (show thumbnail + filename)
- Platform selector: three cards side by side
  - TikTok card, Instagram card, YouTube card
  - Each with platform icon + name + "Known regions pre-mapped"
  - Selected state: cyan border + subtle background
- Process button: same style as download button
- Progress + output same pattern as Download page

### Shared Components
- Toast notifications (top-right, slide in)
- Loading skeleton states
- Error states with red accent
- All buttons: no square corners — border-radius 10px minimum
- All inputs: dark surface, subtle border, cyan focus ring

## Stack
- Backend: FastAPI (Python)
- Frontend: Vanilla HTML + CSS + JS (single index.html + style.css + app.js)
- NO framework — keeps it deployable anywhere
- Downloader: yt-dlp
- Video processing: OpenCV + ffmpeg
- Watermark removal: LaMa AI inpainting (GPU) with temporal variance detection; TELEA fallback

## Features

### Content Downloader
- Input: URL (YouTube, Instagram, TikTok, Twitter/X, Facebook + yt-dlp supported)
- Quality: Always best available (4K where source has it)
- Format: No re-encoding — stream copy (zero quality loss)
- Audio: Best available, merged via ffmpeg
- Output: Local folder (user-configurable, default: ./downloads/)
- Progress: SSE stream from FastAPI → live UI updates

### Watermark Remover
- Input: Drag-drop video or image upload
- Platform: TikTok / Instagram / YouTube selector (used as fallback for images; videos use auto-detection)
- Method (video): Temporal variance detection — samples 30 frames, finds static pixels (low std dev = watermark), builds mask; LaMa AI inpainting fills region
- Method (image): Preset coordinates → LaMa AI inpainting
- Fallback: OpenCV TELEA inpainting if torch/LaMa not installed
- Detection: No platform input needed for video — watermarks detected automatically from the content
- Output: Saved to ./output/ folder
- Progress: SSE stream from FastAPI → live UI updates

## File Structure
project/
├── main.py
├── downloader.py
├── watermark.py
├── static/
│   ├── index.html
│   ├── style.css
│   └── app.js
├── requirements.txt
└── README.md

## API Routes
- GET  /              → serve index.html
- POST /download      → start yt-dlp download, return SSE stream
- POST /remove-watermark → upload video + platform, return SSE progress
- GET  /stream/{job_id} → SSE endpoint for live progress

## Run
uvicorn main:app --reload --port 8000

## Build Order for Claude Code
1. main.py — FastAPI shell + all routes + SSE infrastructure
2. downloader.py — yt-dlp wrapper with progress parsing
3. watermark.py — temporal detection + LaMa inpainting (GPU); preset fallback
4. static/style.css — full design system (dark luxury, glassmorphism)
5. static/index.html — full UI shell with sidebar + both pages
6. static/app.js — all interactivity, SSE consumption, drag-drop

## Critical Rules
- No re-encoding on download (ffmpeg -c copy always)
- No browser file download — local disk only
- Watermark removal uses LaMa AI inpainting (GPU via CUDA) with TELEA fallback
- No React/Vue/frameworks — vanilla only
- GPU available — use where applicable
- Do NOT ask clarifying questions — execute the spec fully
- UI must be production-grade, deployable, premium aesthetic


## Hero Video
- File: atlas.mp4 (place in static/assets/atlas.mp4)
- Used as: fullscreen background video in the hero/landing section
  before the user navigates to Download or Watermark pages
- Implementation:
  - <video autoplay muted loop playsinline> tag
  - object-fit: cover, position absolute, full viewport
  - Dark overlay on top: linear-gradient(rgba(8,12,16,0.55), rgba(8,12,16,0.85))
  - Hero text renders on top of overlay:
    - "MediaStrip" in Clash Display, 80px, white
    - Tagline: "Download. Clean. Keep." in DM Sans, muted
    - Two CTA buttons: "Start Downloading" + "Remove Watermark"
    - Buttons scroll/navigate to their respective tool sections
  - Video loads with fade-in on page ready
  - On mobile: video paused, poster frame shown instead (performance)

## Watermark Remover — File Input Update
- Accept BOTH video and image files
- Supported video: .mp4, .mov, .avi, .mkv, .webm
- Supported image: .jpg, .jpeg, .png, .webp, .tiff
- Drag-drop zone shows accepted formats listed below the icon
- After file drop: detect type (video/image) and show appropriate preview
  - Video: show <video> thumbnail with play icon overlay + filename + duration
  - Image: show <img> preview thumbnail + filename + dimensions
- Backend watermark.py handles both:
  - Video: temporal variance detection → LaMa AI inpainting per frame → ffmpeg audio merge
  - Image: preset coordinates → LaMa AI inpainting
- Output filename: original_name_clean.ext (same extension)
- Platform presets (TikTok/Instagram/YouTube) apply to both file types
  since watermark positions are consistent across video and stills

## SEO & Discoverability (standing mission)
Goal: rank page-1 on Google for brand + winnable long-tail. Owner brand = **JodLx Studio**,
product = **MediaStrip**, founder = Hriddhish Ranjan Sarkar.

### Realistic target tiers (do not waste effort above your weight)
- **Brand (win fast):** jodl, jodlx, jodlx studio, mediastrip
- **Long-tail (win with content+links, months):** "on-page video download button extension",
  "download video without uploading to a server", "local GPU watermark remover",
  "remove tiktok/instagram watermark online free", "browser extension media downloader"
- **Head terms (DO NOT chase):** "video downloader", "media downloader", "online downloader" —
  owned by y2mate/savefrom/snaptube-class domains; unwinnable short-term.

### Positioning rule (penalty avoidance) — IMPORTANT
Google actively demotes generic "downloader" sites and bans them from AdSense. Lead public
copy, titles, and schema with **"watermark remover" + "creator media toolkit" + "local-first /
GPU"**, NOT "video downloader." Downloader is a feature, not the headline. This protects the
whole domain from being buried.

### On-page invariants (keep true on every change)
- Real, crawlable `<h1>` text in the HTML (never JS-only). Hero uses a permanent `.sr-only`
  keyword line inside the H1; decorative wordmark is `aria-hidden`.
- JSON-LD kept valid + in sync: `SoftwareApplication` (author/publisher = JodLx Studio Org),
  `Organization` (full `sameAs` of all real profiles), `FAQPage`, blog `BlogPosting` +
  `BreadcrumbList`. Validate parse after any schema edit.
- All real social/contact links only — never placeholder URLs (instagram.com, x.com) or
  off-domain fake emails. Canonical + OG + Twitter tags present and accurate.
- Every `<img>` has descriptive, keyword-aware `alt`. Headings carry intent keywords
  (not "Under the hood" — say what it does).
- `robots` meta = `index,follow,max-image-preview:large,max-snippet:-1`. Routes answer HEAD.
- Bump `style.css?v=N` whenever CSS changes so cached clients get it.
- sitemap.xml lists every public URL with fresh `lastmod`; robots.txt references it.

### Off-page (human-owned, the real lever — surface as TODOs, can't be coded)
Backlinks + time decide competitive rank: Product Hunt, Reddit (r/DataHoarder, r/software),
Hacker News, AlternativeTo/Slant/SaaSHub directories, dev.to/Medium posts, Chrome Web Store
listing (the listing itself ranks). Reciprocal: site URL in every social bio.

### Workflow
Audit pixels/serps with real fetches, not assumptions. After SEO edits: validate JSON-LD,
re-render to confirm no visible regressions, commit, push (Railway auto-deploys), then advise
GSC "Request indexing" for changed URLs.