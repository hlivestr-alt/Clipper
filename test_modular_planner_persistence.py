from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from clipper_app.modular_planner.repository import PlannerRepository


def run_values(run_id: str):
    return {
        "planner_run_id": run_id, "product": "serum", "requested_template": "standard",
        "ingredient_shortage_policy": "partial", "cta_mode": "no_cta", "requested_count": 2,
        "target_min_duration": 30, "target_max_duration": 60, "seed": "seed",
        "planner_version": "modular-planner-v1", "inventory_snapshot_hash": "inventory",
    }


def item(segment_id: str, role: str, position: int):
    return {
        "segment_id": segment_id, "scan_id": "scan", "scanner_generation": 1, "role": role,
        "source_id": f"source-{position}", "vod_filename": f"vod-{position}.mp4",
        "canonical_path": f"/vod/vod-{position}.mp4", "file_size": 10, "mtime_ns": 20,
        "content_fingerprint": "fingerprint", "start_seconds": position * 20,
        "end_seconds": position * 20 + 20, "duration_seconds": 20, "confidence": 0.9,
        "transcript_text": "transcript", "reason": "accepted",
    }


def composition(ordinal: int, signature: str):
    return {
        "ordinal": ordinal, "requested_template": "standard", "actual_template": "standard",
        "cta_mode": "no_cta", "target_min_duration": 30, "target_max_duration": 60,
        "actual_duration": 40, "distinct_source_count": 2, "selection_score": 90,
        "selection_metadata": {}, "exact_signature": signature, "near_signature": f"near-{signature}",
        "signature_version": "v1", "items": [item(f"hook-{signature}", "hook", 0), item(f"benefit-{signature}", "benefits", 1)],
    }


def test_drafts_survive_restart_and_current_run_memory_includes_removed_and_superseded():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "planner.sqlite3"
        repository = PlannerRepository(path)
        repository.create_run(run_values("run"))
        first = repository.add_composition("run", composition(1, "one"))
        repository.remove_composition("run", first, expected_revision=2)
        second = repository.add_composition("run", composition(1, "two"))
        repository.add_composition("run", composition(1, "three"), supersedes_id=second)
        reopened = PlannerRepository(path)
        detail = reopened.get_run("run")
        assert [row["status"] for row in detail["compositions"]] == ["removed", "superseded", "draft"]
        assert set(reopened.current_run_usage("run")) == {
            "hook-one", "benefit-one", "hook-two", "benefit-two", "hook-three", "benefit-three",
        }


def test_only_approved_compositions_are_persistent_history():
    with tempfile.TemporaryDirectory() as temp:
        repository = PlannerRepository(Path(temp) / "planner.sqlite3")
        repository.create_run(run_values("abandoned"))
        repository.add_composition("abandoned", composition(1, "draft"))
        assert repository.approved_usage(["hook-draft"]) == {}
        repository.create_run(run_values("approved"))
        repository.add_composition("approved", composition(1, "approved"))
        payload = {"schema_version": 1, "planner_run_id": "approved"}
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        repository.approve("approved", 2, payload, hashlib.sha256(encoded).hexdigest())
        assert repository.approved_usage(["hook-approved"])["hook-approved"]["usage_count"] == 1
        assert repository.approved_usage(["hook-draft"]) == {}


def test_draft_signature_can_repeat_across_runs_but_approved_signature_cannot():
    with tempfile.TemporaryDirectory() as temp:
        repository = PlannerRepository(Path(temp) / "planner.sqlite3")
        repository.create_run(run_values("one"))
        repository.add_composition("one", composition(1, "same"))
        repository.create_run(run_values("two"))
        repository.add_composition("two", composition(1, "same"))
        payload = {"schema_version": 1}
        checksum = hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()
        repository.approve("one", 2, payload, checksum)
        with pytest.raises(RuntimeError, match="already been approved"):
            repository.approve("two", 2, payload, checksum)


def test_v11_quality_metadata_round_trips_without_breaking_v1_drafts():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "planner.sqlite3"
        repository = PlannerRepository(path)
        repository.create_run(run_values("v1"))
        old = composition(1, "old")
        repository.add_composition("v1", old)
        assert repository.get_run("v1")["compositions"][0]["items"][0]["ranking_metadata"] == {}

        values = run_values("v11")
        values["planner_version"] = "modular-planner-v1.1"
        repository.create_run(values)
        enriched = composition(1, "enriched")
        enriched["selection_metadata"] = {"hook_benefits_continuity": 0.8}
        enriched["items"][0]["ranking_metadata"] = {
            "joinability": {"joinability_score": 1.0, "boundary_label": "Clean"},
        }
        repository.add_composition("v11", enriched)
        loaded = PlannerRepository(path).get_run("v11")["compositions"][0]
        assert loaded["selection_metadata"]["hook_benefits_continuity"] == 0.8
        assert loaded["items"][0]["ranking_metadata"]["joinability"]["boundary_label"] == "Clean"
