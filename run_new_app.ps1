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

function New-ControlToken {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

if ([string]::IsNullOrWhiteSpace($env:CLIPPER_CONTROL_TOKEN)) {
    if ($FrontendOnly) {
        throw "-FrontendOnly requires CLIPPER_CONTROL_TOKEN in the environment matching the running backend."
    }
    $env:CLIPPER_CONTROL_TOKEN = New-ControlToken
}
else {
    $env:CLIPPER_CONTROL_TOKEN = $env:CLIPPER_CONTROL_TOKEN.Trim()
}
$ControlActor = "desktop:$($env:USERNAME)"
$env:CLIPPER_CONTROL_ACTOR = $ControlActor
$env:CLIPPER_MIGRATE_JOB_STORAGE = "1"

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
