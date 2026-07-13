import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clipper_app.application.read_services import ReadDashboardService
from clipper_app.application.settings import LegacyConfigProvider


@unittest.skipIf(importlib.util.find_spec("fastapi") is None, "fastapi is not installed in this runtime")
class ReadApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from clipper_app.web_api import create_app
        from clipper_app.application.control_services import ControlJobService, SettingsService

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        output = root / "output"
        working = root / "working"
        modules = root / "modules"
        vods = root / "vods"
        output.mkdir()
        working.mkdir()
        modules.mkdir()
        vods.mkdir()
        self.vod_file = vods / "selected.mp4"
        self.vod_file.write_bytes(b"vod")
        (vods / "notes.txt").write_text("not media", encoding="utf-8")
        run_dir = output / "vod__run_001"
        run_dir.mkdir()
        (run_dir / "clip.mp4").write_bytes(b"media")
        state = working / "state.json"
        state.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "queue_status": "idle",
                    "videos": {
                        "vod": {
                            "name": "vod.mp4",
                            "status": "completed",
                            "output_dir": str(run_dir),
                            "stages": {
                                "transcribe": {"status": "done"},
                                "llm": {"status": "done"},
                                "yolo": {"status": "done"},
                                "ffmpeg": {"status": "done", "clips_created": 1},
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        config = SimpleNamespace(
            OUTPUT_DIR=str(output),
            WORKING_DIR=str(working),
            QUEUE_INPUT_DIR=str(vods),
            QUEUE_STATE_FILE=str(state),
            QUEUE_CONTROL_FILE=str(working / "queue_control.json"),
            QUEUE_FOREVER_STATE_FILE=str(working / "queue_forever_state.json"),
            QUEUE_STAGE_ADMISSION_LIMIT=3,
            MODULE_LIBRARY_DIR=str(modules),
            QUEUE_DASHBOARD_RUNNING_STALL_SECONDS=7200.0,
            QUEUE_DASHBOARD_QUEUED_STALL_SECONDS=86400.0,
            VARIANTS_PER_CLIP=1,
            FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
            FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
            FONT_HOOK_FALLBACKS=[],
            SUBTITLE_FONT_DIR="assets/fonts",
            BGM_DIR=str(root / "bgm"),
        )
        service = ReadDashboardService(LegacyConfigProvider(config))
        jobs = ControlJobService(config, run_async=False)
        settings = SettingsService(service.settings_provider)
        queue_controls = mock.Mock()
        queue_controls.execute.return_value = {"control": {}, "supervisor": {}, "queue": {}}
        self.client = TestClient(create_app(
            service,
            job_service=jobs,
            settings_service=settings,
            queue_control_service=queue_controls,
        ))
        self.config = config
        self.jobs = jobs
        self.queue_controls = queue_controls
        self.allowed_artifact = str(run_dir / "clip.mp4")
        self.run_dir = run_dir

    def tearDown(self):
        self.temp.cleanup()

    def test_dashboard_uses_envelope(self):
        response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("data", payload)
        self.assertIn("generated_at", payload)
        self.assertIn("source_signatures", payload)
        self.assertIn("warnings", payload)
        self.assertIn("waiting_videos", payload["data"])
        self.assertIn("stage_waiting", payload["data"])
        self.assertEqual(payload["data"]["stage_admission_limit"], 3)
        self.assertIn("clips_today", payload["data"])
        self.assertEqual(len(payload["data"]["production_days"]), 7)

    def test_queue_vods_and_launch_config_validation(self):
        queue_response = self.client.get("/api/queue")
        self.assertEqual(queue_response.status_code, 200)
        queue_data = queue_response.json()["data"]
        self.assertIn("waiting_videos", queue_data)
        self.assertIn("stage_waiting", queue_data)
        self.assertEqual(queue_data["stage_admission_limit"], 3)

        response = self.client.get("/api/queue/vods")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["exists"])
        self.assertEqual([item["name"] for item in data["files"]], ["selected.mp4"])
        self.assertEqual(data["files"][0]["path"], str(self.vod_file.resolve()))

        denied = self.client.post(
            "/api/control/queue",
            json={
                "action": "stop",
                "launch_config": {
                    "run_mode": "folder_repeat",
                    "pipeline_mode": "full",
                    "variant_mode": "all",
                    "variant_count": 1,
                    "max_clips": 0,
                },
            },
        )
        self.assertEqual(denied.status_code, 400)

    def test_invalid_sort_and_artifact_safety(self):
        self.assertEqual(self.client.get("/api/scores?sort=bad").status_code, 400)
        self.assertEqual(
            self.client.get(
                "/api/modules/library",
                params={"quality_status": "approved", "visual_status": "passed"},
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/artifacts", params={"path": "../config.py"}).status_code, 403)
        self.assertEqual(self.client.get("/api/artifacts", params={"path": self.allowed_artifact}).status_code, 200)

    def test_settings_override_endpoint_returns_control_job(self):
        snapshot = self.client.get("/api/settings/effective").json()["data"]
        response = self.client.put(
            "/api/settings/overrides",
            json={"expected_revision": snapshot["revision"], "overrides": {"MIN_SCORE": 8.0}},
        )
        self.assertEqual(response.status_code, 202)
        job = response.json()["data"]
        self.assertEqual(job["operation"], "settings_update")
        self.assertEqual(job["status"], "completed")
        listed = self.client.get("/api/control/jobs").json()["data"]
        self.assertEqual(listed["total"], 1)
        self.assertNotIn("request", listed["jobs"][0])
        self.assertNotIn("result", listed["jobs"][0])
        detail = self.client.get(f"/api/control/jobs/{job['job_id']}").json()["data"]
        self.assertIn("request", detail)
        self.assertIn("result", detail)

    def test_mutation_path_safety_and_queue_job(self):
        denied = self.client.post("/api/operations/rescore", json={"output_dir": "../config.py"})
        self.assertEqual(denied.status_code, 403)

        path_override = self.client.post(
            "/api/control/queue",
            json={
                "action": "status",
                "control_path": str(self.run_dir / "control.json"),
                "forever_state_path": str(self.run_dir / "forever.json"),
                "queue_state_path": str(self.run_dir / "queue.json"),
            },
        )
        self.assertEqual(path_override.status_code, 422)

        queue_response = self.client.post("/api/control/queue", json={"action": "status"})
        self.assertEqual(queue_response.status_code, 202)
        job = queue_response.json()["data"]
        self.assertEqual(job["operation"], "queue_control")
        self.assertEqual(job["status"], "completed")
        command = self.queue_controls.execute.call_args.args[0]
        self.assertEqual(command.control_path, self.config.QUEUE_CONTROL_FILE)
        self.assertEqual(command.forever_state_path, self.config.QUEUE_FOREVER_STATE_FILE)
        self.assertEqual(command.queue_state_path, self.config.QUEUE_STATE_FILE)
        filtered = self.client.get("/api/control/jobs", params={"operation": "queue_control", "status": "completed"})
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json()["data"]["total"], 1)

    def test_public_state_path_overrides_are_rejected_and_hidden_from_openapi(self):
        for route in ("/api/dashboard", "/api/queue"):
            response = self.client.get(route, params={"state_path": str(self.run_dir / "state.json")})
            self.assertEqual(response.status_code, 400)
            self.assertIn("not supported", response.json()["detail"])

        schema = self.client.get("/openapi.json").json()
        for route in ("/api/dashboard", "/api/queue"):
            parameters = schema["paths"][route]["get"].get("parameters", [])
            self.assertNotIn("state_path", {item["name"] for item in parameters})
        request_properties = schema["components"]["schemas"]["QueueControlRequest"]["properties"]
        self.assertNotIn("control_path", request_properties)
        self.assertNotIn("forever_state_path", request_properties)
        self.assertNotIn("queue_state_path", request_properties)

    def test_working_dir_override_is_shared_by_settings_and_variations(self):
        effective = self.client.get("/api/settings/effective").json()["data"]
        new_working = self.root / "working_override"
        new_working.mkdir()
        response = self.client.put(
            "/api/settings/overrides",
            json={
                "overrides": {"WORKING_DIR": str(new_working)},
                "expected_revision": effective["revision"],
            },
        )
        self.assertEqual(response.status_code, 202)

        data = self.client.get("/api/variations").json()["data"]
        saved = self.client.put(
            "/api/variations",
            json={"profile": data["profile"], "expected_revision": data["profile"]["revision"]},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertTrue((new_working / "variation_profile.json").exists())

    def test_variation_profile_endpoints_save_conflict_preview_and_presets(self):
        snapshot = self.client.get("/api/variations")
        self.assertEqual(snapshot.status_code, 200)
        data = snapshot.json()["data"]
        profile = data["profile"]
        self.assertEqual(profile["variant_count"], 1)
        self.assertIn("preview_source", data)
        self.assertEqual(data["preview_source"]["kind"], "video")
        self.assertIn("before_after_modes", data)

        profile["variant_count"] = 2
        profile["variants"].append(dict(profile["variants"][0]))
        profile["variants"][0]["name"] = "API Control"
        profile["variants"][0]["letterbox_enabled"] = True
        profile["variants"][0]["hook_type"] = "before_after_image"
        profile["variants"][0]["before_after_mode"] = "minimal"
        profile["variants"][0]["subtitle_enabled"] = False
        profile["variants"][1]["name"] = "API Letterbox"
        profile["variants"][1]["letterbox_enabled"] = True
        profile["variants"][1]["mirror_enabled"] = True
        profile["variants"][1]["product_zoom_enabled"] = False
        saved = self.client.put(
            "/api/variations",
            json={"profile": profile, "expected_revision": data["profile"]["revision"]},
        )
        self.assertEqual(saved.status_code, 200)
        saved_profile = saved.json()["data"]["profile"]
        self.assertEqual(saved_profile["variant_count"], 2)
        self.assertEqual(
            [idx for idx, item in enumerate(saved_profile["variants"]) if item["letterbox_enabled"]],
            [0, 1],
        )
        self.assertEqual(saved_profile["variants"][0]["hook_type"], "before_after_image")
        self.assertEqual(saved_profile["variants"][0]["before_after_mode"], "fullscreen")
        self.assertFalse(saved_profile["variants"][0]["subtitle_enabled"])
        self.assertTrue(saved_profile["variants"][1]["mirror_enabled"])
        self.assertFalse(saved_profile["variants"][1]["product_zoom_enabled"])

        stale = self.client.put(
            "/api/variations",
            json={"profile": profile, "expected_revision": "stale"},
        )
        self.assertEqual(stale.status_code, 409)

        missing_preview = self.run_dir / "missing_preview.mp4"
        with mock.patch("variation_profile.FIXED_PREVIEW_SOURCE", missing_preview):
            preview = self.client.post("/api/variations/previews", json={"profile": saved_profile})
        self.assertEqual(preview.status_code, 200)
        preview_data = preview.json()["data"]
        self.assertEqual(preview_data["previews"], [])
        self.assertEqual(preview_data["preview_source"]["kind"], "video")
        self.assertIn("Fixed preview clip", preview_data["message"])

        preset = self.client.post(
            "/api/variations/presets",
            json={"name": "Unit Preset", "profile": saved_profile},
        )
        self.assertEqual(preset.status_code, 200)
        loaded = self.client.get("/api/variations/presets/unit_preset")
        self.assertEqual(loaded.status_code, 200)
        self.assertEqual(loaded.json()["data"]["variant_count"], 2)


if __name__ == "__main__":
    unittest.main()
