"""
segmented.py — IDM-style multi-connection segmented downloader.

Direct file URLs (mp4/jpg/zip/anything with a Content-Length) are split into
parallel HTTP Range segments downloaded concurrently, then reassembled.
Falls back to a single-stream download when the server lacks Range support.

Progress events use the same queue protocol as downloader.py so the existing
SSE pipeline and frontend work unchanged.
"""

import asyncio
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

MIN_SEGMENT_BYTES = 1024 * 1024        # don't split below 1 MB per segment
DEFAULT_CONNECTIONS = 8
MAX_CONNECTIONS = 16
STREAM_CHUNK = 64 * 1024
SEGMENT_RETRIES = 3
PROGRESS_INTERVAL = 0.3                # seconds between progress events

_CT_EXT = {
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-matroska": ".mkv", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
    "audio/aac": ".aac", "audio/flac": ".flac", "audio/ogg": ".ogg",
    "audio/wav": ".wav", "image/jpeg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif", "application/zip": ".zip",
    "application/pdf": ".pdf",
}


def _sanitize_filename(name: str) -> str:
    name = Path(unquote(name)).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    return name[:180] or "download"


def _filename_from_headers(url: str, headers: httpx.Headers, hint: str | None) -> str:
    disposition = headers.get("content-disposition", "")
    match = re.search(r"filename\*=(?:UTF-8'')?\"?([^\";]+)", disposition, re.IGNORECASE)
    if not match:
        match = re.search(r'filename="?([^";]+)', disposition, re.IGNORECASE)
    if match:
        return _sanitize_filename(match.group(1))

    if hint:
        return _sanitize_filename(hint)

    url_name = Path(urlparse(url).path).name
    if url_name and "." in url_name:
        return _sanitize_filename(url_name)

    content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = _CT_EXT.get(content_type, ".bin")
    return _sanitize_filename(url_name or "download") + ext if url_name else f"download{ext}"


def _fmt_speed(bytes_per_sec: float) -> str:
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.0f} KB/s"
    return f"{bytes_per_sec:.0f} B/s"


def _fmt_eta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


async def _probe(client: httpx.AsyncClient, url: str) -> tuple[int, bool, httpx.Headers, str]:
    """Return (size, ranges_supported, headers, final_url).

    HEAD first; servers that reject HEAD get a GET with Range: bytes=0-0 —
    a 206 response proves range support and exposes total size via Content-Range.
    """
    try:
        resp = await client.head(url)
        if resp.status_code < 400:
            size = int(resp.headers.get("content-length") or 0)
            ranges = "bytes" in (resp.headers.get("accept-ranges") or "").lower()
            if size and ranges:
                return size, True, resp.headers, str(resp.url)
    except httpx.HTTPError:
        pass

    req = client.build_request("GET", url, headers={"Range": "bytes=0-0"})
    resp = await client.send(req, stream=True)
    try:
        if resp.status_code == 206:
            content_range = resp.headers.get("content-range", "")
            match = re.search(r"/(\d+)$", content_range)
            size = int(match.group(1)) if match else 0
            return size, size > 0, resp.headers, str(resp.url)
        size = int(resp.headers.get("content-length") or 0)
        return size, False, resp.headers, str(resp.url)
    finally:
        await resp.aclose()


class _Progress:
    """Shared byte counter; throttles SSE progress events."""

    def __init__(self, total: int, queue: asyncio.Queue):
        self.total = total
        self.queue = queue
        self.downloaded = 0
        self._last_emit = 0.0
        self._last_bytes = 0
        self._last_time = time.monotonic()

    async def add(self, n: int):
        self.downloaded += n
        now = time.monotonic()
        if now - self._last_emit < PROGRESS_INTERVAL:
            return
        elapsed = now - self._last_time
        speed = (self.downloaded - self._last_bytes) / elapsed if elapsed > 0 else 0
        self._last_emit = now
        self._last_bytes = self.downloaded
        self._last_time = now
        event = {"type": "progress", "percent": 0.0, "speed": _fmt_speed(speed), "eta": ""}
        if self.total > 0:
            event["percent"] = self.downloaded / self.total * 100.0
            if speed > 0:
                event["eta"] = _fmt_eta((self.total - self.downloaded) / speed)
        await self.queue.put(event)


async def _fetch_segment(
    client: httpx.AsyncClient,
    url: str,
    start: int,
    end: int,
    part_path: Path,
    progress: _Progress,
):
    """Download [start, end] into part_path; resumes from existing bytes on retry."""
    for attempt in range(1, SEGMENT_RETRIES + 1):
        already = part_path.stat().st_size if part_path.exists() else 0
        if start + already > end:
            return
        try:
            headers = {"Range": f"bytes={start + already}-{end}"}
            async with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                with open(part_path, "ab") as fh:
                    async for chunk in resp.aiter_bytes(STREAM_CHUNK):
                        fh.write(chunk)
                        await progress.add(len(chunk))
            return
        except (httpx.HTTPError, OSError):
            if attempt == SEGMENT_RETRIES:
                raise
            await asyncio.sleep(1.5 * attempt)


async def _assemble(parts: list[Path], final_path: Path):
    def _concat():
        with open(final_path, "wb") as out:
            for part in parts:
                with open(part, "rb") as fh:
                    while chunk := fh.read(1024 * 1024):
                        out.write(chunk)
        for part in parts:
            part.unlink(missing_ok=True)

    await asyncio.to_thread(_concat)


async def download_direct(
    url: str,
    output_folder: Path,
    queue: asyncio.Queue,
    referer: str | None = None,
    filename_hint: str | None = None,
    connections: int = DEFAULT_CONNECTIONS,
):
    """IDM-style direct download: probe → split → parallel Range fetch → assemble."""
    output_folder.mkdir(parents=True, exist_ok=True)
    connections = max(1, min(connections, MAX_CONNECTIONS))

    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer

    try:
        timeout = httpx.Timeout(30.0, read=120.0)
        async with httpx.AsyncClient(
            headers=headers, follow_redirects=True, timeout=timeout
        ) as client:
            await queue.put({"type": "log", "value": f"Probing server: {url}"})
            size, ranges_ok, probe_headers, final_url = await _probe(client, url)

            filename = _filename_from_headers(final_url, probe_headers, filename_hint)
            final_path = output_folder / filename
            await queue.put({"type": "filename", "value": filename})

            if ranges_ok and size >= 2 * MIN_SEGMENT_BYTES and connections > 1:
                n = min(connections, max(2, size // MIN_SEGMENT_BYTES))
                await queue.put({
                    "type": "log",
                    "value": f"Range support detected — {size / (1024*1024):.1f} MB across {n} connections",
                })
                progress = _Progress(size, queue)
                seg_size = size // n
                parts: list[Path] = []
                tasks = []
                for i in range(n):
                    start = i * seg_size
                    end = size - 1 if i == n - 1 else (i + 1) * seg_size - 1
                    part = output_folder / f".{filename}.part{i:02d}"
                    part.unlink(missing_ok=True)
                    parts.append(part)
                    tasks.append(_fetch_segment(client, final_url, start, end, part, progress))
                await asyncio.gather(*tasks)
                await queue.put({"type": "log", "value": "Assembling segments..."})
                await _assemble(parts, final_path)
            else:
                reason = "no Range support" if not ranges_ok else "small file"
                await queue.put({"type": "log", "value": f"Single-stream download ({reason})"})
                progress = _Progress(size, queue)
                async with client.stream("GET", final_url) as resp:
                    resp.raise_for_status()
                    with open(final_path, "wb") as fh:
                        async for chunk in resp.aiter_bytes(STREAM_CHUNK):
                            fh.write(chunk)
                            await progress.add(len(chunk))

            await queue.put({"type": "progress", "percent": 100.0, "speed": "", "eta": "00:00"})
            await queue.put({
                "type": "done",
                "filename": filename,
                "files": [str(final_path.resolve())],
            })
    except httpx.HTTPStatusError as e:
        await queue.put({
            "type": "error",
            "message": f"Server returned {e.response.status_code} — file may be protected or expired",
        })
    except Exception as e:
        await queue.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
