import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clipper_app.application.control_services import (
    ControlJobService,
    JobCapacityError,
    JobConflictError,
    JobResultExpiredError,
    SettingsRevisionConflict,
    SettingsService,
)
from clipper_app.application.services import ExportPackagingService
from clipper_app.application.settings import LegacyConfigProvider
from clipper_app.contracts.control_models import (
    ControlJob,
    ControlJobResultMetadata,
    ControlJobStatus,
    ControlOperation,
)
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

    def test_privileged_settings_are_preserved_but_browser_writes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._config(root)
            working = Path(cfg.WORKING_DIR)
            working.mkdir(parents=True)
            overrides_path = working / "settings_overrides.json"
            overrides_path.write_text(
                json.dumps({
                    "overrides": {
                        "MIN_SCORE": 7.5,
                        "LM_STUDIO_BASE_URL": "http://127.0.0.1:1234/v1",
                    }
                }),
                encoding="utf-8",
            )
            provider = LegacyConfigProvider(cfg)
            service = SettingsService(provider)

            service.update({"MIN_SCORE": 8.0})
            persisted = json.loads(overrides_path.read_text(encoding="utf-8"))["overrides"]
            self.assertEqual(persisted["MIN_SCORE"], 8.0)
            self.assertEqual(persisted["LM_STUDIO_BASE_URL"], "http://127.0.0.1:1234/v1")

            with self.assertRaisesRegex(ValueError, "[Oo]perator-managed"):
                service.update({"OUTPUT_DIR": str(root / "elsewhere")})
            with self.assertRaisesRegex(ValueError, "[Oo]perator-managed"):
                service.delete("LM_STUDIO_BASE_URL")

            service.reset()
            persisted = json.loads(overrides_path.read_text(encoding="utf-8"))["overrides"]
            self.assertEqual(persisted, {"LM_STUDIO_BASE_URL": "http://127.0.0.1:1234/v1"})

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
                        "actionable_count": 32,
                        "packaged_count": 30,
                        "pending_count": 2,
                        "packaged_total": 130,
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
            self.assertEqual(summary.result_summary.actionable_count, 32)
            self.assertEqual(summary.result_summary.packaged_count, 30)
            self.assertEqual(summary.result_summary.pending_count, 2)
            self.assertEqual(summary.result_summary.packaged_total, 130)
            self.assertEqual(summary.result_summary.batch_size, 15)
            self.assertTrue(summary.result_summary.dry_run)
            summary_payload = page.model_dump(mode="json")["jobs"][0]
            self.assertNotIn("request", summary_payload)
            self.assertNotIn("result", summary_payload)
            self.assertLess(len(page.model_dump_json()), 2_048)

            detail = service.get(completed.job_id)
            self.assertEqual(detail.request["notes"], large_request)
            self.assertEqual(detail.result["payload"]["manifest"], large_result)
            metadata_payload = json.loads(
                (Path(service.jobs_dir) / f"{completed.job_id}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata_payload["schema_version"], 2)
            self.assertNotIn("result", metadata_payload)
            self.assertTrue(metadata_payload["result_metadata"]["available"])
            self.assertTrue((Path(service.results_dir) / f"{completed.job_id}.json").is_file())
            compact_detail = service.get(completed.job_id, include_result=False)
            self.assertIsNone(compact_detail.result)

    def test_control_job_results_are_bounded_and_queue_videos_are_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ControlJobService(
                self._config(root),
                run_async=False,
                result_max_bytes=1_024,
            )
            large = service.submit(
                operation=ControlOperation.RESCORE,
                request={},
                executor=lambda: {"scores": [{"detail": "x" * 10_000}]},
            )
            result_path = Path(service.results_dir) / f"{large.job_id}.json"
            self.assertLessEqual(result_path.stat().st_size, 1_024)
            self.assertTrue(large.result_metadata.truncated)
            self.assertGreater(large.result_metadata.original_bytes, large.result_metadata.stored_bytes)
            self.assertTrue(large.result["_clipper_result_truncated"])
            preview = service.get_result_preview(large.job_id, max_chars=200)
            self.assertLessEqual(len(preview.preview), 200)
            self.assertTrue(preview.truncated)

            queue_job = service.submit(
                operation=ControlOperation.QUEUE_CONTROL,
                request={"action": "status"},
                executor=lambda: {
                    "control": {"requested": "status"},
                    "queue": {"videos": {"a": {"status": "done"}, "b": {"status": "running"}}, "running": 1},
                },
            )
            self.assertNotIn("videos", queue_job.result["queue"])
            self.assertEqual(queue_job.result["queue"]["video_count"], 2)
            stored = json.loads(
                (Path(service.results_dir) / f"{queue_job.job_id}.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("videos", stored["queue"])
            self.assertEqual(stored["queue"]["video_count"], 2)

    def test_scheduler_checks_conflicts_before_capacity_and_bounds_pending_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ControlJobService(
                self._config(root),
                run_async=True,
                interactive_workers=1,
                interactive_pending=1,
            )
            started = threading.Event()
            release = threading.Event()

            def block():
                started.set()
                self.assertTrue(release.wait(timeout=5))
                return {"done": True}

            first = service.submit(
                operation=ControlOperation.QUEUE_CONTROL,
                request={},
                executor=block,
                conflict_key="shared",
            )
            self.assertTrue(started.wait(timeout=2))
            second = service.submit(
                operation=ControlOperation.SETTINGS_UPDATE,
                request={},
                executor=lambda: {"done": True},
            )

            with self.assertRaises(JobConflictError):
                service.submit(
                    operation=ControlOperation.QUEUE_CONTROL,
                    request={},
                    executor=lambda: {},
                    conflict_key="shared",
                )
            with self.assertRaises(JobCapacityError) as caught:
                service.submit(
                    operation=ControlOperation.MODULE_REVIEW,
                    request={},
                    executor=lambda: {},
                )
            self.assertEqual(caught.exception.retry_after, 5)
            self.assertEqual(caught.exception.job.status, ControlJobStatus.REJECTED)

            release.set()
            service.join(first.job_id, timeout=5)
            service.join(second.job_id, timeout=5)
            self.assertEqual(service.get(first.job_id).status, ControlJobStatus.COMPLETED)
            self.assertEqual(service.get(second.job_id).status, ControlJobStatus.COMPLETED)

    def test_scheduler_serializes_compute_jobs_but_allows_export_in_parallel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ControlJobService(self._config(root), run_async=True)
            compute_started = threading.Event()
            second_compute_started = threading.Event()
            export_started = threading.Event()
            release_first = threading.Event()
            release_second = threading.Event()

            def first_compute():
                compute_started.set()
                self.assertTrue(release_first.wait(timeout=5))
                return {"done": 1}

            def second_compute():
                second_compute_started.set()
                self.assertTrue(release_second.wait(timeout=5))
                return {"done": 2}

            first = service.submit(
                operation=ControlOperation.RESCORE,
                request={},
                executor=first_compute,
            )
            self.assertTrue(compute_started.wait(timeout=2))
            second = service.submit(
                operation=ControlOperation.COMPLIANCE_SCAN,
                request={},
                executor=second_compute,
            )
            export = service.submit(
                operation=ControlOperation.EXPORT_BATCHES,
                request={},
                executor=lambda: export_started.set() or {"packaged_count": 1},
            )
            self.assertTrue(export_started.wait(timeout=2))
            self.assertFalse(second_compute_started.is_set())
            service.join(export.job_id, timeout=5)

            release_first.set()
            self.assertTrue(second_compute_started.wait(timeout=2))
            release_second.set()
            service.join(first.job_id, timeout=5)
            service.join(second.job_id, timeout=5)
            self.assertEqual(service.get(second.job_id).status, ControlJobStatus.COMPLETED)

    def test_retention_removes_old_terminal_data_but_never_active_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ControlJobService(
                self._config(root),
                run_async=False,
                auto_migrate_legacy=False,
                metadata_retention_days=30,
                metadata_retention_max_terminal=10,
                result_retention_days=7,
                result_retention_max_bytes=50_000,
            )
            now = datetime.now(timezone.utc)

            def persist(job_id: str, status: ControlJobStatus, age_days: int, *, expired=False):
                stamp = (now - timedelta(days=age_days)).isoformat(timespec="seconds")
                expires = (
                    now - timedelta(days=1) if expired else now + timedelta(days=7)
                ).isoformat(timespec="seconds")
                payload = b'{"value":"retained"}'
                job = ControlJob(
                    job_id=job_id,
                    operation=ControlOperation.RESCORE,
                    status=status,
                    created_at=stamp,
                    updated_at=stamp,
                    finished_at=stamp if status in {ControlJobStatus.COMPLETED, ControlJobStatus.FAILED} else None,
                    result_metadata=ControlJobResultMetadata(
                        available=True,
                        original_bytes=len(payload),
                        stored_bytes=len(payload),
                        expires_at=expires,
                    ),
                )
                service._save_job(job)
                result_path = Path(service.results_dir) / f"{job_id}.json"
                result_path.write_bytes(payload)
                old_epoch = (now - timedelta(days=age_days)).timestamp()
                import os

                os.utime(result_path, (old_epoch, old_epoch))

            persist("old-terminal", ControlJobStatus.COMPLETED, 40)
            persist("expired-result", ControlJobStatus.COMPLETED, 1, expired=True)
            persist("active", ControlJobStatus.RUNNING, 40, expired=True)

            stats = service.run_retention()

            self.assertIsNone(service.get("old-terminal"))
            self.assertIsNotNone(service.get("expired-result", include_result=False))
            self.assertFalse((Path(service.results_dir) / "expired-result.json").exists())
            with self.assertRaises(JobResultExpiredError):
                service.get_result("expired-result")
            self.assertIsNotNone(service.get("active", include_result=False))
            self.assertTrue((Path(service.results_dir) / "active.json").exists())
            self.assertGreaterEqual(stats["metadata_deleted"], 1)

    def test_byte_cap_pruned_result_is_reported_as_expired(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ControlJobService(
                self._config(root),
                run_async=False,
                result_retention_max_bytes=1,
            )

            completed = service.submit(
                operation=ControlOperation.RESCORE,
                request={},
                executor=lambda: {"value": "retained-then-pruned"},
            )

            metadata = service.get(completed.job_id, include_result=False).result_metadata
            self.assertFalse(metadata.available)
            self.assertGreater(metadata.stored_bytes, 0)
            with self.assertRaises(JobResultExpiredError):
                service.get_result(completed.job_id)

    def test_legacy_migration_is_idempotent_bounded_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._config(root)
            jobs_dir = Path(cfg.WORKING_DIR) / "app_control_jobs"
            jobs_dir.mkdir(parents=True)
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            legacy = ControlJob(
                schema_version=1,
                job_id="legacy",
                operation=ControlOperation.RESCORE,
                status=ControlJobStatus.COMPLETED,
                created_at=stamp,
                updated_at=stamp,
                finished_at=stamp,
                request={"output_dir": "out"},
                result={"scores": [{"detail": "x" * 10_000}]},
            )
            (jobs_dir / "legacy.json").write_text(legacy.model_dump_json(), encoding="utf-8")
            (jobs_dir / "unreadable.json").write_text("{not-json", encoding="utf-8")
            service = ControlJobService(
                cfg,
                run_async=False,
                auto_migrate_legacy=False,
                result_max_bytes=1_024,
            )

            report = service.migrate_legacy_storage()

            self.assertTrue(report["migrated"])
            self.assertEqual(report["jobs_migrated"], 1)
            self.assertEqual(report["truncated_results"], 1)
            self.assertIn("unreadable.json", report["unreadable_files"])
            backup = Path(report["backup_path"])
            self.assertTrue((backup / "unreadable.json").exists())
            metadata = json.loads((Path(service.jobs_dir) / "legacy.json").read_text(encoding="utf-8"))
            self.assertNotIn("result", metadata)
            self.assertLessEqual((Path(service.results_dir) / "legacy.json").stat().st_size, 1_024)
            self.assertFalse(service.migrate_legacy_storage()["migrated"])

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
            self.assertEqual(package.call_args.kwargs["trigger"], "manual")
            self.assertEqual(package.call_args.kwargs["cfg"].MIN_SCORE, 7.0)


if __name__ == "__main__":
    unittest.main()
