const fs = require("node:fs");
const crypto = require("node:crypto");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const RUNTIME_CONFIG_SCHEMA_VERSION = 1;
const BACKEND_HOST = "127.0.0.1";

function generateControlToken() {
  return crypto.randomBytes(32).toString("base64url");
}

function desktopControlActor(username = process.env.USERNAME || process.env.USER || "operator") {
  const safe = String(username || "operator").trim().replace(/[^a-zA-Z0-9@._:+-]+/g, "-").slice(0, 96) || "operator";
  return `desktop:${safe}`;
}

function controlRequestHeaders({ targetUrl, backendPort, token, headers = {} } = {}) {
  if (!token || !isAllowedNavigation(targetUrl, backendPort)) {
    return { ...headers };
  }
  return { ...headers, Authorization: `Bearer ${token}` };
}

function parseArgs(argv = process.argv.slice(1)) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item || !item.startsWith("--")) {
      continue;
    }
    const withoutPrefix = item.slice(2);
    const equals = withoutPrefix.indexOf("=");
    if (equals >= 0) {
      args[withoutPrefix.slice(0, equals)] = withoutPrefix.slice(equals + 1);
      continue;
    }
    const next = argv[index + 1];
    if (next && !next.startsWith("--")) {
      args[withoutPrefix] = next;
      index += 1;
    } else {
      args[withoutPrefix] = "true";
    }
  }
  return args;
}

function normalizeMaybePath(value) {
  if (!value || typeof value !== "string") {
    return null;
  }
  return path.resolve(value);
}

function isProjectRoot(candidate) {
  const root = normalizeMaybePath(candidate);
  if (!root) {
    return false;
  }
  return (
    fs.existsSync(path.join(root, "config.py")) &&
    fs.existsSync(path.join(root, "clipper_app", "web_api.py"))
  );
}

function ancestors(startDir) {
  const start = normalizeMaybePath(startDir);
  if (!start) {
    return [];
  }
  const dirs = [];
  let current = start;
  while (true) {
    dirs.push(current);
    const parent = path.dirname(current);
    if (parent === current) {
      break;
    }
    current = parent;
  }
  return dirs;
}

function findProjectRoot({ cliRoot, envRoot, savedRoot, startDirs = [] } = {}) {
  const directCandidates = [cliRoot, envRoot, savedRoot].filter(Boolean);
  for (const candidate of directCandidates) {
    if (isProjectRoot(candidate)) {
      return normalizeMaybePath(candidate);
    }
  }
  for (const startDir of startDirs) {
    for (const candidate of ancestors(startDir)) {
      if (isProjectRoot(candidate)) {
        return candidate;
      }
    }
  }
  return null;
}

function resolvePythonExe({ cliPython, envPython, savedPython } = {}) {
  for (const candidate of [cliPython, envPython, savedPython]) {
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim();
    }
  }
  return "python";
}

function runtimeConfigPath(userDataPath) {
  return path.join(userDataPath, "runtime.json");
}

function readRuntimeConfig(configPath) {
  try {
    const payload = JSON.parse(fs.readFileSync(configPath, "utf8"));
    if (!payload || typeof payload !== "object") {
      return {};
    }
    if (payload.schema_version !== RUNTIME_CONFIG_SCHEMA_VERSION) {
      return {};
    }
    return {
      project_root: typeof payload.project_root === "string" ? payload.project_root : undefined,
      python_exe: typeof payload.python_exe === "string" ? payload.python_exe : undefined,
      last_backend_port: Number.isInteger(payload.last_backend_port) ? payload.last_backend_port : undefined
    };
  } catch (_error) {
    return {};
  }
}

function writeRuntimeConfig(configPath, config) {
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  const payload = {
    schema_version: RUNTIME_CONFIG_SCHEMA_VERSION,
    project_root: config.project_root || "",
    python_exe: config.python_exe || "",
    last_backend_port: Number.isInteger(config.last_backend_port) ? config.last_backend_port : undefined,
    updated_at: new Date().toISOString()
  };
  fs.writeFileSync(configPath, JSON.stringify(payload, null, 2), "utf8");
}

function getFreePort(host = BACKEND_HOST) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, host, () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : null;
      server.close(() => {
        if (port) {
          resolve(port);
        } else {
          reject(new Error("Could not allocate a backend port."));
        }
      });
    });
  });
}

function buildBackendCommand({ pythonExe, projectRoot, port }) {
  return {
    command: pythonExe,
    args: [
      "-m",
      "uvicorn",
      "clipper_app.web_api:app",
      "--host",
      BACKEND_HOST,
      "--port",
      String(port)
    ],
    cwd: projectRoot
  };
}

function isAllowedNavigation(targetUrl, backendPort) {
  try {
    const parsed = new URL(targetUrl);
    return parsed.protocol === "http:" && parsed.hostname === BACKEND_HOST && parsed.port === String(backendPort);
  } catch (_error) {
    return false;
  }
}

function desktopStartDirs({ appPath, execPath, dirname, cwd } = {}) {
  return [
    cwd || process.cwd(),
    dirname || __dirname,
    dirname ? path.resolve(dirname, "..", "..") : null,
    appPath || null,
    execPath ? path.dirname(execPath) : null,
    os.homedir()
  ].filter(Boolean);
}

function portableRestartCommand({ portableExecutableFile, argv = [] } = {}) {
  if (typeof portableExecutableFile !== "string" || !portableExecutableFile.trim()) {
    return null;
  }
  const command = path.resolve(portableExecutableFile.trim());
  if (!fs.existsSync(command)) {
    return null;
  }
  return {
    command,
    args: Array.isArray(argv) ? argv.map(String) : [],
    cwd: path.dirname(command)
  };
}

module.exports = {
  BACKEND_HOST,
  RUNTIME_CONFIG_SCHEMA_VERSION,
  buildBackendCommand,
  controlRequestHeaders,
  desktopControlActor,
  desktopStartDirs,
  findProjectRoot,
  getFreePort,
  generateControlToken,
  isAllowedNavigation,
  isProjectRoot,
  parseArgs,
  portableRestartCommand,
  readRuntimeConfig,
  resolvePythonExe,
  runtimeConfigPath,
  writeRuntimeConfig
};
