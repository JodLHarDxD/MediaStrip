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
import uuid
import asyncio
import logging
from enum import Enum
from pathlib import Path
from typing import Optional
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

async def resolve_episode(slug: str) -> dict:
    """
    Step 1: slug → episode metadata via myani.cfd API
    Returns episode link (embed URL), anime info, etc.
    """
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, verify=False) as client:
        resp = await client.get(f"{MYANI_API}/episode/{slug}")
        resp.raise_for_status()
        data = resp.json()
        
        if "episode" not in data:
            raise ValueError(f"Episode not found for slug: {slug}")
        
        return data


async def resolve_embed(embed_url: str) -> int:
    """
    Step 2: embed URL → internal player ID (data-id)
    Fetches the megaplay embed page and extracts data-id from HTML.
    """
    async with httpx.AsyncClient(
        headers={**HEADERS, "Referer": "https://hianimes.se/"},
        timeout=15,
        verify=False,
        follow_redirects=True,
    ) as client:
        resp = await client.get(embed_url)
        resp.raise_for_status()
        
        soup = BeautifulSoup(resp.text, "html.parser")
        player_div = soup.find(attrs={"data-id": True})
        
        if not player_div:
            raise ValueError(f"Could not find data-id in embed page: {embed_url}")
        
        return int(player_div["data-id"])


async def resolve_sources(player_id: int, referer: str) -> dict:
    """
    Step 3: player ID → m3u8 URL via megaplay getSources API
    """
    async with httpx.AsyncClient(
        headers={
            **HEADERS,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=15,
        verify=False,
    ) as client:
        resp = await client.get(
            f"{MEGAPLAY_BASE}/stream/getSources",
            params={"id": player_id},
        )
        resp.raise_for_status()
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
    
    # Step 1: Get episode data
    episode_data = await resolve_episode(slug)
    episode = episode_data["episode"]
    anime = episode_data.get("anime", {})
    
    # Pick sub or dub embed URL
    links = episode.get("link", {})
    lang_links = links.get(audio_lang, links.get("sub", []))
    
    if not lang_links:
        raise ValueError(f"No {audio_lang} links found for episode")
    
    embed_url = lang_links[0]
    logger.info(f"Embed URL: {embed_url}")
    
    # Step 2: Get player ID from embed page
    player_id = await resolve_embed(embed_url)
    logger.info(f"Player ID: {player_id}")
    
    # Step 3: Get m3u8 source
    sources = await resolve_sources(player_id, embed_url)
    m3u8_url = sources["sources"]["file"]
    logger.info(f"M3U8 URL: {m3u8_url}")
    
    # Build subtitles list
    subtitles = []
    for track in sources.get("tracks", []):
        if track.get("kind") == "captions":
            subtitles.append({
                "url": track["file"],
                "label": track.get("label", "Unknown"),
                "default": track.get("default", False),
            })
    
    # Probe available qualities via yt-dlp
    qualities = await _probe_qualities(m3u8_url)
    
    return StreamInfo(
        m3u8_url=m3u8_url,
        title=episode.get("title", slug),
        episode_number=episode.get("episodeNumber", 0),
        anime_title=anime.get("Japanese", anime.get("_id", "Unknown")),
        subtitles=subtitles,
        qualities=qualities,
        intro=sources.get("intro"),
        outro=sources.get("outro"),
    )


async def _probe_qualities(m3u8_url: str) -> list:
    """Probe available quality levels from the m3u8."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--no-check-certificates", "--dump-json", m3u8_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        
        if proc.returncode == 0:
            import json
            data = json.loads(stdout)
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
        cmd = _build_audio_cmd(stream.m3u8_url, str(output_file), quality)
    else:
        output_file = job_dir / f"{safe_title}.mp4"
        cmd = _build_video_cmd(stream.m3u8_url, str(output_file), quality)
    
    logger.info(f"Downloading [{output_format.value}]: {' '.join(cmd)}")
    
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            error_msg = stderr.decode(errors="replace")[-500:]
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


def _build_video_cmd(m3u8_url: str, output: str, quality: Quality) -> list:
    """Build yt-dlp command for full MP4 download."""
    cmd = [
        "yt-dlp",
        "--no-check-certificates",
        "--no-warnings",
        "-o", output,
    ]
    
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


def _build_audio_cmd(m3u8_url: str, output: str, quality: Quality) -> list:
    """Build yt-dlp command for audio-only extraction."""
    cmd = [
        "yt-dlp",
        "--no-check-certificates",
        "--no-warnings",
        "-x",                          # extract audio
        "--audio-format", "aac",       # output as AAC (good for VoxDub processing)
        "--audio-quality", "0",        # best audio quality
        "-o", output,
    ]
    
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
