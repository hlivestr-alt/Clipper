param(
    [string]$StreamlitExe = "streamlit",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8501
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSCommandPath

Push-Location $ProjectRoot
try {
    Write-Host "Starting Streamlit dashboard on http://$HostAddress`:$Port"
    & $StreamlitExe run app.py `
        --server.address $HostAddress `
        --server.port $Port `
        --server.headless true
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
