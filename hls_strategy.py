"""
hls_strategy.py — adaptive transport layer for Cloudflare-fronted HLS.

Anime CDNs (mewstream et al.) sit behind Cloudflare and change what they accept
without notice: TLS fingerprint checks, fetch-metadata headers, downloader
sniffing. A single hardcoded header recipe breaks every time they tweak it.

This module makes the *transport* self-healing for the two recurring failure
classes:

  Class A (blocked):  manifest 403s — wrong fingerprint/headers/downloader.
  Class B (expired):  signed URL 404/410s — token died, needs a fresh resolve.

How it adapts:
  - A ladder of download STRATEGIES (impersonate target + header set), ordered
    cheap→robust.
  - A ~1s curl_cffi probe of the master manifest tests a strategy before any
    multi-minute download commits to it. curl_cffi's verdict predicts yt-dlp's.
  - The winning strategy is cached per CDN host and tried first next time, so
    the tool tunes itself to each site.
  - Blocked vs expired is classified from the HTTP status so the caller knows
    whether to advance the ladder (blocked) or re-resolve a fresh URL (expired).

No new heavy dependencies — reuses curl_cffi (already required by the resolver)
and yt-dlp. Everything degrades gracefully if curl_cffi is missing.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("hls_strategy")

try:
    from curl_cffi.requests import AsyncSession as _CurlSession
    _CURL_AVAILABLE = True
except Exception:
    _CURL_AVAILABLE = False


# Browser fetch-metadata headers Cloudflare expects on a cross-site media fetch.
_SECFETCH = {
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}
# Fuller header set for stricter edges.
_SUPERSET = {
    **_SECFETCH,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass(frozen=True)
class Strategy:
    """One transport disguise: a TLS-impersonation target + extra request headers.
    prefer_native keeps yt-dlp on its own HLS downloader — ffmpeg can't impersonate
    and 403s every segment."""
    name: str
    impersonate: str | None
    headers: dict[str, str]
    prefer_native: bool = True


# Ordered cheap/proven -> broader. #1 is the empirically verified winner for
# mewstream as of this writing; the rest are fallbacks for when it rotates.
STRATEGIES: list[Strategy] = [
    Strategy("chrome+secfetch", "chrome", _SECFETCH),
    Strategy("safari+secfetch", "safari", _SECFETCH),
    Strategy("chrome+superset", "chrome", _SUPERSET),
]
_BY_NAME = {s.name: s for s in STRATEGIES}


@dataclass
class PickResult:
    status: str                       # "ok" | "blocked" | "expired" | "skip"
    strategy: Strategy | None
    tried: list[tuple[str, int]] = field(default_factory=list)
    cf_ray: str | None = None

    def summary(self) -> str:
        parts = ", ".join(f"{name}={code}" for name, code in self.tried)
        return parts or "(no probe)"


# ── learned cache ────────────────────────────────────────────────────────────

_CACHE_PATH = Path(os.getenv("MS_HLS_CACHE", "./media/.hls_strategy.json"))


def _load_cache() -> dict[str, str]:
    try:
        return json.loads(_CACHE_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(cache), "utf-8")
    except Exception as e:  # best-effort; ephemeral hosts may be read-only
        logger.debug("hls cache save failed: %s", e)


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def ordered_strategies(host: str) -> list[Strategy]:
    """Strategies to try, learned winner for *host* first."""
    cached = _load_cache().get(host)
    if cached and cached in _BY_NAME:
        winner = _BY_NAME[cached]
        return [winner, *[s for s in STRATEGIES if s.name != cached]]
    return list(STRATEGIES)


def record_winner(host: str, name: str) -> None:
    cache = _load_cache()
    if cache.get(host) != name:
        cache[host] = name
        _save_cache(cache)


# ── status classification ────────────────────────────────────────────────────

def classify(code: int) -> str:
    """ok = usable, blocked = try another disguise, expired = re-resolve URL."""
    if code in (200, 206):
        return "ok"
    if code in (403, 401, 429):
        return "blocked"
    if code in (404, 410):
        return "expired"
    return "blocked"  # unknown -> treat as blocked, advance the ladder


# ── probing + selection ──────────────────────────────────────────────────────

async def _probe(m3u8: str, referer: str | None, strat: Strategy) -> tuple[int, str | None]:
    """Cheap GET of the manifest with one strategy. Returns (status, cf_ray)."""
    headers = dict(strat.headers)
    if referer:
        headers["Referer"] = referer
    async with _CurlSession() as session:
        r = await session.get(
            m3u8, headers=headers, impersonate=strat.impersonate or "chrome", timeout=15
        )
        return r.status_code, r.headers.get("cf-ray")


async def pick_strategy(m3u8: str, referer: str | None) -> PickResult:
    """Walk the (learned-ordered) ladder, returning the first strategy whose
    manifest probe succeeds. Stops early and reports 'expired' if the URL itself
    is dead (re-resolve needed). 'skip' when curl_cffi is unavailable."""
    if not _CURL_AVAILABLE:
        return PickResult("skip", STRATEGIES[0])

    host = _host(m3u8)
    tried: list[tuple[str, int]] = []
    last_ray: str | None = None
    for strat in ordered_strategies(host):
        try:
            code, ray = await _probe(m3u8, referer, strat)
        except Exception as e:
            logger.debug("probe %s failed: %s", strat.name, e)
            tried.append((strat.name, -1))
            continue
        tried.append((strat.name, code))
        last_ray = ray or last_ray
        verdict = classify(code)
        if verdict == "ok":
            record_winner(host, strat.name)
            return PickResult("ok", strat, tried, last_ray)
        if verdict == "expired":
            return PickResult("expired", None, tried, last_ray)
    return PickResult("blocked", None, tried, last_ray)


# ── yt-dlp flag rendering ────────────────────────────────────────────────────

def ytdlp_flags(strat: Strategy, referer: str | None, impersonate_ok: bool = True) -> list[str]:
    """Render a strategy as yt-dlp CLI flags."""
    flags: list[str] = []
    if strat.impersonate and impersonate_ok:
        flags += ["--impersonate", strat.impersonate]
    else:
        # No curl_cffi -> the (weaker) extractor-arg form; better than nothing.
        flags += ["--extractor-args", "generic:impersonate"]
    if strat.prefer_native:
        flags += ["--hls-prefer-native"]
    for key, value in strat.headers.items():
        flags += ["--add-header", f"{key}:{value}"]
    if referer:
        flags += ["--add-header", f"Referer:{referer}"]
    return flags
