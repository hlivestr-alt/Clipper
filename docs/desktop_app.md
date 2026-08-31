# Desktop App

The Windows Electron shell runs the compiled React control app and manages one loopback FastAPI child process. It is an operator wrapper around the existing project runtime, not a self-contained video-processing distribution.

## Commands and Artifact

From `new_app/`:

```powershell
pnpm.cmd desktop:dev
pnpm.cmd desktop:test
pnpm.cmd desktop:portable
```

`desktop:dev` builds the renderer before starting Electron. `desktop:portable` runs the frontend build and Electron runtime tests before packaging.

Current package metadata is version `0.4.1`, product name `Clipper`, and artifact:

```text
new_app/dist-desktop/Clipper-0.4.1-portable.exe
```

The artifact contains Electron, `main.cjs`, `preload.cjs`, `runtime.cjs`, and the compiled renderer under packaged `resources/renderer`. It does not contain Python, this repository, FFmpeg/FFprobe, LM Studio, CUDA/PyTorch, Whisper/YOLO models, VODs, assets outside the renderer, working state, or output media.

## Runtime Resolution

The project root must contain `config.py` and `clipper_app/web_api.py`. Electron resolves it in this order:

1. `--project-root`.
2. `CLIPPER_PROJECT_ROOT`.
3. Saved Electron runtime configuration.
4. Ancestors of useful launch locations, including the current directory, app/executable paths, and home directory.
5. An operator-selected folder.

Python resolution is `--python-exe`, `CLIPPER_PYTHON_EXE`, saved configuration, then `python`. If validation/startup fails, the operator can select `python.exe` through the runtime flow.

The per-user Electron `userData/runtime.json` stores schema version, `project_root`, `python_exe`, `last_backend_port`, and update time. It does not store OAuth credentials or control tokens.

## Startup Lifecycle

1. Acquire a single-instance lock; a second launch focuses the existing window.
2. Resolve the project and Python runtime and allocate a free `127.0.0.1` port.
3. Generate a fresh random control token and actor `desktop:<Windows user>`.
4. Launch:

   ```powershell
   python -m uvicorn clipper_app.web_api:app --host 127.0.0.1 --port <free-port>
   ```

   The child also receives `CLIPPER_DESKTOP=1`, `CLIPPER_CONTROL_TOKEN`, `CLIPPER_CONTROL_ACTOR`, `CLIPPER_MIGRATE_JOB_STORAGE=1`, `CLIPPER_STATIC_DIR`, and `PYTHONUNBUFFERED=1`.

5. Wait up to 45 seconds for `/api/health`.
6. Open a frameless 1440 x 920 window, minimum 960 x 720, at the managed loopback origin.
7. Inject the Bearer token into requests only for that exact origin.
8. On normal close, terminate only the Uvicorn process started by this Electron instance. A Windows process-tree fallback is used if the child does not exit promptly.

If startup fails, a local diagnostic window reports the command, project root, Python path, error, and captured backend output and offers copy/retry/exit actions. Portable restart relaunches the outer executable and preserves its arguments.

## Security Boundaries

- `nodeIntegration` is disabled.
- `contextIsolation` and the Chromium sandbox are enabled.
- Navigation and new windows are restricted to the exact managed loopback origin.
- The only allowed external OAuth target is the expected HTTPS TikTok Business authorization endpoint, opened in the system browser.
- The preload bridge exposes runtime status, window minimize/maximize/close, approved OAuth opening, and app restart only.
- API auth, trusted-host/origin checks, and artifact/path containment remain enforced by FastAPI.
- Tokens are generated per launch and are not saved in `runtime.json` or exposed through the preload bridge.

## Portable Build Behavior

`electron/build-portable.cjs` invokes electron-builder and contains Windows recovery paths for rename timing failures and partially prepared unpacked directories. The compiled renderer is packaged as an external resource and served by FastAPI through `CLIPPER_STATIC_DIR`; hashed assets receive immutable caching while the SPA entry is served without long-term caching.

No installer, auto-update, code-signing identity, or bundled production runtime is currently configured. The Windows package and Electron windows use the canonical Clipper icon from `new_app/assets/icon.ico`.

`run_new_app.ps1` remains the browser-development and rollback launcher.
