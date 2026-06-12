import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

import requests


def _registered_domain(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _write_cookie_jar(cookies, url: str, page_url: str | None, dest: Path) -> Path | None:
    """Write a Netscape-format cookie file for yt-dlp.

    Accepts either structured cookie dicts from the extension (name/value/domain/
    path/secure/expirationDate) or a legacy raw Cookie-header string. yt-dlp's
    extractors only use cookies from a real jar (--cookies) — a Cookie header via
    --add-header never reaches the API requests that need them (YouTube bot-check).
    """
    lines = ["# Netscape HTTP Cookie File"]
    if isinstance(cookies, str):
        # header string has no domain info — pin to the target/page registered domains
        hosts = {urlparse(u).netloc for u in (url, page_url or "") if u.startswith("http")}
        domains = {"." + _registered_domain(h) for h in hosts if h}
        for pair in cookies.split(";"):
            name, _, value = pair.strip().partition("=")
            if name and value:
                for domain in sorted(domains):
                    lines.append(f"{domain}\tTRUE\t/\tFALSE\t0\t{name}\t{value}")
    else:
        for c in cookies or []:
            name = c.get("name")
            if not name:
                continue
            domain = c.get("domain") or ""
            include_sub = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.get("path") or "/"
            secure = "TRUE" if c.get("secure") else "FALSE"
            expiry = int(c.get("expirationDate") or 0)
            lines.append(f"{domain}\t{include_sub}\t{path}\t{secure}\t{expiry}\t{name}\t{c.get('value', '')}")
    if len(lines) == 1:
        return None
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


async def _stream_subprocess(cmd: list[str], line_handler, on_proc=None) -> int:
    """Run *cmd* via subprocess.Popen in a worker thread, streaming stdout lines
    to *line_handler* (a sync callback executed on the event-loop thread).

    Uses Popen instead of asyncio.create_subprocess_exec because some ASGI server
    event loops raise NotImplementedError on create_subprocess_exec. Popen is
    OS-level and works under any loop. Returns the process exit code.
    *on_proc* (optional) receives the Popen handle — used for cancellation.
    """
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    def worker():
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            if on_proc:
                loop.call_soon_threadsafe(on_proc, proc)
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if line:
                    loop.call_soon_threadsafe(line_handler, line)
            proc.stdout.close()
            rc = proc.wait()
            loop.call_soon_threadsafe(done.set_result, rc)
        except Exception as e:  # propagate to the awaiting coroutine
            loop.call_soon_threadsafe(done.set_exception, e)

    threading.Thread(target=worker, daemon=True).start()
    return await done

_ANIME_IMPORT_ERROR: str | None = None
try:
    from anime_extractor import parse_anime_url as _anime_parse, resolve_stream as _anime_resolve
    _ANIME_AVAILABLE = True
except Exception as _e:
    _ANIME_AVAILABLE = False
    _ANIME_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

INSTAGRAM_EMBED_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
}


async def _download_anime(url: str, output_folder: Path, queue: asyncio.Queue):
    """Resolve anime URL → m3u8, then delegate to the standard yt-dlp pipeline."""
    try:
        await queue.put({"type": "log", "value": "Anime URL detected — resolving stream..."})
        stream = await _anime_resolve(url)
        await queue.put({"type": "log", "value": f"Resolved: {stream.anime_title} — {stream.title} (Ep {stream.episode_number})"})
        await queue.put({"type": "filename", "value": f"{stream.anime_title}_ep{stream.episode_number:02d}.mp4"})
        await queue.put({"type": "log", "value": "Handing off m3u8 to yt-dlp..."})
        await download_video(stream.m3u8_url, output_folder, queue, referer=stream.referer)
    except Exception as e:
        await queue.put({"type": "error", "message": f"Anime resolution failed: {type(e).__name__}: {e}"})


_ANIME_URL_PATTERN = re.compile(
    r"https?://(?:hianime[s]?\.(?:se|to|sx|tv|me|watch)|aniwatch\.to|kaido\.to)/watch/"
)


async def download_video(
    url: str,
    output_folder: Path,
    queue: asyncio.Queue,
    referer: str | None = None,
    cookies: str | list | None = None,
    single_item: bool = False,
):
    # Support "URL|referer=https://site.com" syntax for CDNs that check Referer
    if "|referer=" in url:
        url, pipe_referer = url.split("|referer=", 1)
        referer = referer or pipe_referer.strip()

    if _ANIME_AVAILABLE and _anime_parse(url):
        await _download_anime(url, output_folder, queue)
        return
    if not _ANIME_AVAILABLE and _ANIME_URL_PATTERN.search(url):
        err = _ANIME_IMPORT_ERROR or "anime module not loaded"
        await queue.put({"type": "error", "message": f"Anime module failed to load: {err}"})
        return

    output_folder.mkdir(parents=True, exist_ok=True)
    output_template = str(output_folder / "%(title)s_%(id)s.%(ext)s")

    format_selector = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    parsed_url = urlparse(url)
    host = parsed_url.netloc.lower()
    is_instagram_post = "instagram.com" in host and re.search(r"/p/[^/?#]+", parsed_url.path)
    playlist_flag = "--yes-playlist" if is_instagram_post else "--no-playlist"
    # extension per-video clicks: hard cap at one item even if the URL extracts
    # to a feed/channel/playlist (instagram carousels are the exception — their
    # items ARE the single post)
    limit_one = single_item and not is_instagram_post

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--format", format_selector,
        "--merge-output-format", "mp4",
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        playlist_flag,
        "--concurrent-fragments", "8",
        "--progress",
        "--newline",
        "--output", output_template,
    ]

    if limit_one:
        cmd.extend(["--playlist-items", "1"])

    # m3u8 streams (e.g. anime CDNs) are behind Cloudflare — requires browser impersonation
    if parsed_url.path.endswith(".m3u8"):
        cmd.extend(["--extractor-args", "generic:impersonate"])

    if referer:
        cmd.extend(["--add-header", f"Referer:{referer}"])

    # Forward browser cookies for login-gated / session-protected streams.
    # Written OUTSIDE output_folder — everything in there gets published as a
    # download artifact.
    jar_path: Path | None = None
    if cookies:
        fd, tmp = tempfile.mkstemp(prefix="ms_jar_", suffix=".txt")
        os.close(fd)
        jar_path = _write_cookie_jar(cookies, url, referer, Path(tmp))
        if jar_path:
            cmd.extend(["--cookies", str(jar_path)])
        else:
            os.unlink(tmp)

    cmd.append(url)

    try:
        filename = None
        total_items = 1
        current_item = 1
        last_error = None
        last_progress_step = -1  # 0.1% granularity
        last_log_pct = -1  # whole-percent granularity

        def handle_line(line: str):
            nonlocal filename, total_items, current_item, last_error
            nonlocal last_progress_step, last_log_pct

            if line.startswith("ERROR:"):
                last_error = line

            playlist_match = re.search(r"Downloading\s+(\d+)\s+items?\s+of\s+(\d+)", line)
            if playlist_match:
                total_items = max(1, int(playlist_match.group(2)))

            item_match = re.search(r"Downloading item\s+(\d+)\s+of\s+(\d+)", line)
            if item_match:
                current_item = int(item_match.group(1))
                total_items = max(1, int(item_match.group(2)))

            if "[download] Destination:" in line:
                filename = line.split("Destination:")[-1].strip()
                queue.put_nowait({"type": "filename", "value": Path(filename).name})

            thumb_match = re.search(r"Writing .* thumbnail \d+ to:\s+(.+)$", line)
            if thumb_match:
                thumb_path = thumb_match.group(1).strip()
                queue.put_nowait({"type": "filename", "value": Path(thumb_path).name})

            progress_match = re.search(r"\[download\]\s+([\d.]+)%", line)
            if progress_match:
                file_pct = float(progress_match.group(1))
                pct = ((current_item - 1) + (file_pct / 100.0)) / total_items * 100.0
                # HLS fragment downloads print hundreds of near-identical lines;
                # only forward meaningful steps (0.1% for the bar, 1% for the log)
                step = int(pct * 10)
                if step != last_progress_step:
                    last_progress_step = step
                    speed = ""
                    eta = ""
                    speed_match = re.search(r"at\s+([\d.]+\s*\S+/s)", line)
                    if speed_match:
                        speed = speed_match.group(1)
                    eta_match = re.search(r"ETA\s+([\d:]+)", line)
                    if eta_match:
                        eta = eta_match.group(1)
                    queue.put_nowait({"type": "progress", "percent": pct, "speed": speed, "eta": eta})
                if int(pct) == last_log_pct:
                    return
                last_log_pct = int(pct)

            queue.put_nowait({"type": "log", "value": line})

        returncode = await _stream_subprocess(
            cmd, handle_line, on_proc=getattr(queue, "register_proc", None)
        )

        if getattr(queue, "cancelled", False):
            await queue.put({"type": "error", "message": "Download cancelled."})
            return

        if returncode == 0:
            saved_files = [str(path.resolve()) for path in sorted(output_folder.rglob("*")) if path.is_file()]

            if is_instagram_post:
                # yt-dlp downloads videos from mixed carousels but silently skips images;
                # always fetch image items via embed so mixed carousels are complete
                img_files = await _download_instagram_embed_images(url, output_folder, queue)
                if img_files:
                    saved_files = sorted(set(saved_files) | set(img_files), key=lambda p: Path(p).name)

            if not saved_files:
                if is_instagram_post:
                    await queue.put({
                        "type": "log",
                        "value": "yt-dlp returned no files. Trying Instagram embed fallback for carousel images...",
                    })
                    saved_files = await _download_instagram_embed_media(url, output_folder, queue)
                    filename = saved_files[-1] if saved_files else filename

                if not saved_files:
                    if is_instagram_post:
                        message = (
                            "Instagram returned metadata but no downloadable media for this post. "
                            "It may require login, be restricted, or be unsupported by yt-dlp right now."
                        )
                    else:
                        message = "Download finished without producing any files. Try another URL or format."
                    await queue.put({"type": "error", "message": message})
                    return

            await queue.put({"type": "done", "filename": filename or "", "files": saved_files})
        else:
            await queue.put({"type": "error", "message": _friendly_ytdlp_error(last_error)})

    except FileNotFoundError:
        await queue.put({"type": "error", "message": "yt-dlp not found — install it with: pip install yt-dlp or python -m pip install yt-dlp"})
    except Exception as e:
        await queue.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        if jar_path:
            try:
                os.unlink(jar_path)
            except OSError:
                pass


def _friendly_ytdlp_error(last_error: str | None) -> str:
    if not last_error:
        return "yt-dlp exited with an error — check the URL and try again"
    if "Sign in to confirm" in last_error or "not a bot" in last_error:
        return (
            "YouTube blocked this server with a bot-check. Use the MediaStrip browser "
            "extension on the video page instead — it forwards your YouTube login cookies. "
            "Or run MediaStrip locally (python -m uvicorn main:app --port 8000)."
        )
    if "429" in last_error or "Too Many Requests" in last_error:
        return (
            "YouTube is rate-limiting this server (HTTP 429). Wait a few minutes and retry, "
            "or use the browser extension so your own session cookies go with the request."
        )
    return last_error.removeprefix("ERROR:").strip()


def _instagram_shortcode(url: str) -> str | None:
    parsed = urlparse(url)
    match = re.search(r"/p/([^/?#]+)", parsed.path)
    return match.group(1) if match else None


def _instagram_embed_url(url: str) -> str | None:
    shortcode = _instagram_shortcode(url)
    if not shortcode:
        return None
    return (
        f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
        f"?cr=1&v=14&wp=540&rd=https%3A%2F%2Fwww.instagram.com&rp=%2Fp%2F{shortcode}%2F"
    )


def _fetch_text(url: str) -> str:
    response = requests.get(url, headers=INSTAGRAM_EMBED_HEADERS, timeout=30)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text


def _download_binary(url: str, output_path: Path):
    response = requests.get(url, headers=INSTAGRAM_EMBED_HEADERS, timeout=60)
    response.raise_for_status()
    output_path.write_bytes(response.content)


def _best_media_url(node: dict) -> tuple[str | None, str]:
    media_kind = "video" if node.get("__typename") == "GraphVideo" else "image"
    if media_kind == "video" and node.get("video_url"):
        return node["video_url"], media_kind

    display_resources = node.get("display_resources") or []
    if display_resources:
        return display_resources[-1].get("src"), media_kind

    return node.get("display_url"), media_kind


def _normalize_media_ext(media_url: str, media_kind: str) -> str:
    suffix = Path(urlparse(media_url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".mp4", ".mov", ".webm"}:
        return suffix
    return ".mp4" if media_kind == "video" else ".jpg"


def _extract_embed_media_items(url: str) -> list[dict]:
    embed_url = _instagram_embed_url(url)
    if not embed_url:
        return []

    html_text = _fetch_text(embed_url)
    match = re.search(r'"contextJSON":"((?:\\.|[^"])*)"', html_text)
    if not match:
        return []

    context_json = json.loads('"' + match.group(1) + '"')
    data = json.loads(context_json)
    media = ((data.get("context") or {}).get("media")) or {}

    if not media:
        return []

    children = (media.get("edge_sidecar_to_children") or {}).get("edges") or []
    nodes = [edge.get("node") or {} for edge in children] if children else [media]

    items: list[dict] = []
    for index, node in enumerate(nodes, 1):
        media_url, media_kind = _best_media_url(node)
        if not media_url:
            continue

        direct_url = unescape(media_url)
        shortcode = node.get("shortcode") or media.get("shortcode") or f"item{index:02d}"
        items.append({
            "url": direct_url,
            "kind": media_kind,
            "shortcode": shortcode,
            "index": index,
        })

    return items


async def _download_instagram_embed_images(url: str, output_folder: Path, queue: asyncio.Queue) -> list[str]:
    """Download only image items from a carousel — videos are handled by yt-dlp."""
    items = await asyncio.to_thread(_extract_embed_media_items, url)
    image_items = [it for it in items if it["kind"] == "image"]
    if not image_items:
        return []

    post_shortcode = _instagram_shortcode(url) or "instagram"
    saved_files: list[str] = []

    await queue.put({
        "type": "log",
        "value": f"Fetching {len(image_items)} carousel image(s) skipped by yt-dlp...",
    })

    for item in image_items:
        ext = _normalize_media_ext(item["url"], item["kind"])
        output_path = output_folder / f"instagram_{post_shortcode}_{item['index']:02d}_{item['shortcode']}{ext}"
        if output_path.exists():
            saved_files.append(str(output_path.resolve()))
            continue
        await asyncio.to_thread(_download_binary, item["url"], output_path)
        saved_files.append(str(output_path.resolve()))
        await queue.put({"type": "log", "value": f"Saved image {output_path.name}"})

    return saved_files


async def _download_instagram_embed_media(url: str, output_folder: Path, queue: asyncio.Queue) -> list[str]:
    items = await asyncio.to_thread(_extract_embed_media_items, url)
    if not items:
        return []

    post_shortcode = _instagram_shortcode(url) or "instagram"
    saved_files: list[str] = []

    await queue.put({
        "type": "log",
        "value": f"Instagram embed fallback found {len(items)} media item(s). Downloading directly...",
    })

    total = len(items)
    for item in items:
        ext = _normalize_media_ext(item["url"], item["kind"])
        output_path = output_folder / f"instagram_{post_shortcode}_{item['index']:02d}_{item['shortcode']}{ext}"

        await queue.put({"type": "filename", "value": output_path.name})
        await queue.put({"type": "progress", "percent": ((item['index'] - 1) / total) * 100.0})
        await asyncio.to_thread(_download_binary, item["url"], output_path)
        saved_files.append(str(output_path.resolve()))
        await queue.put({"type": "log", "value": f"Saved {output_path.name}"})
        await queue.put({"type": "progress", "percent": (item["index"] / total) * 100.0})

    return saved_files
