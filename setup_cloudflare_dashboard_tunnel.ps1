param(
    [string]$TunnelName = "proya-dashboard",
    [string]$Hostname = "dashboard.proyaofficial.com",
    [string]$ServiceUrl = "http://127.0.0.1:8501",
    [string]$TunnelToken = "",
    [string]$ServiceName = "ProyaDashboardCloudflared",
    [switch]$InstallService,
    [switch]$RunNow,
    [switch]$SkipLoginCheck
)

$ErrorActionPreference = "Stop"
$CloudflaredPath = ""

function Require-Cloudflared {
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "cloudflared is not on PATH. Install Cloudflare Tunnel first."
    }
    $script:CloudflaredPath = $cmd.Source
    Write-Host "Using cloudflared: $($cmd.Source)"
}

function Test-OriginCert {
    $candidates = @(
        (Join-Path $env:USERPROFILE ".cloudflared\cert.pem"),
        (Join-Path $env:USERPROFILE ".cloudflare-warp\cert.pem"),
        (Join-Path $env:USERPROFILE "cloudflare-warp\cert.pem")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $true
        }
    }
    return $false
}

function Invoke-Cloudflared {
    param(
        [string[]]$CloudflaredArgs,
        [string]$Display = ""
    )
    if ([string]::IsNullOrWhiteSpace($Display)) {
        $Display = "cloudflared $($CloudflaredArgs -join ' ')"
    }
    Write-Host $Display
    & cloudflared @CloudflaredArgs
    if ($LASTEXITCODE -ne 0) {
        throw "cloudflared command failed with exit code $LASTEXITCODE"
    }
}

function Install-TokenService {
    param(
        [string]$TargetServiceName,
        [string]$Token
    )
    $existing = Get-Service -Name $TargetServiceName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Warning "Service $TargetServiceName already exists and is $($existing.Status). Not reinstalling it."
        Write-Warning "If this is the wrong tunnel, remove or reconfigure that service manually before reinstalling."
        return
    }

    $binaryPath = '"' + $CloudflaredPath + '" tunnel run --token "' + $Token + '"'
    Write-Host "Installing Windows service $TargetServiceName for tunnel token (token redacted)."
    New-Service `
        -Name $TargetServiceName `
        -DisplayName "Cloudflared tunnel: $TunnelName" `
        -BinaryPathName $binaryPath `
        -StartupType Automatic | Out-Null
    Start-Service -Name $TargetServiceName
    Write-Host "Started service $TargetServiceName."
}

Require-Cloudflared

if (-not [string]::IsNullOrWhiteSpace($TunnelToken)) {
    if ($InstallService) {
        Install-TokenService -TargetServiceName $ServiceName -Token $TunnelToken
    }

    if ($RunNow) {
        Invoke-Cloudflared `
            -CloudflaredArgs @("tunnel", "run", "--token", $TunnelToken) `
            -Display "cloudflared tunnel run --token ***REDACTED***"
    }

    if (-not $InstallService -and -not $RunNow) {
        Write-Host "Token accepted. Add -InstallService to install the Windows service or -RunNow to run in the foreground."
    }

    exit 0
}

if (-not $SkipLoginCheck -and -not (Test-OriginCert)) {
    Write-Warning "No Cloudflare origin cert found for this Windows user."
    Write-Host "Run this once, sign in as an account that can manage proyaofficial.com, then re-run this script:"
    Write-Host "  cloudflared tunnel login"
    Write-Host ""
    Write-Host "Alternative: create the tunnel in the Cloudflare dashboard and run this script with:"
    Write-Host '  .\setup_cloudflare_dashboard_tunnel.ps1 -TunnelToken "TOKEN_FROM_CLOUDFLARE" -InstallService'
    Write-Host "This installs a dedicated Windows service named $ServiceName by default."
    exit 2
}

$infoOutput = & cloudflared tunnel info $TunnelName 2>$null
if ($LASTEXITCODE -ne 0) {
    Invoke-Cloudflared @("tunnel", "create", $TunnelName)
}
else {
    Write-Host "Tunnel already exists: $TunnelName"
}

Invoke-Cloudflared @("tunnel", "route", "dns", $TunnelName, $Hostname)

Write-Host ""
Write-Host "Configure the public hostname in Cloudflare Zero Trust:"
Write-Host "  Hostname: $Hostname"
Write-Host "  Service:  $ServiceUrl"
Write-Host ""
Write-Host "To run the named tunnel in the foreground:"
Write-Host "  cloudflared tunnel run $TunnelName"

if ($InstallService) {
    $existing = Get-Service -Name Cloudflared -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Warning "A Cloudflared service already exists and is $($existing.Status). Not reinstalling it."
        Write-Warning "Review the existing service before changing it."
    }
    else {
        Invoke-Cloudflared @("service", "install")
    }
}

if ($RunNow) {
    Invoke-Cloudflared @("tunnel", "run", $TunnelName)
}
