const { app, BrowserWindow, Menu, dialog, ipcMain } = require("electron");
const { spawn, spawnSync } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");

const {
  BACKEND_HOST,
  buildBackendCommand,
  desktopStartDirs,
  findProjectRoot,
  getFreePort,
  isAllowedNavigation,
  parseArgs,
  readRuntimeConfig,
  resolvePythonExe,
  runtimeConfigPath,
  writeRuntimeConfig
} = require("./runtime.cjs");

let backendProcess = null;
let backendExited = false;
let mainWindow = null;
let currentRuntime = {
  backend_running: false,
  backend_port: null,
  project_root: "",
  python_exe: "",
  backend_command: "",
  last_error: ""
};
const backendLog = [];

function pushLog(source, chunk) {
  const text = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk || "");
  text.split(/\r?\n/).forEach((line) => {
    if (!line) {
      return;
    }
    backendLog.push(`[${source}] ${line}`);
    while (backendLog.length > 200) {
      backendLog.shift();
    }
  });
}

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function errorHtml(title, message, detail) {
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>${escapeHtml(title)}</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, "Segoe UI", system-ui, sans-serif; background: #0b0d12; color: #f4f6fa; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0b0d12; }
    main { width: min(760px, calc(100vw - 48px)); border: 1px solid #353c49; border-radius: 12px; background: #11141a; padding: 28px; box-shadow: 0 24px 80px rgba(0,0,0,.45); }
    .brand { display: flex; align-items: center; gap: 12px; margin-bottom: 28px; color: #a2aaba; font-weight: 650; }
    .mark { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 9px; background: #7c5cfc; color: white; }
    h1 { margin: 0 0 8px; font-size: 1.5rem; }
    p { color: #a2aaba; line-height: 1.55; }
    pre { max-height: 300px; overflow: auto; white-space: pre-wrap; background: #0b0d12; color: #d9deea; border: 1px solid #272c36; border-radius: 8px; padding: 14px; }
    .actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
    button { min-height: 38px; border: 1px solid #353c49; border-radius: 6px; padding: 0 14px; background: #171b23; color: #f4f6fa; font: inherit; cursor: pointer; }
    button.primary { border-color: #7c5cfc; background: #7c5cfc; }
    button:hover { filter: brightness(1.1); }
  </style>
</head>
<body>
  <main>
    <div class="brand"><span class="mark">C</span><span>Clipper desktop</span></div>
    <h1>${escapeHtml(title)}</h1>
    <p>${escapeHtml(message)}</p>
    <pre>${escapeHtml(detail)}</pre>
    <div class="actions">
      <button onclick="navigator.clipboard.writeText(document.querySelector('pre').innerText)">Copy diagnostics</button>
      <button onclick="window.close()">Exit</button>
      <button class="primary" onclick="window.clipperDesktop && window.clipperDesktop.restartApp && window.clipperDesktop.restartApp()">Retry</button>
    </div>
  </main>
</body>
</html>`;
}

function createFailureWindow(title, message, detail) {
  const window = new BrowserWindow({
    width: 860,
    height: 640,
    backgroundColor: "#08111f",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true
    }
  });
  window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(errorHtml(title, message, detail))}`);
  return window;
}

function validatePython(pythonExe) {
  try {
    const result = spawnSync(pythonExe, ["--version"], {
      encoding: "utf8",
      timeout: 10000,
      windowsHide: true
    });
    return result.status === 0;
  } catch (_error) {
    return false;
  }
}

async function chooseProjectRoot(reason) {
  await dialog.showMessageBox({
    type: "info",
    title: "Select Clipper project folder",
    message: "The desktop app needs the clipper project folder.",
    detail: `${reason}\n\nChoose the folder that contains config.py and clipper_app/web_api.py.`
  });
  const result = await dialog.showOpenDialog({
    title: "Select Clipper project folder",
    properties: ["openDirectory"]
  });
  if (result.canceled || !result.filePaths.length) {
    return null;
  }
  return result.filePaths[0];
}

async function choosePythonExe(reason) {
  await dialog.showMessageBox({
    type: "info",
    title: "Select Python executable",
    message: "The desktop app needs the existing Python runtime for this project.",
    detail: `${reason}\n\nChoose the python.exe that can run: python -m uvicorn clipper_app.web_api:app`
  });
  const result = await dialog.showOpenDialog({
    title: "Select Python executable",
    properties: ["openFile"],
    filters: [
      { name: "Python executable", extensions: ["exe"] },
      { name: "All files", extensions: ["*"] }
    ]
  });
  if (result.canceled || !result.filePaths.length) {
    return null;
  }
  return result.filePaths[0];
}

async function resolveRuntime() {
  const args = parseArgs(process.argv.slice(1));
  const configPath = runtimeConfigPath(app.getPath("userData"));
  const saved = readRuntimeConfig(configPath);
  let projectRoot = findProjectRoot({
    cliRoot: args["project-root"],
    envRoot: process.env.CLIPPER_PROJECT_ROOT,
    savedRoot: saved.project_root,
    startDirs: desktopStartDirs({
      appPath: app.getAppPath(),
      execPath: process.execPath,
      dirname: __dirname,
      cwd: process.cwd()
    })
  });

  if (!projectRoot) {
    const chosen = await chooseProjectRoot("No valid project root was found from CLI args, environment, saved config, executable path, or current directory.");
    projectRoot = findProjectRoot({ cliRoot: chosen });
  }
  if (!projectRoot) {
    throw new Error("Project root was not selected or is not valid.");
  }

  let pythonExe = resolvePythonExe({
    cliPython: args["python-exe"],
    envPython: process.env.CLIPPER_PYTHON_EXE,
    savedPython: saved.python_exe
  });
  if (!validatePython(pythonExe)) {
    const chosen = await choosePythonExe(`Python could not be started from: ${pythonExe}`);
    if (!chosen || !validatePython(chosen)) {
      throw new Error("Python executable was not selected or failed validation.");
    }
    pythonExe = chosen;
  }

  const requestedPort = Number.parseInt(String(args["backend-port"] || ""), 10);
  const backendPort = Number.isInteger(requestedPort) && requestedPort > 0
    ? requestedPort
    : await getFreePort(BACKEND_HOST);

  writeRuntimeConfig(configPath, {
    project_root: projectRoot,
    python_exe: pythonExe,
    last_backend_port: backendPort
  });

  return {
    backendPort,
    configPath,
    projectRoot,
    pythonExe
  };
}

function startBackend(runtime) {
  const command = buildBackendCommand({
    pythonExe: runtime.pythonExe,
    projectRoot: runtime.projectRoot,
    port: runtime.backendPort
  });
  const commandText = `${command.command} ${command.args.join(" ")}`;
  currentRuntime = {
    backend_running: false,
    backend_port: runtime.backendPort,
    project_root: runtime.projectRoot,
    python_exe: runtime.pythonExe,
    backend_command: commandText,
    last_error: ""
  };
  backendExited = false;
  backendProcess = spawn(command.command, command.args, {
    cwd: command.cwd,
    env: {
      ...process.env,
      CLIPPER_DESKTOP: "1",
      PYTHONUNBUFFERED: "1"
    },
    windowsHide: true
  });
  backendProcess.stdout.on("data", (chunk) => pushLog("stdout", chunk));
  backendProcess.stderr.on("data", (chunk) => pushLog("stderr", chunk));
  backendProcess.on("error", (error) => {
    currentRuntime.last_error = error.message;
    pushLog("error", error.message);
  });
  backendProcess.on("exit", (code, signal) => {
    backendExited = true;
    currentRuntime.backend_running = false;
    const message = `Backend exited with code=${code ?? ""} signal=${signal ?? ""}`;
    currentRuntime.last_error = message;
    pushLog("exit", message);
  });
  return commandText;
}

function waitForHealth(port, timeoutMs = 45000) {
  const startedAt = Date.now();
  const urlPath = "/api/health";
  return new Promise((resolve, reject) => {
    function attempt() {
      if (backendExited) {
        reject(new Error("Backend process exited before health check succeeded."));
        return;
      }
      const request = http.request(
        {
          host: BACKEND_HOST,
          port,
          path: urlPath,
          method: "GET",
          timeout: 2500
        },
        (response) => {
          response.resume();
          if (response.statusCode && response.statusCode >= 200 && response.statusCode < 300) {
            resolve();
            return;
          }
          retry();
        }
      );
      request.on("timeout", () => {
        request.destroy();
        retry();
      });
      request.on("error", retry);
      request.end();
    }

    function retry() {
      if (Date.now() - startedAt > timeoutMs) {
        reject(new Error(`Timed out waiting for http://${BACKEND_HOST}:${port}${urlPath}`));
        return;
      }
      setTimeout(attempt, 500);
    }

    attempt();
  });
}

function setupNavigationGuard(window, backendPort) {
  window.webContents.on("will-navigate", (event, targetUrl) => {
    if (!isAllowedNavigation(targetUrl, backendPort)) {
      event.preventDefault();
    }
  });
  window.webContents.setWindowOpenHandler((details) => {
    return isAllowedNavigation(details.url, backendPort) ? { action: "allow" } : { action: "deny" };
  });
}

async function createMainWindow(runtime) {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 960,
    minHeight: 720,
    frame: false,
    autoHideMenuBar: true,
    show: false,
    backgroundColor: "#0b0d12",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true
    }
  });
  setupNavigationGuard(mainWindow, runtime.backendPort);
  mainWindow.once("ready-to-show", () => {
    if (mainWindow) {
      mainWindow.show();
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  await mainWindow.loadURL(`http://${BACKEND_HOST}:${runtime.backendPort}/`);
}

function stopBackend() {
  if (!backendProcess || backendProcess.killed) {
    return;
  }
  const child = backendProcess;
  child.kill();
  if (process.platform === "win32" && child.pid) {
    setTimeout(() => {
      if (!backendExited) {
        spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], { windowsHide: true });
      }
    }, 1500).unref();
  }
}

async function bootstrap() {
  const runtime = await resolveRuntime();
  const commandText = startBackend(runtime);
  try {
    await waitForHealth(runtime.backendPort);
  } catch (error) {
    const detail = [
      `Command: ${commandText}`,
      `Project root: ${runtime.projectRoot}`,
      `Python: ${runtime.pythonExe}`,
      "",
      backendLog.join("\n")
    ].join("\n");
    createFailureWindow(
      "Backend startup failed",
      error instanceof Error ? error.message : String(error),
      detail
    );
    return;
  }
  currentRuntime.backend_running = true;
  await createMainWindow(runtime);
}

ipcMain.handle("desktop:get-status", () => ({
  ...currentRuntime,
  recent_log: backendLog.slice(-30)
}));

ipcMain.handle("desktop:window-control", (_event, action) => {
  if (!mainWindow) {
    return { maximized: false };
  }
  if (action === "minimize") {
    mainWindow.minimize();
  } else if (action === "toggle-maximize") {
    if (mainWindow.isMaximized()) {
      mainWindow.unmaximize();
    } else {
      mainWindow.maximize();
    }
  } else if (action === "close") {
    mainWindow.close();
  }
  return { maximized: Boolean(mainWindow && mainWindow.isMaximized()) };
});

ipcMain.handle("desktop:restart-app", () => {
  app.relaunch();
  app.exit(0);
});

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  Menu.setApplicationMenu(null);

  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) {
        mainWindow.restore();
      }
      mainWindow.focus();
    }
  });

  app.whenReady()
    .then(bootstrap)
    .catch((error) => {
      currentRuntime.last_error = error instanceof Error ? error.message : String(error);
      createFailureWindow("Desktop startup failed", currentRuntime.last_error, backendLog.join("\n"));
    });

  app.on("before-quit", stopBackend);
  app.on("window-all-closed", () => {
    stopBackend();
    app.quit();
  });
}
