import os
import tempfile
import unittest
from pathlib import Path

from clipper_app.path_safety import UnsafePathError, resolve_within_root


class PathSafetyTest(unittest.TestCase):
    def test_accepts_relative_parent_segments_and_absolute_paths_inside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "clips" / "clip.mp4"
            target.parent.mkdir()
            target.write_bytes(b"clip")

            relative = resolve_within_root(root, "clips/unused/../clip.mp4", kind="file")
            absolute = resolve_within_root(root, target.resolve(), kind="file")

            self.assertEqual(relative, target.resolve())
            self.assertEqual(absolute, target.resolve())

    def test_rejects_traversal_nul_and_windows_ads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            outside = root.parent / "outside.mp4"
            outside.write_bytes(b"outside")

            with self.assertRaises(UnsafePathError):
                resolve_within_root(root, "../outside.mp4")
            with self.assertRaises(UnsafePathError):
                resolve_within_root(root, "clip.mp4\x00.json")
            with self.assertRaises(UnsafePathError):
                resolve_within_root(root, "clip.mp4:metadata")

    def test_rejects_symlink_or_junction_whose_canonical_target_is_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            root = workspace / "root"
            root.mkdir()
            outside = workspace / "outside"
            outside.mkdir()
            (outside / "clip.mp4").write_bytes(b"outside")
            link = root / "linked"
            try:
                _make_directory_link(outside, link)
            except OSError as exc:
                self.skipTest(f"directory links unavailable: {exc}")

            with self.assertRaises(UnsafePathError):
                resolve_within_root(root, link / "clip.mp4", kind="file")


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
