# AGENTS.md

Last scanned: 2026-05-02, from `C:\Data\clipper_test`.

This repository is a Python automation pipeline for turning long PROYA skincare livestream VODs into short vertical clips for TikTok-style direct-response/social commerce use. It is built for Indonesian livestream content, product-selling moment detection, product/face zooms, karaoke subtitles, before/after overlays, SFX, and high-throughput variant rendering.

## Agent Operating Notes

- The worktree was already dirty before this file was created. Many modified/untracked files are generated artifacts (`working/`, `pipeline.log`, `__pycache__/`, caches, output state). Do not revert or delete them unless the user explicitly asks.
- `rg.exe` existed on this machine but returned `Access is denied` during the scan. Use PowerShell `Get-ChildItem` / `Select-String` fallback if that persists.
- The project has no `.gitignore` in the root as scanned. Large generated files and caches may be tracked or untracked.
- Avoid opening all of `pipeline.log`, `assets.csv`, `highlight_phrases.json`, or `working/` unless needed. Use targeted reads/tails because these files are large and/or mutable.
- Source files contain Unicode comments/text. If editing, preserve UTF-8 and avoid accidental mojibake. This `AGENTS.md` is intentionally ASCII.

## Project Overview

The app processes raw livestream videos through:

1. Speech transcription using `faster-whisper`.
2. Optional WhisperX word-level forced alignment for karaoke-grade timestamps.
3. Brand/product word correction for Indonesian skincare vocabulary.
4. LLM-based sales-moment detection through a local LM Studio OpenAI-compatible endpoint.
5. Optional variant expansion, currently configured for 6 variants per detected moment.
6. YOLOv8 product/host-face scanning around candidate moments.
7. FFmpeg raw clip cutting, overlay planning, subtitle generation, zoom rendering, SFX mixing, and NVENC final encoding.
8. Optional queue orchestration and Streamlit monitoring dashboard.

Primary users are operators/editors generating many PROYA 5X Vitamin C livestream clips from local VOD folders. Future agents should treat this as a local production workflow, not a generic library.

## Tech Stack

### Detected Runtime On This Machine

- OS/shell: Windows, PowerShell.
- Python: `3.11.9`.
- GPU: `NVIDIA GeForce RTX 5090`, driver `596.21`, `32607 MiB` VRAM.
- FFmpeg: `2026-04-16-git-5abc240a27`, Gyan essentials build, with `libx264`, `libass`, `libfreetype`, CUDA/NVENC/NVDEC support.
- FFprobe: same FFmpeg build.
- Torch: `2.12.0.dev20260408+cu128`.

### Python Packages Detected

- `faster-whisper==1.2.1`
- `whisperx==3.1.1`
- `ultralytics==8.4.38`
- `moviepy==1.0.3`
- `opencv-python==4.13.0.92`
- `openai==2.32.0`
- `pillow==11.3.0`
- `streamlit==1.56.0`
- `tqdm==4.67.3`
- `numpy==1.26.4`
- `pandas==3.0.2`
- `psutil==7.2.2`
- `altair==6.0.0`
- `pytest==9.0.3`

### Requirements File

`requirements.txt` declares:

- `faster-whisper>=1.0.0`
- `whisperx>=3.8.5`
- `ultralytics>=8.2.0`
- `moviepy>=1.0.3`
- `opencv-python>=4.9.0`
- `openai>=1.30.0`
- `pillow>=10.0.0`
- `streamlit>=1.35.0`
- `tqdm>=4.66.0`
- `numpy>=1.26.0`

Important mismatch: the installed `whisperx` is `3.1.1`, lower than the declared `>=3.8.5`. The app works around WhisperX import/alignment issues, so test carefully before upgrading WhisperX. Also, `app.py` imports `altair`, `pandas`, and `psutil`, but those are not explicitly listed in `requirements.txt`.

## Install And Setup

Recommended local setup:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pandas psutil altair pytest
```

External dependencies:

- FFmpeg and FFprobe must be on `PATH`.
- NVIDIA driver/CUDA-capable PyTorch are expected for the configured GPU path.
- LM Studio must be running a local OpenAI-compatible server.
- Hugging Face/model download access may be required for `faster-whisper`, WhisperX, and the Indonesian align model unless already cached.
- YOLO weights must exist at `models/proya_best.pt` for product scanning.

No `.env` files were present. Configuration lives in `config.py`; the only runtime environment flag seen in code is `PROYA_QUEUE_FFMPEG_BELOW_NORMAL`, set by `video_queue.py` to lower FFmpeg process priority on Windows.

## Run Commands

Test LM Studio:

```powershell
python main.py --test-lm-studio
```

Run one full video:

```powershell
python main.py --video D:\VOD\livestream.mp4
```

Resume from cached stages:

```powershell
python main.py --video D:\VOD\livestream.mp4 --skip-transcribe
python main.py --video D:\VOD\livestream.mp4 --skip-transcribe --skip-moments
python main.py --video D:\VOD\livestream.mp4 --skip-transcribe --skip-moments --skip-vision
```

Render fewer clips for a smoke test:

```powershell
python main.py --video D:\VOD\livestream.mp4 --max-clips 3 --skip-vision
```

Cut only, without final overlays/editing:

```powershell
python main.py --video D:\VOD\livestream.mp4 --cut-only
```

Queue all videos in a folder:

```powershell
python video_queue.py --input-dir D:\VOD --state-file working\video_queue_state.json --max-inflight-videos 1 --ffmpeg-max-parallel-clips 2
```

Run the Streamlit dashboard:

```powershell
streamlit run app.py
```

Train YOLO:

```powershell
python main.py --train-yolo
```

Create/check SFX folders:

```powershell
python main.py --setup-sfx
```

Preview word corrections:

```powershell
python main.py --video D:\VOD\livestream.mp4 --preview-corrections
```

Import rendered clip inventory into `assets.csv`:

```powershell
python import_assets.py --clips-dir D:\output_clips --output assets.csv
```

## Tests

Existing tests are standard `unittest` files and can also be run with pytest:

```powershell
python -m unittest -v
pytest -q
```

Observed on 2026-05-02:

- `python -m unittest -v`: 6 tests passed.
- `pytest -q`: 6 tests passed, with one warning because pytest could not create `.pytest_cache` due Windows `Access is denied`.

The tests cover queue scheduling/backfill behavior, tagged transcript reuse, FFmpeg progress persistence, and deterministic six-pack variant generation. They do not exercise full transcription, LM Studio, YOLO inference, or FFmpeg rendering.

## Pipeline Details

1. `main.run_pipeline()` validates `video_path`, builds `working/<video_stem>` and `OUTPUT_DIR/<video_stem>` paths, and logs to `pipeline.log`.
2. `transcriber.transcribe()` loads `working/<stem>/transcript.json` if compatible. Otherwise it runs `faster-whisper`, writes `transcript.raw_checkpoint.json`, then aligns with WhisperX if configured.
3. WhisperX alignment defaults to a subprocess via `WHISPERX_ALIGN_IN_SUBPROCESS=True`. Temporary aligned output is `transcript.aligned_subprocess.json` and is removed after loading.
4. `word_corrector.apply_corrections_to_transcript()` mutates transcript segment text and word tokens using `config.WORD_CORRECTIONS`.
5. `transcriber.build_text_chunks()` splits transcript segments into overlapping chunks using `CHUNK_DURATION` and `CHUNK_OVERLAP`.
6. `moment_detector.detect_moments()` sends each chunk to LM Studio, parses JSON, validates timestamps/score/speech density/product focus, builds hook overlay payloads, deduplicates by temporal IoU, sorts by score, assigns `clip_0001` IDs, and caches `moments.json`.
7. `variation_engine.expand_moments_with_variants()` clones each moment when `VARIANTS_PER_CLIP > 1`, attaching a `_variant` object. Current config uses `VARIANTS_PER_CLIP=6` and `VARIANT_SEED=42`.
8. `vision_scanner.build_scan_ranges_from_moments()` creates padded scan windows around moments when `YOLO_SCAN_ONLY_MOMENTS=True`.
9. `vision_scanner.scan_video_for_products()` opens the video with OpenCV, scans ROI frames with Ultralytics YOLO, maps boxes back to full-frame coordinates, groups detections into events, and caches `product_detections.json`.
10. For each expanded moment, `main._process_clip_job()` raw-cuts using `variation_engine.cut_raw_clip_with_variant()` if available; otherwise it falls back to `ffmpeg_editor.cut_raw_clip()`.
11. For final edits, `ffmpeg_editor.edit_clip()` probes the raw cut, extracts clip-relative words/events, creates ASS karaoke subtitles, learns highlight phrases into `highlight_phrases.json`, plans product/face zooms, adds before/after and emoji overlays, builds SFX events, assembles one FFmpeg `filter_complex`, and encodes output.
12. `main.run_pipeline()` writes `manifest.json` in the output folder with clip IDs, variant dirs, hook/product/type/score/timing/status, and progress callbacks for queue state.

## Module And File Structure

### Root Source Files

- `app.py`: Streamlit dashboard for `video_queue_state.json`; renders overview, video table, analytics, queues, settings, and system/GPU metrics.
- `config.py`: Central mutable configuration for paths, LM Studio, Whisper/WhisperX, YOLO, clip scoring, fonts, overlays, SFX, FFmpeg/NVENC, and variation count.
- `ffmpeg_editor.py`: Main rendering backend; public API is `cut_raw_clip`, `edit_clip`, and `get_words_for_clip`; owns ASS subtitles, highlight learning, zoom plans, overlays, SFX filter inputs, and FFmpeg execution.
- `hook_text.py`: Deterministic hook headline/subtext/CTA generator based on moment metadata, pain/benefit/proof regex patterns, and stable seeded picks.
- `import_assets.py`: CLI importer that scans rendered output folders and manifests into `assets.csv` for downstream assignment/upload tracking.
- `main.py`: Single-video CLI entrypoint and orchestration for transcription, moment detection, variation expansion, vision scan, raw cuts, final editing, and manifest writing.
- `moment_detector.py`: LM Studio client and quality-first moment detector; parses model JSON, validates transcript windows, filters weak/repetitive/non-sales content, and writes `moments.json`.
- `sfx_player.py`: SFX trigger planner plus older MoviePy mixer helper; current FFmpeg editor uses the planned events to add SFX inputs in FFmpeg.
- `transcriber.py`: Faster-Whisper transcription, raw checkpointing, WhisperX alignment, fallback handling, cache compatibility, chunk building, and word timing validation.
- `variation_engine.py`: Deterministic variant config generator and FFmpeg raw-cut transform helper for mirror, subtitle style, zoom offset/scale, color grade, speed, crop, and hook style.
- `video_queue.py`: Threaded, persistent multi-video queue runner with separate GPU analysis, YOLO, and FFmpeg queues; stores resumable state in JSON.
- `vision_scanner.py`: YOLOv8 training/inference helper; scans video frames inside ROI, groups detections into schema-versioned events, and filters events for clips.
- `word_corrector.py`: Regex-based transcript/subtitle correction helpers driven by `config.WORD_CORRECTIONS`.
- `test_variation_engine.py`: Unit test for deterministic distinct six-variant style generation.
- `test_video_queue.py`: Unit tests for queue scheduling, tagged redo transcript reuse, and FFmpeg progress state persistence.

### Root Data/Config/Artifact Files

- `README.MD`: Command cheat sheet for pipeline, skip flags, LM Studio test, YOLO training, SFX setup, and Streamlit UI. Terminal rendering shows mojibake, so preserve encoding when editing.
- `requirements.txt`: Minimal Python dependency constraints; currently incomplete for dashboard/test convenience.
- `highlight_phrases.json`: Mutable highlight phrase registry with `version` and `categories` (`benefit`, `result`, `pain`). The renderer learns new phrases during clip rendering.
- `assets.csv`: Large generated inventory, 32,029 rows at scan time, with rendered clip paths under `D:\output_clips`, assignment/upload columns, manifest metadata, and import timestamps.
- `pipeline.log`: Large append-only pipeline/queue log, about 67 MB at scan time. Tail or search it, do not read it wholesale.
- `yolov8n.pt`: YOLOv8 nano pretrained base model used by training.
- `AGENTS.md`: This agent guide.

### `assets/`

- `assets/before_after/`: 42 PNG before/after/result images. `ffmpeg_editor._pick_before_after()` randomly selects an image from here when enabled.
- `assets/emojis/`: 9 PNG emoji overlays (`100.png`, `cry.png`, `eyes.png`, `panda.png`, `scared.png`, `shock.png`, `sun.png`, `sun2.png`, `sun3.png`). `config.EMOJI_CONFIG` references these by keyword rules; `eye.png` is resolved by fallback to `eyes.png`.
- `assets/fonts/`: 5 TTF fonts used by ASS/drawtext: Anton, Montserrat Bold/ExtraBold/SemiBold, and Playfair Display Italic variable font.
- `assets/sfx/product_zoom/`: Product zoom SFX audio files.
- `assets/sfx/highlight_yellow/`: Benefit/attention highlight SFX audio files.
- `assets/sfx/highlight_green/`: Result/proof highlight SFX audio files.
- `assets/sfx/highlight_red/`: Empty SFX folder at scan time; used for pain/problem highlights if files are added.
- `assets/red_arrow.mp4`, `assets/red_arrow_fixed.mp4`, `assets/red_arrow_nogreen.mp4`: Red arrow video assets present in repo; no active code reference was found in the scan.

### `dataset/`

- `dataset/proya.yaml`: Roboflow/YOLO dataset config. Declares `nc: 6` and names `cleanser`, `eye cream`, `host face`, `serum`, `skin cream`, `toner`.
- `dataset/train/images/`: 730 training JPGs.
- `dataset/train/labels/`: 730 YOLO label TXT files.
- `dataset/train/labels.cache`: Ultralytics label cache.
- `dataset/valid/images/`: 265 validation JPGs.
- `dataset/valid/labels/`: 265 validation YOLO label TXT files.
- `dataset/valid/labels.cache`: Ultralytics validation label cache.

### `models/`

- `models/proya_best.pt`: Active trained YOLO weights configured by `config.YOLO_WEIGHTS`.

### `runs/`

- `runs/detect/models/proya_detector*/args.yaml`: Ultralytics training run settings/artifacts.
- `runs/detect/models/proya_detector7/` and `proya_detector8/`: Complete training run outputs with plots, confusion matrices, `results.csv`, `weights/best.pt`, and `weights/last.pt`.
- `runs/detect/models/proya_detector` through `proya_detector6`: Only `args.yaml` files were present at scan time.

### `working/`

Generated cache/state area. At scan time it contained 96 per-run directories, 373 JSON files, and 30 MP4 raw-cut files.

- `working/video_queue_state.json`: Persistent state for `video_queue.py` and data source for `app.py`.
- `working/<video_stem>/transcript.json`: Final transcript with segments, flattened words, and metadata.
- `working/<video_stem>/transcript.raw_checkpoint.json`: Raw faster-whisper checkpoint used for resume and WhisperX fallback.
- `working/<video_stem>/moments.json`: Validated LLM moments cache.
- `working/<video_stem>/product_detections.json`: YOLO event cache.
- `working/<video_stem>/raw_cuts/*.mp4`: Intermediate raw cut clips; normally removed after final rendering, but may remain after interrupted runs.

### `temp_ass/`

- `temp_ass/sub_*.ass`: Temporary ASS subtitle files generated by `ffmpeg_editor._write_ass_file()`. The editor tries to delete them after rendering, but interrupted or active renders can leave files behind.

### `__pycache__/`

- Python bytecode caches. Some `.pyc` files are tracked/modified in git; treat as generated unless the user asks about them.

## Key Config Values And Assumptions

- `INPUT_VIDEO = "D:\VOD"` and `video_queue.py --input-dir` default to `D:\VOD`.
- `OUTPUT_DIR = "D:\output_clips"`.
- `WORKING_DIR = "working"`.
- `LM_STUDIO_BASE_URL = "http://localhost:1234/v1"`.
- `LM_STUDIO_API_KEY = "lm-studio"`.
- `LM_STUDIO_MODEL = "qwen/qwen3.6-27b"`.
- `WHISPER_MODEL_SIZE = "large-v3-turbo"`.
- `WHISPER_DEVICE = "cuda"`.
- `WHISPER_COMPUTE = "float16"`.
- `WHISPER_LANGUAGE = "id"`.
- `WORD_ALIGNMENT_BACKEND = "whisperx"`.
- WhisperX Indonesian default align model is `cahya/wav2vec2-large-xlsr-indonesian`.
- `YOLO_WEIGHTS = "models/proya_best.pt"`.
- `YOLO_PRETRAIN = "yolov8n.pt"`.
- `YOLO_DEVICE = "0"`.
- `YOLO_IMGSZ = 416`.
- `YOLO_HALF = True`.
- `YOLO_SCAN_ONLY_MOMENTS = True`.
- `ROI` scans full width and top 60 percent of the frame.
- `OUTPUT_CODEC = "h264_nvenc"`.
- `OUTPUT_PRESET = "p1"`.
- `OUTPUT_CQ = 35`.
- `MAX_PARALLEL_CLIPS = 6`.
- `ffmpeg_editor` has an in-process NVENC semaphore capped at 3 final encodes.
- `VARIANTS_PER_CLIP = 6`.
- `VARIANT_SEED = 42`.

Config gotcha: many values are defined once, then overridden near the bottom of `config.py` under "Quality-first overrides". Edit the later definitions if changing active clip duration/score/render settings.

## Known Issues, Fragile Areas, And Workarounds

- `requirements.txt` is not a complete environment lock. `app.py` needs `altair`, `pandas`, and `psutil`; tests are easier with `pytest`; detected `whisperx` is lower than the declared constraint.
- `config.PRODUCT_CLASSES` does not match `dataset/proya.yaml`: config maps class 1 to `Serum`, class 2 to `Toner`, and class 6 to `host_face`; dataset declares class 1 `eye cream`, class 2 `host face`, class 3 `serum`, class 5 `toner`, and only 6 classes total. `vision_scanner.py` uses `cfg.PRODUCT_CLASSES` instead of `model.names`, so detection labels/face zoom filtering may be wrong unless the active `models/proya_best.pt` was trained with the config mapping. Verify before changing YOLO behavior.
- `HOST_FACE_CLASS = "host_face"` normalizes underscore to `hostface`; dataset uses `host face`, which normalizes to `hostface`, so matching can still work if class names are correct.
- `dataset/proya.yaml` uses Roboflow-style relative paths (`../train/images`, `../valid/images`). Current folders are under `dataset/train` and `dataset/valid`; verify Ultralytics path resolution before retraining.
- `vision_scanner.train_model()` logs `models/proya_detector/weights/best.pt` as the expected output, but existing complete training artifacts are under `runs/detect/models/proya_detector7` and `proya_detector8`; active weights were copied/placed at `models/proya_best.pt`.
- `runs/detect/models/proya_detector8/args.yaml` says `device: cpu` and `save_dir: E:\clipper\clipper_test\...`, which may not reflect the current `C:\Data\clipper_test` path or current GPU config.
- `main.py` currently does not cleanly reject `python main.py` without `--video` after special command checks; it proceeds to `run_pipeline(video_path=None)`. Always pass `--video` unless using a special command.
- `highlight_phrases.json` is written during rendering with a process-local `threading.Lock`. Multiple independent Python processes rendering at once can still race on this file.
- `ffmpeg_editor._build_zoom_expressions()` caps face zooms because FFmpeg expressions over roughly 4096 characters can fail. If too long, it drops face zooms and keeps product zoom only.
- `ffmpeg_editor` escapes ASS paths for Windows by converting to POSIX-style paths. Be careful when changing path handling around drive letters and colons.
- Raw cuts and temporary ASS files are cleanup-best-effort. Interrupted runs can leave `working/*/raw_cuts/*.mp4` and `temp_ass/sub_*.ass`.
- `pipeline.log` is appended by both `main.py` and `video_queue.py` through `logging.basicConfig` and can grow very large.
- `README.MD` and many source comments display mojibake in PowerShell output, likely encoding/terminal mismatch. Do not rewrite just to "fix" display unless asked.
- Streamlit dashboard reads queue state only; it does not start/stop the queue runner.

## External Services And Models

- LM Studio local server must be running at `http://localhost:1234/v1` with model name matching `config.LM_STUDIO_MODEL`.
- Faster-Whisper downloads/uses `large-v3-turbo` unless cached.
- WhisperX alignment uses Hugging Face model `cahya/wav2vec2-large-xlsr-indonesian` for `id`.
- Ultralytics YOLO loads `models/proya_best.pt` for inference and `yolov8n.pt` for training.
- FFmpeg final encode expects NVENC (`h264_nvenc`) to be available. If running on CPU-only systems, change `OUTPUT_CODEC`, `OUTPUT_PRESET`, `OUTPUT_CQ`/`OUTPUT_CRF`, `YOLO_DEVICE`, `WHISPER_DEVICE`, and `WHISPER_COMPUTE`.

## Output Formats

- `transcript.json`: `segments`, `words`, `metadata`; schema version currently 3.
- `moments.json`: list of validated moments with `start`, `end`, `score`, `hook`, `reason`, `product`, `clip_type`, `keyword_category`, `keywords_found`, `detector_version`, `content_focus`, `selected_text`, `segments`, `quality_checks`, `hook_overlay`, and `clip_id`.
- `product_detections.json`: list of schema-versioned product events with class, times, best/start/end boxes, frame dimensions, detection count, and track samples.
- `manifest.json`: list of rendered clip rows with `clip_id`, `version_dir`, `output_file`, `start`, `end`, `duration`, `score`, `hook`, `product`, `clip_type`, `reason`, `product_events`, and `status`.
- `video_queue_state.json`: state schema version 2 with video entries, per-stage states, attempts, progress counters, timestamps, output/working tags, and run history.
- `assets.csv`: downstream asset inventory with asset IDs, clip IDs, variant metadata, assignment/upload status, file path, file size, manifest path/status, and import timestamp.

## Safe Change Strategy

- For pipeline logic, start with the unit tests, then run a small `--max-clips 1` or `--max-clips 3` command against a cached video before attempting full VOD processing.
- Prefer changing `config.py` for thresholds/styles before modifying algorithmic code.
- If touching YOLO class behavior, reconcile `config.PRODUCT_CLASSES`, `HOST_FACE_CLASS`, `dataset/proya.yaml`, and the active model names first.
- If touching rendering, test both original and variant clips because variant transforms alter word timings and product bounding boxes.
- If touching queue scheduling, run both `python -m unittest -v` and `pytest -q`; queue tests are fast and isolated.
