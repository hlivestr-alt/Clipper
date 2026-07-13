import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clip_scorer import (
    _contact_sheet_validation_clip_paths,
    _write_host_focus_debug_artifacts,
    score_output_folder,
    write_score_artifacts,
    write_scores_report,
)


class ClipScorerSortTest(unittest.TestCase):
    def test_auto_sort_moves_clip_and_updates_score_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "v1" / "clip_0001.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"fake mp4 bytes")

            cfg = SimpleNamespace(
                SCORER_AUTO_SORT_ENABLED=True,
                SCORER_EXPORT_READY_THRESHOLD=7.0,
                SCORER_REVIEW_THRESHOLD=5.0,
            )
            artifacts = write_score_artifacts(
                [
                    {
                        "clip_id": "clip_0001",
                        "clip_path": str(source),
                        "output_file": "v1/clip_0001.mp4",
                        "total_score": 8.0,
                    }
                ],
                root,
                cfg=cfg,
            )

            destination = root / "export_ready" / "v1" / "clip_0001.mp4"
            self.assertFalse(source.exists())
            self.assertTrue(destination.exists())
            self.assertEqual(artifacts["tier_move"]["moved"], 1)

            summary = json.loads((root / "scores_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["clips"][0]["output_file"], "export_ready/v1/clip_0001.mp4")
            self.assertEqual(Path(summary["clips"][0]["clip_path"]), destination.resolve())

    def test_manifest_paths_outside_run_and_packaged_rows_are_not_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            root = workspace / "run"
            root.mkdir()
            outside = workspace / "outside.mp4"
            outside.write_bytes(b"outside")
            (root / "manifest.json").write_text(
                json.dumps(
                    [
                        {"clip_id": "traversal", "output_file": "../outside.mp4"},
                        {"clip_id": "absolute", "output_file": str(outside.resolve())},
                        {
                            "clip_id": "packaged",
                            "output_file": "export_batches/1/packaged.mp4",
                            "export_batch_path": str(workspace / "export_batches" / "1" / "packaged.mp4"),
                        },
                    ]
                ),
                encoding="utf-8",
            )

            with patch("clip_scorer.score_clip_variants", return_value=([], [], {})) as score_variants:
                scores = score_output_folder(root, working_dir=workspace / "working", cfg=SimpleNamespace())

            self.assertEqual(scores, [])
            self.assertEqual(score_variants.call_args.args[0], [])
            self.assertEqual(outside.read_bytes(), b"outside")
            self.assertFalse((workspace / "scores.json").exists())

    def test_in_root_parent_segments_remain_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "v1" / "clip.mp4"
            clip.parent.mkdir()
            clip.write_bytes(b"clip")
            (root / "manifest.json").write_text(
                json.dumps([{"clip_id": "clip", "output_file": "v1/sub/../clip.mp4"}]),
                encoding="utf-8",
            )

            with patch("clip_scorer.score_clip_variants", return_value=([], [], {})) as score_variants:
                score_output_folder(root, working_dir=root / "working", cfg=SimpleNamespace())

            entries = score_variants.call_args.args[0]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["clip_path"], clip.resolve())

    def test_auto_sort_does_not_touch_external_absolute_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            root = workspace / "run"
            root.mkdir()
            outside = workspace / "outside.mp4"
            outside.write_bytes(b"outside")
            cfg = SimpleNamespace(
                SCORER_AUTO_SORT_ENABLED=True,
                SCORER_EXPORT_READY_THRESHOLD=7.0,
                SCORER_REVIEW_THRESHOLD=5.0,
            )

            artifacts = write_score_artifacts(
                [{"clip_id": "outside", "clip_path": str(outside), "total_score": 9.0}],
                root,
                cfg=cfg,
            )

            self.assertEqual(outside.read_bytes(), b"outside")
            self.assertEqual(artifacts["tier_move"]["moved"], 0)
            self.assertFalse((root / "export_ready" / "outside.mp4").exists())

    def test_manifest_junction_escape_is_not_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            root = workspace / "run"
            outside = workspace / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "clip.mp4").write_bytes(b"outside")
            try:
                _make_directory_link(outside, root / "linked")
            except OSError as exc:
                self.skipTest(f"directory links unavailable: {exc}")
            (root / "manifest.json").write_text(
                json.dumps([{"clip_id": "clip", "output_file": "linked/clip.mp4"}]),
                encoding="utf-8",
            )

            with patch("clip_scorer.score_clip_variants", return_value=([], [], {})) as score_variants:
                score_output_folder(root, working_dir=root / "working", cfg=SimpleNamespace())

            self.assertEqual(score_variants.call_args.args[0], [])
            self.assertEqual((outside / "clip.mp4").read_bytes(), b"outside")

    def test_contact_sheet_paths_allow_packaged_output_but_reject_external_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            output_root = workspace / "output"
            run = output_root / "run"
            packaged = output_root / "export_batches" / "1" / "packaged.mp4"
            outside = workspace / "outside.mp4"
            run.mkdir(parents=True)
            packaged.parent.mkdir(parents=True)
            packaged.write_bytes(b"packaged")
            outside.write_bytes(b"outside")
            (run / "scores_summary.json").write_text(
                json.dumps(
                    {
                        "groups": [{"clip_path": str(outside.resolve())}],
                        "clips": [{"clip_path": str(packaged.resolve())}],
                    }
                ),
                encoding="utf-8",
            )
            (run / "manifest.json").write_text(
                json.dumps([{"output_file": "../../outside.mp4"}]),
                encoding="utf-8",
            )

            paths = _contact_sheet_validation_clip_paths(
                run,
                10,
                allowed_output_root=output_root,
            )

            self.assertEqual(paths, [packaged.resolve()])
            self.assertNotIn(outside.resolve(), paths)

    def test_score_report_atomically_replaces_hardlink_without_overwriting_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run = workspace / "run"
            run.mkdir()
            outside = workspace / "outside.txt"
            outside.write_text("keep", encoding="utf-8")
            os.link(outside, run / "scores_report.txt")

            report = write_scores_report([], run, cfg=SimpleNamespace())

            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
            self.assertIn("PROYA Clip Score Trend Report", report.read_text(encoding="utf-8"))

    def test_focus_debug_images_atomically_replace_hardlinks(self):
        try:
            import cv2  # noqa: F401
            import numpy as np
        except ImportError as exc:
            self.skipTest(f"image dependencies unavailable: {exc}")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run = workspace / "run"
            run.mkdir()
            outside_thumb = workspace / "outside_thumb.jpg"
            outside_sheet = workspace / "outside_sheet.jpg"
            outside_thumb.write_bytes(b"keep-thumb")
            outside_sheet.write_bytes(b"keep-sheet")
            os.link(outside_thumb, run / "clip_focus_frame_00.jpg")
            os.link(outside_sheet, run / "clip_focus_debug.jpg")

            _write_host_focus_debug_artifacts(
                run / "clip.mp4",
                "clip",
                [
                    {
                        "sample": {"label": "A", "frame": 1, "time": 0.1, "confidence": 1.0},
                        "frame": np.zeros((32, 32, 3), dtype=np.uint8),
                    }
                ],
            )

            self.assertEqual(outside_thumb.read_bytes(), b"keep-thumb")
            self.assertEqual(outside_sheet.read_bytes(), b"keep-sheet")
            self.assertGreater((run / "clip_focus_frame_00.jpg").stat().st_size, 20)
            self.assertGreater((run / "clip_focus_debug.jpg").stat().st_size, 20)


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
