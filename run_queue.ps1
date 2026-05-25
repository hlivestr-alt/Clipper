param(
    [string]$InputDir = "",
    [string]$StateFile = "",
    [int]$RunNumber = 0,
    [string]$RedoTag = "",
    [int]$MaxRetries = -1,
    [int]$MaxInflightVideos = 0,
    [int]$FfmpegMaxParallelClips = 0,
    [Nullable[int]]$MaxClips = $null,
    [Nullable[double]]$MinScore = $null,
    [double]$PollInterval = 0.0,
    [string]$PythonExe = "python",
    [switch]$ForceRescore,
    [switch]$ForceModules,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSCommandPath

function Resolve-ProjectPath {
    param([string]$PathText)

    if ([System.IO.Path]::IsPathRooted($PathText)) {
        return $PathText
    }
    return Join-Path $ProjectRoot $PathText
}

function Get-QueueConfig {
    param([string]$PythonCommand)

    $Code = @'
import json
from pathlib import Path
import config as cfg

payload = {
    "input_dir": getattr(cfg, "QUEUE_INPUT_DIR", r"D:\VOD"),
    "state_file": getattr(
        cfg,
        "QUEUE_STATE_FILE",
        str(Path(getattr(cfg, "WORKING_DIR", "working")) / "video_queue_state.json"),
    ),
    "start_run_number": getattr(cfg, "QUEUE_START_RUN_NUMBER", 1),
    "max_retries": getattr(cfg, "QUEUE_MAX_RETRIES", 2),
    "max_inflight_videos": getattr(cfg, "QUEUE_MAX_INFLIGHT_VIDEOS", 1),
    "ffmpeg_max_parallel_clips": getattr(cfg, "QUEUE_FFMPEG_MAX_PARALLEL_CLIPS", 2),
    "poll_interval": getattr(cfg, "QUEUE_POLL_INTERVAL", 2.0),
    "module_extraction_enabled": bool(getattr(cfg, "MODULE_EXTRACTION_ENABLED", True)),
    "module_library_dir": getattr(cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules"),
    "compliance_enabled": bool(getattr(cfg, "COMPLIANCE_ENABLED", True)),
    "scorer_enabled": bool(getattr(cfg, "SCORER_ENABLED", True)),
    "scorer_vision_enabled": bool(getattr(cfg, "SCORER_VISION_ENABLED", False)),
    "model_management_enabled": bool(getattr(cfg, "LM_STUDIO_MODEL_MANAGEMENT_ENABLED", False)),
}
print(json.dumps(payload))
'@

    Push-Location $ProjectRoot
    try {
        $JsonText = $Code | & $PythonCommand -
        if ($LASTEXITCODE -ne 0) {
            throw "Could not load queue defaults from config.py"
        }
        return ($JsonText -join "`n") | ConvertFrom-Json
    }
    finally {
        Pop-Location
    }
}

$QueueConfig = Get-QueueConfig -PythonCommand $PythonExe

if ([string]::IsNullOrWhiteSpace($InputDir)) {
    $InputDir = [string]$QueueConfig.input_dir
}
if ([string]::IsNullOrWhiteSpace($StateFile)) {
    $StateFile = [string]$QueueConfig.state_file
}
if ($RunNumber -le 0) {
    $RunNumber = [int]$QueueConfig.start_run_number
}
if ($MaxRetries -lt 0) {
    $MaxRetries = [int]$QueueConfig.max_retries
}
if ($MaxInflightVideos -le 0) {
    $MaxInflightVideos = [int]$QueueConfig.max_inflight_videos
}
if ($FfmpegMaxParallelClips -le 0) {
    $FfmpegMaxParallelClips = [int]$QueueConfig.ffmpeg_max_parallel_clips
}
if ($PollInterval -le 0) {
    $PollInterval = [double]$QueueConfig.poll_interval
}

if ([string]::IsNullOrWhiteSpace($RedoTag)) {
    $RedoTag = "_run_{0:D3}" -f $RunNumber
}

$InputDirPath = Resolve-ProjectPath $InputDir
$StateFilePath = Resolve-ProjectPath $StateFile
$QueueScript = Join-Path $ProjectRoot "video_queue.py"

$PythonArgs = @(
    $QueueScript,
    "--input-dir", $InputDirPath,
    "--state-file", $StateFilePath,
    "--max-retries", "$MaxRetries",
    "--max-inflight-videos", "$MaxInflightVideos",
    "--ffmpeg-max-parallel-clips", "$FfmpegMaxParallelClips",
    "--poll-interval", "$PollInterval",
    "--redo-tag", $RedoTag
)

if ($null -ne $MaxClips -and $MaxClips -gt 0) {
    $PythonArgs += @("--max-clips", "$MaxClips")
}
if ($null -ne $MinScore) {
    $PythonArgs += @("--min-score", "$MinScore")
}
if ($ForceRescore) {
    $PythonArgs += "--force-rescore"
}
if ($ForceModules) {
    $PythonArgs += "--force-modules"
}

Write-Host "Starting PROYA queue pass: $RedoTag"
Write-Host "Input: $InputDirPath"
Write-Host "State: $StateFilePath"
Write-Host ("Features: modules={0}, compliance={1}, scoring={2}, vision-scoring={3}, model-management={4}" -f `
    $QueueConfig.module_extraction_enabled, `
    $QueueConfig.compliance_enabled, `
    $QueueConfig.scorer_enabled, `
    $QueueConfig.scorer_vision_enabled, `
    $QueueConfig.model_management_enabled)
Write-Host "Module library: $($QueueConfig.module_library_dir)"
if ($null -ne $MaxClips -and $MaxClips -gt 0) {
    Write-Host "Clip limit per video: $MaxClips"
}
if ($null -ne $MinScore) {
    Write-Host "Minimum score override: $MinScore"
}
if ($ForceRescore) {
    Write-Host "Force rescore: enabled"
}
if ($ForceModules) {
    Write-Host "Force module recut: enabled"
}

if ($DryRun) {
    Write-Host "$PythonExe $($PythonArgs -join ' ')"
    exit 0
}

Push-Location $ProjectRoot
try {
    & $PythonExe @PythonArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
