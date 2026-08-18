import inspect
import json
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace

import main
import queue_control
import video_queue
from clipper_app.application.catalog import CatalogDatabase, CatalogIndexer
from clipper_app.application.control_services import ControlJobService
from clipper_app.application.settings import LegacyConfigProvider
from clipper_app.contracts import ControlOperation
from stage_cache import (
    _legacy_ffmpeg_fingerprint,
    sidecar_path,
    stage_fingerprint,
    stage_fingerprint_matches,
)


class LegacyModularRemovalTests(unittest.TestCase):
    def test_normal_pipeline_contract_has_no_legacy_modular_arguments_or_imports(self):
        parameters = inspect.signature(main.run_pipeline).parameters
        removed = {
            "extract_modules_only",
            "force_modules",
            "render_modules",
            "modular_only",
            "module_assembly_limit",
            "module_product_zoom",
        }
        self.assertTrue(removed.isdisjoint(parameters))
        source = inspect.getsource(main._run_pipeline_impl)
        self.assertNotIn("module_extractor", source)
        self.assertNotIn("module_assembler", source)

    def test_only_normal_queue_modes_are_launchable_and_new_progress_is_module_free(self):
        self.assertEqual(
            set(video_queue.PIPELINE_MODE_STAGES),
            {"full", "clips_only", "raw_cuts_only"},
        )
        self.assertEqual(video_queue.PIPELINE_MODE_STAGES["full"], video_queue.STAGES)
        self.assertEqual(video_queue.PIPELINE_MODE_STAGES["clips_only"], (video_queue.EDIT_STAGE,))
        self.assertEqual(
            video_queue.PIPELINE_MODE_STAGES["raw_cuts_only"],
            ("transcribe", "llm", video_queue.EDIT_STAGE),
        )
        self.assertFalse(any(key.startswith("module") for key in video_queue.CLIP_PROGRESS_DEFAULTS))
        with self.assertRaisesRegex(ValueError, "legacy unsupported"):
            queue_control.normalize_launch_config({"pipeline_mode": "modules_only"})

    def test_historical_modules_only_state_is_readable_labeled_and_not_mutated_on_continue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue_control.json"
            payload = {
                "schema_version": 1,
                "requested_action": "run",
                "status": "paused",
                "launch_config": {
                    "run_mode": "folder_repeat",
                    "pipeline_mode": "modules_only",
                    "variant_mode": "all",
                    "variant_count": 1,
                    "max_clips": None,
                    "video_path": None,
                },
            }
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            before = path.read_bytes()

            state = queue_control.read_control_state(path)
            self.assertEqual(state["launch_config"]["pipeline_mode"], "modules_only")
            self.assertIn("legacy unsupported", queue_control.launch_summary(state["launch_config"]))
            with self.assertRaisesRegex(ValueError, "legacy unsupported"):
                queue_control.request_continue(path)

            self.assertEqual(path.read_bytes(), before)

    def test_removed_modular_settings_are_ignored_but_unrelated_unknown_keys_still_fail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            overrides = root / "settings_overrides.json"
            overrides.write_text(
                json.dumps({"overrides": {"MODULE_EXTRACTION_ENABLED": True}}),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(WORKING_DIR=str(root), MIN_SCORE=7.0)
            provider = LegacyConfigProvider(cfg, overrides_path=overrides)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                snapshot = provider.snapshot()
            self.assertEqual(snapshot.get("MIN_SCORE"), 7.0)
            self.assertTrue(any("removed legacy modular" in str(item.message) for item in caught))

            overrides.write_text(
                json.dumps({"overrides": {"UNRELATED_UNKNOWN_SETTING": True}}),
                encoding="utf-8",
            )
            provider.invalidate()
            with self.assertRaisesRegex(ValueError, "UNRELATED_UNKNOWN_SETTING"):
                provider.snapshot()

    def test_historical_settings_snapshot_with_removed_keys_still_loads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "snapshot.json"
            path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "revision": "historical-revision-including-removed-values",
                    "values": {"MIN_SCORE": 8.0, "MODULE_ASSEMBLY_ENABLED": True},
                    "sources": {"MIN_SCORE": "legacy_config", "MODULE_ASSEMBLY_ENABLED": "legacy_config"},
                }),
                encoding="utf-8",
            )
            provider = LegacyConfigProvider(SimpleNamespace(WORKING_DIR=str(root)))
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                snapshot = provider.snapshot_from_file(path)
            self.assertEqual(snapshot.as_dict(), {"MIN_SCORE": 8.0})

    def test_catalog_does_not_scan_or_modify_historical_module_media(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output"
            working = root / "working"
            historical = root / "proya_modules"
            output.mkdir()
            working.mkdir()
            historical.mkdir()
            media = historical / "legacy.mp4"
            index = historical / "index.json"
            media.write_bytes(b"historical-media")
            index.write_text('{"modules":[{"module_id":"legacy"}]}', encoding="utf-8")
            before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in historical.iterdir()}
            cfg = SimpleNamespace(
                OUTPUT_DIR=str(output),
                WORKING_DIR=str(working),
                MODULE_LIBRARY_DIR=str(historical),
            )
            database = CatalogDatabase(working / "catalog.sqlite3")
            database.ensure_schema()
            fresh_tables = {row[0] for row in database.query("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("modules", fresh_tables)
            database.execute("CREATE TABLE modules(module_id TEXT PRIMARY KEY, payload_json TEXT)")
            database.execute("INSERT INTO modules(module_id, payload_json) VALUES('legacy', '{}')")
            database.execute(
                "INSERT INTO catalog_sources(path_identity, display_path, domain, mtime_ns, size, sha256, indexed_at) "
                "VALUES('legacy-source', ?, 'modules', 1, 1, 'legacy', '2026-01-01T00:00:00Z')",
                (str(index),),
            )

            result = CatalogIndexer(database, cfg).backfill(force=True)

            self.assertNotIn("modules", result)
            after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in historical.iterdir()}
            self.assertEqual(after, before)
            tables = {row[0] for row in database.query("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("modules", tables)
            self.assertEqual(database.scalar("SELECT payload_json FROM modules WHERE module_id='legacy'"), "{}")
            self.assertEqual(
                database.scalar("SELECT domain FROM catalog_sources WHERE path_identity='legacy-source'"),
                "modules",
            )

    def test_normal_ffmpeg_cache_accepts_pre_removal_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "vod.mp4"
            output = root / "manifest.json"
            video.write_bytes(b"vod")
            output.write_text("[]", encoding="utf-8")
            cfg = SimpleNamespace(OUTPUT_CODEC="h264_nvenc")
            legacy = _legacy_ffmpeg_fingerprint(video, cfg, extra={"max_clips": None, "cut_only": False})
            sidecar_path(output).write_text(json.dumps({"fingerprint": legacy}), encoding="utf-8")

            self.assertNotEqual(legacy, stage_fingerprint(video, cfg, "ffmpeg", extra={"max_clips": None, "cut_only": False}))
            self.assertTrue(
                stage_fingerprint_matches(
                    output,
                    video,
                    cfg,
                    "ffmpeg",
                    extra={"max_clips": None, "cut_only": False},
                )
            )

    def test_legacy_control_operations_deserialize_but_cannot_be_submitted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ControlJobService(
                SimpleNamespace(WORKING_DIR=temp_dir),
                run_async=False,
                auto_migrate_legacy=False,
            )
            with self.assertRaisesRegex(ValueError, "legacy unsupported"):
                service.submit(
                    operation=ControlOperation.MODULE_ASSEMBLY,
                    request={},
                    executor=lambda: {},
                )


if __name__ == "__main__":
    unittest.main()
