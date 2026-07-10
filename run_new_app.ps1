param(
    [string]$PythonExe = "python",
    [string]$PnpmExe = "pnpm",
    [int]$BackendPort = 8765,
    [int]$FrontendPort = 5173,
    [switch]$InstallFrontendDeps,
    [switch]$BackendOnly,
    [switch]$FrontendOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$FrontendDir = Join-Path $Root "new_app"

if ($InstallFrontendDeps) {
    Push-Location $FrontendDir
    try {
        & $PnpmExe install
    }
    finally {
        Pop-Location
    }
}

if ($BackendOnly -and $FrontendOnly) {
    throw "Choose only one of -BackendOnly or -FrontendOnly."
}

if ($BackendOnly) {
    Push-Location $Root
    try {
        & $PythonExe -m uvicorn clipper_app.web_api:app --host 127.0.0.1 --port $BackendPort
    }
    finally {
        Pop-Location
    }
    exit $LASTEXITCODE
}

if ($FrontendOnly) {
    Push-Location $FrontendDir
    try {
        & $PnpmExe dev --host 127.0.0.1 --port $FrontendPort
    }
    finally {
        Pop-Location
    }
    exit $LASTEXITCODE
}

$BackendJob = Start-Job -ScriptBlock {
    param($RootPath, $PythonPath, $Port)
    Set-Location $RootPath
    & $PythonPath -m uvicorn clipper_app.web_api:app --host 127.0.0.1 --port $Port
} -ArgumentList $Root, $PythonExe, $BackendPort

try {
    Start-Sleep -Seconds 2
    Write-Host "Control API: http://127.0.0.1:$BackendPort"
    Write-Host "Control app: http://127.0.0.1:$FrontendPort"
    Push-Location $FrontendDir
    try {
        & $PnpmExe dev --host 127.0.0.1 --port $FrontendPort
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($BackendJob) {
        Stop-Job $BackendJob -ErrorAction SilentlyContinue
        Receive-Job $BackendJob -ErrorAction SilentlyContinue
        Remove-Job $BackendJob -ErrorAction SilentlyContinue
    }
}
