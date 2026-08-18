# Cloudflare Dashboard Access

This runbook describes the existing intended hostname:

```text
https://dashboard.proyaofficial.com
```

Cloudflare Tunnel can publish a FastAPI process that remains bound to `127.0.0.1`, and Cloudflare Access can provide an external identity perimeter. Clipper still enforces its own Bearer-token boundary for every mutation and sensitive read.

## Important Current Limitation

The compiled browser client does not expose or persist `CLIPPER_CONTROL_TOKEN`. Automatic token forwarding exists only in:

- Electron, which injects a fresh token for its exact managed loopback origin.
- `run_new_app.ps1` development mode, where the Vite proxy injects the shared token.

Therefore a plain built SPA served through Cloudflare can load public read endpoints, but protected pages/actions will return `401` or `503`. Cloudflare Access identity is not currently translated into Clipper's Bearer token.

Do not put a shared control token in frontend JavaScript, local storage, a public URL, or Cloudflare-visible static configuration. Treat the remote browser deployment as read-limited until an approved server-side identity-to-actor/token integration is implemented. Electron remains the supported complete control experience.

## Local Backend

Build and start the same-origin app from the repository root:

```powershell
pnpm.cmd --dir new_app build
$env:CLIPPER_CONTROL_TOKEN = '<strong value for authenticated API clients>'
$env:CLIPPER_CONTROL_ACTOR = 'remote:operator'
$env:CLIPPER_ALLOWED_HOSTS = 'dashboard.proyaofficial.com'
python -m uvicorn clipper_app.web_api:app --host 127.0.0.1 --port 8000
```

Local health check:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/api/health -UseBasicParsing
```

The token permits authenticated API clients but, by itself, does not make the compiled browser UI send that token.

## Tunnel Configuration

In Cloudflare Zero Trust, edit the existing tunnel/public hostname:

```text
Subdomain: dashboard
Domain: proyaofficial.com
Type: HTTP
URL: 127.0.0.1:8000
```

Do not add router port forwarding or inbound firewall rules. Keep Uvicorn on loopback.

## Access Policy

Create or retain a self-hosted Access application for `dashboard.proyaofficial.com`, with an appropriate session duration and an allow policy for approved `@proyaofficial.com` identities. Deny everyone else.

Access protects the site perimeter but does not replace Clipper's Bearer auth. Users admitted by Access can see whatever unauthenticated read endpoints expose, so review that read boundary before enabling remote access.

TikTok OAuth callbacks require separate care. The configured `/callback` or `/api/integrations/tiktok/oauth/callback` path must reach the same Clipper instance and bypass an interactive Access challenge, while all other app paths remain protected. Do not log callback query strings.

## Operations

```powershell
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
Get-Service ProyaDashboardCloudflared,Cloudflared -ErrorAction SilentlyContinue
Start-Service ProyaDashboardCloudflared
Stop-Service ProyaDashboardCloudflared
```

To disable external access, stop the dedicated tunnel service or change the Access application to deny all. Confirm the exact Windows service name before acting.

## Troubleshooting

- `400 Invalid host header`: add the exact public hostname to `CLIPPER_ALLOWED_HOSTS` and restart the backend.
- Public page loads but actions fail: expected for the current compiled browser client; verify Electron/Vite locally or use an authenticated API client.
- `401`: the route requires a valid Bearer token.
- `503 Control authentication is not configured`: the backend was started without `CLIPPER_CONTROL_TOKEN`.
- Tunnel offline: verify the Windows service and connector health in Cloudflare Zero Trust.
- Access denied: verify the identity policy and application domain.
- TikTok callback shows an Access login: create the narrowly scoped callback bypass and verify exact redirect-URI equality.

## Security

- Keep port 8000 off the public network and Uvicorn on `127.0.0.1`.
- Keep Access in front of all non-callback routes.
- Never expose the control token to browser code or logs.
- Add only the exact required trusted hostname/origin values.
- Treat callback bypasses and query-string logging as security-sensitive configuration.
