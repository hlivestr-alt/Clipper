# Cloudflare Dashboard Access

This runbook publishes the local Streamlit dashboard at:

```text
https://dashboard.proyaofficial.com
```

Streamlit stays bound to the remote PC at `http://127.0.0.1:8501`. Cloudflare Tunnel carries traffic out from the PC, and Cloudflare Access blocks users before they reach Streamlit.

## Local Dashboard

Start from the project root:

```powershell
.\run_dashboard.ps1
```

Quick local check on the remote PC:

```powershell
Invoke-WebRequest http://127.0.0.1:8501 -UseBasicParsing
```

`.streamlit/config.toml` keeps Streamlit on `127.0.0.1:8501` and sets the browser-facing address to `dashboard.proyaofficial.com`.

## Tunnel Setup

Recommended dashboard-managed setup:

1. Open Cloudflare Zero Trust.
2. Go to Networks > Tunnels.
3. Create a Cloudflared tunnel named `proya-dashboard`.
4. Choose Windows as the connector platform.
5. Copy the tunnel token command shown by Cloudflare.
6. Run PowerShell as Administrator in `C:\Data\clipper_test`.
7. Install the dedicated Windows service:

```powershell
.\setup_cloudflare_dashboard_tunnel.ps1 -TunnelToken "TOKEN_FROM_CLOUDFLARE" -InstallService
```

Public hostname settings:

```text
Subdomain: dashboard
Domain: proyaofficial.com
Type: HTTP
URL: 127.0.0.1:8501
```

Do not add router port forwarding or inbound firewall rules for Streamlit.

## Access Policy

Create a self-hosted Access application:

```text
Application name: PROYA Dashboard
Application domain: dashboard.proyaofficial.com
Session duration: 8h to 24h
Allowed email domain: proyaofficial.com
```

Allow users with `@proyaofficial.com` email addresses and deny everyone else. Treat all admitted users as operators because the dashboard includes queue and review controls.

## Operations

```powershell
Get-Process streamlit -ErrorAction SilentlyContinue
Get-Service ProyaDashboardCloudflared,Cloudflared -ErrorAction SilentlyContinue
Start-Service ProyaDashboardCloudflared
Stop-Service ProyaDashboardCloudflared
```

To temporarily disable external access, disable the Cloudflare Access application, set the policy to deny all users, or stop `ProyaDashboardCloudflared`.

## Troubleshooting

- Dashboard loads forever: confirm Streamlit is running on `127.0.0.1:8501`, the tunnel service targets `http://127.0.0.1:8501`, and `.streamlit/config.toml` has the expected browser address.
- Tunnel offline: check the Windows service and confirm the `proya-dashboard` connector is online in Cloudflare Zero Trust.
- Access denied: confirm the user email ends with `@proyaofficial.com` and the Access application domain is exactly `dashboard.proyaofficial.com`.

## Security

Keep Streamlit bound to `127.0.0.1`. Do not expose port `8501` directly to the internet.
