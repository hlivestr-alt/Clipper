import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import module_extractor as mx


class Cfg:
    MODULE_DURATION_STRICT = False
    MODULE_HOOK_MIN_DURATION = 4.0
    MODULE_HOOK_MAX_DURATION = 8.0
    MODULE_MAIN_MIN_DURATION = 15.0
    MODULE_MAIN_MAX_DURATION = 45.0
    MODULE_CTA_MIN_DURATION = 4.0
    MODULE_CTA_MAX_DURATION = 12.0
    MODULE_SENTENCE_BOUNDARY_TOLERANCE = 2.0
    MODULE_CLASSIFICATION_MIN_CONFIDENCE = 0.6
    MODULE_DEDUPE_IOU_THRESHOLD = 0.5
    MODULE_MAX_CANDIDATES_PER_ROLE = 0
    MODULE_INDEX_VALIDATE_MEDIA = False
    MODULE_INDEX_REPROBE_MEDIA = False
    MODULE_INDEX_LOCK_TIMEOUT = 0.1
    MODULE_FILE_LOCK_TIMEOUT = 0.1
    MODULE_VALIDATE_ON_EXTRACT = False
    MODULE_WORD_FALLBACK_REVIEW_REQUIRED = True
    MODULE_PRODUCT_EVIDENCE_REQUIRED = True
    MODULE_PRODUCT_EVIDENCE_CONTEXT_SECONDS = 12.0
    MODULE_VISUAL_VALIDATION_MIN_HITS = 1
    MODULE_VISUAL_VALIDATION_MIN_CONFIDENCE = 0.55
    MODULE_VISUAL_VALIDATION_SAMPLE_FPS = 1.0
    YOLO_WEIGHTS = "models/proya_best.pt"
    YOLO_IMGSZ = 416
    YOLO_HALF = True
    PRODUCT_CLASSES = {1: "serum"}
    HOST_FACE_CLASS = "host_face"


class StrictCfg(Cfg):
    MODULE_DURATION_STRICT = True


class ExtractVisualCfg(Cfg):
    MODULE_VALIDATE_ON_EXTRACT = True


def transcript_from_sentences(sentences):
    words = []
    segments = []
    t = 0.0
    for idx, sentence in enumerate(sentences):
        sentence_words = []
        for token in sentence.split():
            start = t
            end = t + 0.5
            word = {"word": token, "start": start, "end": end}
            words.append(word)
            sentence_words.append(word)
            t = end + 0.1
        segments.append(
            {
                "id": idx,
                "start": sentence_words[0]["start"],
                "end": sentence_words[-1]["end"],
                "text": sentence,
                "words": sentence_words,
            }
        )
        t += 0.9
    return {"segments": segments, "words": words}


@contextmanager
def null_lock(*_args, **_kwargs):
    yield


class ModuleExtractorTests(unittest.TestCase):
    def test_validate_on_extract_default_is_false(self):
        import config as cfg

        self.assertFalse(cfg.MODULE_VALIDATE_ON_EXTRACT)

    def test_product_and_role_are_strictly_canonicalized(self):
        self.assertEqual(mx.canonical_product("PROYA serum vitamin c"), "serum")
        self.assertEqual(mx.canonical_product("krim mata"), "eye_cream")
        self.assertIsNone(mx.canonical_product("general"))
        self.assertEqual(mx.canonical_role("closing promo"), "cta")
        self.assertIsNone(mx.canonical_role("random"))

    def test_prompt_and_json_parser_expect_bahasa_json_only_candidates(self):
        prompt = mx.SYSTEM_PROMPT.lower()
        self.assertIn("return hanya json array", prompt)
        self.assertIn("bahasa indonesia", prompt)
        parsed = mx.parse_module_candidates_json(
            '```json\n[{"product":"serum","role":"hook","start_hint":1.0}]\n```'
        )
        self.assertEqual(parsed[0]["product"], "serum")

    def test_cta_relaxed_max_accepts_natural_sentence(self):
        transcript = transcript_from_sentences(
            [
                "promo serum ini khusus hari ini.",
                "langsung checkout sekarang stok terbatas.",
                "terima kasih.",
            ]
        )
        candidate = {
            "product": "serum",
            "role": "cta",
            "start_hint": 0.1,
            "target_duration": 7.0,
            "confidence": 0.9,
        }

        snapped, reason = mx.snap_to_sentence_boundaries(candidate, transcript, Cfg)

        self.assertIsNotNone(snapped, reason)
        self.assertLessEqual(snapped["duration"], Cfg.MODULE_CTA_MAX_DURATION)

    def test_strict_duration_mode_uses_original_ranges(self):
        self.assertEqual(mx.role_duration_limits("hook", StrictCfg), {"min": 5.0, "max": 7.0, "default": 6.0})
        self.assertEqual(mx.role_duration_limits("main", StrictCfg), {"min": 20.0, "max": 40.0, "default": 30.0})
        self.assertEqual(mx.role_duration_limits("cta", StrictCfg), {"min": 5.0, "max": 10.0, "default": 7.0})

    def test_word_boundary_fallback_accepts_when_sentence_end_misses_range(self):
        words = []
        for index in range(10):
            words.append({"word": f"kata{index}", "start": float(index), "end": float(index) + 0.4})
        transcript = {
            "words": words,
            "segments": [{"start": 0.0, "end": 9.4, "text": " ".join(word["word"] for word in words), "words": words}],
        }
        candidate = {
            "product": "serum",
            "role": "hook",
            "start_hint": 0.1,
            "target_duration": 6.0,
            "confidence": 0.9,
        }

        snapped, reason = mx.snap_to_sentence_boundaries(candidate, transcript, Cfg)

        self.assertIsNotNone(snapped, reason)
        self.assertEqual(snapped["boundary_mode"], "word_boundary_fallback")
        self.assertGreaterEqual(snapped["duration"], Cfg.MODULE_HOOK_MIN_DURATION)
        self.assertLessEqual(snapped["duration"], Cfg.MODULE_HOOK_MAX_DURATION)

    def test_cut_register_reencodes_without_keyframe_precheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "serum" / "hook" / "serum_hook_20260509_0.mp4"
            candidate = {
                "product": "serum",
                "role": "hook",
                "start": 0.0,
                "end": 6.0,
                "duration": 6.0,
                "transcript_text": "serum bagus sekali.",
                "classification_reason": "hook kuat",
                "confidence": 0.9,
                "words": [],
            }
            video = Path(tmp) / "2026-04-16-10-17-09.mp4"
            video.write_bytes(b"video")

            with mock.patch.object(mx, "cut_module_reencode", return_value=True) as cut, \
                mock.patch.object(mx, "module_file_lock", null_lock), \
                mock.patch.object(
                    mx,
                    "probe_media",
                    return_value={
                        "duration": 6.0,
                        "has_video": True,
                        "has_audio": True,
                        "video_codec": "h264",
                        "audio_codec": "aac",
                    },
                ):
                record = mx.cut_and_register_module(candidate, str(video), target, Cfg)

        cut.assert_called_once()
        self.assertEqual(record["status"], "created")
        self.assertEqual(record["source_date"], "2026-04-16")

    def test_validate_on_extract_scans_source_before_cut_and_uses_bulk_fingerprint(self):
        import module_visual_validator as mvv

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "serum" / "hook" / "serum_hook_20260509_0.mp4"
            candidate = {
                "product": "serum",
                "role": "hook",
                "start": 2.0,
                "end": 8.0,
                "duration": 6.0,
                "transcript_text": "serum bagus sekali.",
                "classification_reason": "hook kuat",
                "confidence": 0.9,
                "words": [{"word": "serum", "start": 2.0, "end": 2.4}],
            }
            video = Path(tmp) / "2026-04-16-10-17-09.mp4"
            video.write_bytes(b"source")
            event = {
                "source": "source_vod_visual_validation",
                "product": "serum",
                "class_name": "serum",
                "relative_start": 0.25,
                "relative_end": 0.75,
                "best_confidence": 0.88,
            }
            order = []

            def fake_scan(**_kwargs):
                order.append("scan")
                return {"hits": 1, "confidence_max": 0.88, "events": [event]}

            def fake_cut(_source, _start, _end, output_path, _cfg):
                order.append("cut")
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"cut-media")
                return True

            with mock.patch.object(mx, "cut_module_reencode", side_effect=fake_cut), \
                mock.patch.object(mx, "module_file_lock", null_lock), \
                mock.patch.object(mvv, "scan_source_video_window_visual_events", side_effect=fake_scan), \
                mock.patch.object(
                    mx,
                    "probe_media",
                    return_value={
                        "duration": 6.0,
                        "has_video": True,
                        "has_audio": True,
                        "video_codec": "h264",
                        "audio_codec": "aac",
                    },
                ):
                record = mx.cut_and_register_module(candidate, str(video), target, ExtractVisualCfg)
                expected_fingerprint = mvv.build_visual_validation_fingerprint(record, ExtractVisualCfg)

        self.assertEqual(order, ["scan", "cut"])
        self.assertEqual(record["visual_validation_status"], "passed")
        self.assertEqual(record["visual_validation_mode"], "source_vod_pre_cut")
        self.assertEqual(record["visual_product_events"], [event])
        self.assertEqual(record["visual_validation_fingerprint"], expected_fingerprint)

    def test_source_vod_zero_events_marks_no_visual_events_without_rejection(self):
        import module_visual_validator as mvv

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "serum" / "hook" / "serum_hook_20260509_0.mp4"
            video = Path(tmp) / "2026-04-16-10-17-09.mp4"
            video.write_bytes(b"source")
            candidate = {
                "product": "serum",
                "role": "hook",
                "start": 0.0,
                "end": 6.0,
                "duration": 6.0,
                "transcript_text": "serum bagus sekali.",
                "classification_reason": "hook kuat",
                "confidence": 0.9,
                "words": [{"word": "serum", "start": 0.0, "end": 0.4}],
            }

            def fake_cut(_source, _start, _end, output_path, _cfg):
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"cut-media")
                return True

            with mock.patch.object(mx, "cut_module_reencode", side_effect=fake_cut), \
                mock.patch.object(mx, "module_file_lock", null_lock), \
                mock.patch.object(
                    mvv,
                    "scan_source_video_window_visual_events",
                    return_value={"hits": 0, "confidence_max": 0.0, "events": []},
                ), \
                mock.patch.object(
                    mx,
                    "probe_media",
                    return_value={"duration": 6.0, "has_video": True, "has_audio": True},
                ):
                record = mx.cut_and_register_module(candidate, str(video), target, ExtractVisualCfg)

        self.assertEqual(record["status"], "created")
        self.assertEqual(record["visual_validation_status"], "failed")
        self.assertEqual(record["visual_validation_reason"], "source_vod_no_visual_events")
        self.assertEqual(record["quality_status"], "no_visual_events")
        self.assertEqual(record["quality_reason"], "source_vod_no_visual_events")

    def test_source_vod_scan_failure_marks_not_run_and_still_registers(self):
        import module_visual_validator as mvv

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "serum" / "hook" / "serum_hook_20260509_0.mp4"
            video = Path(tmp) / "2026-04-16-10-17-09.mp4"
            video.write_bytes(b"source")
            candidate = {
                "product": "serum",
                "role": "hook",
                "start": 0.0,
                "end": 6.0,
                "duration": 6.0,
                "transcript_text": "serum bagus sekali.",
                "classification_reason": "hook kuat",
                "confidence": 0.9,
                "words": [{"word": "serum", "start": 0.0, "end": 0.4}],
            }

            def fake_cut(_source, _start, _end, output_path, _cfg):
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"cut-media")
                return True

            with mock.patch.object(mx, "cut_module_reencode", side_effect=fake_cut), \
                mock.patch.object(mx, "module_file_lock", null_lock), \
                mock.patch.object(mvv, "scan_source_video_window_visual_events", side_effect=RuntimeError("no yolo")), \
                mock.patch.object(
                    mx,
                    "probe_media",
                    return_value={"duration": 6.0, "has_video": True, "has_audio": True},
                ):
                record = mx.cut_and_register_module(candidate, str(video), target, ExtractVisualCfg)

        self.assertEqual(record["status"], "created")
        self.assertEqual(record["visual_validation_status"], "not_run")
        self.assertIn("source_vod_validator_error", record["visual_validation_reason"])

    def test_existing_valid_module_is_not_source_scanned_without_force(self):
        import module_visual_validator as mvv

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "serum" / "hook" / "serum_hook_20260509_0.mp4"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"cut-media")
            video = Path(tmp) / "2026-04-16-10-17-09.mp4"
            video.write_bytes(b"source")
            candidate = {
                "product": "serum",
                "role": "hook",
                "start": 0.0,
                "end": 6.0,
                "duration": 6.0,
                "transcript_text": "serum bagus sekali.",
                "classification_reason": "hook kuat",
                "confidence": 0.9,
                "words": [{"word": "serum", "start": 0.0, "end": 0.4}],
            }
            probe = {"duration": 6.0, "has_video": True, "has_audio": True}
            sidecar = mx._build_sidecar_record(candidate, str(video), target, probe, status="created", cfg=ExtractVisualCfg)
            target.with_suffix(".json").write_text(json.dumps(sidecar), encoding="utf-8")

            with mock.patch.object(mx, "module_file_lock", null_lock), \
                mock.patch.object(mx, "cut_module_reencode", side_effect=AssertionError("unexpected cut")), \
                mock.patch.object(mvv, "scan_source_video_window_visual_events") as scan, \
                mock.patch.object(mx, "probe_media", return_value=probe):
                record = mx.cut_and_register_module(candidate, str(video), target, ExtractVisualCfg)

        scan.assert_not_called()
        self.assertEqual(record["status"], "skipped_existing")

    def test_duplicate_candidates_are_not_source_scanned(self):
        import module_visual_validator as mvv

        transcript = transcript_from_sentences(
            ["serum ini membantu kulit tampak lebih cerah lembap segar sehat akhir."]
        )
        candidates = [
            {"product": "serum", "role": "hook", "start_hint": 0.0, "target_duration": 6.0, "confidence": 0.9},
            {"product": "serum", "role": "hook", "start_hint": 0.0, "target_duration": 6.0, "confidence": 0.9},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "2026-04-16-10-17-09.mp4"
            video.write_bytes(b"source")

            class CfgWithLibrary(ExtractVisualCfg):
                MODULE_LIBRARY_DIR = str(root / "library")
                MODULE_PRODUCT_EVIDENCE_REQUIRED = True

            def fake_cut(_source, _start, _end, output_path, _cfg):
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"cut-media")
                return True

            with mock.patch.object(mx, "_require_portalocker"), \
                mock.patch.object(mx, "_load_or_classify_candidates", return_value=candidates), \
                mock.patch.object(mx, "library_index_lock", null_lock), \
                mock.patch.object(mx, "module_file_lock", null_lock), \
                mock.patch.object(mx, "cut_module_reencode", side_effect=fake_cut), \
                mock.patch.object(
                    mvv,
                    "scan_source_video_window_visual_events",
                    return_value={"hits": 1, "confidence_max": 0.8, "events": [{"product": "serum"}]},
                ) as scan, \
                mock.patch.object(mx, "probe_media", return_value={"duration": 6.0, "has_video": True, "has_audio": True}):
                stats = mx.extract_modules(str(video), transcript, [], str(root / "working"), CfgWithLibrary)

        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["skipped_duplicate"], 1)
        scan.assert_called_once()

    def test_candidate_cap_candidates_are_not_source_scanned(self):
        import module_visual_validator as mvv

        transcript = transcript_from_sentences(
            [
                "serum ini membantu kulit tampak lebih cerah lembap segar sehat akhir.",
                "serum dipakai pagi malam agar kulit terasa nyaman glowing akhir.",
            ]
        )
        candidates = [
            {"product": "serum", "role": "hook", "start_hint": 0.0, "target_duration": 6.0, "confidence": 0.9},
            {"product": "serum", "role": "hook", "start_hint": 6.8, "target_duration": 6.0, "confidence": 0.9},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "2026-04-16-10-17-09.mp4"
            video.write_bytes(b"source")

            class CfgWithCap(ExtractVisualCfg):
                MODULE_LIBRARY_DIR = str(root / "library")
                MODULE_MAX_CANDIDATES_PER_ROLE = 1

            def fake_cut(_source, _start, _end, output_path, _cfg):
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"cut-media")
                return True

            with mock.patch.object(mx, "_require_portalocker"), \
                mock.patch.object(mx, "_load_or_classify_candidates", return_value=candidates), \
                mock.patch.object(mx, "library_index_lock", null_lock), \
                mock.patch.object(mx, "module_file_lock", null_lock), \
                mock.patch.object(mx, "cut_module_reencode", side_effect=fake_cut), \
                mock.patch.object(
                    mvv,
                    "scan_source_video_window_visual_events",
                    return_value={"hits": 1, "confidence_max": 0.8, "events": [{"product": "serum"}]},
                ) as scan, \
                mock.patch.object(mx, "probe_media", return_value={"duration": 6.0, "has_video": True, "has_audio": True}):
                stats = mx.extract_modules(str(video), transcript, [], str(root / "working"), CfgWithCap)

        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["skipped_candidate_cap"], 1)
        self.assertEqual(stats["reject_details"]["candidate_cap_reached"], 1)
        scan.assert_called_once()

    def test_rebuild_index_derives_source_date_from_source_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module_dir = root / "serum" / "hook"
            module_dir.mkdir(parents=True)
            media = module_dir / "serum_hook_20260416_0.mp4"
            media.write_bytes(b"video")
            media.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "schema_version": mx.MODULE_SCHEMA_VERSION,
                        "module_id": media.stem,
                        "product": "serum",
                        "role": "hook",
                        "source_video": r"C:\VOD\2026-04-16-10-17-09.mp4",
                        "file_path": str(media),
                        "start": 0.0,
                        "end": 6.0,
                        "duration": 6.0,
                        "transcript_text": "promo serum",
                        "confidence": 0.9,
                    }
                ),
                encoding="utf-8",
            )

            index = mx.rebuild_library_index(root, Cfg, write=False)

        self.assertEqual(index["modules"][0]["source_date"], "2026-04-16")

    def test_rebuild_index_uses_valid_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module_dir = root / "serum" / "hook"
            module_dir.mkdir(parents=True)
            media = module_dir / "serum_hook_20260509_0.mp4"
            media.write_bytes(b"video")
            sidecar = media.with_suffix(".json")
            sidecar.write_text(
                json.dumps(
                    {
                        "schema_version": mx.MODULE_SCHEMA_VERSION,
                        "module_id": media.stem,
                        "product": "serum",
                        "role": "hook",
                        "source_video": "vod.mp4",
                        "source_video_identity": {"path": "vod.mp4", "size": 1, "mtime_ns": 1},
                        "file_path": str(media),
                        "start": 0.0,
                        "end": 6.0,
                        "duration": 6.0,
                        "transcript_text": "promo serum",
                        "confidence": 0.9,
                    }
                ),
                encoding="utf-8",
            )

            index = mx.rebuild_library_index(root, Cfg, write=True)
            self.assertTrue((root / "index.json").exists())

        self.assertEqual(index["module_count"], 1)

    def test_rebuild_index_trusts_sidecar_ffprobe_without_reprobe(self):
        class ValidateCfg(Cfg):
            MODULE_INDEX_VALIDATE_MEDIA = True
            MODULE_INDEX_REPROBE_MEDIA = False

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module_dir = root / "serum" / "hook"
            module_dir.mkdir(parents=True)
            media = module_dir / "serum_hook_20260509_0.mp4"
            media.write_bytes(b"video")
            media.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "schema_version": mx.MODULE_SCHEMA_VERSION,
                        "module_id": media.stem,
                        "product": "serum",
                        "role": "hook",
                        "source_video": "vod.mp4",
                        "source_video_identity": {"path": "vod.mp4", "size": 1, "mtime_ns": 1},
                        "file_path": str(media),
                        "start": 0.0,
                        "end": 6.0,
                        "duration": 6.0,
                        "transcript_text": "promo serum",
                        "confidence": 0.9,
                        "ffprobe": {"duration": 6.0, "has_video": True, "has_audio": True},
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(mx, "probe_media", side_effect=AssertionError("unexpected reprobe")):
                index = mx.rebuild_library_index(root, ValidateCfg, write=False)

        self.assertEqual(index["module_count"], 1)

    def test_index_lock_fails_loudly_on_timeout(self):
        portalocker = mx._require_portalocker()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.mkdir(parents=True, exist_ok=True)
            lock_path = root / "index.json.lock"
            handle = lock_path.open("a+", encoding="utf-8")
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                with self.assertRaises(RuntimeError):
                    with mx.library_index_lock(root, Cfg):
                        pass
            finally:
                portalocker.unlock(handle)
                handle.close()

    def test_v1_sidecar_is_indexed_with_inferred_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module_dir = root / "serum" / "hook"
            module_dir.mkdir(parents=True)
            media = module_dir / "serum_hook_20260509_0.mp4"
            media.write_bytes(b"video")
            media.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "module_id": media.stem,
                        "product": "serum",
                        "role": "hook",
                        "source_video": "vod.mp4",
                        "file_path": str(media),
                        "start": 0.0,
                        "end": 6.0,
                        "duration": 6.0,
                        "confidence": 0.8,
                        "boundary_mode": "word_boundary_fallback",
                    }
                ),
                encoding="utf-8",
            )

            index = mx.rebuild_library_index(root, Cfg, write=False)

        self.assertEqual(index["module_count"], 1)
        self.assertEqual(index["modules"][0]["quality_status"], "needs_review")

    def test_quality_defaults_mark_word_boundary_for_review(self):
        quality = mx.module_quality_fields({"confidence": 0.9, "boundary_mode": "word_boundary_fallback"}, Cfg)

        self.assertEqual(quality["quality_status"], "needs_review")
        self.assertEqual(quality["quality_reason"], "word_boundary_fallback_requires_review")

    def test_legacy_sentence_without_product_evidence_requires_review(self):
        quality = mx.module_quality_fields(
            {
                "schema_version": 1,
                "product": "serum",
                "confidence": 0.9,
                "boundary_mode": "sentence",
                "transcript_text": "produk ini bagus banget untuk kulit",
            },
            Cfg,
        )

        self.assertEqual(quality["quality_status"], "needs_review")
        self.assertEqual(quality["quality_reason"], "product_evidence_unverified")

    def test_product_evidence_is_required(self):
        self.assertTrue(
            mx.product_has_transcript_evidence("serum", {"transcript_text": "serum vitamin c ini ringan"}, Cfg)
        )
        self.assertFalse(
            mx.product_has_transcript_evidence("serum", {"transcript_text": "produk ini bagus banget"}, Cfg)
        )
        self.assertFalse(
            mx.product_has_transcript_evidence(
                "serum",
                {
                    "transcript_text": "produk ini bagus banget",
                    "classification_reason": "Model menyebut serum, tapi bukan bukti transcript",
                },
                Cfg,
            )
        )
        self.assertTrue(
            mx.product_has_transcript_evidence(
                "serum",
                {
                    "transcript_text": "produk ini bagus banget",
                    "evidence_context_text": "ini serum terbaru dari proya",
                },
                Cfg,
            )
        )

    def test_transcript_context_uses_local_product_evidence_window(self):
        transcript = {
            "words": [
                {"word": "serum", "start": 8.0, "end": 8.3},
                {"word": "ini", "start": 10.0, "end": 10.2},
                {"word": "bagus", "start": 16.0, "end": 16.3},
                {"word": "toner", "start": 40.0, "end": 40.4},
            ]
        }

        context = mx.transcript_context_text(transcript, 16.0, 18.0, Cfg)

        self.assertIn("serum", context)
        self.assertNotIn("toner", context)

    def test_filename_collision_rejects_mismatched_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "serum" / "hook" / "serum_hook_20260509_0.mp4"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"video")
            target.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "schema_version": mx.MODULE_SCHEMA_VERSION,
                        "module_id": target.stem,
                        "product": "serum",
                        "role": "hook",
                        "source_video": "source.mp4",
                        "file_path": str(target),
                        "start": 10.0,
                        "end": 16.0,
                        "duration": 6.0,
                    }
                ),
                encoding="utf-8",
            )
            candidate = {
                "product": "serum",
                "role": "hook",
                "start": 0.0,
                "end": 6.0,
                "duration": 6.0,
                "transcript_text": "serum bagus sekali.",
                "classification_reason": "hook kuat",
                "confidence": 0.9,
                "words": [],
            }
            video = Path(tmp) / "source.mp4"
            video.write_bytes(b"video")

            with mock.patch.object(mx, "module_file_lock", null_lock), \
                mock.patch.object(
                    mx,
                    "probe_media",
                    return_value={"duration": 6.0, "has_video": True, "has_audio": True},
                ):
                with self.assertRaises(mx.ModuleExtractionError) as ctx:
                    mx.cut_and_register_module(candidate, str(video), target, Cfg)

        self.assertEqual(ctx.exception.reason, "filename_collision")

    def test_rejection_reasons_are_public_and_candidate_visible(self):
        stats = {"rejected": 0, "reject_reasons": {}}

        mx._count_reject(stats, "low_confidence")
        mx._count_reject(stats, "cta_duration_outside_bounds")

        candidate = {"product": "serum", "role": "cta"}
        mx._annotate_candidate(candidate, "rejected", "low_confidence")

        self.assertEqual(stats["reject_reasons"]["weak_confidence"], 1)
        self.assertEqual(stats["reject_reasons"]["duration_out_of_range"], 1)
        self.assertEqual(candidate["extraction_status"], "rejected")
        self.assertEqual(candidate["rejection_reason"], "weak_confidence")
        self.assertEqual(candidate["rejection_detail"], "low_confidence")

    def test_previous_post_cut_failure_is_skipped_for_same_policy_unless_forced(self):
        candidate = {
            "_cached_extraction_policy_hash": mx._extraction_policy_hash(Cfg),
            "_previous_extraction_status": "failed",
            "_previous_rejection_detail": "validation_failed",
        }

        self.assertEqual(mx._previous_post_cut_failure(candidate, Cfg, force=False), "validation_failed")
        self.assertIsNone(mx._previous_post_cut_failure(candidate, Cfg, force=True))

    def test_completed_zero_accepted_extraction_skips_same_vod_rerun(self):
        transcript = transcript_from_sentences(
            ["produk ini bagus banget untuk kulit wajah kamu sekarang."]
        )
        candidate = {
            "product": "serum",
            "role": "hook",
            "start_hint": 0.0,
            "target_duration": 6.0,
            "confidence": 0.9,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "2026-05-19-10-27-41.mp4"
            video.write_bytes(b"source")

            class CfgWithLibrary(Cfg):
                MODULE_LIBRARY_DIR = str(root / "library")

            with mock.patch.object(mx, "_require_portalocker"), \
                mock.patch.object(mx, "_load_or_classify_candidates", return_value=[dict(candidate)]), \
                mock.patch.object(mx, "library_index_lock", null_lock), \
                mock.patch.object(mx, "cut_and_register_module", side_effect=AssertionError("unexpected cut")):
                first = mx.extract_modules(str(video), transcript, [], str(root / "working" / "run_001"), CfgWithLibrary)

            with mock.patch.object(mx, "_require_portalocker"), \
                mock.patch.object(mx, "_load_or_classify_candidates", side_effect=AssertionError("unexpected candidate load")) as load, \
                mock.patch.object(mx, "snap_to_sentence_boundaries", side_effect=AssertionError("unexpected snap")), \
                mock.patch.object(mx, "cut_and_register_module", side_effect=AssertionError("unexpected cut")):
                second = mx.extract_modules(str(video), transcript, [], str(root / "working" / "run_002"), CfgWithLibrary)

        self.assertEqual(first["accepted"], 0)
        self.assertEqual(first["reject_details"]["product_evidence_missing"], 1)
        load.assert_not_called()
        self.assertEqual(second["skipped_completed_extraction"], 1)
        self.assertEqual(second["accepted"], 0)
        self.assertEqual(second["previous_rejected"], 1)

    def test_force_modules_bypasses_completed_extraction_state(self):
        transcript = transcript_from_sentences(
            ["produk ini bagus banget untuk kulit wajah kamu sekarang."]
        )
        candidate = {
            "product": "serum",
            "role": "hook",
            "start_hint": 0.0,
            "target_duration": 6.0,
            "confidence": 0.9,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "2026-05-19-10-27-41.mp4"
            video.write_bytes(b"source")

            class CfgWithLibrary(Cfg):
                MODULE_LIBRARY_DIR = str(root / "library")

            with mock.patch.object(mx, "_require_portalocker"), \
                mock.patch.object(mx, "_load_or_classify_candidates", return_value=[dict(candidate)]), \
                mock.patch.object(mx, "library_index_lock", null_lock):
                mx.extract_modules(str(video), transcript, [], str(root / "working" / "run_001"), CfgWithLibrary)

            with mock.patch.object(mx, "_require_portalocker"), \
                mock.patch.object(mx, "_load_or_classify_candidates", return_value=[dict(candidate)]) as load, \
                mock.patch.object(mx, "library_index_lock", null_lock), \
                mock.patch.object(mx, "cut_and_register_module", side_effect=AssertionError("unexpected cut")):
                forced = mx.extract_modules(
                    str(video),
                    transcript,
                    [],
                    str(root / "working" / "run_002"),
                    CfgWithLibrary,
                    force=True,
                )

        load.assert_called_once()
        self.assertEqual(forced["skipped_completed_extraction"], 0)
        self.assertEqual(forced["reject_details"]["product_evidence_missing"], 1)

    def test_policy_change_invalidates_completed_extraction_state(self):
        transcript = transcript_from_sentences(
            ["produk ini bagus banget untuk kulit wajah kamu sekarang."]
        )
        candidate = {
            "product": "serum",
            "role": "hook",
            "start_hint": 0.0,
            "target_duration": 6.0,
            "confidence": 0.9,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "2026-05-19-10-27-41.mp4"
            video.write_bytes(b"source")

            class CfgWithLibrary(Cfg):
                MODULE_LIBRARY_DIR = str(root / "library")

            class ChangedPolicyCfg(CfgWithLibrary):
                MODULE_PRODUCT_EVIDENCE_CONTEXT_SECONDS = 24.0

            with mock.patch.object(mx, "_require_portalocker"), \
                mock.patch.object(mx, "_load_or_classify_candidates", return_value=[dict(candidate)]), \
                mock.patch.object(mx, "library_index_lock", null_lock):
                mx.extract_modules(str(video), transcript, [], str(root / "working" / "run_001"), CfgWithLibrary)

            with mock.patch.object(mx, "_require_portalocker"), \
                mock.patch.object(mx, "_load_or_classify_candidates", return_value=[dict(candidate)]) as load, \
                mock.patch.object(mx, "library_index_lock", null_lock), \
                mock.patch.object(mx, "cut_and_register_module", side_effect=AssertionError("unexpected cut")):
                changed = mx.extract_modules(
                    str(video),
                    transcript,
                    [],
                    str(root / "working" / "run_002"),
                    ChangedPolicyCfg,
                )

        load.assert_called_once()
        self.assertEqual(changed["skipped_completed_extraction"], 0)
        self.assertEqual(changed["reject_details"]["product_evidence_missing"], 1)

    def test_module_output_path_uses_source_stem_to_avoid_same_day_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp)
            candidate = {"product": "serum", "role": "cta", "start": 410.110}

            morning = mx.module_output_path(
                library,
                candidate,
                Path(r"D:\VOD\2026-05-19-10-27-41.mp4"),
                Cfg,
            )
            noon = mx.module_output_path(
                library,
                candidate,
                Path(r"D:\VOD\2026-05-19-11-48-15.mp4"),
                Cfg,
            )

        self.assertNotEqual(morning.name, noon.name)
        self.assertIn("2026-05-19-10-27-41", morning.name)
        self.assertIn("2026-05-19-11-48-15", noon.name)

    def test_module_cut_uses_cpu_encoder_and_config_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "module.mp4"
            seen = {}

            class RunResult:
                returncode = 0
                stderr = ""

            class RuntimeCfg(Cfg):
                OUTPUT_CODEC = "h264_nvenc"
                MODULE_EXTRACT_FFMPEG_TIMEOUT = 300

            def fake_run(cmd, capture_output=None, text=None, timeout=None, check=None):
                seen["cmd"] = cmd
                seen["timeout"] = timeout
                output_path.write_bytes(b"ok")
                return RunResult()

            with mock.patch.object(mx.subprocess, "run", side_effect=fake_run):
                ok = mx.cut_module_reencode("source.mp4", 5.0, 17.0, output_path, RuntimeCfg)

        self.assertTrue(ok)
        self.assertEqual(seen["cmd"][seen["cmd"].index("-c:v") + 1], "libx264")
        self.assertNotIn("h264_nvenc", seen["cmd"])
        self.assertGreaterEqual(seen["timeout"], 300)


if __name__ == "__main__":
    unittest.main()
