from __future__ import annotations

import ntpath
from pathlib import Path
from typing import Literal


class UnsafePathError(ValueError):
    """Raised when an untrusted path escapes its declared filesystem root."""


PathKind = Literal["file", "dir"]


def resolve_within_root(
    root: str | Path,
    value: str | Path,
    *,
    base: str | Path | None = None,
    must_exist: bool = False,
    kind: PathKind | None = None,
) -> Path:
    """Resolve ``value`` without side effects and require it to remain under ``root``.

    Relative values are interpreted from ``base`` (or ``root``). Existing
    symlinks and Windows junctions are resolved by :meth:`Path.resolve`, while
    missing leaf paths are still checked through their resolved parent chain.
    Absolute paths are accepted only when their canonical target is contained
    by the declared root.
    """

    _reject_unsafe_text(root, allow_drive=True)
    _reject_unsafe_text(value, allow_drive=True)
    if base is not None:
        _reject_unsafe_text(base, allow_drive=True)

    canonical_root = Path(root).expanduser().resolve(strict=False)
    canonical_base = canonical_root
    if base is not None:
        raw_base = Path(base).expanduser()
        if not raw_base.is_absolute():
            raw_base = canonical_root / raw_base
        canonical_base = raw_base.resolve(strict=False)
        _require_contained(canonical_base, canonical_root, label="base")

    raw_value = Path(value).expanduser()
    candidate = raw_value if raw_value.is_absolute() else canonical_base / raw_value
    canonical = candidate.resolve(strict=False)
    _require_contained(canonical, canonical_root, label="path")

    if must_exist and not canonical.exists():
        raise UnsafePathError(f"path does not exist: {canonical}")
    if kind == "file" and (not canonical.exists() or not canonical.is_file()):
        raise UnsafePathError(f"path is not a file: {canonical}")
    if kind == "dir" and (not canonical.exists() or not canonical.is_dir()):
        raise UnsafePathError(f"path is not a directory: {canonical}")
    return canonical


def _reject_unsafe_text(value: str | Path, *, allow_drive: bool) -> None:
    text = str(value)
    if "\x00" in text:
        raise UnsafePathError("path contains a NUL byte")

    # ntpath recognizes drive prefixes even when tests run on a non-Windows
    # host. A colon anywhere after a normal drive prefix is an NTFS alternate
    # data stream and must never be accepted from persisted data.
    drive, tail = ntpath.splitdrive(text)
    if drive and not allow_drive:
        raise UnsafePathError("drive-qualified paths are not allowed")
    if ":" in tail:
        raise UnsafePathError("Windows alternate data streams are not allowed")
    if not drive and ":" in text:
        raise UnsafePathError("Windows alternate data streams are not allowed")


def _require_contained(candidate: Path, root: Path, *, label: str) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(f"{label} escapes declared root: {candidate}") from exc
