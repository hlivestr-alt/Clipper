# Desktop App

Phase 4 wraps the FastAPI + React control app in a Windows Electron shell.

The desktop app is a local operator wrapper. It does not bundle Python, FFmpeg,
LM Studio, CUDA packages, models, or production data. It launches the existing
project runtime and serves the built React app through the existing FastAPI app.

## Commands

From `new_app/`:

```powershell
pnpm desktop:dev
pnpm desktop:test
pnpm desktop:portable
```

The portable build writes:

```text
new_app/dist-desktop/PROYA-VOD-Control-0.4.0-portable.exe
```

`run_new_app.ps1` remains available for browser-based development and rollback.

## Runtime Resolution

Electron resolves the project root in this order:

1. `--project-root`
2. `CLIPPER_PROJECT_ROOT`
3. Saved Electron runtime config
4. Executable/current-directory ancestors
5. Operator-selected folder

Electron resolves Python in this order:

1. `--python-exe`
2. `CLIPPER_PYTHON_EXE`
3. Saved Electron runtime config
4. `python` on `PATH`
5. Operator-selected `python.exe`

The saved runtime config lives under Electron `userData` as `runtime.json`.
It stores only `project_root`, `python_exe`, and `last_backend_port`.

## Backend Lifecycle

Electron starts:

```powershell
python -m uvicorn clipper_app.web_api:app --host 127.0.0.1 --port <free_port>
```

It waits for `/api/health`, then opens the main window at the managed local
backend origin. Closing the desktop app terminates only the backend process
started by Electron.

## Security Boundaries

- `nodeIntegration` is disabled.
- `contextIsolation` and `sandbox` are enabled.
- The preload bridge exposes only desktop status.
- Window navigation is restricted to the managed `127.0.0.1:<port>` origin.
- Artifact access remains behind the existing FastAPI safety checks.

## Notes

The portable build includes a Windows fallback for a local Electron Builder
rename timing issue. If Electron Builder leaves `win-unpacked.tmp`, the build
script retries by using the unpacked app as `--prepackaged`.

No installer, auto-update, code-signing identity, app icon, or bundled Python
runtime is included in Phase 4.
