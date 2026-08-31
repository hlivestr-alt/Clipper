from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from clipper_app.application.api_security import ApiSecuritySettings
from clipper_app.application.control_services import ControlJobService, SettingsService
from clipper_app.application.read_services import ReadDashboardService
from clipper_app.application.settings import LegacyConfigProvider


class CloseStub:
    def close(self):
        pass


class PlannerStub:
    pass


class RendererStub(CloseStub):
    pass


class ProductionStub(CloseStub):
    def __init__(self, media: Path):
        self.media = media
        self.created = []
        self.job = {
            "job_id": "production-1", "workflow_mode": "automatic", "product": "serum",
            "requested_base_count": 10, "generated_base_count": 10, "rendered_base_count": 10,
            "failed_base_count": 0, "variants_per_base": 6, "expected_variant_count": 60,
            "generated_variant_count": 60, "failed_variant_count": 0,
            "compliance_passed_count": 60, "compliance_rejected_count": 0,
            "scored_count": 60, "scoring_failed_count": 0, "exported_count": 60,
            "export_failed_count": 0, "variant_profile_id": "active", "variant_profile_revision": "r1",
            "planner_run_id": "planner-1", "planner_manifest_id": "manifest-1",
            "render_run_id": "render-1", "status": "completed", "current_stage": "completed",
            "stage_progress": 100, "warnings": [], "cancel_requested": False,
            "created_at": "2026-08-29T00:00:00+00:00", "started_at": "2026-08-29T00:00:00+00:00",
            "completed_at": "2026-08-29T01:00:00+00:00", "timings": {}, "items": [],
        }

    def profiles(self):
        return {"profiles": [{"profile_id": "active", "name": "Active", "revision": "r1", "variant_count": 6}]}

    def create_job(self, request):
        self.created.append(request)
        return dict(self.job), False

    def list_jobs(self, limit):
        return [dict(self.job)]

    def get_job(self, job_id):
        if job_id != self.job["job_id"]:
            raise KeyError("Unknown modular production job")
        return dict(self.job)

    def continue_job(self, job_id, request):
        del request
        return self.get_job(job_id)

    def cancel(self, job_id):
        return self.get_job(job_id)

    def media_path(self, media_id):
        if media_id != "media-1":
            raise KeyError("Unknown modular production media")
        return self.media


@unittest.skipIf(importlib.util.find_spec("fastapi") is None, "fastapi is not installed")
class ModularProductionApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from clipper_app.web_api import create_app

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        working, vods, output, information = root / "working", root / "vods", root / "output", root / "information"
        for path in (working, vods, output, information):
            path.mkdir()
        state = working / "state.json"
        state.write_text('{"queue_status":"idle","videos":{}}', encoding="utf-8")
        media = output / "final.mp4"
        media.write_bytes(b"0123456789")
        cfg = SimpleNamespace(
            OUTPUT_DIR=str(output), WORKING_DIR=str(working), QUEUE_INPUT_DIR=str(vods),
            QUEUE_STATE_FILE=str(state), QUEUE_CONTROL_FILE=str(working / "control.json"),
            QUEUE_FOREVER_STATE_FILE=str(working / "forever.json"), QUEUE_STAGE_ADMISSION_LIMIT=3,
            QUEUE_DASHBOARD_RUNNING_STALL_SECONDS=7200.0, QUEUE_DASHBOARD_QUEUED_STALL_SECONDS=86400.0,
            VARIANTS_PER_CLIP=6, FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
            FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf", FONT_HOOK_FALLBACKS=[],
            SUBTITLE_FONT_DIR="assets/fonts", BGM_DIR=str(root / "bgm"),
            PRODUCT_INFORMATION_DIR=str(information), WHATSAPP_DIRECT_PC_DELIVERY_ENABLED=True,
            WHATSAPP_LEGACY_DRIVE_WORKFLOW_DISABLED=True, MODSCAN_ENABLED=True,
            LM_STUDIO_MOMENT_MODEL_ID="exact/model",
        )
        reads = ReadDashboardService(LegacyConfigProvider(cfg))
        security = ApiSecuritySettings(
            token="secret", actor="desktop:test", desktop=False,
            allowed_hosts=("testserver",), allowed_origins=("http://127.0.0.1:5173",),
        )
        self.production = ProductionStub(media)
        app = create_app(
            reads,
            job_service=ControlJobService(cfg, run_async=False),
            settings_service=SettingsService(reads.settings_provider),
            queue_control_service=SimpleNamespace(),
            security_settings=security,
            modular_scanner_service=CloseStub(),
            modular_planner_service=PlannerStub(),
            modular_renderer_service=RendererStub(),
            modular_variant_pilot_service=CloseStub(),
            modular_production_service=self.production,
        )
        self.client = TestClient(app, headers={"Authorization": "Bearer secret"})
        self.public = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.public.close()
        self.temp.cleanup()

    def test_production_routes_are_authenticated_fast_and_path_safe(self):
        self.assertEqual(self.public.get("/api/modular-production/jobs").status_code, 401)
        response = self.client.post("/api/modular-production/jobs", json={
            "production_method": "modular_video", "workflow_mode": "automatic", "product": "serum",
            "requested_base_count": 10, "requested_template": "standard", "cta_mode": "use_cta",
            "target_min_duration": 45, "target_max_duration": 75,
            "ingredient_shortage_policy": "partial", "variant_profile_id": "active",
        })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["data"]["expected_variant_count"], 60)
        self.assertNotIn("output_path", response.text)
        self.assertNotIn("canonical_path", response.text)
        self.assertEqual(self.client.get("/api/modular-production/jobs/production-1").status_code, 200)
        self.assertEqual(self.client.post("/api/modular-production/jobs/production-1/cancel").status_code, 202)
        ranged = self.client.get(
            "/api/modular-production/media/media-1", headers={"Authorization": "Bearer secret", "Range": "bytes=2-5"},
        )
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, b"2345")
        self.assertEqual(self.client.get("/api/modular-production/media/missing").status_code, 404)


if __name__ == "__main__":
    unittest.main()
