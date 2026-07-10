# Cloudflare Dashboard Access

This runbook publishes the local FastAPI + React dashboard at:

```text
https://dashboard.proyaofficial.com
```

The production app should be bound to the remote PC at `http://127.0.0.1:8000`.
Cloudflare Tunnel carries traffic out from the PC, and Cloudflare Access blocks
users before they reach the app.

## Local Dashboard

Start from the project root:

```powershell
pnpm --dir new_app build
python -m uvicorn clipper_app.web_api:app --host 127.0.0.1 --port 8000
```

Quick local check on the remote PC:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/api/health -UseBasicParsing
```

## Tunnel Update

Update the existing Cloudflare tunnel manually:

1. Open Cloudflare Zero Trust.
2. Go to Networks > Tunnels.
3. Open the existing dashboard tunnel.
4. Edit the public hostname for `dashboard.proyaofficial.com`.
5. Set the service type to `HTTP`.
6. Set the service URL to `127.0.0.1:8000`.
7. Save the hostname and confirm the connector is healthy.

Public hostname settings:

```text
Subdomain: dashboard
Domain: proyaofficial.com
Type: HTTP
URL: 127.0.0.1:8000
```

Do not add router port forwarding or inbound firewall rules for the app.

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
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
Get-Service ProyaDashboardCloudflared,Cloudflared -ErrorAction SilentlyContinue
Start-Service ProyaDashboardCloudflared
Stop-Service ProyaDashboardCloudflared
```

To temporarily disable external access, disable the Cloudflare Access application, set the policy to deny all users, or stop `ProyaDashboardCloudflared`.

## Troubleshooting

- Dashboard loads forever: confirm the app is running on `127.0.0.1:8000` and the tunnel service targets `http://127.0.0.1:8000`.
- Tunnel offline: check the Windows service and confirm the `proya-dashboard` connector is online in Cloudflare Zero Trust.
- Access denied: confirm the user email ends with `@proyaofficial.com` and the Access application domain is exactly `dashboard.proyaofficial.com`.

## Security

Keep the app bound to `127.0.0.1`. Do not expose port `8000` directly to the internet.
