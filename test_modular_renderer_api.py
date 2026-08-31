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


class ScannerStub:
    def close(self):
        pass


class PlannerStub:
    pass


class RendererStub:
    def __init__(self, media: Path):
        self.media = media
        self.created = []
        self.run = {
            "render_run_id": "render-1", "planner_run_id": "planner-1",
            "planner_manifest_id": "manifest-1", "renderer_version": "modular-renderer-v1",
            "selected_composition_ids": ["composition-1"], "status": "completed",
            "requested_count": 1, "succeeded_count": 1, "failed_count": 0,
            "current_composition_id": None, "created_at": "now", "completed_at": "now",
            "items": [{
                "render_run_id": "render-1", "composition_id": "composition-1",
                "product": "serum", "template": "standard", "ordinal": 1,
                "renderer_version": "modular-renderer-v1", "expected_duration": 6.0,
                "rendered_duration": 6.02, "duration_delta": 0.02, "status": "completed",
                "normalization": {}, "created_at": "now", "completed_at": "now",
            }],
        }

    def create_run(self, request):
        self.created.append(request)
        return self.run, False

    def get_run(self, run_id):
        if run_id != "render-1":
            raise KeyError("Unknown modular render run")
        return self.run

    def list_runs(self, planner_run_id, limit=20):
        return [self.run] if planner_run_id == "planner-1" else []

    def media_path(self, run_id, composition_id):
        if (run_id, composition_id) != ("render-1", "composition-1"):
            raise KeyError("Unknown modular render item")
        return self.media

    def close(self):
        pass


@unittest.skipIf(importlib.util.find_spec("fastapi") is None, "fastapi is not installed")
class ModularRendererApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from clipper_app.web_api import create_app

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        working, vods, output, information = root / "working", root / "vods", root / "output", root / "information"
        for path in (working, vods, output, information):
            path.mkdir()
        state = working / "state.json"; state.write_text('{"queue_status":"idle","videos":{}}', encoding="utf-8")
        media = working / "joined.mp4"; media.write_bytes(b"0123456789")
        cfg = SimpleNamespace(
            OUTPUT_DIR=str(output), WORKING_DIR=str(working), QUEUE_INPUT_DIR=str(vods),
            QUEUE_STATE_FILE=str(state), QUEUE_CONTROL_FILE=str(working / "control.json"),
            QUEUE_FOREVER_STATE_FILE=str(working / "forever.json"), QUEUE_STAGE_ADMISSION_LIMIT=3,
            QUEUE_DASHBOARD_RUNNING_STALL_SECONDS=7200.0, QUEUE_DASHBOARD_QUEUED_STALL_SECONDS=86400.0,
            VARIANTS_PER_CLIP=1, FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
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
        self.renderer = RendererStub(media)
        app = create_app(
            reads, job_service=ControlJobService(cfg, run_async=False),
            settings_service=SettingsService(reads.settings_provider), queue_control_service=SimpleNamespace(),
            security_settings=security, modular_scanner_service=ScannerStub(),
            modular_planner_service=PlannerStub(), modular_renderer_service=self.renderer,
        )
        self.client = TestClient(app, headers={"Authorization": "Bearer secret"})
        self.public = TestClient(app)

    def tearDown(self):
        self.client.close(); self.public.close(); self.temp.cleanup()

    def test_create_is_202_polling_is_private_and_media_supports_ranges(self):
        self.assertEqual(self.public.get("/api/modular-renderer/runs/render-1").status_code, 401)
        response = self.client.post("/api/modular-renderer/runs", json={
            "planner_run_id": "planner-1", "composition_ids": ["composition-1"],
        })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["data"]["status"], "completed")
        self.assertNotIn("output_path", response.text)
        self.assertNotIn("canonical_path", response.text)
        polled = self.client.get("/api/modular-renderer/runs/render-1")
        self.assertEqual(polled.status_code, 200)
        partial = self.client.get(
            "/api/modular-renderer/runs/render-1/media/composition-1", headers={"Range": "bytes=2-5"},
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, b"2345")
        self.assertEqual(partial.headers["accept-ranges"], "bytes")
        self.assertEqual(self.client.get("/api/modular-renderer/runs/render-1/media/../../etc").status_code, 404)


if __name__ == "__main__":
    unittest.main()
