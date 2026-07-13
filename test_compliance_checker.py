import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from compliance_checker import (
    apply_compliance_to_hook_payload,
    apply_compliance_to_words,
    check_compliance,
    compliance_output_root_for_clip,
    compliance_path_for_clip,
    scan_output_dir,
    should_block_result,
    write_compliance_result,
)
from clipper_app.path_safety import UnsafePathError


class ComplianceCheckerTest(unittest.TestCase):
    def test_compliance_path_uses_dedicated_output_folder(self):
        path = compliance_path_for_clip(Path(r"C:\clips\run\v1\clip_0001.mp4"), "clip_0001")

        self.assertEqual(path, Path(r"C:\clips\run\compliance\clip_0001_compliance.json"))

    def test_compliance_path_ignores_sort_tier_folder(self):
        path = compliance_path_for_clip(
            Path(r"C:\clips\run\export_ready\v1\clip_0001.mp4"),
            "clip_0001",
        )

        self.assertEqual(path, Path(r"C:\clips\run\compliance\clip_0001_compliance.json"))

    def test_compliance_writer_requires_and_enforces_run_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run = workspace / "run"
            outside = workspace / "outside"
            run.mkdir()
            outside.mkdir()
            result = {"passed": True}

            with self.assertRaises(TypeError):
                write_compliance_result(outside / "result.json", result)  # type: ignore[call-arg]
            with self.assertRaises(UnsafePathError):
                write_compliance_result(outside / "result.json", result, output_root=run)
            self.assertFalse((outside / "result.json").exists())

            clip = run / "export_ready" / "v1" / "clip.mp4"
            compliance_root = compliance_output_root_for_clip(clip)
            path = compliance_path_for_clip(clip, "clip")
            written = write_compliance_result(path, result, output_root=compliance_root)

            self.assertEqual(compliance_root, run.resolve())
            self.assertEqual(written, run / "compliance" / "clip_compliance.json")
            self.assertTrue(written.exists())

    def test_scan_sidecar_ignores_manifest_traversal_and_sanitizes_clip_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run = workspace / "run"
            working = workspace / "working"
            run.mkdir()
            working.mkdir()
            outside = workspace / "outside.mp4"
            outside.write_bytes(b"outside")
            (run / "manifest.json").write_text(
                json.dumps(
                    [
                        {
                            "clip_id": "../../unsafe:id",
                            "output_file": "../outside.mp4",
                            "start": 0,
                            "end": 1,
                            "product": "Serum",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (working / "transcript.json").write_text(
                json.dumps({"words": [{"word": "serum", "start": 0, "end": 1}]}),
                encoding="utf-8",
            )

            result = scan_output_dir(run, working_dir=working, cfg=_cfg())

            self.assertEqual(result["scanned"], 1)
            sidecars = list((run / "compliance").glob("*_compliance.json"))
            self.assertEqual(len(sidecars), 1)
            self.assertEqual(sidecars[0].parent, run / "compliance")
            self.assertEqual(outside.read_bytes(), b"outside")

    def test_compliance_symlink_escape_fails_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run = workspace / "run"
            working = workspace / "working"
            outside = workspace / "outside"
            run.mkdir()
            working.mkdir()
            outside.mkdir()
            manifest_path = run / "manifest.json"
            manifest_path.write_text(
                json.dumps([{"clip_id": "clip", "output_file": "clip.mp4", "start": 0, "end": 1}]),
                encoding="utf-8",
            )
            original_manifest = manifest_path.read_bytes()
            (working / "transcript.json").write_text(json.dumps({"words": []}), encoding="utf-8")
            try:
                _make_directory_link(outside, run / "compliance")
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            with self.assertRaises(UnsafePathError):
                scan_output_dir(run, working_dir=working, cfg=_cfg())

            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            self.assertEqual(list(outside.iterdir()), [])

    def test_keyword_fallback_detects_all_violation_types(self):
        text = (
            "Serum ini menyembuhkan jerawat, terbaik di dunia, "
            "tanpa merkuri, dan pasti cerah."
        )

        result = check_compliance(text, "Serum", cfg=_cfg(), call_lm=False)

        violation_types = {item["violation_type"] for item in result["violations"]}
        self.assertIn("medical_claim", violation_types)
        self.assertIn("exaggerated_claim", violation_types)
        self.assertIn("prohibited_ingredient_claim", violation_types)
        self.assertIn("absolute_claim", violation_types)
        self.assertGreaterEqual(result["violation_count"], 4)
        self.assertFalse(result["passed"])

    def test_low_severity_auto_fix_updates_clean_text_and_words(self):
        words = [
            {"word": "produk", "start": 0.0, "end": 0.2},
            {"word": "ini", "start": 0.2, "end": 0.4},
            {"word": "pasti", "start": 0.4, "end": 0.7},
            {"word": "cerah", "start": 0.7, "end": 1.0},
            {"word": "dijamin", "start": 1.0, "end": 1.3},
            {"word": "halus", "start": 1.3, "end": 1.6},
        ]

        result = check_compliance(words, "Serum", cfg=_cfg(), call_lm=False)
        cleaned_words = apply_compliance_to_words(words, result)
        cleaned_text = " ".join(item["word"] for item in cleaned_words)

        self.assertTrue(result["auto_fixed"])
        self.assertTrue(result["passed"])
        self.assertIn("kulit tampak lebih cerah", result["cleaned_transcript"])
        self.assertIn("kulit terasa lebih halus", result["cleaned_transcript"])
        self.assertIn("kulit tampak lebih cerah", cleaned_text)
        self.assertNotIn("pasti cerah", cleaned_text)

    def test_high_severity_blocks_export_when_enabled(self):
        result = check_compliance("Serum ini 100% ampuh mengobati jerawat.", "Serum", cfg=_cfg(), call_lm=False)

        self.assertTrue(should_block_result(result, _cfg()))

    def test_clean_transcript_skips_lm_call(self):
        calls = {"count": 0}

        def fake_lm(messages, cfg):
            calls["count"] += 1
            raise AssertionError("LM should not be called for clean text")

        result = check_compliance(
            "Serum ini membantu kulit terasa lebih lembap dengan pemakaian rutin.",
            "Serum",
            cfg=_cfg(),
            lm_callable=fake_lm,
            call_lm=True,
        )

        self.assertEqual(calls["count"], 0)
        self.assertFalse(result["qwen_called"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["violation_count"], 0)

    def test_hook_text_is_scanned_even_when_transcript_is_clean(self):
        result = check_compliance(
            "Serum ini membantu kulit terasa lebih lembap dengan pemakaian rutin.",
            "Serum",
            hook_text={"headline": "100% AMPUH", "subtext": "untuk kulit glowing", "cta": "CHECKOUT"},
            cfg=_cfg(),
            call_lm=False,
        )

        self.assertFalse(result["passed"])
        self.assertTrue(should_block_result(result, _cfg()))
        self.assertTrue(any(item.get("source_field") == "hook" for item in result["violations"]))

    def test_low_severity_hook_text_auto_fix_updates_payload(self):
        hook_payload = {"headline": "PASTI CERAH", "subtext": "dijamin halus", "cta": "CHECKOUT"}

        result = check_compliance(
            "Serum ini membantu kulit terasa lebih lembap.",
            "Serum",
            hook_text=hook_payload,
            cfg=_cfg(),
            call_lm=False,
        )
        cleaned_hook = apply_compliance_to_hook_payload(hook_payload, result)

        self.assertTrue(result["auto_fixed"])
        self.assertTrue(result["passed"])
        self.assertIn("kulit tampak lebih cerah", result["cleaned_hook_text"])
        self.assertEqual(cleaned_hook["headline"], "KULIT TAMPAK LEBIH CERAH")
        self.assertEqual(cleaned_hook["subtext"], "kulit terasa lebih halus")


def _cfg():
    return SimpleNamespace(
        COMPLIANCE_AUTO_FIX=True,
        COMPLIANCE_BLOCK_HIGH=True,
        COMPLIANCE_LM_TIMEOUT=1,
        LM_STUDIO_BASE_URL="http://localhost:1234/v1",
        LM_STUDIO_API_KEY="lm-studio",
        LM_STUDIO_MODEL="qwen/qwen3.6-27b",
    )


def _make_directory_link(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        try:
            import _winapi
        except ImportError:
            raise
        _winapi.CreateJunction(str(target), str(link))


if __name__ == "__main__":
    unittest.main()
