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
            self.assertEqual(result["excluded_variant_count"], 0)
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

    def test_one_variant_per_base_clip_keeps_best_variant_for_backwards_compat(self):
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

            result = package_export_batches(root, cfg=make_cfg(one_variant=True), batch_size=30)

            self.assertEqual(result["packaged_count"], 1)
            self.assertEqual(result["excluded_variant_count"], 5)
            items = package_items(root)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["base_clip_id"], "clip_0018")
            self.assertEqual(items[0]["clip_id"], "clip_0018_v1_cream_soft")
            self.assertEqual(items[0]["selected_variant"], "v1_cream_soft")
            self.assertEqual(
                items[0]["excluded_variants"],
                [
                    "v2_hot_pink",
                    "v3_result_overlay",
                    "v4_host_focus_fast",
                    "v5_clean_commerce",
                    "v0_original",
                ],
            )

    def test_one_variant_per_base_clip_packs_one_variant_per_base(self):
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

            result = package_export_batches(root, cfg=make_cfg(one_variant=True), batch_size=30)

            self.assertEqual(result["packaged_count"], 12)
            items = package_items(root)
            self.assertEqual(len({item["base_clip_id"] for item in items}), 12)
            self.assertTrue(all(item["selected_variant"] == "v0_original" for item in items))

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

    def test_all_export_ready_variants_pack_into_different_folders_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source(
                root,
                "vod_a",
                [
                    {"clip_id": "clip_0018_v0_original", "score": 9.0},
                    {"clip_id": "clip_0018_v2_hot_pink", "score": 8.0},
                    {"clip_id": "clip_0018_v4_host_focus_fast", "score": 7.0},
                ],
            )

            result = package_export_batches(root, cfg=make_cfg(), batch_size=15)

            self.assertEqual(result["packaged_count"], 3)
            folders = {item["clip_id"]: item["batch_folder"] for item in package_items(root)}
            self.assertEqual(
                set(folders),
                {
                    "clip_0018_v0_original",
                    "clip_0018_v2_hot_pink",
                    "clip_0018_v4_host_focus_fast",
                },
            )
            self.assertEqual(len(set(folders.values())), 3)

    def test_different_base_clips_can_share_a_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source(
                root,
                "vod_a",
                [
                    {"clip_id": "clip_0018_v0_original", "score": 9.0},
                    {"clip_id": "clip_0019_v0_original", "score": 8.0},
                ],
            )

            result = package_export_batches(root, cfg=make_cfg(), batch_size=15)

            self.assertEqual(result["packaged_count"], 2)
            self.assertEqual(batch_counts(root), {"1": 2})

    def test_same_base_variants_never_share_a_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source(
                root,
                "vod_a",
                [
                    {"clip_id": "clip_0018_v0_original", "score": 9.0},
                    {"clip_id": "clip_0018_v2_hot_pink", "score": 8.0},
                    {"clip_id": "clip_0019_v0_original", "score": 7.0},
                ],
            )

            result = package_export_batches(root, cfg=make_cfg(), batch_size=15)

            self.assertEqual(result["packaged_count"], 3)
            folders_by_base = base_folders(package_items(root))
            self.assertEqual(len(folders_by_base["clip_0018"]), 2)
            self.assertEqual(len(folders_by_base["clip_0019"]), 1)

    def test_variant_aware_round_robin_still_balances_folder_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = []
            for clip_number in range(1, 21):
                base = f"clip_{clip_number:04d}"
                specs.extend(
                    [
                        {"clip_id": f"{base}_v0_original", "score": 100 - clip_number},
                        {"clip_id": f"{base}_v2_hot_pink", "score": 80 - clip_number},
                    ]
                )
            write_source(root, "vod_a", specs)

            result = package_export_batches(root, cfg=make_cfg(), batch_size=15)

            self.assertEqual(result["packaged_count"], 40)
            counts = batch_counts(root)
            self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
            for folders in base_folders(package_items(root)).values():
                self.assertEqual(len(folders), 2)

    def test_vod_clip_rotation_matches_requested_15_vod_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for vod_number in range(1, 16):
                write_source(root, f"vod_{vod_number:02d}", rotation_specs(clip_count=3))

            result = package_export_batches(root, cfg=make_rotation_cfg(), batch_size=15)

            self.assertEqual(result["allocation_strategy"], "vod_clip_variant_rotation")
            self.assertEqual(result["packaged_count"], 45)
            self.assertEqual(batch_counts(root), {"1": 15, "2": 15, "3": 15})
            items = package_items(root)
            by_vod_clip = {
                (item["normalized_source_vod"], item["base_clip_id"]): item
                for item in items
            }
            for vod_number in range(1, 16):
                for clip_number in range(1, 4):
                    item = by_vod_clip[(f"vod_{vod_number:02d}", f"clip_{clip_number:04d}")]
                    expected_variant = (vod_number - 1 + clip_number - 1) % 6
                    self.assertTrue(item["selected_variant"].startswith(f"v{expected_variant}_"))
                    self.assertEqual(item["batch_folder"], str(clip_number))
                    self.assertEqual(item["vod_index"], vod_number - 1)

            selected_paths = {Path(item["source_path"]) for item in items}
            self.assertTrue(all(not path.exists() for path in selected_paths))
            remaining = list(root.glob("vod_*/export_ready/**/*.mp4"))
            self.assertEqual(len(remaining), (15 * 3 * 6) - 45)

    def test_vod_clip_rotation_uses_second_lane_group_after_15_vods(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for vod_number in range(1, 17):
                write_source(root, f"vod_{vod_number:02d}", rotation_specs(clip_count=2))

            package_export_batches(root, cfg=make_rotation_cfg(), batch_size=15)

            self.assertEqual(batch_counts(root), {"1": 15, "2": 15, "3": 1, "4": 1})
            items = package_items(root)
            vod_16 = [item for item in items if item["normalized_source_vod"] == "vod_16"]
            self.assertEqual({item["batch_folder"] for item in vod_16}, {"3", "4"})
            self.assertTrue(all(item["vod_group"] == 1 for item in vod_16))

    def test_vod_clip_rotation_incremental_matches_bulk_layout(self):
        with tempfile.TemporaryDirectory() as bulk_tmp, tempfile.TemporaryDirectory() as incremental_tmp:
            bulk_root = Path(bulk_tmp)
            incremental_root = Path(incremental_tmp)
            for vod_number in range(1, 17):
                write_source(bulk_root, f"vod_{vod_number:02d}", rotation_specs(clip_count=3))
            package_export_batches(bulk_root, cfg=make_rotation_cfg(), batch_size=15)

            for vod_number in range(1, 17):
                write_source(incremental_root, f"vod_{vod_number:02d}", rotation_specs(clip_count=3))
                package_export_batches(incremental_root, cfg=make_rotation_cfg(), batch_size=15)

            def layout(root):
                return {
                    (item["normalized_source_vod"], item["base_clip_id"]): (
                        item["selected_variant"],
                        item["batch_folder"],
                    )
                    for item in package_items(root)
                }

            self.assertEqual(layout(bulk_root), layout(incremental_root))

    def test_vod_clip_rotation_falls_forward_to_next_export_ready_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source(
                root,
                "vod_01",
                [
                    {"clip_id": "clip_0001_v0_original", "score": 9.0, "tier": "review_needed"},
                    {"clip_id": "clip_0001_v1_first_available", "score": 8.0},
                    {"clip_id": "clip_0001_v3_later_available", "score": 7.0},
                    {"clip_id": "clip_0002_v0_only_available", "score": 9.0},
                ],
            )

            result = package_export_batches(root, cfg=make_rotation_cfg(), batch_size=15)

            self.assertEqual(result["packaged_count"], 2)
            items = {item["base_clip_id"]: item for item in package_items(root)}
            self.assertEqual(items["clip_0001"]["requested_variant"], "v0")
            self.assertEqual(items["clip_0001"]["selected_variant"], "v1_first_available")
            self.assertEqual(items["clip_0001"]["selection_reason"], "rotation_fallback")
            self.assertEqual(items["clip_0002"]["requested_variant"], "v1")
            self.assertEqual(items["clip_0002"]["selected_variant"], "v0_only_available")
            self.assertTrue((root / "vod_01" / "review_needed" / "v0").exists())

    def test_vod_clip_rotation_does_not_package_another_variant_on_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source(root, "vod_01", rotation_specs(clip_count=2))

            first = package_export_batches(root, cfg=make_rotation_cfg(), batch_size=15)
            second = package_export_batches(root, cfg=make_rotation_cfg(), batch_size=15)

            self.assertEqual(first["packaged_count"], 2)
            self.assertEqual(second["packaged_count"], 0)
            self.assertEqual(len(package_items(root)), 2)
            self.assertEqual(len(list(root.glob("vod_01/export_ready/**/*.mp4"))), 10)

    def test_vod_clip_rotation_freezes_legacy_batches_and_vods(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_legacy_batch(root, 1, 15)
            write_source(root, "legacy_vod", rotation_specs(clip_count=1))
            legacy_cfg = make_cfg()
            package_export_batches(root, cfg=legacy_cfg, batch_size=15)
            previous_highest_folder = max(int(name) for name in batch_counts(root))
            write_source(root, "new_vod", rotation_specs(clip_count=2))

            result = package_export_batches(root, cfg=make_rotation_cfg(), batch_size=15)

            self.assertEqual(result["packaged_count"], 2)
            manifest = package_manifest(root)
            self.assertEqual(
                manifest["rotation_layout"]["started_at_folder"],
                previous_highest_folder + 1,
            )
            new_items = [
                item for item in manifest["items"]
                if item.get("allocation_strategy") == "vod_clip_variant_rotation"
            ]
            self.assertEqual(
                {item["batch_folder"] for item in new_items},
                {str(previous_highest_folder + 1), str(previous_highest_folder + 2)},
            )
            self.assertIn("legacy_vod", manifest["rotation_layout"]["legacy_source_vods"])

    def test_can_package_single_vod_output_folder_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source(
                root,
                "vod_a",
                [
                    {"clip_id": "clip_0001_v0_original", "score": 9.0},
                    {"clip_id": "clip_0002_v0_original", "score": 8.0},
                ],
            )
            source_dir = root / "vod_a"

            result = package_export_batches(source_dir, cfg=make_cfg(), batch_size=15)

            self.assertEqual(result["eligible_count"], 2)
            self.assertEqual(result["packaged_count"], 2)
            self.assertEqual(batch_counts(source_dir), {"1": 2})
            self.assertFalse((source_dir / "export_ready" / "v0" / "clip_0001_v0_original_score9.mp4").exists())


def make_cfg(one_variant: bool = False):
    return SimpleNamespace(
        EXPORT_BATCH_DIR_NAME="export_batches",
        EXPORT_BATCH_SIZE=15,
        EXPORT_BATCH_STRATEGY="score_round_robin_all_variants",
        EXPORT_BATCH_VARIANT_COUNT=6,
        EXPORT_PACK_ONE_VARIANT_PER_CLIP=one_variant,
    )


def make_rotation_cfg():
    return SimpleNamespace(
        EXPORT_BATCH_DIR_NAME="export_batches",
        EXPORT_BATCH_SIZE=15,
        EXPORT_BATCH_STRATEGY="vod_clip_variant_rotation",
        EXPORT_BATCH_VARIANT_COUNT=6,
        EXPORT_PACK_ONE_VARIANT_PER_CLIP=False,
    )


def rotation_specs(clip_count: int) -> list[dict]:
    return [
        {
            "clip_id": f"clip_{clip_number:04d}_v{variant_number}_variant_{variant_number}",
            "score": 9.0 - (variant_number * 0.1),
            "version": f"v{variant_number}",
        }
        for clip_number in range(1, clip_count + 1)
        for variant_number in range(6)
    ]


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


def base_folders(items: list[dict]) -> dict[str, set[str]]:
    folders: dict[str, set[str]] = {}
    for item in items:
        folders.setdefault(item["base_clip_id"], set()).add(item["batch_folder"])
    return folders


def write_legacy_batch(root: Path, folder_number: int, count: int) -> None:
    folder = root / "export_batches" / str(folder_number)
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        (folder / f"legacy_{folder_number}_{index:03d}.mp4").write_bytes(
            f"legacy|{folder_number}|{index}".encode("utf-8")
        )


if __name__ == "__main__":
    unittest.main()
