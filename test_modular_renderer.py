from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from array import array
from pathlib import Path
from types import SimpleNamespace

from clipper_app.contracts.modular_renderer_models import ModularRenderRunCreateRequest
from clipper_app.modular_renderer.media import (
    FALLBACK_SEEK_STRATEGY,
    FAST_SEEK_STRATEGY,
    FFmpegPipeline,
    RenderMediaError,
    verify_manifest_source,
)
from clipper_app.modular_renderer.repository import RendererRepository
from clipper_app.modular_renderer.service import ModularRendererConflict, ModularRendererService
from clipper_app.modular_scanner.media import source_record


class PlannerStub:
    def __init__(self, manifest: dict, *, status: str = "approved"):
        self._manifest = manifest
        self.status = status

    def get_run(self, run_id: str) -> dict:
        if run_id != self._manifest["planner_run_id"]:
            raise KeyError("Unknown planner run")
        return {"planner_run_id": run_id, "status": self.status}

    def manifest(self, run_id: str, *, public: bool = True) -> dict:
        if run_id != self._manifest["planner_run_id"]:
            raise KeyError("Unknown planner run")
        payload = json.loads(json.dumps(self._manifest))
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return {
            "manifest_id": "manifest-1", "planner_run_id": run_id,
            "checksum_sha256": hashlib.sha256(encoded).hexdigest(), "payload": payload,
        }


class FakeMedia:
    def __init__(self, fail: set[str] | None = None):
        self.fail = fail or set()
        self.rendered: list[dict] = []

    def render(self, composition, work_dir, final_path, verified, *, source_probe_cache=None):
        self.rendered.append(composition)
        if composition["composition_id"] in self.fail:
            raise RenderMediaError("synthetic_failure", "synthetic media failure")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(b"valid-mp4")
        expected = sum(item["end_seconds"] - item["start_seconds"] for item in composition["items"])
        return {
            "rendered_duration": expected + 0.02, "duration_delta": 0.02,
            "extraction_seconds": 0.1, "concat_encode_seconds": 0.1,
            "normalization": {"target_fps": 30},
            "diagnostics": {"segments": [{"composition_id": composition["composition_id"], "strategy": FAST_SEEK_STRATEGY}]},
        }

    def validate_output(self, path, expected, item_count):
        if path.read_bytes() != b"valid-mp4":
            raise RenderMediaError("ffprobe_failed", "incomplete output is invalid")
        return {"duration": expected + 0.02, "has_video": True, "has_audio": True, "format_name": "mp4"}


class ModularRendererTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.vods, self.working = root / "vods", root / "working"
        self.vods.mkdir(); self.working.mkdir()
        self.source = self.vods / "source.mp4"
        self.source.write_bytes(b"source-media-content")
        record = source_record(self.source, include_duration=False)
        base = {
            "segment_id": "segment", "scan_id": "scan", "scanner_generation": 1,
            "source_id": record["source_id"], "source_filename": self.source.name,
            "canonical_path": record["canonical_path"], "source_file_size": record["file_size"],
            "source_mtime_ns": record["mtime_ns"], "source_content_fingerprint": record["content_fingerprint"],
            "confidence": 0.9, "transcript_text": "exact", "reason": "accepted",
        }
        def composition(identifier: str, ordinal: int, starts=(10.25, 20.5, 31.75)):
            roles = ("hook", "benefits", "cta")
            items = [{**base, "position": index, "role": role, "start_seconds": start,
                      "end_seconds": start + 2.5, "duration_seconds": 2.5,
                      "segment_id": f"{identifier}-{role}"}
                     for index, (role, start) in enumerate(zip(roles, starts))]
            return {"composition_id": identifier, "ordinal": ordinal, "actual_template": "standard", "items": items}
        self.compositions = [composition("composition-1", 1), composition("composition-2", 2), composition("composition-3", 3)]
        self.manifest = {
            "schema_version": 1, "planner_run_id": "planner-1", "product": "serum",
            "compositions": self.compositions,
        }
        self.cfg = SimpleNamespace(WORKING_DIR=str(self.working), QUEUE_INPUT_DIR=str(self.vods))
        self.repository = RendererRepository(self.working / "renderer.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def service(self, *, planner=None, media=None, production_active=lambda: False):
        return ModularRendererService(
            self.cfg, planner=planner or PlannerStub(self.manifest), repository=self.repository,
            media=media or FakeMedia(), production_active=production_active, wait_poll_seconds=0.001,
            start_worker=False,
        )

    def request(self, *ids, rerender=False):
        return ModularRenderRunCreateRequest(
            planner_run_id="planner-1", composition_ids=ids or ("composition-1",), manual_rerender=rerender,
        )

    def test_draft_cannot_render_and_approved_manifest_exact_order_and_timestamps_are_used(self):
        draft = self.service(planner=PlannerStub(self.manifest, status="draft"))
        with self.assertRaises(ModularRendererConflict):
            draft.create_run(self.request())

        media = FakeMedia()
        service = self.service(media=media)
        run, reused = service.create_run(self.request("composition-2", "composition-1"))
        self.assertFalse(reused)
        self.assertEqual(media.rendered, [])
        self.assertEqual(run["selected_composition_ids"], ["composition-1", "composition-2"])
        with self.assertRaisesRegex(ModularRendererConflict, "already active"):
            service.create_run(self.request("composition-1", "composition-3"))
        service.process_pending_once()
        self.assertEqual([row["composition_id"] for row in media.rendered], ["composition-1", "composition-2"])
        self.assertEqual([row["role"] for row in media.rendered[0]["items"]], ["hook", "benefits", "cta"])
        self.assertEqual([row["start_seconds"] for row in media.rendered[0]["items"]], [10.25, 20.5, 31.75])

    def test_standard_ingredient_and_benefit_focus_position_orders_are_preserved(self):
        patterns = {
            "standard": ["hook", "benefits", "cta"],
            "ingredient": ["hook", "ingredients", "benefits", "cta"],
            "benefit_focus": ["hook", "benefits", "benefits", "cta"],
        }
        source_item = self.compositions[0]["items"][0]
        compositions = []
        for ordinal, (template, roles) in enumerate(patterns.items(), 1):
            items = [{**source_item, "position": position, "role": role,
                      "segment_id": f"{template}-{position}", "start_seconds": position * 3.0,
                      "end_seconds": position * 3.0 + 2.0, "duration_seconds": 2.0}
                     for position, role in enumerate(roles)]
            compositions.append({"composition_id": template, "ordinal": ordinal, "actual_template": template, "items": items})
        manifest = {**self.manifest, "compositions": compositions}
        media = FakeMedia()
        service = self.service(planner=PlannerStub(manifest), media=media)
        service.create_run(self.request(*patterns.keys()))
        service.process_pending_once()
        self.assertEqual(
            {row["actual_template"]: [item["role"] for item in row["items"]] for row in media.rendered},
            patterns,
        )

    def test_source_identity_cache_and_failures(self):
        item = self.compositions[0]["items"][0]
        calls = []
        cache = {}
        def fingerprint(path):
            calls.append(path)
            return item["source_content_fingerprint"]
        verify_manifest_source(item, self.vods, cache, fingerprint=fingerprint)
        verify_manifest_source(item, self.vods, cache, fingerprint=fingerprint)
        self.assertEqual(len(calls), 1)

        changed = {**item, "source_content_fingerprint": "changed"}
        with self.assertRaisesRegex(RenderMediaError, "fingerprint"):
            verify_manifest_source(changed, self.vods, {}, fingerprint=fingerprint)
        missing = {**item, "canonical_path": str(self.vods / "missing.mp4")}
        with self.assertRaisesRegex(RenderMediaError, "missing"):
            verify_manifest_source(missing, self.vods, {})
        outside_path = self.working / "outside.mp4"; outside_path.write_bytes(b"x")
        outside_record = source_record(outside_path, include_duration=False)
        outside = {**item, "canonical_path": str(outside_path), "source_file_size": outside_record["file_size"],
                   "source_mtime_ns": outside_record["mtime_ns"], "source_content_fingerprint": outside_record["content_fingerprint"]}
        with self.assertRaisesRegex(RenderMediaError, "outside"):
            verify_manifest_source(outside, self.vods, {})

    def test_idempotency_manual_rerender_restart_and_partial_output(self):
        service = self.service()
        first, reused = service.create_run(self.request())
        again, reused = service.create_run(self.request())
        self.assertTrue(reused); self.assertEqual(first["render_run_id"], again["render_run_id"])
        service.process_pending_once()
        completed = service.get_run(first["render_run_id"])
        self.assertEqual(completed["status"], "completed")

        restarted = ModularRendererService(
            self.cfg, planner=PlannerStub(self.manifest),
            repository=RendererRepository(self.working / "renderer.sqlite3"), media=FakeMedia(),
            production_active=lambda: False, start_worker=False,
        )
        self.assertEqual(restarted.get_run(first["render_run_id"])["items"][0]["status"], "completed")
        rerender, reused = restarted.create_run(self.request(rerender=True))
        self.assertFalse(reused); self.assertNotEqual(rerender["render_run_id"], first["render_run_id"])
        internal = restarted.repository.item(rerender["render_run_id"], "composition-1")
        Path(internal["output_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(internal["output_path"]).write_bytes(b"partial")
        restarted.process_pending_once()
        failed = restarted.get_run(rerender["render_run_id"])
        self.assertEqual(failed["items"][0]["status"], "failed")

    def test_production_wait_and_failure_continuation(self):
        checks = iter((True, True, False, False, False))
        media = FakeMedia(fail={"composition-2"})
        service = self.service(media=media, production_active=lambda: next(checks, False))
        run, _ = service.create_run(self.request("composition-1", "composition-2", "composition-3"))
        service.process_pending_once()
        result = service.get_run(run["render_run_id"])
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["succeeded_count"], 2)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual([item["status"] for item in result["items"]], ["completed", "failed", "completed"])

    def test_ffmpeg_commands_use_exact_ranges_plain_order_and_normalized_output(self):
        commands = []
        def runner(command, **kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"media")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        def probe(path):
            duration = 2.5 if Path(path).name.startswith("segment_") else 7.52
            return {"duration": duration, "width": 1080, "height": 1920, "has_video": True,
                    "has_audio": True, "format_name": "mov,mp4,m4a,3gp,3g2,mj2"}
        pipeline = FFmpegPipeline(runner=runner, probe=probe)
        work = self.working / "media-work"; final = self.working / "joined.mp4"
        composition = self.compositions[0]
        verified = {item["position"]: self.source for item in composition["items"]}
        result = pipeline.render(composition, work, final, verified)
        extracts = commands[:-1]
        self.assertEqual(len(extracts), 3)
        for command, item in zip(extracts, composition["items"]):
            seek_indexes = [index for index, value in enumerate(command) if value == "-ss"]
            self.assertEqual(len(seek_indexes), 2)
            self.assertLess(seek_indexes[0], command.index("-i"))
            self.assertGreater(seek_indexes[1], command.index("-i"))
            seek_total = sum(float(command[index + 1]) for index in seek_indexes)
            self.assertAlmostEqual(seek_total, item["start_seconds"], places=8)
            self.assertEqual(command[command.index("-t") + 1], f"{item['duration_seconds']:.9f}")
            self.assertNotIn("xfade", " ".join(command))
        self.assertEqual(commands[-1][commands[-1].index("-f") + 1], "concat")
        self.assertTrue(final.exists())
        self.assertAlmostEqual(result["rendered_duration"], 7.52)
        self.assertTrue(all(row["strategy"] == FAST_SEEK_STRATEGY for row in result["diagnostics"]["segments"]))

    def test_fast_seek_validation_failure_uses_exact_fallback_and_records_strategy(self):
        commands = []
        state = {"fast": True}
        output = self.working / "fallback.mp4"

        def runner(command, **kwargs):
            commands.append(command)
            state["fast"] = len([value for value in command if value == "-ss"]) == 2
            Path(command[-1]).write_bytes(b"media")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def probe(path):
            return {
                "duration": 99.0 if state["fast"] else 2.5,
                "width": 1080, "height": 1920, "has_video": True, "has_audio": True,
                "format_name": "mp4", "video_duration": 2.5, "audio_duration": 2.5,
            }

        pipeline = FFmpegPipeline(runner=runner, probe=probe)
        result = pipeline._extract(
            self.source, output, 3600.25, 2.5, 1080, 1920,
            source_info={"duration": 7200.0},
        )
        self.assertEqual(len(commands), 2)
        fast_seeks = [index for index, value in enumerate(commands[0]) if value == "-ss"]
        self.assertEqual(len(fast_seeks), 2)
        self.assertLess(fast_seeks[0], commands[0].index("-i"))
        self.assertGreater(fast_seeks[1], commands[0].index("-i"))
        self.assertAlmostEqual(sum(float(commands[0][index + 1]) for index in fast_seeks), 3600.25)
        self.assertGreater(commands[1].index("-ss"), commands[1].index("-i"))
        self.assertEqual(commands[1][commands[1].index("-ss") + 1], "3600.250000000")
        self.assertEqual(commands[1][commands[1].index("-t") + 1], "2.500000000")
        self.assertEqual(result["strategy"], FALLBACK_SEEK_STRATEGY)
        self.assertEqual(result["fallback_reason"], "segment_duration_mismatch")

    def test_source_probe_cache_is_reused_across_compositions(self):
        source_probes = []

        def runner(command, **kwargs):
            Path(command[-1]).write_bytes(b"media")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def probe(path):
            if Path(path) == self.source:
                source_probes.append(path)
                duration = 5000.0
            elif Path(path).name.startswith("segment_"):
                duration = 2.5
            else:
                duration = 7.52
            return {"duration": duration, "width": 1080, "height": 1920, "has_video": True,
                    "has_audio": True, "format_name": "mp4"}

        pipeline = FFmpegPipeline(runner=runner, probe=probe)
        cache = {}
        verified = {item["position"]: self.source for item in self.compositions[0]["items"]}
        pipeline.render(self.compositions[0], self.working / "cache-1", self.working / "cache-1.mp4", verified,
                        source_probe_cache=cache)
        pipeline.render(self.compositions[1], self.working / "cache-2", self.working / "cache-2.mp4", verified,
                        source_probe_cache=cache)
        self.assertEqual(len(source_probes), 1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_real_ffmpeg_early_and_late_fast_ranges_match_v1_decoded_signals(self):
        from clipper_app.modular_renderer.media import probe_media

        source = self.working / "seek-accuracy-source.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=997:sample_rate=48000",
            "-t", "12", "-c:v", "libx264", "-preset", "ultrafast", "-g", "90",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", str(source),
        ], check=True, timeout=60)
        pipeline = FFmpegPipeline()
        source_info = probe_media(source)

        def decoded(path, mapping, fmt):
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", mapping]
            if fmt == "video":
                command.extend(["-f", "framemd5", "-"])
            else:
                command.extend(["-f", "s16le", "-ar", "48000", "-ac", "2", "-"])
            return subprocess.run(command, capture_output=True, check=True, timeout=60).stdout

        def audio_alignment(left, right):
            a = array("h", left)[::2][:48000]
            b = array("h", right)[::2][:48000]
            scores = []
            for shift in range(-2, 3):
                aa = a[max(0, shift):min(len(a), len(a) + shift)]
                bb = b[max(0, -shift):min(len(b), len(b) - shift)]
                dot = sum(x * y for x, y in zip(aa, bb))
                norm_a = sum(x * x for x in aa) ** 0.5
                norm_b = sum(y * y for y in bb) ** 0.5
                scores.append((dot / (norm_a * norm_b), shift))
            return max(scores)

        for label, start in (("early", 0.75), ("late", 8.25)):
            old = self.working / f"{label}-old.mp4"
            new = self.working / f"{label}-new.mp4"
            for output, fast in ((old, False), (new, True)):
                pipeline._run(
                    pipeline._extract_command(source, output, start, 2.0, 320, 240, fast=fast),
                    timeout=60,
                )
                info = pipeline.validate_segment(output, 2.0)
                self.assertAlmostEqual(info["duration"], 2.0, delta=0.15)
            self.assertEqual(decoded(old, "0:v:0", "video"), decoded(new, "0:v:0", "video"))
            correlation, shift = audio_alignment(decoded(old, "0:a:0", "audio"), decoded(new, "0:a:0", "audio"))
            self.assertLessEqual(abs(shift), 1)
            self.assertGreater(correlation, 0.999)

    def test_v1_repository_migrates_additively_and_v11_diagnostics_persist(self):
        import sqlite3

        path = self.working / "migration.sqlite3"
        RendererRepository(path)
        db = sqlite3.connect(path)
        try:
            db.execute("ALTER TABLE modular_render_items DROP COLUMN diagnostics_json")
            db.execute("UPDATE schema_meta SET version=1")
            db.commit()
        finally:
            db.close()
        migrated = RendererRepository(path)
        db = migrated.connect()
        try:
            self.assertEqual(db.execute("SELECT version FROM schema_meta").fetchone()[0], 2)
            columns = {row[1] for row in db.execute("PRAGMA table_info(modular_render_items)")}
        finally:
            db.close()
        self.assertIn("diagnostics_json", columns)

        service = self.service(media=FakeMedia())
        run, _ = service.create_run(self.request())
        service.process_pending_once()
        completed = service.get_run(run["render_run_id"])
        self.assertEqual(completed["renderer_version"], "modular-renderer-v1.1")
        self.assertEqual(completed["items"][0]["diagnostics"]["segments"][0]["strategy"], FAST_SEEK_STRATEGY)


if __name__ == "__main__":
    unittest.main()
