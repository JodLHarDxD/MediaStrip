"""Regression tests for the anime m3u8 Cloudflare-403 + 'Unsupported URL' crash.

Pure-logic (no network). Pins the proven fixes:
  Bug A: the 403 re-resolve fallback must NOT treat the bare CDN host
         (https://megaplay.buzz/) as a re-resolvable player page.
  Bug B: Cloudflare-fronted HLS needs real Chrome impersonation + the NATIVE
         HLS downloader + Sec-Fetch headers. ffmpeg-as-downloader and the
         --extractor-args impersonate form both 403 (verified live).

Run: python test_anime_403_fix.py   (or: python -m pytest test_anime_403_fix.py)
"""
import downloader
from anime_extractor import parse_megaplay_url, parse_anime_url

M3U8 = "https://cdn.mewstream.buzz/anime/abc/def/master.m3u8"
REF = "https://megaplay.buzz/"


def test_manifest_detection():
    assert downloader._is_manifest_url(M3U8) is True
    assert downloader._is_manifest_url("https://x/v.mpd?t=1") is True
    assert downloader._is_manifest_url("https://youtube.com/watch?v=a") is False


def test_hls_flags_carry_impersonation_native_and_secfetch():
    flags = downloader._hls_cloudflare_flags(M3U8)
    joined = " ".join(flags)
    # native downloader (ffmpeg can't impersonate -> 403)
    assert "--hls-prefer-native" in flags
    # real impersonation, not the inert extractor-arg form (when curl_cffi present)
    if downloader._IMPERSONATE_OK:
        assert "--impersonate" in flags and "chrome" in flags
    # browser fetch-metadata headers Cloudflare requires
    assert "Sec-Fetch-Dest:empty" in joined
    assert "Sec-Fetch-Mode:cors" in joined
    assert "Sec-Fetch-Site:cross-site" in joined


def test_non_manifest_gets_no_hls_flags():
    assert downloader._hls_cloudflare_flags("https://youtube.com/watch?v=a") == []


def test_context_flags_compose_referer_and_hls():
    flags = downloader._ytdlp_context_flags(M3U8, referer=REF, jar_path=None)
    assert "--hls-prefer-native" in flags
    assert f"Referer:{REF}" in flags
    # plain video keeps Referer but gets none of the HLS machinery
    yt = downloader._ytdlp_context_flags("https://youtube.com/watch?v=a", referer="https://youtube.com/", jar_path=None)
    assert "--hls-prefer-native" not in yt
    assert not any("Sec-Fetch" in f for f in yt)


def test_bare_cdn_host_is_not_reresolvable():
    # Bug A: anime path's referer is the bare host -> must match NO resolver,
    # so the 403 fallback won't feed it to yt-dlp's generic extractor.
    assert parse_megaplay_url("https://megaplay.buzz/") is False
    assert parse_anime_url("https://megaplay.buzz/") is None


def test_sniffed_stream_url_is_reresolvable():
    assert parse_megaplay_url("https://megaplay.buzz/stream/s-2/161029/sub") is True


# ── adaptive strategy ladder ─────────────────────────────────────────────────
import hls_strategy as hs


def test_status_classification():
    assert hs.classify(200) == "ok"
    assert hs.classify(206) == "ok"
    assert hs.classify(403) == "blocked"
    assert hs.classify(429) == "blocked"
    assert hs.classify(404) == "expired"
    assert hs.classify(410) == "expired"
    assert hs.classify(500) == "blocked"  # unknown -> advance the ladder


def test_ytdlp_flags_render_impersonate_native_headers():
    strat = hs.STRATEGIES[0]  # chrome+secfetch
    flags = hs.ytdlp_flags(strat, "https://megaplay.buzz/", impersonate_ok=True)
    assert "--impersonate" in flags and "chrome" in flags
    assert "--hls-prefer-native" in flags
    assert "Referer:https://megaplay.buzz/" in flags
    assert any(f.startswith("Sec-Fetch-") for f in flags)
    # no curl_cffi -> degrade to extractor-arg form, never a bare impersonate
    deg = hs.ytdlp_flags(strat, None, impersonate_ok=False)
    assert "--impersonate" not in deg
    assert "generic:impersonate" in deg


def test_learned_winner_leads_the_ladder(tmp_path=None, monkeypatch=None):
    # learned host -> that strategy is tried first; others follow
    host = "cdn.example.test"
    orig_load = hs._load_cache
    hs._load_cache = lambda: {host: "safari+secfetch"}
    try:
        order = hs.ordered_strategies(host)
        assert order[0].name == "safari+secfetch"
        assert {s.name for s in order} == {s.name for s in hs.STRATEGIES}
    finally:
        hs._load_cache = orig_load


def test_unknown_host_keeps_default_order():
    order = hs.ordered_strategies("nope.unknown.test")
    assert [s.name for s in order] == [s.name for s in hs.STRATEGIES]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} passed")
