from __future__ import annotations

import logging
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from clipper_app.application.log_tail import TAIL_MAX_BYTES, reverse_tail
from clipper_app.application.logging_utils import (
    LockedSizeRotatingFileHandler,
    append_rotating_text,
    rotate_file_if_oversize,
)


def _append_records_in_process(path: str, prefix: str, count: int) -> None:
    for index in range(count):
        append_rotating_text(
            path,
            f"{prefix}-{index:03d}\n",
            max_bytes=128,
            backup_count=20,
        )


class LoggingUtilsTests(unittest.TestCase):
    def test_handler_rotates_complete_records_without_holding_a_stream(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pipeline.log"
            handler = LockedSizeRotatingFileHandler(
                path,
                max_bytes=80,
                backup_count=2,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.getLogger(f"rotation-test-{id(self)}")
            logger.handlers = [handler]
            logger.propagate = False
            logger.setLevel(logging.INFO)

            for index in range(12):
                logger.info("record-%02d-xxxxxxxx", index)

            self.assertIsNone(handler.stream)
            self.assertTrue(path.exists())
            self.assertTrue(path.with_name("pipeline.log.1").exists())
            self.assertTrue(path.with_name("pipeline.log.2").exists())
            for candidate in (path, path.with_name("pipeline.log.1"), path.with_name("pipeline.log.2")):
                payload = candidate.read_bytes()
                self.assertTrue(payload.endswith(b"\n"))
                self.assertLessEqual(len(payload), 80)
                self.assertTrue(all(line.startswith(b"record-") for line in payload.splitlines()))

            handler.close()
            logger.handlers = []

    def test_legacy_oversized_file_keeps_only_recent_line_aligned_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pipeline.log"
            path.write_bytes(b"".join(f"line-{index:03d}\n".encode() for index in range(40)))

            append_rotating_text(path, "fresh\n", max_bytes=90, backup_count=2)

            backup = path.with_name("pipeline.log.1")
            self.assertEqual(path.read_text(encoding="utf-8"), "fresh\n")
            self.assertLessEqual(backup.stat().st_size, 90)
            self.assertTrue(backup.read_bytes().endswith(b"line-039\n"))
            self.assertTrue(backup.read_bytes().startswith(b"line-"))
            self.assertNotIn(b"line-000", backup.read_bytes())

    def test_concurrent_appenders_do_not_interleave_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "audit.jsonl"

            def write(index: int) -> None:
                append_rotating_text(
                    path,
                    f"record-{index:04d}\n",
                    max_bytes=1024 * 1024,
                    backup_count=1,
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(write, range(200)))

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 200)
            self.assertEqual(set(lines), {f"record-{index:04d}" for index in range(200)})

    def test_spawned_processes_rotate_without_losing_or_interleaving_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pipeline.log"
            context = multiprocessing.get_context("spawn")
            processes = [
                context.Process(target=_append_records_in_process, args=(str(path), prefix, 100))
                for prefix in ("left", "right")
            ]

            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=30)
                self.assertEqual(process.exitcode, 0)

            candidates = [path, *[path.with_name(f"{path.name}.{index}") for index in range(1, 21)]]
            lines = [
                line
                for candidate in candidates
                if candidate.exists()
                for line in candidate.read_text(encoding="utf-8").splitlines()
            ]
            expected = {f"{prefix}-{index:03d}" for prefix in ("left", "right") for index in range(100)}
            self.assertEqual(len(lines), 200)
            self.assertEqual(set(lines), expected)

    def test_prelaunch_rotation_rotates_at_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue_supervisor_launch.log"
            path.write_bytes(b"a\n" * 32)

            rotated = rotate_file_if_oversize(path, max_bytes=64, backup_count=3)

            self.assertTrue(rotated)
            self.assertFalse(path.exists())
            self.assertEqual(path.with_name(f"{path.name}.1").read_bytes(), b"a\n" * 32)

    def test_reverse_tail_is_bounded_and_returns_newest_first(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pipeline.log"
            with path.open("wb") as handle:
                for index in range(500_000):
                    handle.write(f"line-{index:06d}\n".encode())

            result = reverse_tail(path, line_limit=200)

            self.assertFalse(result.reached_start)
            self.assertIsNone(result.total_lines)
            self.assertLessEqual(result.bytes_read, 64 * 1024)
            self.assertEqual(len(result.lines), 200)
            self.assertEqual(result.lines[0].text, "line-499999")
            self.assertEqual(result.lines[-1].text, "line-499800")
            self.assertTrue(all(line.line_number is None for line in result.lines))

    def test_reverse_tail_caps_a_single_unfinished_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pipeline.log"
            path.write_bytes(b"x" * (TAIL_MAX_BYTES + 1))

            result = reverse_tail(path, line_limit=10)

            self.assertEqual(result.bytes_read, TAIL_MAX_BYTES)
            self.assertTrue(result.partial_oldest_line)
            self.assertEqual(result.lines, ())


if __name__ == "__main__":
    unittest.main()
