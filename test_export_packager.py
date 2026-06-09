import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from export_packager import package_export_batches


class ExportPackagerTest(unittest.TestCase):
    def test_score_round_robin_distribution_with_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = [
                {"clip_id": f"clip_{index:04d}_v0_original", "score": 101 - index}
                for index in range(1, 66)
            ]
            write_source(root, "vod_a", specs)

            result = package_export_batches(root, cfg=make_cfg(), batch_size=30)

            self.assertEqual(result["packaged_count"], 65)
            self.assertEqual(batch_counts(root), {"1": 22, "2": 22, "3": 21})
            self.assertLessEqual(max(batch_counts(root).values()), 30)

            items = package_items(root)
            by_score = {int(item["total_score"]): item["batch_folder"] for item in items}
            self.assertEqual(by_score[100], "1")
            self.assertEqual(by_score[99], "2")
            self.assertEqual(by_score[98], "3")
            self.assertEqual(by_score[97], "1")

            for item in items:
                destination = Path(item["destination_path"])
                self.assertTrue(destination.exists())
                self.assertTrue(destination.parent.name.isdigit())
                self.assertEqual(destination.parent.parent.name, "export_batches")

    def test_dedupes_by_normalized_source_and_clip_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source(
                root,
                "VOD A",
                [
                    {
                        "clip_id": "clip_0001_v0_original",
                        "score": 9.0,
                        "filename": "first.mp4",
                        "content": b"first unique file",
                    },
                    {
                        "clip_id": "clip_0001_v0_original",
                        "score": 8.0,
                        "filename": "second.mp4",
                        "content": b"second unique file",
                    },
                ],
            )

            result = package_export_batches(root, cfg=make_cfg(), batch_size=30)

            self.assertEqual(result["packaged_count"], 1)
            self.assertEqual(result["excluded_variant_count"], 1)
            items = package_items(root)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["clip_id"], "clip_0001_v0_original")

    def test_dedupes_by_first_64k_content_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            duplicate_content = b"same first bytes" * 64
            write_source(
                root,
                "vod_a",
                [
                    {
                        "clip_id": "clip_0001_v0_original",
                        "score": 9.0,
                        "filename": "alpha.mp4",
                        "content": duplicate_content,
                    }
                ],
            )
            write_source(
                root,
                "vod_b",
                [
                    {
                        "clip_id": "clip_9999_v1_variant",
                        "score": 8.0,
                        "filename": "renamed.mp4",
                        "content": duplicate_content,
                    }
                ],
            )

            result = package_export_batches(root, cfg=make_cfg(), batch_size=30)

            self.assertEqual(result["packaged_count"], 1)
            self.assertEqual(result["duplicate_candidate_count"], 1)
            items = package_items(root)
            self.assertEqual(items[0]["clip_id"], "clip_0001_v0_original")

    def test_append_only_keeps_existing_assignments_and_adds_new_unique_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source(
                root,
                "vod_a",
                [
                    {"clip_id": "clip_0001_v0_original", "score": 9.0},
                    {"clip_id": "clip_0002_v0_original", "score": 8.0},
                    {"clip_id": "clip_0003_v0_original", "score": 7.0},
                ],
            )
            first = package_export_batches(root, cfg=make_cfg(), batch_size=2)
            self.assertEqual(first["packaged_count"], 3)
            original_destinations = {
                item["source_clip_key"]: item["destination_path"]
                for item in package_items(root)
            }

            write_source(
                root,
                "vod_b",
                [
                    {"clip_id": "clip_0100_v0_original", "score": 10.0},
                    {"clip_id": "clip_0101_v0_original", "score": 6.0},
                ],
            )
            second = package_export_batches(root, cfg=make_cfg(), batch_size=2)

            self.assertEqual(second["packaged_count"], 2)
            self.assertEqual(batch_counts(root), {"1": 2, "2": 2, "3": 1})
            updated_items = package_items(root)
            for item in updated_items:
                if item["source_clip_key"] in original_destinations:
                    self.assertEqual(item["destination_path"], original_destinations[item["source_clip_key"]])

    def test_legacy_cutoff_freezes_existing_folders_and_starts_next_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_legacy_batch(root, 1, 30)
            write_legacy_batch(root, 2, 10)
            write_source(
                root,
                "vod_new",
                [{"clip_id": "clip_0100_v0_original", "score": 9.0}],
            )

            result = package_export_batches(root, cfg=make_cfg(), batch_size=15)

            self.assertEqual(result["legacy_batch_folder_cutoff"], 2)
            self.assertEqual(batch_counts(root), {"1": 30, "2": 10, "3": 1})
            items = package_items(root)
            self.assertEqual(items[0]["batch_folder"], "3")
            manifest = package_manifest(root)
            self.assertEqual(manifest["batch_size"], 15)
            self.assertEqual(manifest["legacy_batch_folder_cutoff"], 2)

    def test_post_cutoff_batches_can_be_topped_up_to_new_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_legacy_batch(root, 1, 30)
            write_source(
                root,
                "vod_a",
                [
                    {"clip_id": f"clip_{index:04d}_v0_original", "score": 100 - index}
                    for index in range(1, 11)
                ],
            )

            first = package_export_batches(root, cfg=make_cfg(), batch_size=15)
            self.assertEqual(first["legacy_batch_folder_cutoff"], 1)
            self.assertEqual(batch_counts(root), {"1": 30, "2": 10})

            write_source(
                root,
                "vod_b",
                [
                    {"clip_id": f"clip_{index:04d}_v0_original", "score": 100 - index}
                    for index in range(101, 111)
                ],
            )

            second = package_export_batches(root, cfg=make_cfg(), batch_size=15)

            self.assertEqual(second["legacy_batch_folder_cutoff"], 1)
            counts = batch_counts(root)
            self.assertEqual(counts, {"1": 30, "2": 15, "3": 5})
            self.assertLessEqual(
                max(count for folder, count in counts.items() if int(folder) > 1),
                15,
            )

    def test_excludes_failed_compliance_blocked_and_review_tiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source(
                root,
                "vod_a",
                [
                    {"clip_id": "clip_0001_v0_original", "score": 9.0},
                    {"clip_id": "clip_0002_v0_original", "score": 8.0, "status": "failed"},
                    {
                        "clip_id": "clip_0003_v0_original",
                        "score": 7.0,
                        "status": "compliance_blocked",
                    },
                    {
                        "clip_id": "clip_0004_v0_original",
                        "score": 6.0,
                        "tier": "review_needed",
                    },
                ],
            )

            result = package_export_batches(root, cfg=make_cfg(), batch_size=30)

            self.assertEqual(result["packaged_count"], 1)
            self.assertEqual(package_items(root)[0]["clip_id"], "clip_0001_v0_original")

    def test_one_variant_per_base_clip_uses_stable_variant_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source(
                root,
                "vod_a",
                [
                    {"clip_id": "clip_0018_v0_original", "score": 1.0},
                    {"clip_id": "clip_0018_v1_cream_soft", "score": 9.0},
                    {"clip_id": "clip_0018_v2_hot_pink", "score": 8.0},
                    {"clip_id": "clip_0018_v3_result_overlay", "score": 7.0},
                    {"clip_id": "clip_0018_v4_host_focus_fast", "score": 6.0},
                    {"clip_id": "clip_0018_v5_clean_commerce", "score": 5.0},
                ],
            )

            result = package_export_batches(root, cfg=make_cfg(), batch_size=30)

            self.assertEqual(result["packaged_count"], 1)
            self.assertEqual(result["excluded_variant_count"], 5)
            items = package_items(root)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["base_clip_id"], "clip_0018")
            self.assertEqual(items[0]["clip_id"], "clip_0018_v4_host_focus_fast")
            self.assertEqual(items[0]["selected_variant"], "v4_host_focus_fast")
            self.assertEqual(
                items[0]["excluded_variants"],
                [
                    "v0_original",
                    "v1_cream_soft",
                    "v2_hot_pink",
                    "v3_result_overlay",
                    "v5_clean_commerce",
                ],
            )

    def test_one_variant_per_base_clip_does_not_collapse_to_v0(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = []
            for clip_number in range(1, 13):
                base = f"clip_{clip_number:04d}"
                specs.extend(
                    [
                        {"clip_id": f"{base}_v0_original", "score": 9.0},
                        {"clip_id": f"{base}_v1_product_broll_open", "score": 9.0},
                        {"clip_id": f"{base}_v2_tight_product_focus", "score": 9.0},
                        {"clip_id": f"{base}_v3_result_overlay_broll", "score": 9.0},
                        {"clip_id": f"{base}_v4_host_focus_fast_broll", "score": 9.0},
                        {"clip_id": f"{base}_v5_clean_commerce", "score": 9.0},
                    ]
                )
            write_source(root, "vod_a", specs)

            result = package_export_batches(root, cfg=make_cfg(), batch_size=30)

            self.assertEqual(result["packaged_count"], 12)
            selected_variants = {
                item["selected_variant"]
                for item in package_items(root)
            }
            self.assertGreater(len(selected_variants), 1)
            self.assertIn("v0_original", selected_variants)
            self.assertTrue(any(variant != "v0_original" for variant in selected_variants))

    def test_one_variant_per_base_clip_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source(
                root,
                "vod_a",
                [
                    {"clip_id": "clip_0018_v0_original", "score": 1.0},
                    {"clip_id": "clip_0018_v1_cream_soft", "score": 9.0},
                    {"clip_id": "clip_0018_v2_hot_pink", "score": 8.0},
                    {"clip_id": "clip_0018_v3_result_overlay", "score": 7.0},
                    {"clip_id": "clip_0018_v4_host_focus_fast", "score": 6.0},
                    {"clip_id": "clip_0018_v5_clean_commerce", "score": 5.0},
                ],
            )

            result = package_export_batches(root, cfg=make_cfg(one_variant=False), batch_size=30)

            self.assertEqual(result["packaged_count"], 6)
            self.assertEqual(result["excluded_variant_count"], 0)
            self.assertEqual(len(package_items(root)), 6)


def make_cfg(one_variant: bool = True):
    return SimpleNamespace(
        EXPORT_BATCH_DIR_NAME="export_batches",
        EXPORT_BATCH_SIZE=15,
        EXPORT_PACK_ONE_VARIANT_PER_CLIP=one_variant,
    )


def write_source(root: Path, source_name: str, specs: list[dict]) -> None:
    source_dir = root / source_name
    manifest = []
    clips = []
    for index, spec in enumerate(specs, start=1):
        clip_id = spec["clip_id"]
        score = float(spec["score"])
        tier = spec.get("tier", "export_ready")
        version = spec.get("version", "v0")
        filename = spec.get("filename") or f"{clip_id}_score{int(score)}.mp4"
        relative = f"{tier}/{version}/{filename}"
        clip_path = source_dir / relative
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        content = spec.get("content")
        if content is None:
            content = f"{source_name}|{clip_id}|{index}".encode("utf-8")
        clip_path.write_bytes(content)
        row = {
            "clip_id": clip_id,
            "status": spec.get("status", "ok"),
            "output_file": relative,
            "scorer_total_score": score,
            "product": spec.get("product", "serum"),
            "clip_type": "variant",
        }
        manifest.append(row)
        clips.append(
            {
                "clip_id": clip_id,
                "output_file": relative,
                "clip_path": str(clip_path.resolve()),
                "total_score": score,
                "product": row["product"],
                "clip_type": row["clip_type"],
                "status": row["status"],
            }
        )
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source_dir / "scores_summary.json").write_text(
        json.dumps({"clips": clips, "groups": []}),
        encoding="utf-8",
    )


def package_items(root: Path) -> list[dict]:
    return package_manifest(root)["items"]


def package_manifest(root: Path) -> dict:
    return json.loads((root / "export_batches" / "_manifest.json").read_text(encoding="utf-8"))


def batch_counts(root: Path) -> dict[str, int]:
    return {
        folder.name: len(list(folder.glob("*.mp4")))
        for folder in sorted((root / "export_batches").iterdir(), key=lambda path: path.name)
        if folder.is_dir()
    }


def write_legacy_batch(root: Path, folder_number: int, count: int) -> None:
    folder = root / "export_batches" / str(folder_number)
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        (folder / f"legacy_{folder_number}_{index:03d}.mp4").write_bytes(
            f"legacy|{folder_number}|{index}".encode("utf-8")
        )


if __name__ == "__main__":
    unittest.main()
