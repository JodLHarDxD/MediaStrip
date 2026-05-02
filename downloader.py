import asyncio
import json
import re
import sys
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).parent / "anime_module"))
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
        await queue.put({"type": "log", "value": "Anime URL detected — resolving stream via myani.cfd..."})
        stream = await _anime_resolve(url)
        await queue.put({"type": "log", "value": f"Resolved: {stream.anime_title} — {stream.title} (Ep {stream.episode_number})"})
        await queue.put({"type": "filename", "value": f"{stream.anime_title}_ep{stream.episode_number:02d}.mp4"})
        await queue.put({"type": "log", "value": "Handing off m3u8 to yt-dlp..."})
        await download_video(stream.m3u8_url, output_folder, queue)
    except Exception as e:
        await queue.put({"type": "error", "message": f"Anime resolution failed: {type(e).__name__}: {e}"})


_ANIME_URL_PATTERN = re.compile(
    r"https?://(?:hianime[s]?\.(?:se|to|sx|tv|me|watch)|aniwatch\.to|kaido\.to)/watch/"
)


async def download_video(url: str, output_folder: Path, queue: asyncio.Queue):
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

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--format", format_selector,
        "--merge-output-format", "mp4",
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        playlist_flag,
        "--progress",
        "--newline",
        "--output", output_template,
    ]

    # m3u8 streams (e.g. anime CDNs) are behind Cloudflare — requires browser impersonation
    if parsed_url.path.endswith(".m3u8"):
        cmd.extend(["--extractor-args", "generic:impersonate"])

    cmd.append(url)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        filename = None
        total_items = 1
        current_item = 1

        async for line_bytes in process.stdout:
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            playlist_match = re.search(r"Downloading\s+(\d+)\s+items?\s+of\s+(\d+)", line)
            if playlist_match:
                total_items = max(1, int(playlist_match.group(2)))

            item_match = re.search(r"Downloading item\s+(\d+)\s+of\s+(\d+)", line)
            if item_match:
                current_item = int(item_match.group(1))
                total_items = max(1, int(item_match.group(2)))

            if "[download] Destination:" in line:
                filename = line.split("Destination:")[-1].strip()
                await queue.put({"type": "filename", "value": Path(filename).name})

            thumb_match = re.search(r"Writing .* thumbnail \d+ to:\s+(.+)$", line)
            if thumb_match:
                thumb_path = thumb_match.group(1).strip()
                await queue.put({"type": "filename", "value": Path(thumb_path).name})

            progress_match = re.search(r"\[download\]\s+([\d.]+)%", line)
            if progress_match:
                file_pct = float(progress_match.group(1))
                pct = ((current_item - 1) + (file_pct / 100.0)) / total_items * 100.0
                speed = ""
                eta = ""
                speed_match = re.search(r"at\s+([\d.]+\s*\S+/s)", line)
                if speed_match:
                    speed = speed_match.group(1)
                eta_match = re.search(r"ETA\s+([\d:]+)", line)
                if eta_match:
                    eta = eta_match.group(1)
                await queue.put({"type": "progress", "percent": pct, "speed": speed, "eta": eta})

            await queue.put({"type": "log", "value": line})

        await process.wait()

        if process.returncode == 0:
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
            await queue.put({"type": "error", "message": "yt-dlp exited with an error — check the URL and try again"})

    except FileNotFoundError:
        await queue.put({"type": "error", "message": "yt-dlp not found — install it with: pip install yt-dlp or python -m pip install yt-dlp"})
    except Exception as e:
        await queue.put({"type": "error", "message": f"{type(e).__name__}: {e}"})


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
