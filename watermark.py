import asyncio
import os
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

PRESETS: dict[str, list[dict]] = {
    "tiktok": [
        {"xf": 0.75, "yf": 0.88, "wf": 0.25, "hf": 0.12},
        {"xf": 0.00, "yf": 0.78, "wf": 0.60, "hf": 0.08},
    ],
    "instagram": [
        {"xf": 0.00, "yf": 0.88, "wf": 0.55, "hf": 0.12},
        {"xf": 0.40, "yf": 0.15, "wf": 0.60, "hf": 0.30},
    ],
    "youtube": [
        {"xf": 0.78, "yf": 0.88, "wf": 0.22, "hf": 0.12},
    ],
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tiff"}

NUM_SAMPLE_FRAMES = 30
VARIANCE_THRESHOLD = 8.0
MAX_COMPONENT_AREA_RATIO = 0.15
MIN_COMPONENT_AREA_PX = 50

# LaMa singleton — loaded once, reused across all jobs
_lama_instance = None
_lama_available = None  # None=not tried, True=loaded, False=failed


def _get_lama():
    global _lama_instance, _lama_available
    if _lama_available is False:
        return None
    if _lama_instance is not None:
        return _lama_instance
    try:
        import torch  # noqa: F401
        from simple_lama_inpainting import SimpleLama
        _lama_instance = SimpleLama()  # auto-detects CUDA
        _lama_available = True
        return _lama_instance
    except Exception:
        _lama_available = False
        return None


def _build_mask(presets: list[dict], w: int, h: int) -> np.ndarray:
    mask = np.zeros((h, w), dtype=np.uint8)
    for p in presets:
        x = int(p["xf"] * w)
        y = int(p["yf"] * h)
        bw = int(p["wf"] * w)
        bh = int(p["hf"] * h)
        mask[y : y + bh, x : x + bw] = 255
    return mask


def _lama_inpaint_frame(lama, bgr_frame: np.ndarray, mask: np.ndarray) -> np.ndarray:
    rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
    pil_image = Image.fromarray(rgb)
    pil_mask = Image.fromarray(mask)
    result_pil = lama(pil_image, pil_mask)
    return cv2.cvtColor(np.array(result_pil), cv2.COLOR_RGB2BGR)


def _detect_watermark_mask(cap: cv2.VideoCapture, w: int, h: int) -> np.ndarray | None:
    """
    Sample frames, compute per-pixel temporal std deviation.
    Low-variance pixels = static overlay = candidate watermark region.
    """
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total < 5:
        return None

    n = min(NUM_SAMPLE_FRAMES, total)
    sample_indices = np.linspace(0, total - 1, n, dtype=int)
    frames_gray = []

    for idx in sample_indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ret, frame = cap.read()
        if ret:
            frames_gray.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32))

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    if len(frames_gray) < 5:
        return None

    stack = np.stack(frames_gray, axis=0)
    std_map = np.std(stack, axis=0)
    raw_mask = (std_map < VARIANCE_THRESHOLD).astype(np.uint8) * 255

    # Remove components that are too small (noise) or too large (static background)
    frame_area = w * h
    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(raw_mask)
    filtered = np.zeros_like(raw_mask)
    for label in range(1, n_labels):
        area = stats[label, cv2.CC_STAT_AREA]
        if MIN_COMPONENT_AREA_PX <= area <= MAX_COMPONENT_AREA_RATIO * frame_area:
            filtered[labels == label] = 255

    if not np.any(filtered):
        return None

    # Morphological cleanup — scale kernels with resolution
    scale = max(1, min(w, h) // 480)
    k_close = cv2.getStructuringElement(cv2.MORPH_RECT, (15 * scale, 15 * scale))
    k_dilate = cv2.getStructuringElement(cv2.MORPH_RECT, (7 * scale, 7 * scale))
    closed = cv2.morphologyEx(filtered, cv2.MORPH_CLOSE, k_close)
    final = cv2.dilate(closed, k_dilate)

    # Sanity check: reject degenerate masks
    coverage = np.count_nonzero(final) / frame_area
    if not (0.0001 <= coverage <= 0.20):
        return None

    return final


async def remove_watermark(input_path: Path, output_path: Path, platform: str, queue: asyncio.Queue):
    presets = PRESETS.get(platform.lower(), PRESETS["tiktok"])
    is_image = input_path.suffix.lower() in IMAGE_EXTS
    try:
        if is_image:
            await _process_image(input_path, output_path, presets, queue)
        else:
            await _process_video(input_path, output_path, presets, queue)
    except Exception as e:
        await queue.put({"type": "error", "message": f"{type(e).__name__}: {e}"})


async def _process_image(input_path: Path, output_path: Path, presets: list[dict], queue: asyncio.Queue):
    await queue.put({"type": "log", "value": f"Processing image {input_path.name}..."})
    await queue.put({"type": "filename", "value": input_path.name})
    await queue.put({"type": "progress", "percent": 10})

    img = cv2.imread(str(input_path))
    h, w = img.shape[:2]
    mask = _build_mask(presets, w, h)

    await queue.put({"type": "progress", "percent": 30})

    lama = _get_lama()
    if lama is not None:
        await queue.put({"type": "log", "value": "Using LaMa AI inpainting (GPU)..."})
        result = _lama_inpaint_frame(lama, img, mask)
    else:
        await queue.put({"type": "log", "value": "LaMa unavailable — using TELEA fallback..."})
        result = cv2.inpaint(img, mask, inpaintRadius=5, flags=cv2.INPAINT_TELEA)

    await queue.put({"type": "progress", "percent": 90})

    result_rgb = cv2.cvtColor(result, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(result_rgb)
    save_kw: dict = {}
    if output_path.suffix.lower() in (".jpg", ".jpeg"):
        save_kw["quality"] = 95
    pil_img.save(output_path, **save_kw)

    await queue.put({"type": "progress", "percent": 100})
    await queue.put({"type": "done", "filename": str(output_path), "files": [str(output_path)]})


async def _process_video(input_path: Path, output_path: Path, presets: list[dict], queue: asyncio.Queue):
    await queue.put({"type": "log", "value": f"Opening {input_path.name}..."})
    await queue.put({"type": "filename", "value": input_path.name})

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        await queue.put({"type": "error", "message": "Could not open video file"})
        return

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    await queue.put({"type": "log", "value": f"Resolution: {w}x{h}, {total_frames} frames @ {fps:.1f}fps"})
    await queue.put({"type": "log", "value": "Analyzing frames for watermark detection..."})
    await queue.put({"type": "progress", "percent": 5})

    mask = _detect_watermark_mask(cap, w, h)

    if mask is not None:
        await queue.put({"type": "log", "value": "Watermark region detected automatically."})
    else:
        await queue.put({"type": "log", "value": "Auto-detection inconclusive — using platform preset."})
        mask = _build_mask(presets, w, h)

    await queue.put({"type": "progress", "percent": 10})

    lama = _get_lama()
    if lama is not None:
        await queue.put({"type": "log", "value": "Using LaMa AI inpainting (GPU) for all frames..."})
    else:
        await queue.put({"type": "log", "value": "LaMa unavailable — using TELEA inpainting (CPU)..."})

    temp_path = output_path.with_suffix(".tmp.mp4")
    fourcc = cv2.VideoWriter.fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(temp_path), fourcc, fps, (w, h))

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if lama is not None:
            inpainted = _lama_inpaint_frame(lama, frame, mask)
        else:
            inpainted = cv2.inpaint(frame, mask, inpaintRadius=5, flags=cv2.INPAINT_TELEA)

        writer.write(inpainted)
        frame_idx += 1

        if total_frames > 0 and frame_idx % max(1, total_frames // 20) == 0:
            pct = 10.0 + min(80.0, (frame_idx / total_frames) * 80)
            await queue.put({"type": "progress", "percent": pct})
            await queue.put({"type": "log", "value": f"Processed {frame_idx}/{total_frames} frames"})
            await asyncio.sleep(0)

    cap.release()
    writer.release()
    await asyncio.sleep(0.5)  # let Windows release the file handle before ffmpeg reads it

    await queue.put({"type": "log", "value": "Merging audio track..."})
    await queue.put({"type": "progress", "percent": 92})

    merge_cmd = [
        "ffmpeg", "-y",
        "-i", str(temp_path),
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "1:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "copy",
        str(output_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *merge_cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()

    try:
        os.remove(temp_path)
    except OSError:
        pass

    if proc.returncode == 0:
        await queue.put({"type": "progress", "percent": 100})
        await queue.put({"type": "done", "filename": str(output_path), "files": [str(output_path)]})
    else:
        await queue.put({"type": "error", "message": "ffmpeg audio merge failed"})
