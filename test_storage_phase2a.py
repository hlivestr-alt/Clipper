from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from clipper_app.storage.migration_phase2a import (
    SAFE_HARDLINK,
    SAFE_RAW,
    SAFE_TRANSCRIPT,
    Phase2AMigrator,
    _external_output_inventory,
    _external_vod_inventory,
)
from clipper_app.storage.models import LifecycleClass
from clipper_app.storage.transcripts import (
    resolve_effective_raw_checkpoint_path,
    resolve_effective_transcript_path,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _transcript(source: Path, text: str = "hello") -> dict:
    return {
        "segments": [{"start": 0.0, "end": 1.0, "text": text}],
        "words": [{"start": 0.0, "end": 1.0, "word": text}],
        "metadata": {
            "schema_version": 3,
            "transcriber": "faster-whisper",
            "word_alignment_backend": "whisperx",
            "desired_word_alignment_backend": "whisperx",
            "source_video_path": str(source.resolve()),
            "whisper_model_size": "large-v3-turbo",
            "whisper_language": "id",
            "whisper_beam_size": 5,
            "whisper_best_of": 5,
            "language": "id",
            "timestamp_precision": "float_seconds",
            "raw_word_timestamps_available": True,
        },
    }


def _checkpoint(source: Path, text: str = "hello") -> dict:
    payload = _transcript(source, text)
    payload["metadata"] = {
        **payload["metadata"],
        "word_alignment_backend": "raw_checkpoint",
        "checkpoint_kind": "raw_transcription",
    }
    return payload


def _add_run(project: Path, name: str, source: Path, *, text: str = "hello", raw: bool = True) -> Path:
    run = project / "working" / name
    _write_json(run / "transcript.json", _transcript(source, text))
    if raw:
        _write_json(run / "transcript.raw_checkpoint.json", _checkpoint(source, text))
    stat = source.stat()
    _write_json(run / "transcript.fingerprint.json", {
        "fingerprint": {
            "stage": "transcribe",
            "video": {"path": str(source.resolve()).casefold(), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
            "model_name": "large-v3-turbo",
            "config_hash": "historical-config",
            "extra": {},
        }
    })
    return run


def _queue(project: Path, source: Path, runs: list[tuple[Path, str, dict | None]]) -> None:
    current_path, current_status, current_stages = runs[0]
    history = []
    for run, status, stages in runs[1:]:
        history.append({
            "working_dir": str(run.relative_to(project)), "output_dir": str(project / "outputs" / run.name),
            "status": status, "current_stage": None, "stages": stages or {}, "archived_at": "2026-01-02T00:00:00Z",
        })
    video = {
        "path": str(source.resolve()), "working_dir": str(current_path.relative_to(project)),
        "output_dir": str(project / "outputs" / current_path.name), "status": current_status,
        "current_stage": None, "stages": current_stages or {}, "run_history": history,
    }
    _write_json(project / "working" / "video_queue_state.json", {
        "schema_version": 2, "queue_status": "stopped", "videos": {source.name.casefold(): video},
    })


def test_legacy_duplicates_commit_reference_then_retire_and_rerun_idempotently(tmp_path: Path) -> None:
    source = tmp_path / "vod.mp4"
    source.write_bytes(b"source-video")
    run_a = _add_run(tmp_path, "run-a", source)
    run_b = _add_run(tmp_path, "run-b", source)
    _queue(tmp_path, source, [(run_a, "completed", {}), (run_b, "completed", {})])
    migrator = Phase2AMigrator(tmp_path, "phase2a_test")
    plan = migrator.discover()
    assert plan["summary"]["classifications"][SAFE_TRANSCRIPT]["count"] == 2

    applied = migrator.apply()
    assert not (run_a / "transcript.json").exists()
    assert not (run_b / "transcript.raw_checkpoint.json").exists()
    resolved_a = resolve_effective_transcript_path(run_a)
    resolved_b = resolve_effective_transcript_path(run_b)
    assert resolved_a and resolved_b and resolved_a == resolved_b
    assert resolve_effective_raw_checkpoint_path(run_a) == resolve_effective_raw_checkpoint_path(run_b)
    action_count = len(applied["actions"])

    rerun = migrator.apply()
    assert len(rerun["actions"]) == action_count
    assert rerun["summary"]["failure_count"] == 0


def test_different_content_or_source_identity_is_not_collapsed(tmp_path: Path) -> None:
    source_a = tmp_path / "a.mp4"
    source_b = tmp_path / "b.mp4"
    source_a.write_bytes(b"source-a")
    source_b.write_bytes(b"source-b")
    run_a = _add_run(tmp_path, "run-a", source_a, text="one")
    run_b = _add_run(tmp_path, "run-b", source_a, text="two")
    run_c = _add_run(tmp_path, "run-c", source_b, text="one")
    _queue(tmp_path, source_a, [(run_a, "completed", {}), (run_b, "completed", {})])
    state = json.loads((tmp_path / "working" / "video_queue_state.json").read_text())
    state["videos"][source_b.name] = {
        "path": str(source_b), "working_dir": str(run_c.relative_to(tmp_path)),
        "output_dir": str(tmp_path / "outputs" / run_c.name), "status": "completed",
        "current_stage": None, "stages": {}, "run_history": [],
    }
    _write_json(tmp_path / "working" / "video_queue_state.json", state)
    migrator = Phase2AMigrator(tmp_path, "phase2a_identity")
    migrator.discover()
    rows = migrator.journal.candidate_rows(classification=SAFE_TRANSCRIPT)
    assert len({row["canonical_path"] for row in rows}) == 3


def test_missing_metadata_defaults_keep_and_interruption_keeps_original(tmp_path: Path) -> None:
    source = tmp_path / "vod.mp4"
    source.write_bytes(b"source")
    invalid = _add_run(tmp_path, "invalid", source)
    _write_json(invalid / "transcript.json", {"segments": []})
    valid = _add_run(tmp_path, "valid", source)
    _queue(tmp_path, source, [(invalid, "completed", {}), (valid, "completed", {})])
    migrator = Phase2AMigrator(tmp_path, "phase2a_interrupt")
    migrator.discover()
    retained = [row for row in migrator.journal.candidate_rows() if row["original_path"].endswith("invalid\\transcript.json")]
    assert retained and retained[0]["classification"] == "LEGACY_UNVERIFIED_KEEP"
    with mock.patch.object(migrator.transcripts, "import_legacy", side_effect=RuntimeError("interrupted")):
        result = migrator.apply()
    assert (valid / "transcript.json").is_file()
    assert result["summary"]["failure_count"] == 1
    assert any(action["error"] and "interrupted" in action["error"] for action in result["actions"])


def _raw_run(
    project: Path,
    source: Path,
    name: str,
    *,
    owner_status: str = "completed",
    stage_status: str = "done",
    successor: bool = True,
) -> tuple[Path, dict]:
    run = project / "working" / name
    raw = run / "raw_cuts" / "clip_0001_v0_original_raw.mp4"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"raw-media")
    output = project / "outputs" / name / "clip.mp4"
    if successor:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"final-media")
    manifest = project / "outputs" / name / "manifest.json"
    _write_json(manifest, [{
        "clip_id": "clip_0001_v0_original", "status": "ok", "compliance_passed": True,
        "compliance_blocked": False, "output_path": str(output),
    }])
    stages = {"ffmpeg": {"status": stage_status, "manifest_path": str(manifest)}}
    return raw, {
        "working_dir": str(run.relative_to(project)), "output_dir": str(output.parent),
        "status": owner_status, "current_stage": None, "stages": stages,
    }


def test_raw_cleanup_only_deletes_terminal_validated_successor(tmp_path: Path) -> None:
    source = tmp_path / "vod.mp4"
    source.write_bytes(b"source")
    safe, safe_owner = _raw_run(tmp_path, source, "safe")
    missing, missing_owner = _raw_run(tmp_path, source, "missing", successor=False)
    failed, failed_owner = _raw_run(tmp_path, source, "failed", owner_status="failed")
    retry, retry_owner = _raw_run(tmp_path, source, "retry", stage_status="failed")
    unknown, _unknown_owner = _raw_run(tmp_path, source, "unknown")
    video = {
        "path": str(source), **safe_owner,
        "run_history": [missing_owner, failed_owner, retry_owner],
    }
    _write_json(tmp_path / "working" / "video_queue_state.json", {
        "queue_status": "stopped", "videos": {"vod": video},
    })
    migrator = Phase2AMigrator(tmp_path, "phase2a_raw")
    plan = migrator.discover()
    assert plan["summary"]["classifications"][SAFE_RAW]["count"] == 1
    migrator.apply()
    assert not safe.exists()
    assert missing.exists()
    assert failed.exists()
    assert retry.exists()
    assert unknown.exists()


def test_broll_hardlink_alias_preserves_paths_and_registry(tmp_path: Path) -> None:
    intro = tmp_path / "assets" / "broll_intro" / "Serum" / "clip.mov"
    product = tmp_path / "assets" / "product_broll" / "serum" / "different-name.mov"
    intro.parent.mkdir(parents=True)
    product.parent.mkdir(parents=True)
    intro.write_bytes(b"immutable-broll")
    product.write_bytes(b"immutable-broll")
    _write_json(tmp_path / "working" / "video_queue_state.json", {"queue_status": "stopped", "videos": {}})
    migrator = Phase2AMigrator(tmp_path, "phase2a_broll")
    plan = migrator.discover()
    assert plan["summary"]["classifications"][SAFE_HARDLINK]["count"] == 1
    migrator.apply()
    assert intro.is_file() and product.is_file()
    assert os.path.samefile(intro, product)
    assert migrator.registry.all_artifacts()
    assert migrator.registry.active_references(migrator.registry.all_artifacts()[0]["artifact_id"])


def test_hardlink_failure_is_recorded_and_original_remains(tmp_path: Path) -> None:
    intro = tmp_path / "assets" / "broll_intro" / "Serum" / "clip.mov"
    product = tmp_path / "assets" / "product_broll" / "serum" / "clip.mov"
    intro.parent.mkdir(parents=True)
    product.parent.mkdir(parents=True)
    intro.write_bytes(b"same")
    product.write_bytes(b"same")
    _write_json(tmp_path / "working" / "video_queue_state.json", {"queue_status": "stopped", "videos": {}})
    migrator = Phase2AMigrator(tmp_path, "phase2a_link_failure")
    migrator.discover()
    with mock.patch("clipper_app.storage.migration_phase2a.os.link", side_effect=OSError("blocked")):
        result = migrator.apply()
    assert intro.read_bytes() == b"same" and product.read_bytes() == b"same"
    assert not os.path.samefile(intro, product)
    assert any(action["state"] == "FAILED" and "blocked" in action["error"] for action in result["actions"])


def test_pinned_duplicate_blocks_alias_migration(tmp_path: Path) -> None:
    intro = tmp_path / "assets" / "broll_intro" / "Serum" / "clip.mov"
    product = tmp_path / "assets" / "product_broll" / "serum" / "clip.mov"
    intro.parent.mkdir(parents=True)
    product.parent.mkdir(parents=True)
    intro.write_bytes(b"same")
    product.write_bytes(b"same")
    _write_json(tmp_path / "working" / "video_queue_state.json", {"queue_status": "stopped", "videos": {}})
    migrator = Phase2AMigrator(tmp_path, "phase2a_pinned")
    migrator.registry.register_artifact(
        artifact_id="final-asset", artifact_type="CLIP", canonical_path=intro,
        fingerprint="same", lifecycle_class=LifecycleClass.FINAL, pinned=True,
    )
    plan = migrator.discover()
    assert SAFE_HARDLINK not in plan["summary"]["classifications"]
    assert plan["summary"]["classifications"]["KEEP_PINNED_OR_AMBIGUOUS"]["count"] == 1
    assert not os.path.samefile(intro, product)


def test_external_inventory_is_metadata_only_and_unknown_is_protected(tmp_path: Path) -> None:
    output = tmp_path / "output"
    vod = tmp_path / "vod"
    unknown = output / "legacy" / "clip.mp4"
    review = output / "review_needed" / "clip.mp4"
    source = vod / "source.mp4"
    for path, data in ((unknown, b"unknown"), (review, b"review"), (source, b"source")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    output_result = _external_output_inventory(output)
    vod_result = _external_vod_inventory(vod)
    assert output_result["lifecycle"]["UNKNOWN"]["bytes"] == len(b"unknown")
    assert output_result["proven_safe_reclaimable_bytes"] == 0
    assert vod_result["proven_safe_reclaimable_bytes"] == 0
    assert unknown.is_file() and review.is_file() and source.is_file()
