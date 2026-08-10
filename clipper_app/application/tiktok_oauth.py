from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import portalocker


OAUTH_BASE_URL = "https://business-api.tiktok.com"
ADVERTISER_TOKEN_ENDPOINT = f"{OAUTH_BASE_URL}/open_api/v1.3/oauth2/access_token/"
SHORT_TOKEN_REFRESH_ENDPOINT = f"{OAUTH_BASE_URL}/open_api/v1.3/tt_user/oauth2/refresh_token/"
AUTHORIZATION_ENDPOINT = f"{OAUTH_BASE_URL}/portal/auth"
SUPPORTED_CALLBACK_PATHS = frozenset({"/callback", "/api/integrations/tiktok/oauth/callback"})


class TikTokOAuthError(RuntimeError):
    pass


class TikTokAuthorizationRequired(TikTokOAuthError):
    pass


@dataclass(frozen=True)
class TikTokCredentials:
    access_token: str
    advertiser_id: str


def environment_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value or os.name != "nt":
        return value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            raw, _kind = winreg.QueryValueEx(key, name)
        return str(raw or "").strip()
    except (FileNotFoundError, OSError):
        return ""


@dataclass(frozen=True)
class TikTokOAuthConfig:
    app_id: str
    app_secret: str
    redirect_uri: str
    token_store_path: Path
    state_store_path: Path
    encryption_key: str
    refresh_skew_seconds: int = 900
    state_ttl_seconds: int = 600

    @classmethod
    def from_environment(cls, cfg: Any) -> "TikTokOAuthConfig":
        working = Path(str(getattr(cfg, "WORKING_DIR", "working") or "working"))
        if not working.is_absolute():
            working = Path.cwd() / working
        default_token_path = working / "secrets" / "tiktok_oauth.tokens"
        token_path = Path(environment_value("TIKTOK_TOKEN_STORE") or default_token_path)
        if not token_path.is_absolute():
            token_path = Path.cwd() / token_path
        state_path = Path(environment_value("TIKTOK_OAUTH_STATE_STORE") or token_path.with_suffix(".states.json"))
        if not state_path.is_absolute():
            state_path = Path.cwd() / state_path
        return cls(
            app_id=environment_value("TIKTOK_APP_ID"),
            app_secret=environment_value("TIKTOK_APP_SECRET"),
            redirect_uri=environment_value("TIKTOK_REDIRECT_URI"),
            token_store_path=token_path.resolve(),
            state_store_path=state_path.resolve(),
            encryption_key=environment_value("TIKTOK_TOKEN_ENCRYPTION_KEY"),
            refresh_skew_seconds=max(60, int(environment_value("TIKTOK_TOKEN_REFRESH_SKEW_SECONDS") or 900)),
            state_ttl_seconds=max(60, min(3600, int(environment_value("TIKTOK_OAUTH_STATE_TTL_SECONDS") or 600))),
        )

    def validate_app(self) -> None:
        if not self.app_id or not self.app_secret:
            raise TikTokOAuthError("TikTok OAuth requires TIKTOK_APP_ID and TIKTOK_APP_SECRET.")

    def validate_redirect(self) -> None:
        if not self.redirect_uri:
            raise TikTokOAuthError("TikTok OAuth requires an explicit TIKTOK_REDIRECT_URI.")
        try:
            parsed = urllib.parse.urlsplit(self.redirect_uri)
        except ValueError as exc:
            raise TikTokOAuthError("TIKTOK_REDIRECT_URI is malformed.") from exc
        local = parsed.hostname in {"127.0.0.1", "localhost"}
        if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
            raise TikTokOAuthError("TIKTOK_REDIRECT_URI must use HTTPS outside localhost.")
        normalized_path = parsed.path.rstrip("/") or "/"
        if normalized_path not in SUPPORTED_CALLBACK_PATHS:
            raise TikTokOAuthError(
                "TIKTOK_REDIRECT_URI must point to /callback or /api/integrations/tiktok/oauth/callback on this backend."
            )


class _Cipher:
    def encrypt(self, payload: bytes) -> bytes:
        raise NotImplementedError

    def decrypt(self, payload: bytes) -> bytes:
        raise NotImplementedError


class _DpapiCipher(_Cipher):
    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    def _transform(self, payload: bytes, function_name: str) -> bytes:
        buffer = ctypes.create_string_buffer(payload)
        source = self._Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        destination = self._Blob()
        crypt32 = ctypes.windll.crypt32
        function = getattr(crypt32, function_name)
        if function_name == "CryptProtectData":
            ok = function(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(destination))
        else:
            ok = function(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(destination))
        if not ok:
            raise TikTokOAuthError("Windows could not decrypt the TikTok credential store.")
        try:
            return ctypes.string_at(destination.pbData, destination.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(destination.pbData)

    def encrypt(self, payload: bytes) -> bytes:
        return b"dpapi-v1:" + base64.urlsafe_b64encode(self._transform(payload, "CryptProtectData"))

    def decrypt(self, payload: bytes) -> bytes:
        if not payload.startswith(b"dpapi-v1:"):
            raise TikTokOAuthError("TikTok credential store encryption format is invalid.")
        try:
            encrypted = base64.urlsafe_b64decode(payload.split(b":", 1)[1])
        except (ValueError, binascii.Error) as exc:
            raise TikTokOAuthError("TikTok credential store is corrupt.") from exc
        return self._transform(encrypted, "CryptUnprotectData")


class _FernetCipher(_Cipher):
    def __init__(self, key: str) -> None:
        if not key:
            raise TikTokOAuthError(
                "TIKTOK_TOKEN_ENCRYPTION_KEY is required for encrypted token storage on this platform."
            )
        try:
            from cryptography.fernet import Fernet

            self._fernet = Fernet(key.encode("ascii"))
        except (ImportError, ValueError, UnicodeEncodeError) as exc:
            raise TikTokOAuthError("TIKTOK_TOKEN_ENCRYPTION_KEY must be a valid Fernet key.") from exc

    def encrypt(self, payload: bytes) -> bytes:
        return b"fernet-v1:" + self._fernet.encrypt(payload)

    def decrypt(self, payload: bytes) -> bytes:
        if not payload.startswith(b"fernet-v1:"):
            raise TikTokOAuthError("TikTok credential store encryption format is invalid.")
        try:
            return self._fernet.decrypt(payload.split(b":", 1)[1])
        except Exception as exc:
            raise TikTokOAuthError("TikTok credential store could not be decrypted.") from exc


class _UnavailableCipher(_Cipher):
    def __init__(self, message: str) -> None:
        self.message = message

    def encrypt(self, payload: bytes) -> bytes:
        raise TikTokOAuthError(self.message)

    def decrypt(self, payload: bytes) -> bytes:
        raise TikTokOAuthError(self.message)


def _default_cipher(config: TikTokOAuthConfig) -> _Cipher:
    if os.name == "nt":
        return _DpapiCipher()
    try:
        return _FernetCipher(config.encryption_key)
    except TikTokOAuthError as exc:
        return _UnavailableCipher(str(exc))


class EncryptedTokenStore:
    def __init__(self, path: Path, cipher: _Cipher) -> None:
        self.path = path
        self.cipher = cipher
        self._thread_lock = threading.RLock()

    def ensure_available(self) -> None:
        if isinstance(self.cipher, _UnavailableCipher):
            raise TikTokOAuthError(self.cipher.message)

    @contextmanager
    def exclusive(self) -> Iterator["EncryptedTokenStore"]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(f"{self.path}.lock")
        with self._thread_lock, portalocker.Lock(str(lock_path), mode="a", timeout=30):
            yield self

    def load_unlocked(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            raw = self.cipher.decrypt(self.path.read_bytes())
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TikTokOAuthError("TikTok credential store is unreadable.") from exc
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise TikTokOAuthError("TikTok credential store is incomplete.")
        return payload

    def save_unlocked(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self.cipher.encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class OAuthStateStore:
    def __init__(self, path: Path, ttl_seconds: int) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self._thread_lock = threading.RLock()

    def issue(self) -> str:
        state = secrets.token_urlsafe(32)
        digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
        now = int(time.time())
        with self._locked() as states:
            states[digest] = now + self.ttl_seconds
        return state

    def consume(self, state: str) -> None:
        digest = hashlib.sha256(str(state or "").encode("utf-8")).hexdigest()
        now = int(time.time())
        found = False
        with self._locked() as states:
            expires_at = int(states.pop(digest, 0) or 0)
            found = expires_at >= now
        if not found:
            raise TikTokOAuthError("TikTok OAuth state is invalid, expired, or already used.")

    @contextmanager
    def _locked(self) -> Iterator[dict[str, int]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, portalocker.Lock(str(self.path) + ".lock", mode="a", timeout=30):
            states: dict[str, int] = {}
            if self.path.is_file():
                try:
                    payload = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        states = {str(key): int(value) for key, value in payload.items()}
                except (OSError, ValueError, json.JSONDecodeError):
                    states = {}
            now = int(time.time())
            states = {key: expiry for key, expiry in states.items() if expiry >= now}
            yield states
            self.path.write_text(json.dumps(states, separators=(",", ":")), encoding="utf-8")
            os.chmod(self.path, 0o600)


class TikTokOAuthService:
    def __init__(
        self,
        config: TikTokOAuthConfig,
        *,
        cipher: _Cipher | None = None,
        http_post: Any = None,
        clock: Any = None,
    ) -> None:
        self.config = config
        self.store = EncryptedTokenStore(config.token_store_path, cipher or _default_cipher(config))
        self.states = OAuthStateStore(config.state_store_path, config.state_ttl_seconds)
        self.http_post = http_post or _post_json
        self.clock = clock or time.time

    @classmethod
    def from_environment(cls, cfg: Any) -> "TikTokOAuthService":
        return cls(TikTokOAuthConfig.from_environment(cfg))

    def authorization_url(self) -> dict[str, Any]:
        self.config.validate_app()
        self.config.validate_redirect()
        self.store.ensure_available()
        state = self.states.issue()
        query = urllib.parse.urlencode({
            "app_id": self.config.app_id,
            "state": state,
            "redirect_uri": self.config.redirect_uri,
        })
        return {
            "authorization_url": f"{AUTHORIZATION_ENDPOINT}?{query}",
            "redirect_uri": self.config.redirect_uri,
            "expires_in": self.config.state_ttl_seconds,
        }

    def exchange_callback(self, auth_code: str, state: str) -> dict[str, Any]:
        self.config.validate_app()
        self.config.validate_redirect()
        self.store.ensure_available()
        if not auth_code or len(auth_code) > 2048:
            raise TikTokOAuthError("TikTok callback did not include a valid authorization code.")
        self.states.consume(state)
        payload = self.http_post(ADVERTISER_TOKEN_ENDPOINT, {
            "app_id": self.config.app_id,
            "secret": self.config.app_secret,
            "auth_code": auth_code,
        })
        record = self._record_from_response(payload, existing=None, source="authorization_code")
        with self.store.exclusive():
            self.store.save_unlocked(record)
        return self._public_record(record)

    def credentials(self) -> TikTokCredentials:
        with self.store.exclusive():
            record = self.store.load_unlocked()
            if record is None:
                record = self._legacy_record()
                if record is not None:
                    self.store.save_unlocked(record)
            if record is None or record.get("invalidated_at"):
                raise TikTokAuthorizationRequired("TikTok authorization is required.")
            record = self._refresh_if_needed(record)
            advertiser_id = str(record.get("selected_advertiser_id") or "")
            if not advertiser_id:
                raise TikTokAuthorizationRequired("Select an authorized TikTok advertiser account.")
            return TikTokCredentials(str(record["access_token"]), advertiser_id)

    def select_advertiser(self, advertiser_id: str) -> dict[str, Any]:
        value = str(advertiser_id or "").strip()
        with self.store.exclusive():
            record = self.store.load_unlocked()
            if record is None:
                raise TikTokAuthorizationRequired("TikTok authorization is required.")
            if value not in {str(item) for item in record.get("advertiser_ids") or []}:
                raise TikTokOAuthError("Advertiser is not authorized by the stored TikTok token.")
            record["selected_advertiser_id"] = value
            record["updated_at"] = _iso_timestamp(self.clock())
            self.store.save_unlocked(record)
            return self._public_record(record)

    def mark_authorization_invalid(self, reason: str = "TikTok rejected the stored access token.") -> None:
        with self.store.exclusive():
            record = self.store.load_unlocked()
            if record is None:
                return
            record["invalidated_at"] = _iso_timestamp(self.clock())
            record["invalid_reason"] = _sanitize_message(reason)
            record["updated_at"] = record["invalidated_at"]
            self.store.save_unlocked(record)

    def status(self) -> dict[str, Any]:
        config_error = ""
        storage_error = ""
        try:
            self.config.validate_app()
            self.config.validate_redirect()
        except TikTokOAuthError as exc:
            config_error = str(exc)
        record = None
        try:
            self.store.ensure_available()
            with self.store.exclusive():
                record = self.store.load_unlocked()
                if record is None:
                    record = self._legacy_record()
                    if record is not None:
                        self.store.save_unlocked(record)
        except TikTokOAuthError as exc:
            storage_error = str(exc)
        public = self._public_record(record) if record else {}
        return {
            "app_configured": bool(self.config.app_id and self.config.app_secret),
            "redirect_configured": bool(self.config.redirect_uri),
            "redirect_uri": self.config.redirect_uri,
            "callback_supported": _callback_supported(self.config.redirect_uri),
            "storage_path": str(self.config.token_store_path),
            "storage_encrypted": not bool(storage_error),
            "connected": bool(record and not record.get("invalidated_at")),
            "authorization_required": not bool(record and not record.get("invalidated_at")),
            "configuration_error": config_error,
            "storage_error": storage_error,
            **public,
        }

    def _legacy_record(self) -> dict[str, Any] | None:
        access_token = environment_value("TIKTOK_ACCESS_TOKEN")
        advertiser_id = environment_value("TIKTOK_ADVERTISER_ID")
        if not access_token or not advertiser_id:
            return None
        now = self.clock()
        return {
            "schema_version": 1,
            "flow": "advertiser_long_term",
            "access_token": access_token,
            "refresh_token": "",
            "expires_at": None,
            "refresh_token_expires_at": None,
            "advertiser_ids": [advertiser_id],
            "selected_advertiser_id": advertiser_id,
            "scope": [],
            "obtained_at": _iso_timestamp(now),
            "updated_at": _iso_timestamp(now),
            "source": "legacy_environment_migration",
        }

    def _refresh_if_needed(self, record: dict[str, Any]) -> dict[str, Any]:
        expires_at = record.get("expires_at")
        if expires_at is None or float(expires_at) - self.clock() > self.config.refresh_skew_seconds:
            return record
        refresh_token = str(record.get("refresh_token") or "")
        refresh_expires = record.get("refresh_token_expires_at")
        if not refresh_token or (refresh_expires is not None and float(refresh_expires) <= self.clock()):
            record["invalidated_at"] = _iso_timestamp(self.clock())
            record["invalid_reason"] = "Stored TikTok token expired and cannot be refreshed."
            self.store.save_unlocked(record)
            raise TikTokAuthorizationRequired("TikTok authorization has expired and must be renewed.")
        self.config.validate_app()
        payload = self.http_post(SHORT_TOKEN_REFRESH_ENDPOINT, {
            "client_id": self.config.app_id,
            "client_secret": self.config.app_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        refreshed = self._record_from_response(payload, existing=record, source="refresh_token")
        self.store.save_unlocked(refreshed)
        return refreshed

    def _record_from_response(
        self, payload: dict[str, Any], *, existing: dict[str, Any] | None, source: str
    ) -> dict[str, Any]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        access_token = str(data.get("access_token") or "")
        if not access_token:
            raise TikTokOAuthError("TikTok token response did not include an access token.")
        now = self.clock()
        advertisers = [str(item) for item in data.get("advertiser_ids") or (existing or {}).get("advertiser_ids") or []]
        preferred = environment_value("TIKTOK_ADVERTISER_ID")
        selected = str((existing or {}).get("selected_advertiser_id") or "")
        if preferred in advertisers:
            selected = preferred
        elif selected not in advertisers:
            selected = advertisers[0] if len(advertisers) == 1 else ""
        expires_in = _optional_positive_number(data.get("expires_in"))
        refresh_expires_in = _optional_positive_number(data.get("refresh_token_expires_in"))
        refresh_token = str(data.get("refresh_token") or (existing or {}).get("refresh_token") or "")
        return {
            "schema_version": 1,
            "flow": "refreshable_short_term" if refresh_token else "advertiser_long_term",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": now + expires_in if expires_in else None,
            "refresh_token_expires_at": now + refresh_expires_in if refresh_expires_in else (existing or {}).get("refresh_token_expires_at"),
            "advertiser_ids": advertisers,
            "selected_advertiser_id": selected,
            "scope": data.get("scope") or (existing or {}).get("scope") or [],
            "open_id": str(data.get("open_id") or (existing or {}).get("open_id") or ""),
            "obtained_at": (existing or {}).get("obtained_at") or _iso_timestamp(now),
            "updated_at": _iso_timestamp(now),
            "source": source,
        }

    @staticmethod
    def _public_record(record: dict[str, Any] | None) -> dict[str, Any]:
        if not record:
            return {}
        return {
            "flow": record.get("flow"),
            "refresh_supported": bool(record.get("refresh_token")),
            "access_token_expires_at": _iso_timestamp(record.get("expires_at")) if record.get("expires_at") else None,
            "refresh_token_expires_at": _iso_timestamp(record.get("refresh_token_expires_at")) if record.get("refresh_token_expires_at") else None,
            "advertiser_ids": list(record.get("advertiser_ids") or []),
            "selected_advertiser_id": record.get("selected_advertiser_id") or None,
            "scope": record.get("scope") or [],
            "updated_at": record.get("updated_at"),
            "invalidated_at": record.get("invalidated_at"),
            "invalid_reason": record.get("invalid_reason") or "",
        }


def _post_json(url: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TikTokOAuthError(f"TikTok token exchange failed with HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise TikTokOAuthError(f"TikTok token exchange failed: {type(exc).__name__}.") from exc
    if not isinstance(result, dict):
        raise TikTokOAuthError("TikTok token exchange returned an invalid response.")
    if result.get("code") not in {0, "0", None}:
        raise TikTokOAuthError(_sanitize_message(str(result.get("message") or "TikTok rejected the token request.")))
    return result


def _callback_supported(redirect_uri: str) -> bool:
    try:
        return (urllib.parse.urlsplit(redirect_uri).path.rstrip("/") or "/") in SUPPORTED_CALLBACK_PATHS
    except ValueError:
        return False


def _optional_positive_number(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _iso_timestamp(value: Any) -> str:
    numeric = float(value)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(numeric))


def _sanitize_message(message: str) -> str:
    sanitized = str(message or "TikTok OAuth failed.").replace("\r", " ").replace("\n", " ")
    for name in ("TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN"):
        secret = environment_value(name)
        if secret:
            sanitized = sanitized.replace(secret, "[redacted]")
    return " ".join(sanitized.split())[:500]
