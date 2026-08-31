from __future__ import annotations

import importlib.util
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clipper_app.application.api_security import ApiSecuritySettings
from clipper_app.application.control_services import ControlJobService, SettingsService
from clipper_app.application.read_services import ReadDashboardService
from clipper_app.application.settings import LegacyConfigProvider
from clipper_app.modular_planner import ModularPlannerService
from clipper_app.modular_planner.repository import PlannerRepository
from clipper_app.modular_scanner.media import source_record
from clipper_app.modular_scanner.repository import ScannerRepository, utc_now
from clipper_app.modular_scanner.service import ModularScannerService


@unittest.skipIf(importlib.util.find_spec("fastapi") is None, "fastapi is not installed")
class ModularPlannerApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from clipper_app.web_api import create_app

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.vods, self.working = root / "vods", root / "working"
        output, information = root / "output", root / "information"
        for path in (self.vods, self.working, output, information):
            path.mkdir()
        self.vod = self.vods / "source.mp4"
        self.vod.write_bytes(b"planner-source-media")
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
        scanner_repository = ScannerRepository(self.working / "modular_library.sqlite3")
        source = source_record(self.vod, include_duration=False)
        source["duration_seconds"] = 1000.0
        self.source = scanner_repository.upsert_source(source)
        scan = scanner_repository.create_scan(
            self.source["source_id"], "scan", "modscan-v3.2", "modscan-prompt-v3", "exact/model",
        )
        scanner_repository.update_scan(scan["scan_id"], "completed", completed_at=utc_now())
        with scanner_repository.transaction() as db:
            for role, count in (("hook", 12), ("benefits", 20), ("ingredients", 2), ("cta", 4)):
                for index in range(count):
                    start = float((index * 23 + len(role)) % 700)
                    transcript = "Hari ini Rp99.000 promo beli 2 gratis 1" if role == "cta" else f"{role} {index}"
                    db.execute(
                        """INSERT INTO segments(segment_id,scan_id,source_id,vod_filename,product,role,
                           start_seconds,end_seconds,duration_seconds,confidence,transcript_text,reason,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            f"{role}-{index}", scan["scan_id"], self.source["source_id"], self.vod.name,
                            "serum", role, start, start + 20, 20, 0.8 + index / 1000, transcript,
                            "accepted", utc_now(),
                        ),
                    )
        scanner = ModularScannerService(
            self.cfg, repository=scanner_repository, analyzer_factory=lambda: mock.Mock(),
            production_active=lambda: False, start_worker=False,
        )
        self.planner = ModularPlannerService(
            self.cfg,
            repository=PlannerRepository(self.working / "modular_planner.sqlite3"),
        )
        read_service = ReadDashboardService(LegacyConfigProvider(self.cfg))
        security = ApiSecuritySettings(
            token="secret", actor="desktop:test", desktop=False,
            allowed_hosts=("testserver",), allowed_origins=("http://127.0.0.1:5173",),
        )
        self.queue_control = mock.Mock()
        app = create_app(
            read_service,
            job_service=ControlJobService(self.cfg, run_async=False),
            settings_service=SettingsService(read_service.settings_provider),
            queue_control_service=self.queue_control,
            security_settings=security,
            modular_scanner_service=scanner,
            modular_planner_service=self.planner,
        )
        self.client = TestClient(app, headers={"Authorization": "Bearer secret"})
        self.public = TestClient(app)
        self.scanner = scanner

    def tearDown(self):
        self.client.close()
        self.public.close()
        self.scanner.close()
        self.temp.cleanup()

    def request(self, **updates):
        payload = {
            "production_method": "modular_video", "product": "serum", "requested_count": 6,
            "requested_template": "standard", "cta_mode": "use_cta",
            "target_min_duration": 45, "target_max_duration": 75,
            "ingredient_shortage_policy": "partial", "seed": "api-seed",
        }
        payload.update(updates)
        return payload

    def test_planner_routes_require_auth_and_never_use_queue_control(self):
        self.assertEqual(self.public.get("/api/modular-planner/inventory?product=serum").status_code, 401)
        response = self.client.post("/api/modular-planner/runs", json=self.request())
        self.assertEqual(response.status_code, 201)
        data = response.json()["data"]
        self.assertEqual(data["generated_count"], 6)
        self.assertEqual(data["planner_version"], "modular-planner-v1.1")
        self.assertIn("joinability", data["compositions"][0]["items"][0]["ranking_metadata"])
        self.assertNotIn("canonical_path", response.text)
        self.assertTrue(all(item["role"] == "cta" for c in data["compositions"] for item in c["items"] if item["role"] == "cta"))
        self.assertIn("Rp99.000", response.text)
        self.queue_control.execute.assert_not_called()

        inventory = self.client.get("/api/modular-planner/inventory?product=serum").json()["data"]
        self.assertIn("joinability", inventory["roles"]["hook"])

    def test_regenerate_preserves_fallback_actual_structure_and_range(self):
        response = self.client.post("/api/modular-planner/runs", json=self.request(
            requested_count=2, requested_template="ingredient", ingredient_shortage_policy="fallback_to_standard",
            target_min_duration=45, target_max_duration=75,
        ))
        self.assertEqual(response.status_code, 201)
        run = response.json()["data"]
        composition = next(item for item in run["compositions"] if item["status"] == "draft")
        self.assertEqual(composition["requested_template"], "ingredient")
        self.assertEqual(composition["actual_template"], "standard")
        regenerated = self.client.post(
            f"/api/modular-planner/runs/{run['planner_run_id']}/compositions/{composition['composition_id']}/regenerate",
            json={"expected_revision": run["revision"]},
        )
        self.assertEqual(regenerated.status_code, 200)
        active = next(item for item in regenerated.json()["data"]["compositions"] if item["status"] == "draft")
        self.assertEqual(active["actual_template"], "standard")
        self.assertEqual(active["cta_mode"], composition["cta_mode"])
        self.assertEqual(active["target_min_duration"], 45)
        self.assertEqual(active["target_max_duration"], 75)
        self.assertEqual(regenerated.json()["data"]["product"], "serum")
        self.assertNotEqual(active["exact_signature"], composition["exact_signature"])

    def test_remove_partial_approve_and_manifest_redaction(self):
        created = self.client.post("/api/modular-planner/runs", json=self.request(requested_count=3)).json()["data"]
        first = next(item for item in created["compositions"] if item["status"] == "draft")
        removed = self.client.post(
            f"/api/modular-planner/runs/{created['planner_run_id']}/compositions/{first['composition_id']}/remove",
            json={"expected_revision": created["revision"]},
        ).json()["data"]
        self.assertEqual(removed["generated_count"], 2)
        approved_response = self.client.post(
            f"/api/modular-planner/runs/{created['planner_run_id']}/approve",
            json={"expected_revision": removed["revision"]},
        )
        self.assertEqual(approved_response.status_code, 200)
        approved = approved_response.json()["data"]
        self.assertEqual(approved["status"], "approved")
        manifest = self.client.get(f"/api/modular-planner/runs/{created['planner_run_id']}/manifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertNotIn("canonical_path", manifest.text)
        internal = self.planner.manifest(created["planner_run_id"], public=False)
        self.assertIn("canonical_path", internal["payload"]["compositions"][0]["items"][0])
        self.queue_control.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
