# TikTok Business OAuth Setup

Clipper uses TikTok API for Business advertiser authorization for the Trends workspace. It is not a TikTok publishing integration or the general public Research API.

The advertiser access token returned by the implemented long-term-token flow is stored until authorization is revoked. Clipper also understands refresh-capable short-term token records and refreshes them under process/file locks when expiry metadata and a refresh token are present.

## Callback Ownership

Clipper implements both callback paths:

- `/callback`.
- `/api/integrations/tiktok/oauth/callback`.

Choose one public HTTPS URL that routes to this Clipper backend and configure the exact same value in the TikTok Business developer application and `TIKTOK_REDIRECT_URI`.

For example, use `https://proyaofficial.com/callback` only when that hostname/path reaches Clipper. The n8n callback `https://n8n.proyaofficial.com/rest/oauth2-credential/callback` belongs to n8n and cannot complete Clipper's direct OAuth lifecycle unless n8n is deliberately made the credential owner.

The public callback must:

- reach the same deployment that created the short-lived OAuth state;
- bypass interactive Cloudflare Access/login challenges for that exact callback path;
- preserve the HTTPS host and path configured in TikTok;
- avoid reverse-proxy/query-string logs because the temporary code arrives in the query string;
- remain the only unauthenticated exception needed for the OAuth flow.

Add the public hostname to `CLIPPER_ALLOWED_HOSTS`. The callback middleware removes the query string from normal downstream request handling and returns `Cache-Control: no-store`/`Pragma: no-cache`.

## Required Configuration

Set these in the backend environment or deployment secret manager, never in `config.py` or frontend source:

```text
TIKTOK_APP_ID=<TikTok Business app ID>
TIKTOK_APP_SECRET=<TikTok Business app secret>
TIKTOK_REDIRECT_URI=https://your-clipper-domain.example/callback
```

Windows uses the current user's DPAPI, so a separate encryption key is not required. Linux/macOS require a stable Fernet key:

```text
TIKTOK_TOKEN_ENCRYPTION_KEY=<stable Fernet key from the deployment secret manager>
```

Do not rotate/remove the key while its token store exists without a migration. The encrypted store defaults to `working/secrets/tiktok_oauth.tokens`; override it with `TIKTOK_TOKEN_STORE`. OAuth state defaults alongside the working secrets area and can be overridden with `TIKTOK_OAUTH_STATE_STORE`.

Optional timing variables include `TIKTOK_TOKEN_REFRESH_SKEW_SECONDS` and `TIKTOK_OAUTH_STATE_TTL_SECONDS`.

`TIKTOK_ACCESS_TOKEN` and `TIKTOK_ADVERTISER_ID` exist only for one-time migration when no encrypted store is present. Remove the legacy access-token variable after `/api/integrations/tiktok/oauth/status` confirms encrypted connected state.

## Initial Authorization

1. Start the control app with the required environment available to FastAPI.
2. Open **Trends** and select **Connect TikTok**.
3. Electron opens only the approved TikTok Business authorization URL in the system browser. Browser development opens the same provider flow.
4. Complete advertiser authorization.
5. TikTok returns to the configured Clipper callback. Clipper validates/consumes state, exchanges the one-time code, encrypts the credential, and never persists the code.
6. If multiple advertiser accounts were authorized, select one in Trends. The selection is saved with the encrypted credential.

The callback response tells the operator whether to return to Clipper or select an advertiser. Provider errors shown/logged by the service are sanitized to avoid token disclosure.

## Trends Runtime

After connection, the Trends page can:

- retrieve ranked hashtags and video references for country/window/category inputs;
- store snapshots and provider diagnostics in the shared SQLite catalog;
- require an explicit rights confirmation before `yt-dlp` downloads;
- validate, checksum, and link approved local media under `TREND_MEDIA_DIR`;
- analyze selected media with FFprobe/OpenCV/transcript metrics and optional Qwen-VL semantics;
- store fingerprints/patterns and display a suggested variation profile without applying it.

Current configuration pins `yt-dlp==2026.7.4`, reserves 5 GiB free space, permits two concurrent downloads, and leaves `TREND_QWEN_ENABLED=False`. These values are implementation defaults, not TikTok guarantees.

If TikTok rejects/revokes a long-term token, Clipper marks authorization required and presents **Connect TikTok** again. Refresh-capable credentials are rotated and saved atomically after a successful refresh.

## Verification and Troubleshooting

- `app_configured=false`: check `TIKTOK_APP_ID` and `TIKTOK_APP_SECRET` in the backend process.
- `redirect_configured=false` or callback unsupported: check the HTTPS URI and supported Clipper path.
- Callback reaches Cloudflare login: narrowly bypass Access for the exact callback path.
- `storage_encrypted=false`/storage error: verify Windows user identity or the Fernet key and storage permissions.
- Connected but no advertiser selected: choose one of the returned advertiser IDs in Trends.
- Download unavailable: verify `yt-dlp`, rights confirmation, provider media type/availability, and free-space diagnostics.

Automated tests mock provider/media behavior. A live authorization/discovery run remains a deployment smoke test.
