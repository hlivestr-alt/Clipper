import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clipper_app.application.control_services import (
    ControlJobService,
    JobConflictError,
    SettingsRevisionConflict,
    SettingsService,
)
from clipper_app.application.services import ExportPackagingService
from clipper_app.application.settings import LegacyConfigProvider
from clipper_app.contracts.control_models import ControlJob, ControlJobStatus, ControlOperation
from clipper_app.contracts.models import ExportPackagingCommand


class ControlServiceTests(unittest.TestCase):
    def _config(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            OUTPUT_DIR=str(root / "output"),
            WORKING_DIR=str(root / "working"),
            MODULE_LIBRARY_DIR=str(root / "modules"),
            MIN_SCORE=7.0,
            MAX_PARALLEL_CLIPS=4,
            QUEUE_START_RUN_NUMBER=12,
            SCORER_FORCE_RESCORE=False,
        )

    def test_settings_service_writes_registry_limited_overrides_without_mutating_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._config(root)
            Path(cfg.WORKING_DIR).mkdir(parents=True)
            provider = LegacyConfigProvider(cfg)
            service = SettingsService(provider)
            revision = service.effective_snapshot().revision

            snapshot = service.update({"MIN_SCORE": 8.5}, expected_revision=revision)

            payload = json.loads((Path(cfg.WORKING_DIR) / "settings_overrides.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["overrides"]["MIN_SCORE"], 8.5)
            self.assertEqual(cfg.MIN_SCORE, 7.0)
            self.assertEqual(snapshot.get("MIN_SCORE"), 8.5)
            self.assertEqual(provider.snapshot().get("MIN_SCORE"), 8.5)

            with self.assertRaises(SettingsRevisionConflict):
                service.update({"MIN_SCORE": 8.0}, expected_revision=revision)
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                service.update({"WORD_CORRECTIONS": "nope"})

            deleted = service.delete("MIN_SCORE", expected_revision=provider.snapshot().revision)
            self.assertEqual(deleted.get("MIN_SCORE"), 7.0)

    def test_settings_relationship_validation_rejects_update_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._config(root)
            cfg.MIN_CLIP_DURATION = 25.0
            cfg.MAX_CLIP_DURATION = 60.0
            cfg.CHUNK_DURATION = 300
            cfg.CHUNK_OVERLAP = 45
            cfg.OUTPUT_CODEC = "h264_nvenc"
            cfg.OUTPUT_NVENC_PRESET = "p4"
            overrides_path = root / "settings_overrides.json"
            provider = LegacyConfigProvider(cfg, overrides_path=overrides_path)
            service = SettingsService(provider)
            revision = service.effective_snapshot().revision

            with self.assertRaisesRegex(ValueError, "MIN_CLIP_DURATION"):
                service.update(
                    {"MIN_CLIP_DURATION": 70.0},
                    expected_revision=revision,
                )
            self.assertFalse(overrides_path.exists())

            with self.assertRaisesRegex(ValueError, "CHUNK_OVERLAP"):
                service.update(
                    {"CHUNK_OVERLAP": 300},
                    expected_revision=revision,
                )
            self.assertFalse(overrides_path.exists())
    def test_settings_provider_ignores_stale_variant_overrides(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._config(root)
            working = Path(cfg.WORKING_DIR)
            working.mkdir(parents=True)
            (working / "settings_overrides.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "overrides": {
                        "MIN_SCORE": 8.25,
                        "VARIANTS_PER_CLIP": 6,
                        "VARIANT_FFMPEG_BAKE": False,
                        "BEFORE_AFTER_ENABLED": False,
                    },
                }),
                encoding="utf-8",
            )
            provider = LegacyConfigProvider(cfg)

            snapshot = provider.snapshot()

            self.assertEqual(snapshot.get("MIN_SCORE"), 8.25)
            self.assertIsNone(snapshot.get("VARIANTS_PER_CLIP"))
            runtime_snapshot = provider.snapshot({"VARIANTS_PER_CLIP": 3})
            self.assertEqual(runtime_snapshot.get("VARIANTS_PER_CLIP"), 3)

    def test_legacy_model_alias_override_is_read_as_canonical_setting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._config(root)
            cfg.LM_STUDIO_MOMENT_MODEL_ID = "base-model"
            overrides_path = root / "settings_overrides.json"
            overrides_path.write_text(
                json.dumps({"overrides": {"LM_STUDIO_MODEL": "legacy-model"}}),
                encoding="utf-8",
            )
            provider = LegacyConfigProvider(cfg, overrides_path=overrides_path)
            runtime = provider.runtime_view(provider.snapshot())
            self.assertEqual(runtime.LM_STUDIO_MOMENT_MODEL_ID, "legacy-model")
            self.assertEqual(runtime.LM_STUDIO_MODEL, "legacy-model")

    def test_control_job_service_persists_completed_failed_rejected_and_audit_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._config(root)
            service = ControlJobService(cfg, run_async=False)

            completed = service.submit(
                operation=ControlOperation.RESCORE,
                request={"output_dir": "out"},
                executor=lambda: {"scores": [{"clip_id": "c1"}]},
                conflict_key="rescore:out",
            )
            self.assertEqual(completed.status, ControlJobStatus.COMPLETED)
            self.assertEqual(completed.result["scores"][0]["clip_id"], "c1")

            failed = service.submit(
                operation=ControlOperation.COMPLIANCE_SCAN,
                request={"output_dir": "out"},
                executor=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            )
            self.assertEqual(failed.status, ControlJobStatus.FAILED)
            self.assertIn("boom", failed.error)

            stale = ControlJob(
                job_id="stale",
                operation=ControlOperation.MODULE_ASSEMBLY,
                status=ControlJobStatus.RUNNING,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                request={},
                conflict_key="module_assembly",
            )
            stale_path = Path(service.jobs_dir) / "stale.json"
            stale_path.write_text(stale.model_dump_json(), encoding="utf-8")
            recovered = ControlJobService(cfg, run_async=False)
            self.assertEqual(recovered.get("stale").status, ControlJobStatus.INTERRUPTED)

            blocking = ControlJob(
                job_id="blocking",
                operation=ControlOperation.EXPORT_BATCHES,
                status=ControlJobStatus.RUNNING,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                request={},
                conflict_key="export_batches",
            )
            (Path(recovered.jobs_dir) / "blocking.json").write_text(blocking.model_dump_json(), encoding="utf-8")
            with self.assertRaises(JobConflictError) as caught:
                recovered.submit(
                    operation=ControlOperation.EXPORT_BATCHES,
                    request={},
                    executor=lambda: {},
                    conflict_key="export_batches",
                )
            self.assertIsNotNone(caught.exception.job)
            self.assertEqual(caught.exception.job.status, ControlJobStatus.REJECTED)

            audit_lines = Path(recovered.audit_path).read_text(encoding="utf-8").splitlines()
            self.assertTrue(any('"status": "completed"' in line for line in audit_lines))
            self.assertTrue(any('"status": "failed"' in line for line in audit_lines))
            self.assertTrue(any('"status": "interrupted"' in line for line in audit_lines))
            self.assertTrue(any('"status": "rejected"' in line for line in audit_lines))

    def test_control_job_list_uses_compact_summaries_but_detail_remains_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ControlJobService(self._config(root), run_async=False)
            large_request = "request-data" * 100_000
            large_result = "result-data" * 200_000

            completed = service.submit(
                operation=ControlOperation.EXPORT_BATCHES,
                request={"notes": large_request},
                executor=lambda: {
                    "payload": {
                        "eligible_count": 48,
                        "packaged_count": 30,
                        "batch_size": 15,
                        "dry_run": True,
                        "manifest": large_result,
                    }
                },
            )

            page = service.list(limit=12)
            self.assertEqual(page.total, 1)
            summary = page.jobs[0]
            self.assertEqual(summary.job_id, completed.job_id)
            self.assertEqual(summary.result_summary.eligible_count, 48)
            self.assertEqual(summary.result_summary.packaged_count, 30)
            self.assertEqual(summary.result_summary.batch_size, 15)
            self.assertTrue(summary.result_summary.dry_run)
            summary_payload = page.model_dump(mode="json")["jobs"][0]
            self.assertNotIn("request", summary_payload)
            self.assertNotIn("result", summary_payload)
            self.assertLess(len(page.model_dump_json()), 2_048)

            detail = service.get(completed.job_id)
            self.assertEqual(detail.request["notes"], large_request)
            self.assertEqual(detail.result["payload"]["manifest"], large_result)

    def test_control_audit_uses_bounded_rotating_append(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with mock.patch("clipper_app.application.control_services.append_rotating_text") as append:
                service = ControlJobService(self._config(root), run_async=False)
                service.submit(
                    operation=ControlOperation.RESCORE,
                    request={"output_dir": "out"},
                    executor=lambda: {"updated": 1},
                )

            self.assertGreaterEqual(append.call_count, 3)
            for call in append.call_args_list:
                self.assertEqual(call.kwargs["max_bytes"], 10 * 1024 * 1024)
                self.assertEqual(call.kwargs["backup_count"], 5)
                self.assertTrue(call.args[1].endswith("\n"))

    def test_export_packaging_service_uses_runtime_settings_view(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._config(root)
            Path(cfg.OUTPUT_DIR).mkdir(parents=True)
            provider = LegacyConfigProvider(cfg)
            service = ExportPackagingService(provider)

            with mock.patch("export_packager.package_export_batches", return_value={"moved": 0}) as package:
                result = service.package(ExportPackagingCommand(batch_size=3, dry_run=True))

            self.assertEqual(result.payload["moved"], 0)
            self.assertEqual(package.call_args.args[0], cfg.OUTPUT_DIR)
            self.assertEqual(package.call_args.kwargs["batch_size"], 3)
            self.assertTrue(package.call_args.kwargs["dry_run"])
            self.assertEqual(package.call_args.kwargs["cfg"].MIN_SCORE, 7.0)


if __name__ == "__main__":
    unittest.main()
