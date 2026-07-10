# PROYA VOD Clipper

Local Python pipeline for turning long PROYA skincare livestream VODs into short vertical direct-response clips.

## Core Commands

```powershell
python main.py --test-lm-studio
python main.py --video D:\VOD\livestream.mp4
python main.py --video D:\VOD\livestream.mp4 --skip-transcribe
python main.py --video D:\VOD\livestream.mp4 --skip-transcribe --skip-moments
python main.py --video D:\VOD\livestream.mp4 --skip-transcribe --skip-moments --skip-vision
python main.py --video D:\VOD\livestream.mp4 --max-clips 3 --skip-vision
python main.py --video D:\VOD\livestream.mp4 --cut-only
```

## Queue And Dashboard

```powershell
python video_queue.py --input-dir D:\VOD --state-file working\video_queue_state.json --max-inflight-videos 1 --ffmpeg-max-parallel-clips 2
.\run_new_app.ps1
```

The FastAPI + React app is the production operations and review surface for queue health, scores, compliance, module readiness, rendered clips, queue controls, settings overrides, operational jobs, module review, and read visibility; see [docs/control_app.md](docs/control_app.md).
The Windows Electron portable desktop wrapper is documented in [docs/desktop_app.md](docs/desktop_app.md).
Historical Phase 2 read-only architecture notes remain in [docs/read_first_app.md](docs/read_first_app.md).

Production commands and dashboard mutations share the typed application boundary documented in [docs/backend_boundary.md](docs/backend_boundary.md).

## Setup And Maintenance

```powershell
pip install -r requirements.txt
python main.py --setup-sfx
python main.py --preview-ba
python main.py --video D:\VOD\livestream.mp4 --preview-corrections
python main.py --train-yolo
```

## Modules, Review, And Export

```powershell
python main.py --video D:\VOD\livestream.mp4 --extract-modules-only
python main.py --assemble-modules
python main.py --module-library-report
python main.py --module-review-queue
python main.py --validate-modules-visual-only
python main.py --package-export-batches --dry-run
```

## Tests

```powershell
python -m unittest -v
pytest -q
```

Heavy external dependencies include FFmpeg/FFprobe, LM Studio, CUDA-capable PyTorch, Whisper/WhisperX models, and YOLO weights at `models/proya_best.pt`.
