"""
MediaStrip - Anime Extraction Module
=====================================
Resolves anime streaming URLs (hianime, etc.) to downloadable streams.
Gives user the choice: full MP4 or audio-only extraction.

Chain: hianime URL → myani.cfd API → megaplay embed → m3u8 → download

Drop this into your existing MediaStrip project.
"""

import re
import os
import sys
import uuid
import asyncio
import logging
import subprocess
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("anime_extractor")


# ─── Config ────────────────────────────────────────────────────────────────────

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./media/downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}

# API endpoints discovered via reverse-engineering
MYANI_API = "https://myani.cfd/api"
MEGAPLAY_BASE = "https://megaplay.buzz"

# myani.cfd / megaplay sit behind Cloudflare — plain httpx (wrong TLS fingerprint)
# gets stalled on the bot challenge. curl_cffi impersonates a real browser to pass it.
try:
    from curl_cffi.requests import AsyncSession as _CurlSession
    _CURL_AVAILABLE = True
except Exception:
    _CURL_AVAILABLE = False


async def _fetch(url: str, headers: dict | None = None, params: dict | None = None, timeout: int = 20):
    """GET with Cloudflare-bypassing TLS impersonation (curl_cffi), httpx fallback.

    Returned object exposes .status_code, .text, .json() in both backends.
    """
    merged = {**HEADERS, **(headers or {})}
    if _CURL_AVAILABLE:
        async with _CurlSession() as session:
            return await session.get(
                url, headers=merged, params=params, timeout=timeout, impersonate="chrome"
            )
    async with httpx.AsyncClient(
        headers=merged, timeout=timeout, verify=False, follow_redirects=True
    ) as client:
        return await client.get(url, params=params)


def _guard_upstream(resp, name: str):
    """Raise a clear, user-facing error when a reverse-engineered upstream is down."""
    if resp.status_code >= 500:
        raise ValueError(
            f"Anime source API ({name}) returned {resp.status_code} — it is currently down. "
            "This is an upstream outage, not your download. Try again later."
        )
    if resp.status_code != 200:
        raise ValueError(f"Anime source API ({name}) error {resp.status_code}.")


# ─── Types ──────────────────────────────────────────────────────────────────────

class OutputFormat(str, Enum):
    MP4 = "mp4"          # full video + audio
    AUDIO = "audio"      # audio-only (aac/opus)


class Quality(str, Enum):
    BEST = "best"
    Q1080 = "1080"
    Q720 = "720"
    Q360 = "360"


@dataclass
class StreamInfo:
    """Resolved stream metadata before download."""
    m3u8_url: str
    title: str
    episode_number: int
    anime_title: str
    duration: Optional[str] = None
    subtitles: list = field(default_factory=list)
    qualities: list = field(default_factory=list)
    intro: Optional[dict] = None
    outro: Optional[dict] = None
    referer: Optional[str] = None  # CDN Referer required for the m3u8 download


@dataclass 
class DownloadResult:
    """What the user gets back."""
    job_id: str
    status: str  # "completed" | "failed" | "processing"
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    format: Optional[str] = None
    error: Optional[str] = None


# ─── URL Parser ─────────────────────────────────────────────────────────────────

# Supports multiple hianime mirror domains
HIANIME_PATTERN = re.compile(
    r"https?://(?:hianime[s]?\.(?:se|to|sx|tv|me|watch)|"
    r"aniwatch\.to|"
    r"kaido\.to)"
    r"/watch/([a-zA-Z0-9-]+)"
)

def parse_anime_url(url: str) -> Optional[str]:
    """Extract episode slug from a hianime-family URL."""
    match = HIANIME_PATTERN.search(url)
    if match:
        return match.group(1)
    
    # Fallback: try to extract slug from any URL with /watch/ pattern
    fallback = re.search(r"/watch/([a-zA-Z0-9-]+)", url)
    if fallback:
        return fallback.group(1)
    
    return None


# ─── Resolution Chain ───────────────────────────────────────────────────────────

MEGAPLAY_EMBED_RE = re.compile(r"https://megaplay\.buzz/stream/[^\"'\\\s]+")


def _title_from_slug(slug: str) -> str:
    """Fallback anime title: turn a slug into Title Case, dropping the episode tail."""
    cleaned = re.sub(r"-episode-\d+.*$", "", slug)
    cleaned = re.sub(r"-[a-z0-9]{5,8}$", "", cleaned)  # trailing hash id
    return cleaned.replace("-", " ").strip().title() or slug


async def resolve_watch_page(url: str, audio_lang: str = "sub") -> dict:
    """Direct resolver — no myani.cfd.

    The hianime watch page server-renders the episode data, including the
    megaplay embed URL, straight into the HTML. Scrape it instead of relying
    on the (frequently down) myani.cfd API.

    Returns {embed_url, anime_title, episode_number, title}.
    """
    parsed = urlparse(url)
    site_root = f"{parsed.scheme}://{parsed.netloc}/"

    try:
        resp = await _fetch(url, headers={"Referer": site_root}, timeout=20)
    except Exception as e:
        raise ValueError(
            f"Anime site ({parsed.netloc}) is unreachable — it may be down. Try again later."
        ) from e

    _guard_upstream(resp, parsed.netloc)
    html = resp.text

    # Megaplay embed for the requested language. The active <iframe> is the sub
    # player; dub (when present) lives in the escaped episode JSON.
    embed_url = None
    if audio_lang == "dub":
        dub = re.search(r'\\"dub\\":\[\\"(https://megaplay\.buzz/stream/[^"\\]+)\\"', html)
        if dub:
            embed_url = dub.group(1)
    if not embed_url:
        iframe = re.search(r'<iframe[^>]+src="(https://megaplay\.buzz/stream/[^"]+)"', html)
        if iframe:
            embed_url = iframe.group(1)
    if not embed_url:
        any_embed = MEGAPLAY_EMBED_RE.search(html)
        embed_url = any_embed.group(0) if any_embed else None
    if not embed_url:
        raise ValueError(
            "No megaplay player found on the watch page — the anime site structure "
            "may have changed."
        )

    slug = parse_anime_url(url) or ""
    title_match = (
        re.search(r'\\"title\\":\\"([^"\\]+)\\",\\"totalEpisodes\\"', html)
        or re.search(r'"title":"([^"]+)","totalEpisodes"', html)
    )
    anime_title = title_match.group(1).strip() if title_match else _title_from_slug(slug)

    ep_match = re.search(r"episode-(\d+)", url) or re.search(r"[?&]ep=(\d+)", url)
    episode_number = int(ep_match.group(1)) if ep_match else 0

    title = f"{anime_title} Episode {episode_number}" if episode_number else anime_title

    logger.info(f"Direct resolve: {anime_title} ep{episode_number} → {embed_url}")
    return {
        "embed_url": embed_url,
        "anime_title": anime_title,
        "episode_number": episode_number,
        "title": title,
    }


async def resolve_episode(slug: str) -> dict:
    """
    Step 1: slug → episode metadata via myani.cfd API
    Returns episode link (embed URL), anime info, etc.
    """
    try:
        resp = await _fetch(f"{MYANI_API}/episode/{slug}", timeout=20)
    except Exception as e:
        raise ValueError(
            "Anime source API (myani.cfd) is unreachable — it may be down. "
            "This is an upstream outage, not your download. Try again later."
        ) from e

    _guard_upstream(resp, "myani.cfd")
    data = resp.json()

    if "episode" not in data:
        raise ValueError(f"Episode not found for slug: {slug}")

    return data


async def resolve_embed(embed_url: str) -> int:
    """
    Step 2: embed URL → internal player ID (data-id)
    Fetches the megaplay embed page and extracts data-id from HTML.
    """
    try:
        resp = await _fetch(embed_url, headers={"Referer": "https://hianimes.se/"}, timeout=20)
    except Exception as e:
        raise ValueError(
            "Anime player host (megaplay.buzz) is unreachable — it may be down. Try again later."
        ) from e

    _guard_upstream(resp, "megaplay.buzz")
    soup = BeautifulSoup(resp.text, "html.parser")
    player_div = soup.find(attrs={"data-id": True})

    if not player_div:
        raise ValueError(f"Could not find data-id in embed page: {embed_url}")

    return int(player_div["data-id"])


async def resolve_sources(player_id: int, referer: str) -> dict:
    """
    Step 3: player ID → m3u8 URL via megaplay getSources API
    """
    try:
        resp = await _fetch(
            f"{MEGAPLAY_BASE}/stream/getSources",
            headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"},
            params={"id": player_id},
            timeout=20,
        )
    except Exception as e:
        raise ValueError(
            "Anime player host (megaplay.buzz) is unreachable — it may be down. Try again later."
        ) from e

    _guard_upstream(resp, "megaplay.buzz")
    data = resp.json()

    if "sources" not in data or "file" not in data.get("sources", {}):
        raise ValueError(f"No sources found for player ID: {player_id}")

    return data


async def resolve_stream(url: str, audio_lang: str = "sub") -> StreamInfo:
    """
    Full resolution chain: anime page URL → StreamInfo with m3u8
    
    audio_lang: "sub" for JP audio + EN subs, "dub" for EN dub
    """
    slug = parse_anime_url(url)
    if not slug:
        raise ValueError(f"Could not parse anime URL: {url}")

    logger.info(f"Resolving slug: {slug}")

    # Step 1: Get the megaplay embed URL + metadata.
    # Primary: scrape the watch page directly (no myani.cfd dependency).
    # Fallback: legacy myani.cfd API, in case the page structure changes.
    meta: dict
    try:
        meta = await resolve_watch_page(url, audio_lang)
        embed_url = meta["embed_url"]
        anime_title = meta["anime_title"]
        episode_number = meta["episode_number"]
        title = meta["title"]
    except Exception as direct_err:
        logger.warning(f"Direct resolve failed ({direct_err}); falling back to myani.cfd")
        episode_data = await resolve_episode(slug)
        episode = episode_data["episode"]
        anime = episode_data.get("anime", {})
        links = episode.get("link", {})
        lang_links = links.get(audio_lang, links.get("sub", []))
        if not lang_links:
            raise ValueError(f"No {audio_lang} links found for episode")
        embed_url = lang_links[0]
        anime_title = anime.get("Japanese", anime.get("_id", "Unknown"))
        episode_number = episode.get("episodeNumber", 0)
        title = episode.get("title", slug)

    logger.info(f"Embed URL: {embed_url}")

    # Step 2: megaplay embed page → player data-id
    player_id = await resolve_embed(embed_url)
    logger.info(f"Player ID: {player_id}")

    # Step 3: getSources → m3u8
    sources = await resolve_sources(player_id, embed_url)
    m3u8_url = sources["sources"]["file"]
    logger.info(f"M3U8 URL: {m3u8_url}")

    # The mewstream CDN checks Referer — downloads 403 without it
    referer = f"{MEGAPLAY_BASE}/"

    subtitles = []
    for track in sources.get("tracks", []):
        if track.get("kind") == "captions":
            subtitles.append({
                "url": track["file"],
                "label": track.get("label", "Unknown"),
                "default": track.get("default", False),
            })

    qualities = await _probe_qualities(m3u8_url, referer)

    return StreamInfo(
        m3u8_url=m3u8_url,
        title=title,
        episode_number=episode_number,
        anime_title=anime_title,
        subtitles=subtitles,
        qualities=qualities,
        intro=sources.get("intro"),
        outro=sources.get("outro"),
        referer=referer,
    )


MEGAPLAY_STREAM_PATTERN = re.compile(r"https?://megaplay\.buzz/stream/\S+")


def parse_megaplay_url(url: str) -> bool:
    """True for bare megaplay embed/stream URLs — what the browser extension's
    in-iframe catcher sends when the user is inside the anime player frame."""
    return bool(MEGAPLAY_STREAM_PATTERN.match(url))


async def resolve_megaplay_stream(embed_url: str) -> StreamInfo:
    """Resolve a megaplay embed URL directly to its m3u8 (chain steps 2+3 —
    no hianime watch page involved)."""
    player_id = await resolve_embed(embed_url)
    sources = await resolve_sources(player_id, embed_url)
    m3u8_url = sources["sources"]["file"]

    subtitles = []
    for track in sources.get("tracks", []):
        if track.get("kind") == "captions":
            subtitles.append({
                "url": track["file"],
                "label": track.get("label", "Unknown"),
                "default": track.get("default", False),
            })

    # /stream/s-2/161029/sub → "megaplay_161029_sub"
    parts = [p for p in embed_url.split("?")[0].split("/") if p]
    ep_id = parts[-2] if len(parts) >= 2 else "stream"
    lang = parts[-1] if parts else "sub"

    return StreamInfo(
        m3u8_url=m3u8_url,
        title=f"megaplay_{ep_id}_{lang}",
        episode_number=0,
        anime_title="anime",
        subtitles=subtitles,
        intro=sources.get("intro"),
        outro=sources.get("outro"),
        referer=f"{MEGAPLAY_BASE}/",
    )


async def _probe_qualities(m3u8_url: str, referer: Optional[str] = None) -> list:
    """Probe available quality levels from the m3u8.

    Runs yt-dlp via subprocess.run in a thread (not asyncio.create_subprocess_exec,
    which raises NotImplementedError on some ASGI event loops). Non-critical —
    returns [] on any failure.
    """
    cmd = [
        sys.executable, "-m", "yt_dlp", "--no-check-certificates", "--dump-json",
        "--extractor-args", "generic:impersonate",
    ]
    if referer:
        cmd += ["--add-header", f"Referer:{referer}"]
    cmd.append(m3u8_url)

    def _run():
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    try:
        proc = await asyncio.to_thread(_run)
        if proc.returncode == 0:
            import json
            data = json.loads(proc.stdout)
            return [
                {
                    "format_id": f["format_id"],
                    "resolution": f.get("resolution", "unknown"),
                    "tbr": f.get("tbr"),
                    "ext": f.get("ext", "mp4"),
                }
                for f in data.get("formats", [])
            ]
    except Exception as e:
        logger.warning(f"Quality probe failed: {e}")

    return []


# ─── Download Engine ────────────────────────────────────────────────────────────

async def download_stream(
    stream: StreamInfo,
    output_format: OutputFormat = OutputFormat.MP4,
    quality: Quality = Quality.BEST,
) -> DownloadResult:
    """
    Download resolved stream as MP4 or audio-only.
    Returns DownloadResult with file path.
    """
    job_id = str(uuid.uuid4())
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    
    # Sanitize filename
    safe_title = re.sub(r'[^\w\s-]', '', stream.title).strip().replace(' ', '_')
    
    if output_format == OutputFormat.AUDIO:
        output_file = job_dir / f"{safe_title}.aac"
        cmd = _build_audio_cmd(stream.m3u8_url, str(output_file), quality, stream.referer)
    else:
        output_file = job_dir / f"{safe_title}.mp4"
        cmd = _build_video_cmd(stream.m3u8_url, str(output_file), quality, stream.referer)

    logger.info(f"Downloading [{output_format.value}]: {' '.join(cmd)}")

    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True
        )

        if proc.returncode != 0:
            error_msg = (proc.stderr or "")[-500:]
            logger.error(f"Download failed: {error_msg}")
            return DownloadResult(
                job_id=job_id,
                status="failed",
                format=output_format.value,
                error=error_msg,
            )
        
        # Find the actual output file (yt-dlp might adjust extension)
        actual_file = _find_output_file(job_dir, safe_title)
        
        if actual_file and actual_file.exists():
            return DownloadResult(
                job_id=job_id,
                status="completed",
                file_path=str(actual_file),
                file_size=actual_file.stat().st_size,
                format=output_format.value,
            )
        else:
            return DownloadResult(
                job_id=job_id,
                status="failed",
                format=output_format.value,
                error="Output file not found after download",
            )
    
    except Exception as e:
        logger.error(f"Download exception: {e}")
        return DownloadResult(
            job_id=job_id,
            status="failed",
            format=output_format.value,
            error=str(e),
        )


def _ytdlp_base() -> list:
    """yt-dlp invocation that works in Docker (module, not a PATH binary) and
    passes Cloudflare via browser impersonation."""
    return [
        sys.executable, "-m", "yt_dlp",
        "--no-check-certificates",
        "--no-warnings",
        "--extractor-args", "generic:impersonate",
    ]


def _build_video_cmd(m3u8_url: str, output: str, quality: Quality, referer: Optional[str] = None) -> list:
    """Build yt-dlp command for full MP4 download."""
    cmd = _ytdlp_base() + ["-o", output]
    if referer:
        cmd += ["--add-header", f"Referer:{referer}"]

    # Quality selection
    if quality == Quality.Q1080:
        cmd.extend(["-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]"])
    elif quality == Quality.Q720:
        cmd.extend(["-f", "bestvideo[height<=720]+bestaudio/best[height<=720]"])
    elif quality == Quality.Q360:
        cmd.extend(["-f", "bestvideo[height<=360]+bestaudio/best[height<=360]"])
    else:
        cmd.extend(["-f", "best"])
    
    cmd.append(m3u8_url)
    return cmd


def _build_audio_cmd(m3u8_url: str, output: str, quality: Quality, referer: Optional[str] = None) -> list:
    """Build yt-dlp command for audio-only extraction."""
    cmd = _ytdlp_base() + [
        "-x",                          # extract audio
        "--audio-format", "aac",       # output as AAC (good for VoxDub processing)
        "--audio-quality", "0",        # best audio quality
        "-o", output,
    ]
    if referer:
        cmd += ["--add-header", f"Referer:{referer}"]

    # For audio, just grab best available (audio track is same across qualities)
    cmd.append(m3u8_url)
    return cmd


def _find_output_file(job_dir: Path, base_name: str) -> Optional[Path]:
    """Find the actual output file (yt-dlp might change extension)."""
    # Check exact match first
    for ext in [".mp4", ".aac", ".m4a", ".opus", ".webm", ".mkv"]:
        candidate = job_dir / f"{base_name}{ext}"
        if candidate.exists():
            return candidate
    
    # Fallback: find any media file in the directory
    for f in job_dir.iterdir():
        if f.suffix in {".mp4", ".aac", ".m4a", ".opus", ".webm", ".mkv", ".mp3"}:
            return f
    
    return None


# ─── FastAPI Router ─────────────────────────────────────────────────────────────

def create_router():
    """
    Create FastAPI router for anime extraction endpoints.
    
    Usage in your main app:
        from anime_extractor import create_router
        app.include_router(create_router(), prefix="/anime")
    """
    from fastapi import APIRouter, HTTPException, Query
    from fastapi.responses import FileResponse, JSONResponse
    
    router = APIRouter(tags=["anime"])
    
    @router.get("/resolve")
    async def resolve_anime_url(
        url: str = Query(..., description="Anime page URL (e.g., hianimes.se/watch/...)"),
        lang: str = Query("sub", description="Audio language: 'sub' (JP+EN subs) or 'dub' (EN dub)"),
    ):
        """
        Resolve an anime URL to stream info.
        Returns m3u8 URL, available qualities, subtitles, etc.
        User can then call /download with their preferred format.
        """
        try:
            stream = await resolve_stream(url, audio_lang=lang)
            return {
                "status": "resolved",
                "title": stream.title,
                "episode": stream.episode_number,
                "anime": stream.anime_title,
                "m3u8_url": stream.m3u8_url,
                "qualities": stream.qualities,
                "subtitles": stream.subtitles,
                "intro": stream.intro,
                "outro": stream.outro,
                "download_options": {
                    "mp4": f"/anime/download?url={url}&format=mp4&lang={lang}",
                    "audio": f"/anime/download?url={url}&format=audio&lang={lang}",
                },
            }
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.exception("Resolution failed")
            raise HTTPException(status_code=500, detail=f"Resolution failed: {e}")
    
    @router.get("/download")
    async def download_anime(
        url: str = Query(..., description="Anime page URL"),
        format: OutputFormat = Query(OutputFormat.MP4, description="'mp4' or 'audio'"),
        quality: Quality = Query(Quality.BEST, description="'best', '1080', '720', '360'"),
        lang: str = Query("sub", description="'sub' or 'dub'"),
    ):
        """
        Download anime episode as MP4 or audio-only.
        
        - format=mp4   → full video + audio (.mp4)
        - format=audio  → audio track only (.aac) — for VoxDub samples
        """
        try:
            stream = await resolve_stream(url, audio_lang=lang)
            result = await download_stream(stream, output_format=format, quality=quality)
            
            if result.status == "completed" and result.file_path:
                return FileResponse(
                    path=result.file_path,
                    filename=Path(result.file_path).name,
                    media_type=(
                        "video/mp4" if format == OutputFormat.MP4 
                        else "audio/aac"
                    ),
                )
            else:
                raise HTTPException(
                    status_code=500,
                    detail=f"Download failed: {result.error}",
                )
        
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Download failed")
            raise HTTPException(status_code=500, detail=f"Download failed: {e}")
    
    @router.get("/download/async")
    async def download_anime_async(
        url: str = Query(..., description="Anime page URL"),
        format: OutputFormat = Query(OutputFormat.MP4),
        quality: Quality = Query(Quality.BEST),
        lang: str = Query("sub"),
    ):
        """
        Start download in background, return job_id immediately.
        Poll /status/{job_id} for completion.
        """
        try:
            stream = await resolve_stream(url, audio_lang=lang)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        
        job_id = str(uuid.uuid4())
        
        # Fire and forget
        asyncio.create_task(
            _background_download(job_id, stream, format, quality)
        )
        
        return {
            "job_id": job_id,
            "status": "processing",
            "title": stream.title,
            "format": format.value,
        }
    
    async def _background_download(
        job_id: str, stream: StreamInfo, fmt: OutputFormat, quality: Quality
    ):
        """Background download task."""
        result = await download_stream(stream, output_format=fmt, quality=quality)
        # Store result for polling (in production, use Redis/DB)
        _job_results[job_id] = result
    
    _job_results: dict[str, DownloadResult] = {}
    
    @router.get("/status/{job_id}")
    async def check_status(job_id: str):
        """Check download job status."""
        result = _job_results.get(job_id)
        if not result:
            return {"job_id": job_id, "status": "processing"}
        
        response = {
            "job_id": job_id,
            "status": result.status,
            "format": result.format,
        }
        
        if result.status == "completed":
            response["file_path"] = result.file_path
            response["file_size"] = result.file_size
            response["download_url"] = f"/anime/file/{job_id}"
        elif result.status == "failed":
            response["error"] = result.error
        
        return response
    
    @router.get("/file/{job_id}")
    async def serve_file(job_id: str):
        """Serve a completed download file."""
        result = _job_results.get(job_id)
        if not result or result.status != "completed" or not result.file_path:
            raise HTTPException(status_code=404, detail="File not found")
        
        path = Path(result.file_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="File no longer available")
        
        media_type = "video/mp4" if result.format == "mp4" else "audio/aac"
        return FileResponse(path=str(path), filename=path.name, media_type=media_type)
    
    return router


# ─── Standalone Test ────────────────────────────────────────────────────────────

async def _test():
    """Quick test: resolve a URL and show what we get."""
    import json
    
    test_url = "https://hianimes.se/watch/gachiakuta-episode-5-ykisb7"
    
    print(f"Resolving: {test_url}")
    print("=" * 60)
    
    stream = await resolve_stream(test_url)
    
    print(f"Title:    {stream.title}")
    print(f"Anime:    {stream.anime_title}")
    print(f"Episode:  {stream.episode_number}")
    print(f"M3U8:     {stream.m3u8_url}")
    print(f"Subs:     {len(stream.subtitles)} tracks")
    print(f"Quality:  {json.dumps(stream.qualities, indent=2)}")
    print()
    print("Download options:")
    print(f"  MP4:   /anime/download?url={test_url}&format=mp4")
    print(f"  Audio: /anime/download?url={test_url}&format=audio")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_test())
