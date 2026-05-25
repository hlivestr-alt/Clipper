param(
    [Parameter(Position = 0)]
    [ValidateSet("status", "stop", "continue", "start")]
    [string]$Action = "status",
    [string]$ControlFile = "",
    [string]$RunStateFile = "",
    [string]$StateFile = "",
    [string]$PythonExe = "python",
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSCommandPath
$ControlScript = Join-Path $ProjectRoot "queue_control.py"

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

$PythonArgs = @($ControlScript, $Action)

if (-not [string]::IsNullOrWhiteSpace($ControlFile)) {
    $PythonArgs += @("--control-file", (Resolve-ProjectPath $ControlFile))
}
if (-not [string]::IsNullOrWhiteSpace($RunStateFile)) {
    $PythonArgs += @("--forever-state-file", (Resolve-ProjectPath $RunStateFile))
}
if (-not [string]::IsNullOrWhiteSpace($StateFile)) {
    $PythonArgs += @("--queue-state-file", (Resolve-ProjectPath $StateFile))
}
if ($Json) {
    $PythonArgs += "--json"
}

Push-Location $ProjectRoot
try {
    & $PythonExe @PythonArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
