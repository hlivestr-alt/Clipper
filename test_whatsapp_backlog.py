from __future__ import annotations

from pathlib import Path
import hashlib

import pytest

import whatsapp_backlog as backlog_module
from whatsapp_backlog import BacklogCoordinator, BatchSource, load_source_ledger
from whatsapp_media import DeliveryComplianceResult, MediaPolicy, ProcessingAction, source_identity


def _coordinator(tmp_path: Path, *, policy: MediaPolicy | None = None) -> BacklogCoordinator:
    source = tmp_path / "source"
    batch = source / "10"
    batch.mkdir(parents=True, exist_ok=True)
    destination = tmp_path / "mirror"
    return BacklogCoordinator(
        source,
        destination,
        [BatchSource(10, batch)],
        policy=policy or MediaPolicy(),
        relevant_options={"workers": 1},
    )


def test_source_ledger_preserves_numeric_gaps_and_reports_invalid_folders(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "10").mkdir(parents=True)
    (source / "15").mkdir()
    (source / "affiliate-a").mkdir()
    batches, invalid = load_source_ledger(source)
    assert [item.batch_number for item in batches] == [10, 15]
    assert invalid == ["affiliate-a"]


def test_resume_finds_exact_compatible_unfinished_run(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    run_id = coordinator.create_run()
    coordinator.run_id = run_id
    coordinator._take_run_ownership()
    coordinator._finish_run("resumable", "test interruption")
    assert coordinator.find_resume_run() == run_id
    assert coordinator.find_resume_run(run_id) == run_id


def test_new_run_refuses_unfinished_destination(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    coordinator.create_run()
    with pytest.raises(RuntimeError, match="Unfinished destination runs"):
        coordinator.create_run()


def test_resume_rejects_policy_revision_mismatch(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    run_id = coordinator.create_run()
    coordinator.run_id = run_id
    coordinator._take_run_ownership()
    coordinator._finish_run("resumable", "test interruption")
    changed = _coordinator(
        tmp_path,
        policy=MediaPolicy(revision="whatsapp-media-v2"),
    )
    with pytest.raises(RuntimeError, match="No compatible resumable run"):
        changed.find_resume_run()


def test_policy_migration_updates_run_in_place_and_preserves_staging(tmp_path: Path) -> None:
    legacy = _coordinator(
        tmp_path,
        policy=MediaPolicy(revision="whatsapp-media-v3-clipper-stale-nclx"),
    )
    run_id = legacy.create_run()
    legacy.run_id = run_id
    legacy._take_run_ownership()
    legacy._finish_run("resumable", "test interruption")

    current = _coordinator(tmp_path)
    migration = current.migrate_run_policy(run_id)
    assert migration["migrated"] is True
    assert migration["old_policy_revision"] == "whatsapp-media-v3-clipper-stale-nclx"
    assert migration["policy_revision"] == current.policy.revision
    assert current.find_resume_run(run_id) == run_id


def test_policy_migration_refuses_running_run(tmp_path: Path) -> None:
    legacy = _coordinator(
        tmp_path,
        policy=MediaPolicy(revision="whatsapp-media-v3-clipper-stale-nclx"),
    )
    run_id = legacy.create_run()
    legacy.run_id = run_id
    legacy._take_run_ownership()
    current = _coordinator(tmp_path)
    with pytest.raises(RuntimeError, match="stopped resumable"):
        current.migrate_run_policy(run_id)


def test_policy_resume_adopts_valid_staged_output_without_reencoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = _coordinator(tmp_path)
    source = tmp_path / "source" / "10" / "clip.mp4"
    source.write_bytes(b"source")
    run_id = coordinator.create_run()
    coordinator.run_id = run_id
    destination = tmp_path / "mirror" / "_tmp" / run_id / "10" / "clip.mp4"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"already-validated-output")
    identity = source_identity(source)
    coordinator._record_file(
        10,
        Path("clip.mp4"),
        identity,
        {"path": str(source)},
        "completed",
        "transcode",
        ProcessingAction.BACKLOG_TRANSCODED.value,
        None,
    )
    before = destination.read_bytes()
    calls: list[Path] = []

    def valid(path: Path, **_kwargs):
        calls.append(Path(path))
        return DeliveryComplianceResult(True, coordinator.policy.revision, "backlog_transcoded")

    monkeypatch.setattr(backlog_module, "validate_delivery", valid)
    action = coordinator._process_file(
        10, source, Path("clip.mp4"), destination
    )
    assert action == ProcessingAction.BACKLOG_TRANSCODED.value
    assert destination.read_bytes() == before
    assert calls == [destination]


def test_policy_resume_discards_invalid_staged_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(tmp_path)
    source = tmp_path / "source" / "10" / "clip.mp4"
    source.write_bytes(b"not-a-video")
    run_id = coordinator.create_run()
    coordinator.run_id = run_id
    destination = tmp_path / "mirror" / "_tmp" / run_id / "10" / "clip.mp4"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"invalid-staged-output")
    identity = source_identity(source)
    coordinator._record_file(
        10,
        Path("clip.mp4"),
        identity,
        {"path": str(source)},
        "completed",
        "transcode",
        ProcessingAction.BACKLOG_TRANSCODED.value,
        None,
    )
    monkeypatch.setattr(
        backlog_module,
        "validate_delivery",
        lambda *_args, **_kwargs: DeliveryComplianceResult(
            False,
            coordinator.policy.revision,
            ProcessingAction.BACKLOG_TRANSCODED.value,
            failure_codes=["invalid_staged_output"],
        ),
    )
    with pytest.raises(RuntimeError):
        coordinator._process_file(10, source, Path("clip.mp4"), destination)
    assert not destination.exists()


def test_published_batch_path_is_not_rewritten_during_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source" / "10"
    source.mkdir(parents=True)
    (source / "clip.mp4").write_bytes(b"source")
    destination = tmp_path / "mirror" / "10"
    destination.mkdir(parents=True)
    published = destination / "clip.mp4"
    published.write_bytes(b"immutable-published")
    before = hashlib.sha256(published.read_bytes()).hexdigest()
    coordinator = BacklogCoordinator(
        tmp_path / "source",
        tmp_path / "mirror",
        [BatchSource(10, source)],
        policy=MediaPolicy(),
        adopt_existing=True,
    )
    coordinator.run_id = coordinator.create_run()
    monkeypatch.setattr(coordinator, "_adopt_or_verify_existing", lambda *_args: None)
    result = coordinator._process_batch(BatchSource(10, source))
    assert result["counts"]["complete_batches"] == 1
    assert hashlib.sha256(published.read_bytes()).hexdigest() == before


def test_source_and_destination_cannot_overlap(tmp_path: Path) -> None:
    source = tmp_path / "source"
    batch = source / "10"
    batch.mkdir(parents=True)
    coordinator = BacklogCoordinator(
        source,
        source / "mirror",
        [BatchSource(10, batch)],
        policy=MediaPolicy(),
    )
    with pytest.raises(ValueError, match="must not overlap"):
        coordinator.create_run()
