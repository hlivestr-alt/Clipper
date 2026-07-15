const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const projectRoot = path.resolve(__dirname, "..");
const outputRoot = path.join(projectRoot, "dist-desktop");
const unpacked = path.join(outputRoot, "win-unpacked");
const unpackedTmp = path.join(outputRoot, "win-unpacked.tmp");
const appStage = path.join(outputRoot, "app-stage");
const portableExe = path.join(outputRoot, `Clipper-${readPackageJson().version}-portable.exe`);

function readPackageJson() {
  return JSON.parse(fs.readFileSync(path.join(projectRoot, "package.json"), "utf8"));
}

function assertInsideProject(target) {
  const resolved = path.resolve(target);
  if (!resolved.startsWith(projectRoot + path.sep)) {
    throw new Error(`Refusing to touch path outside project: ${resolved}`);
  }
  return resolved;
}

function removeGenerated(target) {
  const resolved = assertInsideProject(target);
  fs.rmSync(resolved, { recursive: true, force: true });
}

function sleep(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function renameWithRetry(source, destination) {
  let lastError = null;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      fs.renameSync(source, destination);
      return;
    } catch (error) {
      lastError = error;
      sleep(500);
    }
  }
  console.warn(`Rename did not settle quickly (${lastError && lastError.message}); copying unpacked app instead.`);
  removeGenerated(destination);
  fs.cpSync(source, destination, { recursive: true });
}

function hasPackagedApp(unpackedDir) {
  return (
    fs.existsSync(path.join(unpackedDir, "resources", "app.asar")) ||
    fs.existsSync(path.join(unpackedDir, "resources", "app", "package.json"))
  );
}

function appExecutablePath(unpackedDir) {
  const packageJson = readPackageJson();
  const productName = packageJson.build?.productName || packageJson.productName || packageJson.name || "Clipper";
  return path.join(unpackedDir, `${productName}.exe`);
}

function copyFile(source, destination) {
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.copyFileSync(source, destination);
}

function stageAppPayload() {
  removeGenerated(appStage);
  fs.mkdirSync(appStage, { recursive: true });
  fs.cpSync(path.join(projectRoot, "electron"), path.join(appStage, "electron"), { recursive: true });

  const packageJson = readPackageJson();
  const appPackageJson = {
    name: packageJson.name,
    version: packageJson.version,
    description: packageJson.description,
    author: packageJson.author,
    private: true,
    type: packageJson.type,
    main: packageJson.main
  };
  fs.writeFileSync(path.join(appStage, "package.json"), `${JSON.stringify(appPackageJson, null, 2)}\n`, "utf8");

  copyFile(path.join(projectRoot, "index.html"), path.join(appStage, "index.html"));
}

function prepareFallbackExecutable(unpackedDir) {
  const electronExe = path.join(unpackedDir, "electron.exe");
  const appExe = appExecutablePath(unpackedDir);
  if (!fs.existsSync(electronExe)) {
    throw new Error(`Electron executable is missing from fallback package: ${electronExe}`);
  }
  copyFile(electronExe, appExe);
}

async function createFallbackAppPackage(unpackedDir) {
  const resourcesDir = path.join(unpackedDir, "resources");
  const appDir = path.join(resourcesDir, "app");
  fs.mkdirSync(resourcesDir, { recursive: true });
  fs.rmSync(path.join(resourcesDir, "app.asar"), { force: true });
  removeGenerated(appDir);
  const rendererDir = path.join(resourcesDir, "renderer");
  fs.rmSync(rendererDir, { recursive: true, force: true });
  fs.cpSync(path.join(projectRoot, "dist"), rendererDir, { recursive: true });
  stageAppPayload();
  fs.cpSync(appStage, appDir, { recursive: true });
  removeGenerated(appStage);
  if (!hasPackagedApp(unpackedDir)) {
    throw new Error(`Portable prepackage is missing app resources: ${unpackedDir}`);
  }
  prepareFallbackExecutable(unpackedDir);
}

function assertPortableBuilt() {
  const stat = fs.existsSync(portableExe) ? fs.statSync(portableExe) : null;
  if (!stat || stat.size <= 0) {
    throw new Error(`Portable executable was not created: ${portableExe}`);
  }
  if (!hasPackagedApp(unpacked)) {
    throw new Error(`win-unpacked is missing packaged app resources: ${unpacked}`);
  }
  if (!fs.existsSync(appExecutablePath(unpacked))) {
    throw new Error(`win-unpacked is missing the app executable: ${appExecutablePath(unpacked)}`);
  }
}

function electronBuilder(args) {
  const cli = require.resolve("electron-builder/cli.js");
  const result = spawnSync(process.execPath, [cli, ...args], {
    cwd: projectRoot,
    stdio: "inherit",
    shell: false
  });
  if (result.error) {
    console.error(result.error.message);
  }
  return result;
}

async function main() {
  removeGenerated(unpacked);
  removeGenerated(unpackedTmp);
  removeGenerated(appStage);

  const first = electronBuilder(["--win", "portable"]);
  if (first.status === 0) {
    assertPortableBuilt();
    return;
  }

  if (fs.existsSync(unpacked) && fs.existsSync(path.join(unpacked, "electron.exe"))) {
    console.warn("electron-builder created the unpacked runtime before packaging failed; staging app resources and retrying with --prepackaged.");
    await createFallbackAppPackage(assertInsideProject(unpacked));
  } else if (fs.existsSync(unpackedTmp)) {
    console.warn("electron-builder left win-unpacked.tmp after a failed rename; preparing app resources and retrying with --prepackaged.");
    renameWithRetry(assertInsideProject(unpackedTmp), assertInsideProject(unpacked));
    await createFallbackAppPackage(assertInsideProject(unpacked));
  } else {
    process.exit(first.status || 1);
  }

  const second = electronBuilder(["--win", "portable", "--prepackaged", unpacked]);
  if (second.status !== 0) {
    process.exit(second.status || 1);
  }
  assertPortableBuilt();
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
