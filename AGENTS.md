# AGENTS.md

Last cleaned: 2026-05-31, from `C:\Data\clipper_test`.

This repository turns long PROYA Indonesian skincare livestream VODs into short vertical commerce clips. Treat it as a local production workflow for operators/editors, not a generic Python library.

## Agent Operating Notes

- The worktree may already be dirty. Generated artifacts include `working/`, `pipeline.log`, `temp_ass/`, `runs/`, `assets.csv`, `highlight_phrases.json`, `__pycache__/`, and caches. Do not revert or delete them unless explicitly asked.
- Use targeted reads/tails for large mutable files, especially `pipeline.log`, `assets.csv`, `highlight_phrases.json`, and `working/`.
- Source files contain Unicode comments/text. Preserve UTF-8 and avoid accidental mojibake.
- Configuration values may be overridden later in `config.py`; check the bottom of the file before assuming an earlier value is active.

## Pipeline Shape

1. Transcribe with Faster-Whisper and optional WhisperX word alignment.
2. Apply Indonesian skincare/brand word corrections.
3. Detect sales moments through local LM Studio.
4. Expand variants, scan product/face events with YOLO, and cut/render with FFmpeg/NVENC.
5. Score, compliance-check, package exports, and optionally build/review modular clip libraries.

## Common Commands

```powershell
python main.py --test-lm-studio
python main.py --video D:\VOD\livestream.mp4
python main.py --video D:\VOD\livestream.mp4 --max-clips 3 --skip-vision
python video_queue.py --input-dir D:\VOD --state-file working\video_queue_state.json
streamlit run app.py
.\run_dashboard.ps1
python -m unittest -v
pytest -q
```

## Important Files

- `main.py`: single-video orchestration and CLI entrypoint.
- `transcriber.py`, `moment_detector.py`, `variation_engine.py`, `vision_scanner.py`, `ffmpeg_editor.py`: core transcription, moment, variant, vision, and render stages.
- `video_queue.py`, `queue_control.py`, `queue_supervisor.py`, `queue_state_health.py`: queue execution, controls, supervision, and health summaries.
- `app.py`: Streamlit dashboard for queue status, review, scoring, compliance, modules, and controls.
- `clip_scorer.py`, `compliance_checker.py`, `export_packager.py`: post-render scoring, policy checks, and affiliate export packaging.
- `module_extractor.py`, `module_assembler.py`, `module_review.py`, `module_visual_validator.py`, `module_readiness.py`, `module_report.py`: modular clip library workflows.
- `config.py`: central settings for paths, models, thresholds, render options, SFX, overlays, scoring, and modules.

## External Expectations

- FFmpeg/FFprobe must be on `PATH`.
- LM Studio should serve the configured OpenAI-compatible endpoint.
- GPU/CUDA paths are expected for Whisper, YOLO, and NVENC unless config is changed.
- `models/proya_best.pt` is the active YOLO inference weights path.
- The Streamlit dashboard is local by default; Cloudflare access setup is documented under `docs/`.

## Safe Change Strategy

- For pipeline logic, run unit tests first, then smoke-test with cached data and a small `--max-clips` value.
- If touching YOLO class behavior, reconcile `config.PRODUCT_CLASSES`, `HOST_FACE_CLASS`, `dataset/proya.yaml`, and active model names.
- If touching rendering, test original and variant clips because variant transforms alter timing and boxes.
- If touching queue scheduling or controls, run both `python -m unittest -v` and `pytest -q`.

## Documentation Cleanup Note

On 2026-05-31, approved markdown cleanup retired the generated Impeccable app critique, the pytest cache README, and the standalone product brief after its guidance was folded into `DESIGN.md`.
