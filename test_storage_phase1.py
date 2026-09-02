from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import transcriber
from clipper_app.storage.inventory import DryRunReclamationPlanner, StorageInventoryService
from clipper_app.storage.models import CleanupClassification, LifecycleClass
from clipper_app.storage.raw_lifecycle import RawLifecycleManager
from clipper_app.storage.reconciliation import ModularProductionReconciler
from clipper_app.storage.registry import ArtifactRegistry
from clipper_app.storage.transcripts import (
    REFERENCE_NAME,
    TranscriptArtifactStore,
    build_transcript_descriptor,
    resolve_effective_transcript_path,
)


def cfg(working: Path, **overrides):
    values = {
        "WORKING_DIR": str(working),
        "WHISPER_MODEL_SIZE": "large-v3",
        "WHISPER_LANGUAGE": "id",
        "WHISPER_BEAM_SIZE": 5,
        "WHISPER_BEST_OF": 5,
        "WHISPER_COMPUTE": "float16",
        "WORD_ALIGNMENT_BACKEND": "whisperx",
        "WHISPERX_ALIGN_MODEL": "model-a",
        "WHISPERX_INTERPOLATE_METHOD": "nearest",
        "WHISPERX_MAX_SEGMENT_SECONDS": 30,
        "WHISPERX_ALIGN_IN_SUBPROCESS": True,
        "WHISPERX_ACCEPT_RAW_FALLBACK_CACHE": True,
        "WHISPERX_DEVICE": "cuda",
        "WORD_CORRECTIONS": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def transcript_payload() -> dict:
    return {
        "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "hello", "words": []}],
        "words": [{"word": "hello", "start": 0.0, "end": 0.5}],
        "metadata": {"schema_version": 3, "word_alignment_backend": "whisperx"},
    }


def test_same_source_and_settings_reuse_one_canonical_artifact(tmp_path: Path):
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"video-one")
    settings = cfg(tmp_path / "working")
    calls = 0

    def fake(_video, _output, _cfg):
        nonlocal calls
        calls += 1
        return transcript_payload()

    with mock.patch.object(transcriber, "_transcribe_to_directory", side_effect=fake):
        first = transcriber.transcribe(str(video), str(tmp_path / "working" / "run-a"), settings)
        second = transcriber.transcribe(str(video), str(tmp_path / "working" / "run-b"), settings)

    assert first == second
    assert calls == 1
    assert not (tmp_path / "working" / "run-a" / "transcript.json").exists()
    assert not (tmp_path / "working" / "run-b" / "transcript.json").exists()
    first_ref = json.loads((tmp_path / "working" / "run-a" / REFERENCE_NAME).read_text(encoding="utf-8"))
    second_ref = json.loads((tmp_path / "working" / "run-b" / REFERENCE_NAME).read_text(encoding="utf-8"))
    assert first_ref["artifact_id"] == second_ref["artifact_id"]


def test_source_bytes_transcription_and_alignment_change_artifact_identity(tmp_path: Path):
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"same-size-A")
    settings = cfg(tmp_path / "working")
    first = build_transcript_descriptor(video, settings, 3)
    original_stat = video.stat()
    video.write_bytes(b"same-size-B")
    os.utime(video, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    changed_bytes = build_transcript_descriptor(video, settings, 3)
    changed_transcription = build_transcript_descriptor(video, cfg(tmp_path / "working", WHISPER_BEAM_SIZE=7), 3)
    changed_alignment = build_transcript_descriptor(video, cfg(tmp_path / "working", WHISPERX_ALIGN_MODEL="model-b"), 3)
    assert len({first["artifact_id"], changed_bytes["artifact_id"], changed_transcription["artifact_id"], changed_alignment["artifact_id"]}) == 4


def test_missing_or_interrupted_canonical_artifact_never_resolves(tmp_path: Path):
    working = tmp_path / "working"
    store = TranscriptArtifactStore(working)
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"video")
    descriptor = build_transcript_descriptor(video, cfg(working), 3)
    staging = store.new_staging_dir(descriptor["artifact_id"])
    (staging / "transcript.json").write_text(json.dumps(transcript_payload()), encoding="utf-8")
    run = working / "run"
    run.mkdir(parents=True)
    (run / REFERENCE_NAME).write_text(json.dumps({
        "artifact_id": descriptor["artifact_id"], "fingerprint": descriptor["fingerprint"],
        "canonical_path": str(staging),
    }), encoding="utf-8")
    assert resolve_effective_transcript_path(run) is None


def test_concurrent_import_creates_one_valid_artifact(tmp_path: Path):
    working = tmp_path / "working"
    video = tmp_path / "vod.mp4"
    video.write_bytes(b"video")
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(transcript_payload()), encoding="utf-8")
    descriptor = build_transcript_descriptor(video, cfg(working), 3)
    store = TranscriptArtifactStore(working)
    ids: list[str] = []

    def worker():
        ids.append(store.import_legacy(legacy, descriptor).artifact_id)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert ids == [descriptor["artifact_id"]] * 4
    assert store.find(descriptor) is not None


def test_legacy_run_resolves_direct_transcript(tmp_path: Path):
    run = tmp_path / "legacy-run"
    run.mkdir()
    transcript = run / "transcript.json"
    transcript.write_text("{}", encoding="utf-8")
    assert resolve_effective_transcript_path(run) == transcript


def test_unknown_final_export_pending_and_active_reference_are_blocked(tmp_path: Path):
    root = tmp_path / "files"
    root.mkdir()
    registry = ArtifactRegistry(tmp_path / "registry.sqlite3")
    unknown = root / "unknown.bin"
    unknown.write_bytes(b"u")
    for name, lifecycle in (("final", LifecycleClass.FINAL), ("export", LifecycleClass.EXPORT), ("pending", LifecycleClass.PENDING)):
        path = root / f"{name}.bin"
        path.write_bytes(name.encode())
        registry.register_artifact(
            artifact_id=name, artifact_type="CLIP", canonical_path=path,
            fingerprint=name, lifecycle_class=lifecycle,
        )
    cache = root / "cache.bin"
    cache.write_bytes(b"cache")
    registry.register_artifact(
        artifact_id="cache", artifact_type="CACHE", canonical_path=cache,
        fingerprint="cache", lifecycle_class=LifecycleClass.CACHE,
    )
    registry.add_reference("cache", owner_type="job", owner_id="active", role="retry")
    snapshot = StorageInventoryService(registry).scan({"test": root})
    by_name = {Path(row.path).name: row for row in snapshot.records}
    assert by_name["unknown.bin"].cleanup_eligibility == CleanupClassification.BLOCKED_UNKNOWN_OWNER
    assert by_name["final.bin"].cleanup_eligibility == CleanupClassification.BLOCKED_FINAL
    assert by_name["export.bin"].cleanup_eligibility == CleanupClassification.BLOCKED_EXPORT
    assert by_name["pending.bin"].cleanup_eligibility == CleanupClassification.BLOCKED_PENDING
    assert by_name["cache.bin"].cleanup_eligibility == CleanupClassification.BLOCKED_ACTIVE_REFERENCE


def test_regenerable_requires_existing_dependency_and_dry_run_never_deletes(tmp_path: Path):
    root = tmp_path / "files"
    root.mkdir()
    source = root / "source.mp4"
    source.write_bytes(b"source")
    derived = root / "derived.bin"
    derived.write_bytes(b"derived")
    registry = ArtifactRegistry(tmp_path / "registry.sqlite3")
    registry.register_artifact(
        artifact_id="derived", artifact_type="DERIVED", canonical_path=derived,
        fingerprint="derived", lifecycle_class=LifecycleClass.REGENERABLE,
        regenerable=True, regeneration_evidence={"source": str(source)},
    )
    snapshot = StorageInventoryService(registry).scan({"test": root})
    plan = DryRunReclamationPlanner(registry).plan(snapshot)
    item = next(row for row in plan["items"] if row["artifact_id"] == "derived")
    assert item["cleanup_eligibility"] == CleanupClassification.SAFE_CANDIDATE
    assert item["reason"] and item["references_checked"] == 0
    assert plan["dry_run"] is True and plan["historical_deletion_performed"] is False
    assert derived.exists()
    with pytest.raises(PermissionError):
        DryRunReclamationPlanner(registry).execute(plan)


def test_raw_cleanup_requires_validated_successor_and_committed_manifest(tmp_path: Path):
    manager = RawLifecycleManager(ArtifactRegistry(tmp_path / "registry.sqlite3"))
    raw = tmp_path / "raw.mp4"
    final = tmp_path / "final.mp4"
    manifest = tmp_path / "manifest.json"
    raw.write_bytes(b"raw")
    final.write_bytes(b"final")
    manifest.write_text(json.dumps([{"clip_id": "clip-1", "status": "ok"}]), encoding="utf-8")
    manager.register_new(raw, owner_job="clip-1")
    assert manager.cleanup_after_manifest_commit(
        raw, successor_path=final, manifest_path=manifest, clip_id="clip-1",
        validation={"compliant": True},
    )
    assert not raw.exists()


@pytest.mark.parametrize("mode", ["failed", "interrupted", "missing_validation"])
def test_raw_failure_interruption_or_missing_validation_retains_retry_data(tmp_path: Path, mode: str):
    manager = RawLifecycleManager(ArtifactRegistry(tmp_path / f"{mode}.sqlite3"))
    raw = tmp_path / f"{mode}.raw.mp4"
    final = tmp_path / f"{mode}.final.mp4"
    manifest = tmp_path / f"{mode}.manifest.json"
    raw.write_bytes(b"raw")
    final.write_bytes(b"final")
    manifest.write_text(json.dumps([{"clip_id": mode, "status": "ok"}]), encoding="utf-8")
    manager.register_new(raw, owner_job=mode)
    if mode == "failed":
        manager.mark_failed(raw)
    elif mode == "interrupted":
        manager.mark_failed(raw, interrupted=True)
    else:
        assert not manager.cleanup_after_manifest_commit(
            raw, successor_path=final, manifest_path=manifest, clip_id=mode, validation=None,
        )
    assert raw.exists()


def test_reconciliation_does_not_use_ambiguous_or_basename_only_match(tmp_path: Path):
    database = tmp_path / "production.sqlite3"
    output = tmp_path / "output"
    output.mkdir()
    for folder in (output / "a", output / "b"):
        folder.mkdir()
        (folder / "same.mp4").write_bytes(b"x")
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE modular_production_jobs(job_id TEXT PRIMARY KEY, output_directory TEXT NOT NULL);
            CREATE TABLE modular_production_variants(
                job_id TEXT, composition_id TEXT, variant_index INTEGER, media_id TEXT,
                output_path TEXT, status TEXT, variant_id TEXT, variant_name TEXT,
                duration REAL, file_size INTEGER, lineage_json TEXT, created_at TEXT
            );
        """)
        db.execute("INSERT INTO modular_production_jobs VALUES('job',?)", (str(output),))
        db.execute(
            "INSERT INTO modular_production_variants VALUES('job','c',0,'media-x',?,'completed','v','v',0,1,'{}','now')",
            (str(output / "missing" / "same.mp4"),),
        )
    (output / "manifest.json").write_text(json.dumps([]), encoding="utf-8")
    summary = ModularProductionReconciler(database, ArtifactRegistry(tmp_path / "registry.sqlite3")).reconcile(apply=True)
    assert summary["updated_rows"] == 0
    assert summary["classifications"] == {"MISSING": 1}
