from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clipper_app.application.api_security import ApiSecuritySettings
from clipper_app.application.control_services import ControlJobService, SettingsService
from clipper_app.application.read_services import ReadDashboardService
from clipper_app.application.settings import LegacyConfigProvider
from clipper_app.modular_scanner.media import source_record
from clipper_app.modular_scanner.repository import ScannerRepository
from clipper_app.modular_scanner.service import ModularScannerService


@unittest.skipIf(importlib.util.find_spec("fastapi") is None, "fastapi is not installed")
class ModularScannerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        from clipper_app.web_api import create_app

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.vods = root / "vods"
        self.working = root / "working"
        output = root / "output"
        information = root / "information"
        for path in (self.vods, self.working, output, information):
            path.mkdir()
        self.content = bytes(range(100))
        self.vod = self.vods / "selected.mp4"
        self.vod.write_bytes(self.content)
        state = self.working / "state.json"
        state.write_text('{"queue_status":"idle","videos":{}}', encoding="utf-8")
        self.cfg = SimpleNamespace(
            OUTPUT_DIR=str(output), WORKING_DIR=str(self.working), QUEUE_INPUT_DIR=str(self.vods),
            QUEUE_STATE_FILE=str(state), QUEUE_CONTROL_FILE=str(self.working / "control.json"),
            QUEUE_FOREVER_STATE_FILE=str(self.working / "forever.json"), QUEUE_STAGE_ADMISSION_LIMIT=3,
            QUEUE_DASHBOARD_RUNNING_STALL_SECONDS=7200.0, QUEUE_DASHBOARD_QUEUED_STALL_SECONDS=86400.0,
            VARIANTS_PER_CLIP=1, FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
            FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf", FONT_HOOK_FALLBACKS=[],
            SUBTITLE_FONT_DIR="assets/fonts", BGM_DIR=str(root / "bgm"),
            PRODUCT_INFORMATION_DIR=str(information), WHATSAPP_DIRECT_PC_DELIVERY_ENABLED=True,
            WHATSAPP_LEGACY_DRIVE_WORKFLOW_DISABLED=True, MODSCAN_ENABLED=True,
            LM_STUDIO_MOMENT_MODEL_ID="exact/model",
        )
        read_service = ReadDashboardService(LegacyConfigProvider(self.cfg))
        jobs = ControlJobService(self.cfg, run_async=False)
        repository = ScannerRepository(self.working / "modular_library.sqlite3")
        source = source_record(self.vod, include_duration=False)
        source["duration_seconds"] = 10.0
        self.source = repository.upsert_source(source)
        self.scanner = ModularScannerService(
            self.cfg,
            repository=repository,
            analyzer_factory=lambda: mock.Mock(),
            production_active=lambda: False,
            start_worker=False,
        )
        security = ApiSecuritySettings(
            token="secret", actor="desktop:test", desktop=False,
            allowed_hosts=("testserver",), allowed_origins=("http://127.0.0.1:5173",),
        )
        self.app = create_app(
            read_service,
            job_service=jobs,
            settings_service=SettingsService(read_service.settings_provider),
            queue_control_service=mock.Mock(),
            security_settings=security,
            modular_scanner_service=self.scanner,
        )
        self.client = TestClient(self.app, headers={"Authorization": "Bearer secret"})
        self.public = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.public.close()
        self.scanner.close()
        self.temp.cleanup()

    def test_all_scanner_reads_and_writes_require_auth(self) -> None:
        self.assertEqual(self.public.get("/api/modular-scanner/sources").status_code, 401)
        self.assertEqual(self.public.get(f"/api/modular-scanner/media/{self.source['source_id']}").status_code, 401)
        self.assertEqual(self.public.post("/api/modular-scanner/scans", json={"source_id": self.source["source_id"]}).status_code, 401)

    def test_source_listing_is_opaque_and_selection_does_not_scan(self) -> None:
        response = self.client.get("/api/modular-scanner/sources")
        self.assertEqual(response.status_code, 200)
        listed = response.json()["data"]["sources"][0]
        self.assertEqual(listed["source_id"], self.source["source_id"])
        self.assertNotIn("canonical_path", listed)
        self.assertNotIn(str(self.vods), response.text)
        self.assertEqual(self.scanner.history(self.source["source_id"]), [])

    def test_scan_body_accepts_only_opaque_source_id(self) -> None:
        invalid = self.client.post("/api/modular-scanner/scans", json={"source_id": self.source["source_id"], "path": str(self.vod)})
        self.assertEqual(invalid.status_code, 422)
        response = self.client.post("/api/modular-scanner/scans", json={"source_id": self.source["source_id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["scan"]["status"], "queued")

    def test_media_supports_http_range_and_head(self) -> None:
        url = f"/api/modular-scanner/media/{self.source['source_id']}"
        response = self.client.get(url, headers={"Range": "bytes=10-19"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, self.content[10:20])
        self.assertEqual(response.headers["content-range"], "bytes 10-19/100")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        invalid = self.client.get(url, headers={"Range": "bytes=500-600"})
        self.assertEqual(invalid.status_code, 416)
        head = self.client.head(url)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.headers["content-length"], "100")

    def test_media_rejects_source_outside_input_root(self) -> None:
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"outside")
        record = source_record(outside, include_duration=False)
        record["duration_seconds"] = 1.0
        self.scanner.repository.upsert_source(record)
        response = self.client.get(f"/api/modular-scanner/media/{record['source_id']}")
        self.assertEqual(response.status_code, 403)

    def test_media_rejects_changed_source(self) -> None:
        self.vod.write_bytes(b"changed")
        response = self.client.get(f"/api/modular-scanner/media/{self.source['source_id']}")
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
