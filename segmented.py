"""
segmented.py — IDM-style multi-connection segmented downloader.

Direct file URLs (mp4/jpg/zip/anything with a Content-Length) are split into
parallel HTTP Range segments downloaded concurrently, then reassembled.
Falls back to a single-stream download when the server lacks Range support.

Files larger than MS_PART_THRESHOLD_MB are delivered in sequential parts so
a memory-budgeted host (Railway counts disk writes as memory) never holds
more than one part: part N lands -> user downloads it -> user confirms ->
part N is deleted and part N+1 starts.

Progress events use the same queue protocol as downloader.py so the existing
SSE pipeline and frontend work unchanged.
"""

import asyncio
import math
import os
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

# Chunked delivery: above the threshold, deliver in sequential parts
PART_THRESHOLD = int(os.environ.get("MS_PART_THRESHOLD_MB", "300")) * 1024 * 1024
PART_SIZE = int(os.environ.get("MS_PART_SIZE_MB", "200")) * 1024 * 1024


class _Cancelled(Exception):
    """User cancelled the job (JobChannel.cancelled flag)."""

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
        if getattr(self.queue, "cancelled", False):
            raise _Cancelled()
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


async def _parallel_range_fetch(
    client: httpx.AsyncClient,
    url: str,
    start: int,
    end: int,
    dest: Path,
    queue: asyncio.Queue,
    connections: int,
):
    """Fetch [start, end] into *dest* using parallel Range segments."""
    span = end - start + 1
    n = min(connections, max(1, span // MIN_SEGMENT_BYTES))
    progress = _Progress(span, queue)
    seg = span // n
    parts: list[Path] = []
    tasks = []
    for i in range(n):
        s = start + i * seg
        e = end if i == n - 1 else start + (i + 1) * seg - 1
        part = dest.parent / f".{dest.name}.seg{i:02d}"
        part.unlink(missing_ok=True)
        parts.append(part)
        tasks.append(_fetch_segment(client, url, s, e, part, progress))
    await asyncio.gather(*tasks)
    await _assemble(parts, dest)


async def _download_in_parts(
    client: httpx.AsyncClient,
    url: str,
    size: int,
    filename: str,
    output_folder: Path,
    queue: asyncio.Queue,
    connections: int,
):
    """Sequential part delivery for files too big for the host's memory budget.

    After each part the job pauses: the user downloads the part to their
    device and confirms ('Delete & continue'), which deletes the part
    server-side and resumes. Parts are exact byte slices — concatenating
    them reproduces the original file.
    """
    total_parts = math.ceil(size / PART_SIZE)
    await queue.put({
        "type": "log",
        "value": (
            f"File is {size / (1024*1024):.0f} MB — exceeds the server's memory budget. "
            f"Delivering in {total_parts} parts of ~{PART_SIZE // (1024*1024)} MB."
        ),
    })

    wait_resume = getattr(queue, "wait_resume", None)

    for idx in range(total_parts):
        start = idx * PART_SIZE
        end = min(size, (idx + 1) * PART_SIZE) - 1
        part_name = f"{filename}.part{idx + 1:02d}"
        part_path = output_folder / part_name

        await queue.put({"type": "filename", "value": f"{part_name} ({idx + 1}/{total_parts})"})
        await _parallel_range_fetch(client, url, start, end, part_path, queue, connections)

        queue.pending_part = part_path  # what /continue deletes
        await queue.put({
            "type": "part",
            "index": idx + 1,
            "total": total_parts,
            "name": part_name,
            "path": str(part_path.resolve()),
            "size": end - start + 1,
            "last": idx + 1 == total_parts,
        })

        if idx + 1 < total_parts:
            await queue.put({
                "type": "log",
                "value": f"Part {idx + 1}/{total_parts} ready — download it, then press 'Delete part & continue'.",
            })
            if wait_resume is None or not await wait_resume():
                raise _Cancelled()

    await queue.put({
        "type": "log",
        "value": 'All parts delivered. Rejoin on your device: cmd /c copy /b "name.part01"+"name.part02"+... "name"',
    })
    await queue.put({"type": "progress", "percent": 100.0, "speed": "", "eta": "00:00"})
    last_part = output_folder / f"{filename}.part{total_parts:02d}"
    await queue.put({
        "type": "done",
        "filename": f"{filename}.part{total_parts:02d}",
        "files": [str(last_part.resolve())],
    })


async def download_direct(
    url: str,
    output_folder: Path,
    queue: asyncio.Queue,
    referer: str | None = None,
    filename_hint: str | None = None,
    cookies: str | None = None,
    connections: int = DEFAULT_CONNECTIONS,
):
    """IDM-style direct download: probe → split → parallel Range fetch → assemble."""
    output_folder.mkdir(parents=True, exist_ok=True)
    connections = max(1, min(connections, MAX_CONNECTIONS))

    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    if cookies:
        headers["Cookie"] = cookies

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

            if ranges_ok and size > PART_THRESHOLD:
                await _download_in_parts(client, final_url, size, filename, output_folder, queue, connections)
                return

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
    except _Cancelled:
        await queue.put({"type": "error", "message": "Download cancelled."})
    except httpx.HTTPStatusError as e:
        await queue.put({
            "type": "error",
            "message": f"Server returned {e.response.status_code} — file may be protected or expired",
        })
    except Exception as e:
        await queue.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
