# MediaStrip — Local Dev Guide

## First-time setup

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt
```

Optional (better YouTube extraction — JS challenge solving):
install [deno](https://deno.land) — `winget install DenoLand.Deno`

## Start server

```bash
python -m uvicorn main:app --port 8000
```

Open: http://localhost:8000

## Stop server

`Ctrl+C` in the terminal running uvicorn.

## Notes

- LaMa model (~196MB) downloads automatically on first watermark job;
  cached at `C:\Users\<you>\.cache\torch\hub\checkpoints\big-lama.pt`
- GPU (CUDA) used automatically if available
- Downloads → `./downloads/` · processed files → `./output/`
- Browser extension: see [extension/README.md](extension/README.md)
- Live instance: https://mediastrip.jodlx.in (Railway, auto-deploys from main)
