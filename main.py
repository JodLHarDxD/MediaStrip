import asyncio
import json
import mimetypes
import os
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import quote, urlparse

mimetypes.add_type("image/svg+xml", ".svg")

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from downloader import download_video
from segmented import download_direct
from watermark import remove_watermark

try:
    from anime_extractor import create_router as _create_anime_router
    _ANIME_ROUTER: object = _create_anime_router()
except Exception:
    _ANIME_ROUTER = None

# ── Rate limiter ──────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)

# ── Upload limits ─────────────────────────────────────────────────────────────
MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB

# ── Allowed URL schemes for download ─────────────────────────────────────────
ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def _validate_download_url(url: str) -> None:
    """Block SSRF: reject non-HTTP schemes and loopback/internal hosts."""
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(400, "Invalid URL")
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise HTTPException(400, "Only http/https URLs are allowed")
    host = (parsed.hostname or "").lower()
    if host in BLOCKED_HOSTS or host.startswith("169.254.") or host.startswith("10.") or host.startswith("192.168."):
        raise HTTPException(400, "URL not allowed")


def _safe_filename(raw: str) -> str:
    """Return only the basename, stripping any path components."""
    return Path(raw).name or "upload"


# ── Security headers middleware ───────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.cdnfonts.com https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.cdnfonts.com https://fonts.gstatic.com; "
            "media-src 'self' blob:; "
            "img-src 'self' data: blob:; "
            "connect-src 'self';"
        )
        return response

CLEANUP_MAX_AGE_SECONDS = 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 60 * 60


def _cleanup_old_files():
    now = time.time()
    cutoff = now - CLEANUP_MAX_AGE_SECONDS
    for root_dir in [DOWNLOADS_DIR, OUTPUT_DIR, UPLOADS_DIR]:
        for item in root_dir.iterdir():
            try:
                if item.stat().st_mtime < cutoff:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
            except OSError:
                pass


# Hosted containers (Railway) count files written to the ephemeral disk
# against the MEMORY limit (cgroup page cache) — finished downloads parked in
# downloads/ pin the container at its ceiling. Evict oldest first over the cap.
STORAGE_CAP_MB = int(os.environ.get("MS_STORAGE_CAP_MB", "400"))


def _evict_to_cap(cap_bytes: int | None = None):
    cap = STORAGE_CAP_MB * 1024 * 1024 if cap_bytes is None else cap_bytes
    entries: list[tuple[float, int, Path]] = []
    for root_dir in [DOWNLOADS_DIR, OUTPUT_DIR, UPLOADS_DIR]:
        if not root_dir.exists():
            continue
        for item in root_dir.iterdir():
            try:
                size = (
                    sum(p.stat().st_size for p in item.rglob("*") if p.is_file())
                    if item.is_dir()
                    else item.stat().st_size
                )
                entries.append((item.stat().st_mtime, size, item))
            except OSError:
                pass

    total = sum(size for _, size, _ in entries)
    if total <= cap:
        return

    running = {jid for jid, ch in job_queues.items() if ch.finished_at is None}
    for _, size, item in sorted(entries):
        if total <= cap:
            break
        if item.name in running or any(item.name.startswith(jid) for jid in running):
            continue  # never evict a job still downloading/processing
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            total -= size
        except OSError:
            pass


def _schedule_eviction():
    try:
        asyncio.get_running_loop()
        asyncio.create_task(asyncio.to_thread(_evict_to_cap))
    except RuntimeError:
        _evict_to_cap()


async def _cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        await asyncio.to_thread(_cleanup_old_files)
        await asyncio.to_thread(_evict_to_cap)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(_evict_to_cap)  # boot under the cap
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="MediaStrip", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DOWNLOADS_DIR = BASE_DIR / "downloads"
OUTPUT_DIR = BASE_DIR / "output"
UPLOADS_DIR = BASE_DIR / "uploads"
MEDIA_BUCKETS = {
    "downloads": DOWNLOADS_DIR,
    "output": OUTPUT_DIR,
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}

for d in [DOWNLOADS_DIR, OUTPUT_DIR, UPLOADS_DIR]:
    d.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if _ANIME_ROUTER is not None:
    app.include_router(_ANIME_ROUTER, prefix="/anime")

class JobChannel:
    """Broadcast channel for one job's progress events.

    Drop-in for the asyncio.Queue producers already use (put / put_nowait),
    but every subscriber receives every event — a bare Queue splits events
    randomly between concurrent /stream readers (extension panel + web UI
    watching the same job), desyncing progress and starving one of them of
    the final 'done'. Late subscribers get the full history replayed.
    """

    MAX_HISTORY = 800

    def __init__(self):
        self.history: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.finished_at: float | None = None
        self.proc = None  # subprocess handle, registered by the downloader
        self.cancelled = False
        self.pending_part: Path | None = None  # chunked delivery: part awaiting pickup
        self._resume = asyncio.Event()

    async def put(self, event: dict):
        self.put_nowait(event)

    def put_nowait(self, event: dict):
        self.history.append(event)
        if len(self.history) > self.MAX_HISTORY:
            # drop the oldest log/progress noise; keep structural events
            for i, ev in enumerate(self.history):
                if ev.get("type") in ("log", "progress"):
                    del self.history[i]
                    break
            else:
                del self.history[0]
        if event.get("type") in ("done", "error"):
            self.finished_at = time.time()
        for q in list(self.subscribers):
            q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for ev in self.history:
            q.put_nowait(ev)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.subscribers.discard(q)

    def register_proc(self, proc):
        self.proc = proc
        if self.cancelled:  # cancel arrived before the process spawned
            self._kill()

    async def wait_resume(self) -> bool:
        """Chunked delivery: block until the user confirms the part was picked
        up (resume) or cancels. Returns False on cancel."""
        self._resume.clear()
        await self._resume.wait()
        return not self.cancelled

    def resume(self):
        self._resume.set()

    def cancel(self):
        self.cancelled = True  # cooperative flag for in-process downloads
        self._resume.set()  # unblock a part-wait so the job can exit
        self._kill()

    def _kill(self):
        p = self.proc
        if p is None or p.poll() is not None:
            return
        try:
            p.terminate()
        except OSError:
            pass


job_queues: dict[str, JobChannel] = {}
JOB_CHANNEL_TTL = 3600  # keep finished jobs around for late viewers / reloads


def _new_job_channel(job_id: str) -> JobChannel:
    now = time.time()
    for jid, ch in list(job_queues.items()):
        if ch.finished_at and now - ch.finished_at > JOB_CHANNEL_TTL:
            job_queues.pop(jid, None)
    _schedule_eviction()  # free disk/memory budget before the new job lands
    channel = JobChannel()
    job_queues[job_id] = channel
    return channel


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "assets" / "mediastrip-favicon.svg", media_type="image/svg+xml")


@app.get("/robots.txt", include_in_schema=False)
async def robots():
    return FileResponse(BASE_DIR / "robots.txt", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap():
    return FileResponse(BASE_DIR / "sitemap.xml", media_type="application/xml")


SITE_URL = "https://mediastrip.jodlx.in"
BLOG_DIR = STATIC_DIR / "blog"


def _render_blog_post(slug: str) -> str:
    article_path = BLOG_DIR / f"{slug}.html"
    meta_path = BLOG_DIR / f"{slug}.meta.json"
    if not article_path.is_file() or not meta_path.is_file():
        raise HTTPException(404, "Post not found")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    article_html = article_path.read_text(encoding="utf-8")
    canonical = f"{SITE_URL}/blog/{slug}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{meta['seo_title']}</title>
<meta name="description" content="{meta['meta_description']}">
<link rel="canonical" href="{canonical}">

<meta property="og:title" content="{meta['seo_title']}">
<meta property="og:description" content="{meta['meta_description']}">
<meta property="og:url" content="{canonical}">
<meta property="og:type" content="article">
<meta property="og:image" content="{SITE_URL}/static/og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:site_name" content="MediaStrip">

<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{meta['seo_title']}">
<meta name="twitter:description" content="{meta['meta_description']}">
<meta name="twitter:image" content="{SITE_URL}/static/og-image.png">

<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": "{meta['seo_title']}",
  "description": "{meta['meta_description']}",
  "url": "{canonical}",
  "image": "{SITE_URL}/static/og-image.png",
  "wordCount": {meta['word_count']},
  "keywords": "{meta['primary_keyword']}",
  "mainEntityOfPage": {{"@type": "WebPage", "@id": "{canonical}"}},
  "publisher": {{"@type": "Organization", "name": "MediaStrip", "url": "{SITE_URL}/"}}
}}
</script>

<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,300;9..40,400;9..40,500;9..40,600&family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500&display=swap" rel="stylesheet">
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<style>
:root {{ --bg: #05030b; --ink: #e9e2f5; --muted: #8a7fa8; --accent: #d4b8ff; --rule: rgba(212,184,255,0.14); }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: var(--bg); color: var(--ink); font-family: "DM Sans", system-ui, sans-serif; line-height: 1.7; -webkit-font-smoothing: antialiased; }}
.blog-nav {{ max-width: 760px; margin: 0 auto; padding: 32px 24px 0; display: flex; justify-content: space-between; align-items: center; }}
.blog-nav a {{ color: var(--muted); text-decoration: none; font-size: 14px; letter-spacing: 0.04em; text-transform: uppercase; transition: color 0.2s; }}
.blog-nav a:hover {{ color: var(--accent); }}
.blog-nav .brand {{ color: var(--ink); font-weight: 500; font-size: 16px; text-transform: none; letter-spacing: 0; }}
.blog-nav .brand span {{ color: var(--accent); }}
.blog-post {{ max-width: 720px; margin: 0 auto; padding: 48px 24px 96px; }}
.blog-post-header {{ border-bottom: 1px solid var(--rule); padding-bottom: 32px; margin-bottom: 40px; }}
.blog-post h1 {{ font-family: "Fraunces", Georgia, serif; font-weight: 500; font-size: clamp(32px, 5vw, 48px); line-height: 1.15; margin: 0 0 16px; letter-spacing: -0.01em; }}
.blog-post-meta {{ color: var(--muted); font-size: 14px; margin: 0; letter-spacing: 0.03em; }}
.blog-post-body h2 {{ font-family: "Fraunces", Georgia, serif; font-weight: 500; font-size: 26px; margin: 48px 0 16px; color: var(--ink); letter-spacing: -0.005em; }}
.blog-post-body h3 {{ font-size: 19px; font-weight: 600; margin: 32px 0 12px; color: var(--ink); }}
.blog-post-body p {{ margin: 0 0 20px; font-size: 17px; color: var(--ink); }}
.blog-post-body ul, .blog-post-body ol {{ margin: 0 0 24px; padding-left: 24px; }}
.blog-post-body li {{ margin-bottom: 8px; font-size: 17px; }}
.blog-post-body a {{ color: var(--accent); text-decoration: none; border-bottom: 1px solid rgba(212,184,255,0.35); transition: border-color 0.2s; }}
.blog-post-body a:hover {{ border-bottom-color: var(--accent); }}
.blog-post-body code {{ background: rgba(212,184,255,0.08); padding: 2px 6px; border-radius: 4px; font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 0.92em; color: var(--accent); }}
.blog-post-body strong {{ color: var(--ink); font-weight: 600; }}
.blog-footer {{ max-width: 720px; margin: 0 auto; padding: 32px 24px 48px; border-top: 1px solid var(--rule); text-align: center; color: var(--muted); font-size: 14px; }}
.blog-footer a {{ color: var(--accent); text-decoration: none; }}
</style>
</head>
<body>
<nav class="blog-nav">
  <a href="/" class="brand">Media<span>Strip</span></a>
  <a href="/">← Back to app</a>
</nav>
{article_html}
<footer class="blog-footer">
  <p>Try <a href="/">MediaStrip</a> — local-first GPU watermark removal and 4K media downloading.</p>
</footer>
</body>
</html>"""


@app.get("/blog/{slug}")
async def serve_blog_post(slug: str):
    if not slug.replace("-", "").isalnum():
        raise HTTPException(404, "Post not found")
    return HTMLResponse(_render_blog_post(slug))


@app.api_route("/", methods=["GET", "HEAD"])
async def serve_index():
    # HEAD must answer 200 (not 405) — crawlers and link validators probe with HEAD
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


class DownloadRequest(BaseModel):
    url: str


def _resolve_bucket_base(bucket: str) -> Path:
    base = MEDIA_BUCKETS.get(bucket)
    if base is None:
        raise HTTPException(404, "Unknown media bucket")
    return base.resolve()


def _resolve_media_path(bucket: str, file_path: str) -> Path:
    base = _resolve_bucket_base(bucket)
    target = (base / file_path).resolve()
    if target != base and base not in target.parents:
        raise HTTPException(404, "File not found")
    if not target.is_file():
        raise HTTPException(404, "File not found")
    return target


def _artifact_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return "file"


def _artifact_sort_key(path: Path) -> tuple[str, int, str]:
    kind_order = {"video": 0, "image": 1, "file": 2}
    kind = _artifact_kind(path)
    return (path.stem.lower(), kind_order[kind], path.name.lower())


def _build_public_url(bucket: str, relative_path: Path, endpoint: str) -> str:
    return f"/{endpoint}/{bucket}/{quote(relative_path.as_posix(), safe='/')}"


def _artifact_from_path(path: Path) -> dict:
    resolved = path.resolve()
    for bucket, base in MEDIA_BUCKETS.items():
        base_resolved = base.resolve()
        if resolved == base_resolved or base_resolved in resolved.parents:
            relative = resolved.relative_to(base_resolved)
            kind = _artifact_kind(resolved)
            poster_url = None

            if kind == "video":
                poster_path = resolved.with_suffix(".jpg")
                if poster_path != resolved and poster_path.is_file():
                    poster_relative = poster_path.relative_to(base_resolved)
                    poster_url = _build_public_url(bucket, poster_relative, "media")

            return {
                "name": resolved.name,
                "kind": kind,
                "url": _build_public_url(bucket, relative, "media"),
                "download_url": _build_public_url(bucket, relative, "media-download"),
                "poster_url": poster_url,
                "size_bytes": resolved.stat().st_size,
            }
    raise ValueError(f"Unsupported media path: {path}")


def _serialize_artifacts(paths: list[str | Path]) -> list[dict]:
    unique_paths: list[Path] = []
    seen: set[str] = set()

    for raw_path in paths:
        if not raw_path:
            continue
        candidate = Path(raw_path).resolve()
        if not candidate.is_file():
            continue
        candidate_key = str(candidate)
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        unique_paths.append(candidate)

    return [_artifact_from_path(path) for path in sorted(unique_paths, key=_artifact_sort_key)]


@app.post("/download")
@limiter.limit("10/minute")
async def start_download(request: Request, body: DownloadRequest, background_tasks: BackgroundTasks):
    _validate_download_url(body.url)
    job_id = str(uuid.uuid4())
    _new_job_channel(job_id)
    output_folder = DOWNLOADS_DIR / job_id
    output_folder.mkdir(parents=True, exist_ok=True)
    background_tasks.add_task(download_video, body.url, output_folder, job_queues[job_id])
    return {"job_id": job_id}


# ── Browser extension API ─────────────────────────────────────────────────────
DIRECT_MEDIA_EXTS = {
    ".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".gif",
    ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav",
    ".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp",
    ".zip", ".pdf",
}
MANIFEST_EXTS = {".m3u8", ".mpd"}


class ExtensionDownloadRequest(BaseModel):
    url: str
    page_url: str | None = None
    kind: str = "auto"  # "direct" | "manifest" | "page" | "auto"
    filename: str | None = None
    # Cookies for login-gated streams: structured dicts (extension >= 1.3.1,
    # carries domain/path so yt-dlp gets a real cookie jar) or a legacy
    # Cookie-header string (older extensions).
    cookies: str | list[dict] | None = None


def _cookie_header(cookies: str | list[dict] | None) -> str | None:
    """Flatten cookies to a Cookie-header string for the direct HTTP downloader."""
    if not cookies:
        return None
    if isinstance(cookies, str):
        return cookies
    pairs = [f"{c['name']}={c.get('value', '')}" for c in cookies if c.get("name")]
    return "; ".join(pairs) or None


def _classify_extension_url(url: str, kind: str) -> str:
    if kind in {"direct", "manifest", "page"}:
        return kind
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in MANIFEST_EXTS:
        return "manifest"
    if ext in DIRECT_MEDIA_EXTS:
        return "direct"
    return "page"


@app.get("/api/extension/ping")
async def extension_ping():
    return {"ok": True, "app": "mediastrip"}


@app.post("/api/extension/download")
@limiter.limit("20/minute")
async def extension_download(
    request: Request, body: ExtensionDownloadRequest, background_tasks: BackgroundTasks
):
    _validate_download_url(body.url)
    kind = _classify_extension_url(body.url, body.kind)

    job_id = str(uuid.uuid4())
    _new_job_channel(job_id)
    output_folder = DOWNLOADS_DIR / job_id
    output_folder.mkdir(parents=True, exist_ok=True)

    if kind == "direct":
        background_tasks.add_task(
            download_direct,
            body.url,
            output_folder,
            job_queues[job_id],
            referer=body.page_url,
            filename_hint=body.filename,
            cookies=_cookie_header(body.cookies),
        )
    elif kind == "manifest":
        background_tasks.add_task(
            download_video,
            body.url,
            output_folder,
            job_queues[job_id],
            referer=body.page_url,
            cookies=body.cookies,
        )
    else:
        background_tasks.add_task(
            download_video,
            body.url,
            output_folder,
            job_queues[job_id],
            cookies=body.cookies,
            # the user clicked ONE video — never let a page that extracts to a
            # feed/playlist fan out into a mass download
            single_item=True,
        )

    return {"job_id": job_id, "kind": kind, "watch_url": f"/?job={job_id}"}


@app.post("/remove-watermark")
@limiter.limit("5/minute")
async def start_watermark_removal(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    platform: str = Form("tiktok"),
):
    job_id = str(uuid.uuid4())
    _new_job_channel(job_id)

    safe_name = _safe_filename(file.filename or "upload")
    upload_path = UPLOADS_DIR / f"{job_id}_{safe_name}"

    # Stream to disk in chunks — reading the whole upload into RAM (up to
    # 500 MB) blows the container's memory ceiling
    size = 0
    with open(upload_path, "wb") as fh:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                fh.close()
                upload_path.unlink(missing_ok=True)
                raise HTTPException(413, f"File too large — maximum is {MAX_UPLOAD_BYTES // (1024*1024)} MB")
            fh.write(chunk)

    output_dir = OUTPUT_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    output_path = output_dir / f"{stem}_clean{suffix}"
    background_tasks.add_task(remove_watermark, upload_path, output_path, platform, job_queues[job_id])
    return {"job_id": job_id}


@app.get("/media/{bucket}/{file_path:path}")
async def serve_generated_media(bucket: str, file_path: str):
    return FileResponse(_resolve_media_path(bucket, file_path))


@app.get("/media-download/{bucket}/{file_path:path}")
async def download_generated_media(bucket: str, file_path: str):
    target = _resolve_media_path(bucket, file_path)
    return FileResponse(target, filename=target.name)


@app.get("/api/catalog")
async def catalog():
    """Everything already downloaded/processed on this server — lets a fresh
    page visit show the catalog instead of only live-job results."""
    groups: list[dict] = []

    if DOWNLOADS_DIR.exists():
        loose: list[Path] = []
        for entry in DOWNLOADS_DIR.iterdir():
            if entry.is_dir():
                files = [p for p in entry.rglob("*") if p.is_file()]
                artifacts = _serialize_artifacts(files)
                if artifacts:
                    groups.append({"source": "download", "ts": entry.stat().st_mtime, "artifacts": artifacts})
            elif entry.is_file():
                loose.append(entry)
        artifacts = _serialize_artifacts(loose)
        if artifacts:
            groups.append({"source": "download", "ts": max(p.stat().st_mtime for p in loose), "artifacts": artifacts})

    if OUTPUT_DIR.exists():
        out_files = [p for p in OUTPUT_DIR.iterdir() if p.is_file()]
        artifacts = _serialize_artifacts(out_files)
        if artifacts:
            groups.append({"source": "output", "ts": max(p.stat().st_mtime for p in out_files), "artifacts": artifacts})

    groups.sort(key=lambda g: g["ts"])  # oldest first — the client prepends
    return {"groups": groups[-50:]}


@app.post("/api/job/{job_id}/cancel")
@limiter.limit("30/minute")
async def cancel_job(request: Request, job_id: str):
    channel = job_queues.get(job_id)
    if channel is None:
        raise HTTPException(404, "Job not found")
    if channel.finished_at:
        return {"ok": False, "status": "already finished"}
    channel.cancel()
    return {"ok": True}


@app.post("/api/job/{job_id}/continue")
@limiter.limit("60/minute")
async def continue_job(request: Request, job_id: str):
    """Chunked delivery: the user picked up the current part — delete it
    server-side (frees the memory budget) and start the next part."""
    channel = job_queues.get(job_id)
    if channel is None:
        raise HTTPException(404, "Job not found")
    if channel.pending_part:
        try:
            channel.pending_part.unlink(missing_ok=True)
        except OSError:
            pass
        channel.pending_part = None
    channel.resume()
    return {"ok": True}


@app.post("/api/cleanup")
@limiter.limit("10/minute")
async def cleanup_storage(request: Request):
    """Manual 'free server storage' — wipe every finished job's files."""
    await asyncio.to_thread(_evict_to_cap, 0)
    return {"ok": True}


@app.get("/stream/{job_id}")
async def stream_progress(job_id: str):
    if job_id not in job_queues:
        raise HTTPException(404, "Job not found")

    channel = job_queues[job_id]
    queue = channel.subscribe()

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    if data.get("type") == "done":
                        done_files = data.get("files") or ([data["filename"]] if data.get("filename") else [])
                        data = {**data, "artifacts": _serialize_artifacts(done_files)}
                    elif data.get("type") == "part" and data.get("path"):
                        arts = _serialize_artifacts([data["path"]])
                        data = {**data, "artifact": arts[0] if arts else None}
                    yield f"data: {json.dumps(data)}\n\n"
                    if data.get("type") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        finally:
            channel.unsubscribe(queue)  # channel itself expires via TTL

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
