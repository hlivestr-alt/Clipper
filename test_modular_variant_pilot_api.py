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
from test_modular_renderer_api import PlannerStub, RendererStub, ScannerStub


class PilotStub:
    def __init__(self, media: Path):
        self.media = media; self.requests = []
        self.run = {
            "run_id": "pilot-1", "profile_id": "active", "profile_revision": "rev", "status": "completed",
            "requested_base_count": 1, "requested_variant_count": 6, "succeeded_base_count": 1,
            "failed_base_count": 0, "total_expected_outputs": 6, "total_completed_outputs": 6,
            "items": [],
        }

    def profiles(self): return {"profiles": [{"profile_id": "active", "name": "Active", "revision": "rev", "variant_count": 6}], "required_variant_count": 6}
    def eligible(self, planner_run_id): return [{"render_run_id": "render-1", "composition_id": "composition-1", "product": "serum", "ordinal": 1, "renderer_version": "modular-renderer-v1.1", "base_identity": "sha"}]
    def create_run(self, request): self.requests.append(request); return self.run, False
    def get_run(self, run_id):
        if run_id != "pilot-1": raise KeyError("unknown")
        return self.run
    def list_runs(self, limit=20): return [self.run]
    def media_path(self, media_id):
        if media_id != "media-1": raise KeyError("unknown")
        return self.media
    def close(self): pass


@unittest.skipIf(importlib.util.find_spec("fastapi") is None, "fastapi is not installed")
class ModularVariantPilotApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from clipper_app.web_api import create_app
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        working, vods, output, information = root / "working", root / "vods", root / "output", root / "information"
        for path in (working, vods, output, information): path.mkdir()
        state = working / "state.json"; state.write_text('{"queue_status":"idle","videos":{}}')
        media = working / "variant.mp4"; media.write_bytes(b"0123456789")
        cfg = SimpleNamespace(
            OUTPUT_DIR=str(output), WORKING_DIR=str(working), QUEUE_INPUT_DIR=str(vods), QUEUE_STATE_FILE=str(state),
            QUEUE_CONTROL_FILE=str(working / "control.json"), QUEUE_FOREVER_STATE_FILE=str(working / "forever.json"),
            QUEUE_STAGE_ADMISSION_LIMIT=3, QUEUE_DASHBOARD_RUNNING_STALL_SECONDS=7200.0,
            QUEUE_DASHBOARD_QUEUED_STALL_SECONDS=86400.0, VARIANTS_PER_CLIP=6,
            FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf", FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
            FONT_HOOK_FALLBACKS=[], SUBTITLE_FONT_DIR="assets/fonts", BGM_DIR=str(root / "bgm"),
            PRODUCT_INFORMATION_DIR=str(information), WHATSAPP_DIRECT_PC_DELIVERY_ENABLED=True,
            WHATSAPP_LEGACY_DRIVE_WORKFLOW_DISABLED=True, MODSCAN_ENABLED=True, LM_STUDIO_MOMENT_MODEL_ID="exact/model",
        )
        reads = ReadDashboardService(LegacyConfigProvider(cfg))
        security = ApiSecuritySettings(token="secret", actor="test", desktop=False, allowed_hosts=("testserver",), allowed_origins=())
        self.pilot = PilotStub(media)
        app = create_app(
            reads, job_service=ControlJobService(cfg, run_async=False), settings_service=SettingsService(reads.settings_provider),
            queue_control_service=SimpleNamespace(), security_settings=security, modular_scanner_service=ScannerStub(),
            modular_planner_service=PlannerStub(), modular_renderer_service=RendererStub(media),
            modular_variant_pilot_service=self.pilot,
        )
        self.client = TestClient(app, headers={"Authorization": "Bearer secret"}); self.public = TestClient(app)

    def tearDown(self): self.client.close(); self.public.close(); self.temp.cleanup()

    def test_202_id_only_contract_polling_and_media_ranges(self):
        self.assertEqual(self.public.get("/api/modular-variant-pilot/runs/pilot-1").status_code, 401)
        response = self.client.post("/api/modular-variant-pilot/runs", json={
            "bases": [{"render_run_id": "render-1", "composition_id": "composition-1"}], "profile_id": "active",
        })
        self.assertEqual(response.status_code, 202); self.assertEqual(response.json()["data"]["total_expected_outputs"], 6)
        self.assertNotIn("path", response.text)
        rejected = self.client.post("/api/modular-variant-pilot/runs", json={"bases": [{"path": "C:/arbitrary.mp4"}]})
        self.assertEqual(rejected.status_code, 422)
        partial = self.client.get("/api/modular-variant-pilot/media/media-1", headers={"Range": "bytes=2-5"})
        self.assertEqual(partial.status_code, 206); self.assertEqual(partial.content, b"2345")


if __name__ == "__main__": unittest.main()
