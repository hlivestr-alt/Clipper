import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clipper_app.application.tiktok_oauth import (
    EncryptedTokenStore,
    TikTokAuthorizationRequired,
    TikTokOAuthConfig,
    TikTokOAuthError,
    TikTokOAuthService,
    _Cipher,
)


class _TestCipher(_Cipher):
    def encrypt(self, payload: bytes) -> bytes:
        return b"encrypted:" + payload[::-1]

    def decrypt(self, payload: bytes) -> bytes:
        if not payload.startswith(b"encrypted:"):
            raise TikTokOAuthError("not encrypted")
        return payload.removeprefix(b"encrypted:")[::-1]


class TikTokOAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = TikTokOAuthConfig(
            app_id="app-id",
            app_secret="app-secret",
            redirect_uri="https://proyaofficial.com/callback",
            token_store_path=root / "tokens.enc",
            state_store_path=root / "states.json",
            encryption_key="",
            refresh_skew_seconds=900,
            state_ttl_seconds=600,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _service(self, http_post, clock=lambda: 1_800_000_000.0):
        return TikTokOAuthService(
            self.config, cipher=_TestCipher(), http_post=http_post, clock=clock
        )

    def test_authorization_code_is_exchanged_once_and_never_persisted(self):
        calls = []

        def exchange(url, payload):
            calls.append((url, dict(payload)))
            return {"code": 0, "data": {
                "access_token": "stored-access-token",
                "advertiser_ids": ["advertiser-1"],
                "scope": [4],
            }}

        service = self._service(exchange)
        started = service.authorization_url()
        state = dict(
            item.split("=", 1)
            for item in started["authorization_url"].split("?", 1)[1].split("&")
        )["state"]
        result = service.exchange_callback("single-use-code", state)

        self.assertEqual(result["selected_advertiser_id"], "advertiser-1")
        self.assertEqual(service.credentials().access_token, "stored-access-token")
        self.assertEqual(len(calls), 1)
        self.assertNotIn(b"single-use-code", self.config.token_store_path.read_bytes())
        self.assertNotIn(b"stored-access-token", self.config.token_store_path.read_bytes())
        with self.assertRaisesRegex(TikTokOAuthError, "already used"):
            service.exchange_callback("single-use-code", state)

    def test_long_term_advertiser_token_has_no_scheduled_refresh(self):
        calls = []

        def exchange(_url, payload):
            calls.append(payload)
            return {"code": 0, "data": {
                "access_token": "long-term",
                "advertiser_ids": ["advertiser-1"],
            }}

        service = self._service(exchange, clock=lambda: 2_000_000_000.0)
        started = service.authorization_url()
        from urllib.parse import parse_qs, urlsplit
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        service.exchange_callback("code", state)
        for _ in range(3):
            self.assertEqual(service.credentials().access_token, "long-term")
        self.assertEqual(len(calls), 1)
        self.assertFalse(service.status()["refresh_supported"])
        self.assertIsNone(service.status()["access_token_expires_at"])

    def test_concurrent_expiry_triggers_exactly_one_refresh_and_persists_rotation(self):
        now = 2_000_000_000.0
        refresh_calls = 0
        call_lock = threading.Lock()

        def refresh(_url, payload):
            nonlocal refresh_calls
            with call_lock:
                refresh_calls += 1
            self.assertEqual(payload["refresh_token"], "refresh-old")
            return {"code": 0, "data": {
                "access_token": "access-new",
                "refresh_token": "refresh-new",
                "expires_in": 86400,
                "refresh_token_expires_in": 31536000,
            }}

        service = self._service(refresh, clock=lambda: now)
        with service.store.exclusive():
            service.store.save_unlocked({
                "schema_version": 1,
                "flow": "refreshable_short_term",
                "access_token": "access-old",
                "refresh_token": "refresh-old",
                "expires_at": now + 60,
                "refresh_token_expires_at": now + 100000,
                "advertiser_ids": ["advertiser-1"],
                "selected_advertiser_id": "advertiser-1",
                "scope": [],
                "obtained_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            })
        values = []
        threads = [threading.Thread(target=lambda: values.append(service.credentials().access_token)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(refresh_calls, 1)
        self.assertEqual(values, ["access-new"] * 8)
        with service.store.exclusive():
            persisted = service.store.load_unlocked()
        self.assertEqual(persisted["refresh_token"], "refresh-new")
        self.assertGreater(persisted["expires_at"], now)

    def test_expired_nonrefreshable_token_requires_reauthorization(self):
        now = 2_000_000_000.0
        service = self._service(lambda *_args: self.fail("refresh must not be called"), clock=lambda: now)
        with service.store.exclusive():
            service.store.save_unlocked({
                "schema_version": 1, "flow": "short", "access_token": "old", "refresh_token": "",
                "expires_at": now - 1, "refresh_token_expires_at": None,
                "advertiser_ids": ["advertiser-1"], "selected_advertiser_id": "advertiser-1",
                "scope": [], "obtained_at": "old", "updated_at": "old",
            })
        with self.assertRaises(TikTokAuthorizationRequired):
            service.credentials()
        self.assertTrue(service.status()["authorization_required"])

    def test_legacy_environment_token_migrates_to_encrypted_store(self):
        service = self._service(lambda *_args: self.fail("network must not be called"))
        with mock.patch.dict(os.environ, {
            "TIKTOK_ACCESS_TOKEN": "legacy-token",
            "TIKTOK_ADVERTISER_ID": "legacy-advertiser",
        }, clear=False):
            credentials = service.credentials()
        self.assertEqual(credentials.advertiser_id, "legacy-advertiser")
        self.assertNotIn(b"legacy-token", self.config.token_store_path.read_bytes())

    def test_multiple_advertisers_require_one_persisted_selection(self):
        service = self._service(lambda *_args: {})
        with service.store.exclusive():
            service.store.save_unlocked({
                "schema_version": 1, "flow": "advertiser_long_term", "access_token": "token", "refresh_token": "",
                "expires_at": None, "refresh_token_expires_at": None,
                "advertiser_ids": ["one", "two"], "selected_advertiser_id": "", "scope": [],
                "obtained_at": "now", "updated_at": "now",
            })
        with self.assertRaisesRegex(TikTokAuthorizationRequired, "Select"):
            service.credentials()
        selected = service.select_advertiser("two")
        self.assertEqual(selected["selected_advertiser_id"], "two")
        self.assertEqual(service.credentials().advertiser_id, "two")

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows-specific")
    def test_windows_default_store_uses_dpapi(self):
        from clipper_app.application.tiktok_oauth import _default_cipher

        store = EncryptedTokenStore(self.config.token_store_path, _default_cipher(self.config))
        with store.exclusive():
            store.save_unlocked({"access_token": "dpapi-secret"})
            loaded = store.load_unlocked()
        self.assertEqual(loaded["access_token"], "dpapi-secret")
        self.assertNotIn(b"dpapi-secret", self.config.token_store_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
