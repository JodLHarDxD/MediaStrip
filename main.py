import asyncio
import json
import mimetypes
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
from watermark import remove_watermark

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
            "script-src 'self' 'unsafe-inline'; "
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
                        import shutil
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
            except OSError:
                pass


async def _cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        await asyncio.to_thread(_cleanup_old_files)


@asynccontextmanager
async def lifespan(app: FastAPI):
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

job_queues: dict[str, asyncio.Queue] = {}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "assets" / "mediastrip-favicon.svg", media_type="image/svg+xml")


@app.get("/")
async def serve_index():
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
    job_queues[job_id] = asyncio.Queue()
    output_folder = DOWNLOADS_DIR / job_id
    output_folder.mkdir(parents=True, exist_ok=True)
    background_tasks.add_task(download_video, body.url, output_folder, job_queues[job_id])
    return {"job_id": job_id}


@app.post("/remove-watermark")
@limiter.limit("5/minute")
async def start_watermark_removal(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    platform: str = Form("tiktok"),
):
    # Enforce upload size limit
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large — maximum is {MAX_UPLOAD_BYTES // (1024*1024)} MB")

    job_id = str(uuid.uuid4())
    job_queues[job_id] = asyncio.Queue()

    safe_name = _safe_filename(file.filename or "upload")
    upload_path = UPLOADS_DIR / f"{job_id}_{safe_name}"
    upload_path.write_bytes(content)

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


@app.get("/stream/{job_id}")
async def stream_progress(job_id: str):
    if job_id not in job_queues:
        raise HTTPException(404, "Job not found")

    queue = job_queues[job_id]

    async def event_generator() -> AsyncGenerator[str, None]:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=30.0)
                if data.get("type") == "done":
                    done_files = data.get("files") or ([data["filename"]] if data.get("filename") else [])
                    data = {**data, "artifacts": _serialize_artifacts(done_files)}
                yield f"data: {json.dumps(data)}\n\n"
                if data.get("type") in ("done", "error"):
                    job_queues.pop(job_id, None)
                    break
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
