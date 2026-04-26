# MediaStrip

**Download anything. Remove watermarks. Keep quality.**

A premium, self-hostable web app built for content creators — powered by yt-dlp, OpenCV, and LaMa AI inpainting. Clean dark UI, zero dependencies on third-party services.

Live → [mediastrip-jodl.up.railway.app](https://mediastrip-jodl.up.railway.app)

---

## Features

### Content Downloader
- Paste any URL — YouTube, Instagram, TikTok, Twitter/X, Facebook, and 1000+ sites via yt-dlp
- Always downloads best available quality (4K where the source has it)
- Zero re-encoding — stream copy via ffmpeg (no quality loss)
- Live progress stream with speed, ETA, and filename detection

### Watermark Remover
- Upload video or image (MP4, MOV, AVI, MKV, WEBM, JPG, PNG, WEBP)
- Auto-detects watermark region via temporal variance analysis across sampled frames
- AI inpainting via [LaMa](https://github.com/advimman/lama) (GPU) with OpenCV TELEA as CPU fallback
- Platform presets for TikTok, Instagram, YouTube when auto-detection is inconclusive
- Audio track preserved on video output

---

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI + uvicorn |
| Downloader | yt-dlp |
| Video processing | OpenCV + ffmpeg |
| AI inpainting | LaMa (`simple-lama-inpainting`) + CUDA |
| Frontend | Vanilla HTML / CSS / JS |
| Deployment | Docker + Railway |

---

## Running Locally

### Prerequisites
- Python 3.11+
- ffmpeg installed and on `PATH`
- (Optional) CUDA-capable GPU for LaMa AI inpainting

### Setup

```bash
git clone https://github.com/JodLHarDxD/MediaStrip.git
cd MediaStrip
pip install -r requirements.txt
```

For GPU watermark removal (optional):
```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
pip install simple-lama-inpainting
```

### Run

```bash
uvicorn main:app --reload --port 8080
```

Open `http://localhost:8080`

---

## Docker

```bash
docker build -t mediastrip .
docker run -p 8080:8080 mediastrip
```

---

## Deploying to Railway

The repo includes `railway.toml` — Railway will auto-detect and use the Dockerfile.

1. Fork or push this repo to GitHub
2. Create a new Railway project → **Deploy from GitHub repo**
3. Railway builds the Docker image and assigns a public URL
4. Make sure the Railway networking port matches `PORT` (default: `8080`)

No environment variables are required beyond `PORT`.

---

## API

| Method | Route | Description |
|---|---|---|
| `GET` | `/` | Serves the web UI |
| `POST` | `/download` | Starts a yt-dlp download job, returns `job_id` |
| `POST` | `/remove-watermark` | Uploads a file, starts watermark removal, returns `job_id` |
| `GET` | `/stream/{job_id}` | SSE stream — live progress events for a job |
| `GET` | `/media/{bucket}/{path}` | Serves a processed file |
| `GET` | `/media-download/{bucket}/{path}` | Forces download with filename |

Progress events are [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events) with JSON payloads: `log`, `progress`, `filename`, `done`, `error`, `ping`.

---

## Project Structure

```
MediaStrip/
├── main.py           # FastAPI app + all routes + SSE infrastructure
├── downloader.py     # yt-dlp wrapper with live progress parsing
├── watermark.py      # Temporal detection + LaMa inpainting (GPU/CPU fallback)
├── requirements.txt
├── Dockerfile
├── railway.toml
└── static/
    ├── index.html
    ├── style.css
    ├── app.js
    └── assets/
```

---

## Notes

- Downloaded and processed files are stored locally under `downloads/`, `output/`, and `uploads/`. Files older than 24 hours are automatically cleaned up.
- LaMa AI inpainting loads on first use (lazy singleton) — first watermark job will be slower while the model initializes.
- No database, no auth, no external services required.
