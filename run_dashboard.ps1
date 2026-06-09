param(
    [string]$StreamlitExe = "streamlit",
    [string]$PythonExe = "python",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8501
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSCommandPath

Push-Location $ProjectRoot
try {
    $fragmentCheck = @"
import streamlit as st
raise SystemExit(0 if hasattr(st, "fragment") else 1)
"@
    $fragmentCheck | & $PythonExe - | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Streamlit is missing st.fragment; upgrading Streamlit for the dashboard..."
        & $PythonExe -m pip install --upgrade "streamlit>=1.37.0"
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }

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
