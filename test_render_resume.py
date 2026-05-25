import tempfile
import unittest
from pathlib import Path

from main import _build_clip_job, _completed_resume_rows


class RenderResumeTests(unittest.TestCase):
    def test_completed_resume_rows_skip_failed_and_require_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            raw_dir = output_dir / "raw"
            raw_dir.mkdir()
            ok_output = output_dir / "v1" / "clip_0001.mp4"
            ok_output.parent.mkdir()
            ok_output.write_bytes(b"ok")

            moments = [
                {"clip_id": "clip_0001", "start": 0, "end": 10, "score": 9, "hook": "a"},
                {"clip_id": "clip_0002", "start": 10, "end": 20, "score": 8, "hook": "b"},
                {"clip_id": "clip_0003", "start": 20, "end": 30, "score": 7, "hook": "c"},
            ]
            jobs = [_build_clip_job(moment, index, str(output_dir), raw_dir) for index, moment in enumerate(moments)]
            manifest = [
                {"clip_id": "clip_0001", "status": "ok", "output_file": "v1/clip_0001.mp4"},
                {"clip_id": "clip_0002", "status": "failed", "output_file": "clip_0002.mp4"},
                {"clip_id": "clip_0003", "status": "compliance_blocked", "output_file": "clip_0003.mp4"},
            ]

            rows = _completed_resume_rows(jobs, manifest, output_dir)

            self.assertEqual([row["clip_id"] for row in rows], ["clip_0001", "clip_0003"])


if __name__ == "__main__":
    unittest.main()
