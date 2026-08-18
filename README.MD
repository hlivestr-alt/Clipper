# PROYA VOD Clipper

Windows-first local production system for turning long Indonesian PROYA skincare livestream VODs into vertical short-video variants. The repository includes the video pipeline, resumable multi-VOD queue, scoring and compliance, reusable modules, variation profiles and previews, TikTok trend research, WhatsApp-ready export/delivery state, a FastAPI control API, a React operator app, and a portable Electron shell.

The default production sources of truth are still the files under `working/`, `D:\output_clips`, and `D:\proya_modules`. A rebuildable SQLite catalog and SQLite queue authority are available behind rollout flags; see [docs/long_term_storage_rollout.md](docs/long_term_storage_rollout.md).

## Prerequisites

- Python and the packages in `requirements.txt`.
- FFmpeg and FFprobe on `PATH`.
- pnpm and Node.js for `new_app/`.
- A running LM Studio OpenAI-compatible server for moment detection and other configured local-model work.
- A CUDA-capable PyTorch/Whisper/YOLO environment for the configured GPU paths.
- Trained YOLO weights at `models/proya_best.pt`.

The checked-in defaults are machine-specific: VODs are read from `D:\VOD`, finished clips go to `D:\output_clips`, reusable modules go to `D:\proya_modules`, and temporary/cached state goes under `working/`. Review `config.py` before a production run.

## Quick Start

Clone the repository into the current project-directory name and run commands from its root. Git tracks repository contents with relative paths, so the checkout directory can be renamed without changing the index or remote configuration.

```powershell
git clone https://github.com/hlivestr-alt/Clipper.git "Clipper Ai Trends"
Set-Location ".\Clipper Ai Trends"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py --test-lm-studio
```

## Operator App

For browser development, install frontend dependencies once and start the FastAPI and Vite processes together. The launcher generates a per-process control token and the Vite proxy adds it to API requests.

```powershell
.\run_new_app.ps1 -PnpmExe pnpm.cmd -InstallFrontendDeps
.\run_new_app.ps1 -PnpmExe pnpm.cmd
```

Default development URLs are `http://127.0.0.1:5173` for React and `http://127.0.0.1:8765` for FastAPI.

The app covers Overview; Production Live/Queue; Clip and Compliance Review; TikTok Trends; Variants; Modules; Deliveries; Jobs/Logs; and Settings/Diagnostics. Mutations and sensitive reads require the generated Bearer token. See [docs/control_app.md](docs/control_app.md), [docs/read_first_app.md](docs/read_first_app.md), and [docs/backend_boundary.md](docs/backend_boundary.md).

Build or run the Windows desktop shell from `new_app/`:

```powershell
pnpm.cmd desktop:dev
pnpm.cmd desktop:test
pnpm.cmd desktop:portable
```

The current portable artifact is `new_app/dist-desktop/Clipper-0.4.1-portable.exe`. It bundles Electron and the compiled React renderer, not Python, FFmpeg, models, production data, or the project itself. See [docs/desktop_app.md](docs/desktop_app.md).

## Single-VOD Pipeline

```powershell
python main.py --video "D:\VOD\livestream.mp4"
python main.py --video "D:\VOD\livestream.mp4" --skip-transcribe
python main.py --video "D:\VOD\livestream.mp4" --skip-transcribe --skip-moments --skip-vision
python main.py --video "D:\VOD\livestream.mp4" --max-clips 3 --skip-vision
python main.py --video "D:\VOD\livestream.mp4" --cut-only
```

The normal path transcribes and word-aligns speech, selects moments through LM Studio, optionally scans for products with YOLO, expands each base moment through the active variation profile, renders resumably with FFmpeg, runs compliance checks, scores outputs, and updates manifests/read models. Evidence-backed dynamic text can use approved PDF/DOCX sources under `assets/information/`.

## Queue and Supervisor

For a single queue pass:

```powershell
.\run_queue.ps1 -InputDir "D:\VOD" -DryRun
.\run_queue.ps1 -InputDir "D:\VOD"
```

For continuous supervised operation and graceful control:

```powershell
.\run_queue_forever.ps1 -RunMode folder_repeat -PipelineMode full -VariantMode all -DryRun
.\run_queue_control.ps1 status -Json
.\run_queue_control.ps1 stop
.\run_queue_control.ps1 continue
```

The queue supports `single_video`, `folder_once`, and `folder_repeat`; `full`, `clips_only`, `modules_only`, and `raw_cuts_only`; and `all`, `original`, or a custom variant count. JSON queue state remains the default. The rollout and maintenance commands are documented in [docs/long_term_storage_rollout.md](docs/long_term_storage_rollout.md).

## Modules, Review, and Export

```powershell
python main.py --video "D:\VOD\livestream.mp4" --extract-modules-only
.\run_module_assembly.ps1 -DryRun
python main.py --module-library-report
python main.py --module-review-queue
python main.py --validate-modules-visual-only
python main.py --package-export-batches --dry-run
```

Module extraction and assembly are implemented but disabled in the normal `config.py` path; the explicit commands enable the intended one-off workflows. Export packaging uses append-only numbered folders, duplicate protection, and diversity-first rolling allocation. With `WHATSAPP_DELIVERY_ENABLED=True`, newly published batches are also registered in the WhatsApp delivery state.

## Variants, Product Information, and Trends

- The active profile is stored at `working/variation_profile.json`; presets and preview renders live under `working/variation_presets/` and `working/variation_previews/`.
- Approved searchable `.pdf` and `.docx` product sources belong in `assets/information/`; the generated index is `working/product_information_index.json`.
- TikTok Business OAuth, discovery, rights-confirmed downloads, local media linking, editing fingerprints, and read-only profile suggestions are available on `/trends`. Setup is in [docs/tiktok_oauth_setup.md](docs/tiktok_oauth_setup.md).
- The current Variants UI and persistence/render boundaries are documented in [VARIANT_PAGE_AUDIT.md](VARIANT_PAGE_AUDIT.md).

## WhatsApp-Ready Delivery

WhatsApp-compatible batch conversion and direct assignment/send-state APIs are implemented, but direct claims are fail-closed by default. Keep `WHATSAPP_DIRECT_PC_DELIVERY_ENABLED=False` until the legacy Drive workflow is disabled and the cutover checklist is complete.

```powershell
python scripts/whatsapp_backlog.py
python scripts/whatsapp_backlog.py --execute --batch-range 6901:6905
```

See [docs/whatsapp_delivery_operations.md](docs/whatsapp_delivery_operations.md) before running a conversion or enabling delivery.

## Fixed 15-Second Text Overlays

`scripts/add_proya_cleanser_text.py` burns one of three hard-coded Indonesian PROYA timelines into a source MP4: `cleanser` (default), `vitamin-c-serum`, or `vitamin-c-sheet-mask`. It validates an input close to 15 seconds, scans `assets/` recursively for fonts, and exports a 720 x 1280 H.264/AAC MP4.

```powershell
python scripts/add_proya_cleanser_text.py --input "C:\path with spaces\input.mp4" --output "working\captioned.mp4" --assets-dir assets --dry-run
python scripts/add_proya_cleanser_text.py --preset vitamin-c-serum --input "C:\path with spaces\serum.mp4" --output "working\serum_captioned.mp4" --assets-dir assets
```

## Tests

```powershell
python -m pytest -q
cd new_app
pnpm.cmd test
pnpm.cmd build
pnpm.cmd desktop:test
```

As of 2026-08-10, the current worktree passes 500 Python tests (plus 27 subtests), 82 frontend tests across 18 files, the production frontend build, and 11 Electron runtime tests. GPU/model/live-provider/end-to-end media behavior still requires workstation smoke testing.

## Documentation Map

- [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md): current architecture, project map, runtime contracts, and future-session guidance.
- [docs/backend_boundary.md](docs/backend_boundary.md): application/service boundary, compatibility modes, security, jobs, and events.
- [docs/control_app.md](docs/control_app.md): operator UI and mutation behavior.
- [docs/desktop_app.md](docs/desktop_app.md): Electron runtime and packaging.
- [docs/long_term_storage_rollout.md](docs/long_term_storage_rollout.md): catalog/queue migration and rollback.
- [docs/cloudflare_dashboard_access.md](docs/cloudflare_dashboard_access.md): remote-access constraints and Cloudflare runbook.
- [docs/tiktok_oauth_setup.md](docs/tiktok_oauth_setup.md): TikTok Business credentials and callback ownership.
- [docs/whatsapp_delivery_operations.md](docs/whatsapp_delivery_operations.md): media conversion, state, n8n contract, and cutover gate.
