# Cloudflare Tunnel Access Runbook

This runbook publishes the local Streamlit dashboard at:

```text
https://dashboard.proyaofficial.com
```

The dashboard stays bound to the remote PC only at `http://127.0.0.1:8501`. Cloudflare Tunnel carries traffic out from the PC to Cloudflare, and Cloudflare Access blocks users before they reach Streamlit.

## Local Streamlit

Start the dashboard from the project root:

```powershell
.\run_dashboard.ps1
```

The project config in `.streamlit/config.toml` keeps Streamlit on `127.0.0.1:8501` and sets the browser-facing address to `dashboard.proyaofficial.com`.

Quick local check on the remote PC:

```powershell
Invoke-WebRequest http://127.0.0.1:8501 -UseBasicParsing
```

## Recommended Cloudflare Setup

Use this path when setting up from the Cloudflare dashboard.

1. Open Cloudflare Zero Trust.
2. Go to Networks > Tunnels.
3. Create a Cloudflared tunnel named `proya-dashboard`.
4. Choose Windows as the connector platform.
5. Copy the tunnel token command shown by Cloudflare.
6. On the remote PC, run PowerShell as Administrator in `C:\Data\clipper_test`.
7. Install the tunnel service with the token:

```powershell
.\setup_cloudflare_dashboard_tunnel.ps1 -TunnelToken "TOKEN_FROM_CLOUDFLARE" -InstallService
```

By default this creates a dedicated Windows service named `ProyaDashboardCloudflared`. This avoids overwriting any existing generic `Cloudflared` service on the PC.

8. In the tunnel Public Hostname settings, add:

```text
Subdomain: dashboard
Domain: proyaofficial.com
Type: HTTP
URL: 127.0.0.1:8501
```

Do not add router port forwarding or inbound firewall rules for Streamlit.

## CLI-Managed Alternative

Use this path only if the Windows user is allowed to manage `proyaofficial.com` in Cloudflare.

```powershell
cloudflared tunnel login
.\setup_cloudflare_dashboard_tunnel.ps1 -InstallService
```

The helper script creates or reuses `proya-dashboard`, routes DNS for `dashboard.proyaofficial.com`, and avoids replacing an existing `Cloudflared` Windows service.

## Cloudflare Access Policy

Create a self-hosted Access application:

```text
Application name: PROYA Dashboard
Application domain: dashboard.proyaofficial.com
Session duration: 8h to 24h
Allowed email domain: proyaofficial.com
Admin/owner email: admin@proyaofficial.com
```

Policy behavior:

- Allow users with email addresses ending in `@proyaofficial.com`.
- Deny everyone else.
- Keep this as an operator console; no app-level view-only mode exists in this phase.
- Enable Cloudflare Access login/audit logs if available in the account.

## Operations

Check the dashboard process:

```powershell
Get-Process streamlit -ErrorAction SilentlyContinue
```

Check Cloudflared service status:

```powershell
Get-Service ProyaDashboardCloudflared,Cloudflared -ErrorAction SilentlyContinue
```

Start or stop Cloudflared:

```powershell
Start-Service ProyaDashboardCloudflared
Stop-Service ProyaDashboardCloudflared
```

Temporarily disable external dashboard access:

- Preferred: disable the Cloudflare Access application or set the policy to deny all users.
- Alternative: stop the `ProyaDashboardCloudflared` service.

Add or remove users:

- If access is by email domain, manage who owns an active `@proyaofficial.com` email account.
- For tighter control later, replace the domain rule with explicit email addresses.

## Test Checklist

Local:

- Open `http://127.0.0.1:8501` on the remote PC.
- Confirm Streamlit is not exposed with router port forwarding.

Tunnel:

- Open `https://dashboard.proyaofficial.com` from an office browser.
- Confirm Cloudflare Access appears before Streamlit.
- Sign in with an `@proyaofficial.com` account.
- Confirm the dashboard loads, refreshes, and queue status updates.

Access control:

- Try a non-`@proyaofficial.com` email and confirm access is denied.
- Test a mobile browser with an allowed email.

Reliability:

- Restart Streamlit only and confirm the tunnel works again once Streamlit returns.
- Stop `cloudflared` and confirm external access fails closed.
- Start `cloudflared` and confirm external access returns.

## Troubleshooting

Dashboard loads forever:

- Confirm Streamlit is running on `127.0.0.1:8501`.
- Confirm the tunnel service target is `http://127.0.0.1:8501`.
- Confirm `.streamlit/config.toml` has `browser.serverAddress = "dashboard.proyaofficial.com"`.
- Leave CORS and XSRF protection enabled unless diagnosing a proxy issue briefly.

Tunnel offline:

- Check `Get-Service ProyaDashboardCloudflared,Cloudflared -ErrorAction SilentlyContinue`.
- In Cloudflare Zero Trust, confirm the `proya-dashboard` connector is online.
- If the service was installed with a token, manage public hostnames from the Cloudflare dashboard.
- If using CLI-managed tunnels, run `cloudflared tunnel info proya-dashboard`.

Access denied for a valid user:

- Confirm the user email ends with `@proyaofficial.com`.
- Confirm the Access application domain is exactly `dashboard.proyaofficial.com`.
- Confirm the allow policy is above any deny policy that would block the user.

Security notes:

- Keep Streamlit bound to `127.0.0.1`.
- Do not expose port `8501` directly to the internet.
- Treat all admitted users as operators because the dashboard includes queue and review controls.
