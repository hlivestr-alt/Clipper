from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .models import LifecycleClass
from .registry import ArtifactRegistry, canonical_json


REFERENCE_NAME = "transcript.artifact.json"
MANIFEST_NAME = "artifact.json"
TRANSCRIPT_NAME = "transcript.json"
RAW_CHECKPOINT_NAME = "transcript.raw_checkpoint.json"
ARTIFACT_SCHEMA_VERSION = 1
FULL_HASH_LIMIT = 64 * 1024 * 1024
SAMPLE_BYTES = 1024 * 1024


TRANSCRIPTION_KEYS = (
    "WHISPER_MODEL_SIZE", "WHISPER_LANGUAGE", "WHISPER_BEAM_SIZE", "WHISPER_BEST_OF",
    "WHISPER_COMPUTE", "WORD_CORRECTIONS",
)
ALIGNMENT_KEYS = (
    "WORD_ALIGNMENT_BACKEND", "WHISPERX_ALIGN_MODEL", "WHISPERX_INTERPOLATE_METHOD",
    "WHISPERX_MAX_SEGMENT_SECONDS", "WHISPERX_ALIGN_IN_SUBPROCESS",
    "WHISPERX_ACCEPT_RAW_FALLBACK_CACHE", "WHISPERX_DEVICE",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda row: str(row[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_byte_identity(path: str | Path) -> dict[str, Any]:
    """Return a strong identity without routinely hashing multi-GiB VODs in full."""
    source = Path(path).resolve()
    stat = source.stat()
    digest = hashlib.sha256()
    if stat.st_size <= FULL_HASH_LIMIT:
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(SAMPLE_BYTES), b""):
                digest.update(chunk)
        method = "sha256-full-v1"
    else:
        offsets = sorted({0, max(0, stat.st_size // 2 - SAMPLE_BYTES // 2), max(0, stat.st_size - SAMPLE_BYTES)})
        with source.open("rb") as handle:
            for offset in offsets:
                handle.seek(offset)
                chunk = handle.read(SAMPLE_BYTES)
                digest.update(str(offset).encode("ascii"))
                digest.update(len(chunk).to_bytes(8, "big"))
                digest.update(chunk)
        method = "sha256-sampled-3x1m-v1"
    return {
        "algorithm": method,
        "digest": digest.hexdigest(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def build_transcript_descriptor(video_path: str | Path, cfg: Any, schema_version: int) -> dict[str, Any]:
    source = Path(video_path).resolve()
    transcription = {
        "implementation": "clipper-faster-whisper-v2",
        "faster_whisper_version": _package_version("faster-whisper"),
        "settings": {key: _jsonable(getattr(cfg, key, None)) for key in TRANSCRIPTION_KEYS},
        "vad_filter": True,
        "vad_min_silence_duration_ms": 800,
        "word_timestamps": True,
    }
    alignment = {
        "implementation": "clipper-whisperx-alignment-v2",
        "whisperx_version": _package_version("whisperx"),
        "settings": {key: _jsonable(getattr(cfg, key, None)) for key in ALIGNMENT_KEYS},
    }
    source_identity = source_byte_identity(source)
    identity = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "transcript_schema_version": int(schema_version),
        "source_byte_identity": source_identity,
        "transcription": transcription,
        "alignment": alignment,
    }
    fingerprint = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return {
        **identity,
        "artifact_id": f"transcript_{fingerprint}",
        "fingerprint": fingerprint,
        "source_path": str(source),
    }


@dataclass(frozen=True)
class TranscriptArtifact:
    artifact_id: str
    root: Path
    transcript_path: Path
    raw_checkpoint_path: Path | None
    manifest: dict[str, Any]


class TranscriptArtifactStore:
    def __init__(self, working_root: str | Path, registry: ArtifactRegistry | None = None):
        self.working_root = Path(working_root).resolve(strict=False)
        self.root = self.working_root / "artifacts" / "transcripts"
        self.staging_root = self.root / ".staging"
        self.lock_root = self.root / ".locks"
        self.registry = registry or ArtifactRegistry.from_working_dir(self.working_root)

    @classmethod
    def from_config(cls, cfg: Any) -> "TranscriptArtifactStore":
        return cls(getattr(cfg, "WORKING_DIR", "working"))

    def artifact_root(self, artifact_id: str) -> Path:
        return self.root / artifact_id

    def find(self, descriptor: dict[str, Any]) -> TranscriptArtifact | None:
        root = self.artifact_root(str(descriptor["artifact_id"]))
        return self._load_valid(root, expected_fingerprint=str(descriptor["fingerprint"]))

    def _load_valid(self, root: Path, *, expected_fingerprint: str | None = None) -> TranscriptArtifact | None:
        return _load_valid_artifact(root, expected_fingerprint=expected_fingerprint)

    def attach(self, run_dir: str | Path, artifact: TranscriptArtifact, descriptor: dict[str, Any]) -> Path:
        target_dir = Path(run_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        reference = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "artifact_id": artifact.artifact_id,
            "artifact_type": "TRANSCRIPT",
            "identity_kind": descriptor.get("identity_kind", "phase1-processing-fingerprint-v1"),
            "fingerprint": descriptor["fingerprint"],
            "canonical_path": str(artifact.root),
            "transcript_path": str(artifact.transcript_path),
            "raw_checkpoint_path": str(artifact.raw_checkpoint_path) if artifact.raw_checkpoint_path else None,
            "source_byte_identity": descriptor["source_byte_identity"],
            "attached_at": _utc_now(),
        }
        target = target_dir / REFERENCE_NAME
        _write_json_atomic(target, reference)
        self.registry.add_reference(
            artifact.artifact_id,
            owner_type="working_run",
            owner_id=str(target_dir.resolve(strict=False)),
            role="transcript",
            metadata={"reference_path": str(target)},
        )
        return target

    @contextmanager
    def lock(self, artifact_id: str, *, timeout: float = 600.0) -> Iterator[None]:
        self.lock_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.lock_root / f"{artifact_id}.lock"
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, canonical_json({"pid": os.getpid(), "created_at": _utc_now()}).encode("utf-8"))
                os.close(fd)
                break
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                    if age > max(timeout, 3600.0):
                        lock_path.unlink()
                        continue
                except OSError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for transcript artifact {artifact_id}")
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass

    def new_staging_dir(self, artifact_id: str) -> Path:
        self.staging_root.mkdir(parents=True, exist_ok=True)
        path = self.staging_root / f"{artifact_id}.{os.getpid()}.{uuid4().hex}.partial"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def commit(self, staging: Path, descriptor: dict[str, Any]) -> TranscriptArtifact:
        transcript_path = staging / TRANSCRIPT_NAME
        if not transcript_path.is_file():
            raise ValueError("Cannot commit transcript artifact without transcript.json")
        files: dict[str, Any] = {}
        for name in (TRANSCRIPT_NAME, RAW_CHECKPOINT_NAME):
            path = staging / name
            if path.is_file():
                files[name] = {"size": path.stat().st_size, "sha256": file_sha256(path)}
        manifest = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "artifact_id": descriptor["artifact_id"],
            "artifact_type": "TRANSCRIPT",
            "fingerprint": descriptor["fingerprint"],
            "state": "COMMITTED",
            "created_at": _utc_now(),
            "source_path": descriptor["source_path"],
            "source_byte_identity": descriptor["source_byte_identity"],
            "transcription": descriptor["transcription"],
            "alignment": descriptor["alignment"],
            "transcript_schema_version": descriptor["transcript_schema_version"],
            "identity_kind": descriptor.get("identity_kind", "phase1-processing-fingerprint-v1"),
            "historical_evidence": descriptor.get("historical_evidence"),
            "files": files,
        }
        _write_json_atomic(staging / MANIFEST_NAME, manifest)
        final = self.artifact_root(str(descriptor["artifact_id"]))
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staging, final)
        except FileExistsError:
            existing = self._load_valid(final, expected_fingerprint=str(descriptor["fingerprint"]))
            if existing is None:
                raise
            shutil.rmtree(staging, ignore_errors=True)
            return existing
        artifact = self._load_valid(final, expected_fingerprint=str(descriptor["fingerprint"]))
        if artifact is None:
            raise RuntimeError("Committed transcript artifact failed validation")
        total_size = sum(int(row["size"]) for row in files.values())
        regenerable = bool(descriptor.get("regenerable", True))
        lifecycle = LifecycleClass(str(descriptor.get(
            "lifecycle_class",
            LifecycleClass.REGENERABLE if regenerable else LifecycleClass.PERMANENT_STATE,
        )))
        self.registry.register_artifact(
            artifact_id=artifact.artifact_id,
            artifact_type="TRANSCRIPT",
            canonical_path=artifact.transcript_path,
            size_bytes=total_size,
            content_identity=files[TRANSCRIPT_NAME]["sha256"],
            fingerprint=str(descriptor["fingerprint"]),
            owner_identity=canonical_json(descriptor["source_byte_identity"]),
            lifecycle_class=lifecycle,
            regenerable=regenerable,
            pinned=not regenerable,
            pin_reason="legacy_transcript_not_reproducible" if not regenerable else None,
            regeneration_evidence={
                "source": descriptor["source_path"],
                "source_byte_identity": descriptor["source_byte_identity"],
                "transcription": descriptor["transcription"],
                "alignment": descriptor["alignment"],
            },
        )
        return artifact

    def import_legacy(
        self,
        legacy_transcript: str | Path,
        descriptor: dict[str, Any],
        *,
        legacy_raw_checkpoint: str | Path | None = None,
    ) -> TranscriptArtifact:
        with self.lock(str(descriptor["artifact_id"])):
            existing = self.find(descriptor)
            if existing:
                return existing
            staging = self.new_staging_dir(str(descriptor["artifact_id"]))
            try:
                shutil.copy2(Path(legacy_transcript), staging / TRANSCRIPT_NAME)
                if legacy_raw_checkpoint and Path(legacy_raw_checkpoint).is_file():
                    shutil.copy2(Path(legacy_raw_checkpoint), staging / RAW_CHECKPOINT_NAME)
                return self.commit(staging, descriptor)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _reference_artifact(run_dir: Path) -> TranscriptArtifact | None:
    reference_path = run_dir / REFERENCE_NAME
    try:
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        root = Path(str(reference["canonical_path"]))
        artifact = _load_valid_artifact(root, expected_fingerprint=str(reference.get("fingerprint") or "") or None)
        if artifact and artifact.artifact_id == reference.get("artifact_id"):
            return artifact
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return None


def _load_valid_artifact(root: Path, *, expected_fingerprint: str | None = None) -> TranscriptArtifact | None:
    manifest_path = root / MANIFEST_NAME
    transcript_path = root / TRANSCRIPT_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("state") != "COMMITTED":
            return None
        if expected_fingerprint and manifest.get("fingerprint") != expected_fingerprint:
            return None
        transcript_meta = manifest.get("files", {}).get(TRANSCRIPT_NAME, {})
        if not transcript_path.is_file():
            return None
        if transcript_path.stat().st_size != int(transcript_meta.get("size") or -1):
            return None
        if file_sha256(transcript_path) != transcript_meta.get("sha256"):
            return None
        raw_path = root / RAW_CHECKPOINT_NAME
        raw_meta = manifest.get("files", {}).get(RAW_CHECKPOINT_NAME)
        if raw_meta:
            if not raw_path.is_file() or raw_path.stat().st_size != int(raw_meta.get("size") or -1):
                return None
            if file_sha256(raw_path) != raw_meta.get("sha256"):
                return None
        else:
            raw_path = None
        return TranscriptArtifact(str(manifest["artifact_id"]), root, transcript_path, raw_path, manifest)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def resolve_effective_transcript_path(run_dir: str | Path) -> Path | None:
    root = Path(run_dir)
    legacy = root / TRANSCRIPT_NAME
    if legacy.is_file():
        return legacy
    artifact = _reference_artifact(root)
    return artifact.transcript_path if artifact else None


def resolve_effective_raw_checkpoint_path(run_dir: str | Path) -> Path | None:
    root = Path(run_dir)
    legacy = root / RAW_CHECKPOINT_NAME
    if legacy.is_file():
        return legacy
    artifact = _reference_artifact(root)
    return artifact.raw_checkpoint_path if artifact else None


def reference_metadata(run_dir: str | Path) -> dict[str, Any] | None:
    try:
        return json.loads((Path(run_dir) / REFERENCE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
