from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .models import LifecycleClass
from .registry import ArtifactRegistry, stable_id


def managed_working_dir(cfg: Any, scoped_path: str | Path) -> Path:
    """Resolve registry ownership without leaking test operations into production state."""
    configured = getattr(cfg, "WORKING_DIR", None) if cfg is not None else None
    if configured:
        return Path(str(configured))
    resolved = Path(scoped_path).resolve(strict=False)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    try:
        relative = resolved.relative_to(temp_root)
    except ValueError:
        return Path("working")
    fixture_root = temp_root / relative.parts[0] if relative.parts else temp_root
    return fixture_root / ".clipper_test_working"


def quick_content_identity(path: str | Path, *, sample_bytes: int = 1024 * 1024) -> str:
    source = Path(path)
    stat = source.stat()
    digest = hashlib.sha256()
    digest.update(stat.st_size.to_bytes(8, "big"))
    with source.open("rb") as handle:
        digest.update(handle.read(sample_bytes))
        if stat.st_size > sample_bytes:
            handle.seek(max(0, stat.st_size - sample_bytes))
            digest.update(handle.read(sample_bytes))
    return f"sha256-size-edges-v1:{digest.hexdigest()}"


class ManagedPublisher:
    """Crash-evidenced file publication with registry and domain reconciliation hooks."""

    def __init__(self, registry: ArtifactRegistry):
        self.registry = registry

    @classmethod
    def from_working_dir(cls, working_dir: str | Path) -> "ManagedPublisher":
        return cls(ArtifactRegistry.from_working_dir(working_dir))

    def move(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        artifact_id: str | None = None,
        artifact_type: str = "CLIP",
        lifecycle_class: LifecycleClass = LifecycleClass.PENDING,
        owner_identity: str | None = None,
        reconcile: Callable[[Path, Path, str], None] | None = None,
        evidence: dict[str, Any] | None = None,
        replace: bool = False,
        move_impl: Callable[[str, str], Any] | None = None,
    ) -> dict[str, Any]:
        source_path = Path(source).resolve(strict=True)
        destination_path = Path(destination).resolve(strict=False)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        size = source_path.stat().st_size
        identity = quick_content_identity(source_path)
        tracked_source = self.registry.artifact_for_path(source_path)
        artifact_id = artifact_id or (
            str(tracked_source["artifact_id"]) if tracked_source else stable_id("clip", [identity, owner_identity or str(source_path)])
        )
        operation_id = self.registry.begin_publish(
            source_path, destination_path, artifact_id=artifact_id,
            lifecycle_class=lifecycle_class, owner_identity=owner_identity,
            size_bytes=size, content_identity=identity, evidence=evidence,
        )
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.exists() and not replace:
            self.registry.update_publish(operation_id, "FAILED", error="destination_exists")
            raise FileExistsError(destination_path)

        try:
            same_volume = source_path.drive.casefold() == destination_path.drive.casefold()
            if same_volume:
                if move_impl is not None:
                    if replace and destination_path.exists():
                        destination_path.unlink()
                    move_impl(str(source_path), str(destination_path))
                elif replace:
                    os.replace(source_path, destination_path)
                else:
                    source_path.rename(destination_path)
            else:
                staged = destination_path.with_name(f".{destination_path.name}.{uuid4().hex}.partial")
                shutil.copy2(source_path, staged)
                if staged.stat().st_size != size or quick_content_identity(staged) != identity:
                    staged.unlink(missing_ok=True)
                    raise IOError("published copy failed content verification")
                os.replace(staged, destination_path)
            self.registry.update_publish(operation_id, "MOVED")
            if destination_path.stat().st_size != size or quick_content_identity(destination_path) != identity:
                raise IOError("destination failed post-move verification")
            if reconcile is not None:
                reconcile(source_path, destination_path, operation_id)
            self.registry.register_artifact(
                artifact_id=artifact_id, artifact_type=artifact_type,
                canonical_path=destination_path, size_bytes=size, content_identity=identity,
                fingerprint=identity, owner_identity=owner_identity,
                lifecycle_class=lifecycle_class,
                regenerable=False if lifecycle_class in {LifecycleClass.FINAL, LifecycleClass.EXPORT} else None,
                regeneration_evidence=evidence or {},
            )
            if not same_volume:
                source_path.unlink()
            self.registry.update_publish(operation_id, "COMMITTED")
            return {
                "operation_id": operation_id, "artifact_id": artifact_id,
                "source_path": str(source_path), "destination_path": str(destination_path),
                "content_identity": identity, "size_bytes": size,
            }
        except Exception as exc:
            self.registry.update_publish(operation_id, "FAILED", error=f"{type(exc).__name__}: {exc}")
            raise

    def record_completed_transition(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        lifecycle_class: LifecycleClass,
        owner_identity: str | None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an already crash-safe batch rename performed by a domain service."""
        source_path = Path(source).resolve(strict=False)
        destination_path = Path(destination).resolve(strict=True)
        size = destination_path.stat().st_size
        identity = quick_content_identity(destination_path)
        artifact_id = stable_id("clip", [identity, owner_identity or str(destination_path)])
        operation_id = self.registry.begin_publish(
            source_path, destination_path, artifact_id=artifact_id,
            lifecycle_class=lifecycle_class, owner_identity=owner_identity,
            size_bytes=size, content_identity=identity, evidence=evidence,
        )
        self.registry.update_publish(operation_id, "MOVED")
        self.registry.register_artifact(
            artifact_id=artifact_id, artifact_type="CLIP", canonical_path=destination_path,
            size_bytes=size, content_identity=identity, fingerprint=identity,
            owner_identity=owner_identity, lifecycle_class=lifecycle_class,
            regenerable=False if lifecycle_class == LifecycleClass.EXPORT else None,
            regeneration_evidence=evidence or {},
        )
        self.registry.update_publish(operation_id, "COMMITTED")
        return {"operation_id": operation_id, "artifact_id": artifact_id}
