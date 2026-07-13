const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  BACKEND_HOST,
  buildBackendCommand,
  controlRequestHeaders,
  desktopControlActor,
  desktopStartDirs,
  findProjectRoot,
  generateControlToken,
  isAllowedNavigation,
  isProjectRoot,
  parseArgs,
  portableRestartCommand,
  readRuntimeConfig,
  resolvePythonExe,
  runtimeConfigPath,
  writeRuntimeConfig
} = require("./runtime.cjs");

function makeTempProject() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "clipper-desktop-"));
  fs.mkdirSync(path.join(root, "clipper_app"), { recursive: true });
  fs.writeFileSync(path.join(root, "config.py"), "# test\n", "utf8");
  fs.writeFileSync(path.join(root, "clipper_app", "web_api.py"), "# test\n", "utf8");
  return root;
}

test("parseArgs supports equals, value, and boolean flags", () => {
  assert.deepEqual(
    parseArgs(["--project-root=C:\\Data\\clipper_test", "--python-exe", "python.exe", "--debug"]),
    {
      "project-root": "C:\\Data\\clipper_test",
      "python-exe": "python.exe",
      debug: "true"
    }
  );
});

test("project root resolver honors precedence and ancestor search", () => {
  const first = makeTempProject();
  const second = makeTempProject();
  const nested = path.join(second, "new_app", "electron");
  fs.mkdirSync(nested, { recursive: true });

  assert.equal(isProjectRoot(first), true);
  assert.equal(findProjectRoot({ cliRoot: first, envRoot: second }), first);
  assert.equal(findProjectRoot({ startDirs: [nested] }), second);
});

test("python resolver respects CLI, environment, saved config, then python", () => {
  assert.equal(resolvePythonExe({ cliPython: "cli", envPython: "env", savedPython: "saved" }), "cli");
  assert.equal(resolvePythonExe({ envPython: "env", savedPython: "saved" }), "env");
  assert.equal(resolvePythonExe({ savedPython: "saved" }), "saved");
  assert.equal(resolvePythonExe({}), "python");
});

test("runtime config read and write handles missing and corrupt files safely", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "clipper-runtime-config-"));
  const configPath = runtimeConfigPath(root);
  assert.deepEqual(readRuntimeConfig(configPath), {});

  fs.writeFileSync(configPath, "{not-json", "utf8");
  assert.deepEqual(readRuntimeConfig(configPath), {});

  writeRuntimeConfig(configPath, {
    project_root: "C:\\Data\\clipper_test",
    python_exe: "python.exe",
    last_backend_port: 8765
  });
  assert.deepEqual(readRuntimeConfig(configPath), {
    project_root: "C:\\Data\\clipper_test",
    python_exe: "python.exe",
    last_backend_port: 8765
  });
});

test("backend command uses project root cwd and uvicorn module invocation", () => {
  const command = buildBackendCommand({
    pythonExe: "python.exe",
    projectRoot: "C:\\Data\\clipper_test",
    port: 43210
  });
  assert.equal(command.command, "python.exe");
  assert.equal(command.cwd, "C:\\Data\\clipper_test");
  assert.deepEqual(command.args, [
    "-m",
    "uvicorn",
    "clipper_app.web_api:app",
    "--host",
    BACKEND_HOST,
    "--port",
    "43210"
  ]);
});

test("navigation guard allows only the managed local backend origin", () => {
  assert.equal(isAllowedNavigation("http://127.0.0.1:8765/", 8765), true);
  assert.equal(isAllowedNavigation("http://127.0.0.1:8765/api/health", 8765), true);
  assert.equal(isAllowedNavigation("http://127.0.0.1:5173/", 8765), false);
  assert.equal(isAllowedNavigation("https://127.0.0.1:8765/", 8765), false);
  assert.equal(isAllowedNavigation("https://example.com", 8765), false);
});

test("control token is strong, unique, and injected only for managed origin", () => {
  const first = generateControlToken();
  const second = generateControlToken();
  assert.notEqual(first, second);
  assert.ok(first.length >= 43);
  assert.match(first, /^[A-Za-z0-9_-]+$/);

  const allowed = controlRequestHeaders({
    targetUrl: "http://127.0.0.1:8765/api/artifacts",
    backendPort: 8765,
    token: first,
    headers: { Accept: "video/mp4" }
  });
  assert.equal(allowed.Authorization, `Bearer ${first}`);
  assert.equal(allowed.Accept, "video/mp4");

  const denied = controlRequestHeaders({
    targetUrl: "https://example.com/api",
    backendPort: 8765,
    token: first,
    headers: {}
  });
  assert.equal(denied.Authorization, undefined);
  assert.equal(desktopControlActor("Jane User"), "desktop:Jane-User");
});

test("desktopStartDirs includes useful launch anchors", () => {
  const dirs = desktopStartDirs({
    appPath: "C:\\app",
    execPath: "C:\\bin\\PROYA.exe",
    dirname: "C:\\Data\\clipper_test\\new_app\\electron",
    cwd: "C:\\Data\\clipper_test"
  });
  assert.equal(dirs.includes("C:\\Data\\clipper_test"), true);
  assert.equal(dirs.includes("C:\\bin"), true);
});

test("portable restart targets the outer executable and preserves launch arguments", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "clipper-portable-restart-"));
  const executable = path.join(root, "Clipper-portable.exe");
  fs.writeFileSync(executable, "test", "utf8");

  assert.deepEqual(
    portableRestartCommand({
      portableExecutableFile: executable,
      argv: ["--project-root", "C:\\Data\\clipper_test"]
    }),
    {
      command: executable,
      args: ["--project-root", "C:\\Data\\clipper_test"],
      cwd: root
    }
  );
  assert.equal(portableRestartCommand({ portableExecutableFile: path.join(root, "missing.exe") }), null);
  assert.equal(portableRestartCommand({}), null);
});
