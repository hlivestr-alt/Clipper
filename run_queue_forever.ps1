param(
    [string]$InputDir = "",
    [string]$StateFile = "",
    [string]$RunStateFile = "",
    [string]$ControlFile = "",
    [int]$StartRunNumber = 0,
    [int]$MaxRetries = -1,
    [int]$MaxInflightVideos = 0,
    [int]$FfmpegMaxParallelClips = 0,
    [Nullable[int]]$MaxClips = $null,
    [Nullable[double]]$MinScore = $null,
    [double]$PollInterval = 0.0,
    [double]$ScanInterval = 0.0,
    [double]$StableSeconds = -1.0,
    [int]$RestartDelaySeconds = 0,
    [int]$BetweenRunsDelaySeconds = 0,
    [string]$PythonExe = "python",
    [switch]$ForceRescore,
    [switch]$ForceModules,
    [switch]$RetryFailed,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSCommandPath
$SupervisorScript = Join-Path $ProjectRoot "queue_supervisor.py"

function Resolve-ProjectPath {
    param([string]$PathText)
    if ([string]::IsNullOrWhiteSpace($PathText)) {
        return ""
    }
    if ([System.IO.Path]::IsPathRooted($PathText)) {
        return $PathText
    }
    return Join-Path $ProjectRoot $PathText
}

$PythonArgs = @($SupervisorScript)

if (-not [string]::IsNullOrWhiteSpace($InputDir)) {
    $PythonArgs += @("--input-dir", (Resolve-ProjectPath $InputDir))
}
if (-not [string]::IsNullOrWhiteSpace($StateFile)) {
    $PythonArgs += @("--state-file", (Resolve-ProjectPath $StateFile))
}
if (-not [string]::IsNullOrWhiteSpace($RunStateFile)) {
    $PythonArgs += @("--forever-state-file", (Resolve-ProjectPath $RunStateFile))
}
if (-not [string]::IsNullOrWhiteSpace($ControlFile)) {
    $PythonArgs += @("--control-file", (Resolve-ProjectPath $ControlFile))
}
if ($StartRunNumber -gt 0) {
    $PythonArgs += @("--start-run-number", "$StartRunNumber")
}
if ($MaxRetries -ge 0) {
    $PythonArgs += @("--max-retries", "$MaxRetries")
}
if ($MaxInflightVideos -gt 0) {
    $PythonArgs += @("--max-inflight-videos", "$MaxInflightVideos")
}
if ($FfmpegMaxParallelClips -gt 0) {
    $PythonArgs += @("--ffmpeg-max-parallel-clips", "$FfmpegMaxParallelClips")
}
if ($null -ne $MaxClips -and $MaxClips -gt 0) {
    $PythonArgs += @("--max-clips", "$MaxClips")
}
if ($null -ne $MinScore) {
    $PythonArgs += @("--min-score", "$MinScore")
}
if ($PollInterval -gt 0) {
    $PythonArgs += @("--poll-interval", "$PollInterval")
}
if ($ScanInterval -gt 0) {
    $PythonArgs += @("--scan-interval", "$ScanInterval")
}
if ($StableSeconds -ge 0) {
    $PythonArgs += @("--stable-seconds", "$StableSeconds")
}
if ($RestartDelaySeconds -gt 0) {
    $PythonArgs += @("--restart-delay-seconds", "$RestartDelaySeconds")
}
if ($BetweenRunsDelaySeconds -gt 0) {
    $PythonArgs += @("--between-runs-delay-seconds", "$BetweenRunsDelaySeconds")
}
if ($ForceRescore) {
    $PythonArgs += "--force-rescore"
}
if ($ForceModules) {
    $PythonArgs += "--force-modules"
}
if ($RetryFailed) {
    $PythonArgs += "--retry-failed"
}
if ($DryRun) {
    $PythonArgs += "--dry-run"
}

Write-Host "Starting PROYA resumable queue supervisor..."
Write-Host "$PythonExe $($PythonArgs -join ' ')"

Push-Location $ProjectRoot
try {
    & $PythonExe @PythonArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
