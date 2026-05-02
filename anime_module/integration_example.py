"""
Integration Guide: Adding anime_extractor to MediaStrip
========================================================

Drop anime_extractor.py into your project root alongside main.py.
Then add these lines to your existing FastAPI app:
"""

# ─── In your main.py ────────────────────────────────────────────────────────────

# Add this import at the top:
# from anime_extractor import create_router as create_anime_router

# Then wherever you set up your routes, add:
# app.include_router(create_anime_router(), prefix="/anime")

# That's it. You now have these endpoints:
#
#   GET /anime/resolve?url=...&lang=sub
#       → Returns stream info + download links (user picks format)
#
#   GET /anime/download?url=...&format=mp4&quality=best&lang=sub
#       → Direct download as MP4 (video + audio)
#
#   GET /anime/download?url=...&format=audio&quality=best&lang=sub
#       → Direct download as audio-only AAC (for VoxDub)
#
#   GET /anime/download/async?url=...&format=mp4
#       → Start background download, returns job_id
#
#   GET /anime/status/{job_id}
#       → Poll job progress
#
#   GET /anime/file/{job_id}
#       → Download completed file


# ─── Minimal standalone server (for testing) ────────────────────────────────────

if __name__ == "__main__":
    from fastapi import FastAPI
    from anime_extractor import create_router
    import uvicorn

    app = FastAPI(title="MediaStrip - Anime Module")
    app.include_router(create_router(), prefix="/anime")

    # CORS for frontend
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    uvicorn.run(app, host="0.0.0.0", port=8000)
