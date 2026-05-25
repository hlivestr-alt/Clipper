import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from clip_scorer import write_score_artifacts


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


if __name__ == "__main__":
    unittest.main()
