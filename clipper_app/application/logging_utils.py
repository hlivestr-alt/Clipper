from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import portalocker


PIPELINE_LOG_MAX_BYTES = 25 * 1024 * 1024
PIPELINE_LOG_BACKUP_COUNT = 4
SUPERVISOR_LOG_MAX_BYTES = 10 * 1024 * 1024
SUPERVISOR_LOG_BACKUP_COUNT = 3
AUDIT_LOG_MAX_BYTES = 10 * 1024 * 1024
AUDIT_LOG_BACKUP_COUNT = 5


def _backup_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


@contextmanager
def _rotation_lock(path: Path) -> Iterator[None]:
    """Serialize appends and rotations across threads and processes."""
    lock_path = path.with_name(f"{path.name}.rotate.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_handle:
        portalocker.lock(lock_handle, portalocker.LOCK_EX)
        try:
            yield
        finally:
            portalocker.unlock(lock_handle)


def _cap_file_to_recent_lines(path: Path, max_bytes: int) -> None:
    """Replace an oversized legacy file with its newest complete-line suffix."""
    size = path.stat().st_size
    if size <= max_bytes:
        return

    with path.open("rb") as source:
        source.seek(size - max_bytes)
        suffix = source.read(max_bytes)

    # The read starts at an arbitrary byte. Drop that partial line so a backup
    # never begins with a fragment (and never exceeds the configured limit).
    first_newline = suffix.find(b"\n")
    suffix = suffix[first_newline + 1 :] if first_newline >= 0 else b""

    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp_path.open("wb") as target:
            target.write(suffix)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _rotate_current_locked(path: Path, backup_count: int) -> None:
    if not path.exists():
        return
    if backup_count <= 0:
        path.unlink(missing_ok=True)
        return

    for index in range(backup_count, 1, -1):
        source = _backup_path(path, index - 1)
        destination = _backup_path(path, index)
        destination.unlink(missing_ok=True)
        if source.exists():
            os.replace(source, destination)

    first_backup = _backup_path(path, 1)
    first_backup.unlink(missing_ok=True)
    os.replace(path, first_backup)


def _prepare_append_locked(
    path: Path,
    incoming_bytes: int,
    *,
    max_bytes: int,
    backup_count: int,
) -> None:
    if not path.exists():
        return

    current_size = path.stat().st_size
    if current_size > max_bytes:
        _cap_file_to_recent_lines(path, max_bytes)
        _rotate_current_locked(path, backup_count)
    elif current_size > 0 and current_size + incoming_bytes > max_bytes:
        _rotate_current_locked(path, backup_count)


def append_rotating_text(
    path: str | Path,
    text: str,
    *,
    max_bytes: int,
    backup_count: int,
    encoding: str = "utf-8",
    errors: str = "backslashreplace",
) -> None:
    """Append one text record without retaining an open Windows file handle."""
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode(encoding, errors=errors)
    with _rotation_lock(target):
        _prepare_append_locked(
            target,
            len(payload),
            max_bytes=max_bytes,
            backup_count=backup_count,
        )
        with target.open("ab") as handle:
            handle.write(payload)
            handle.flush()


def rotate_file_if_oversize(
    path: str | Path,
    *,
    max_bytes: int,
    backup_count: int,
) -> bool:
    """Rotate an existing file before a long-lived child process opens it."""
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _rotation_lock(target):
        if not target.exists() or target.stat().st_size < max_bytes:
            return False
        try:
            if target.stat().st_size > max_bytes:
                _cap_file_to_recent_lines(target, max_bytes)
            _rotate_current_locked(target, backup_count)
        except OSError:
            # A previous Windows child can briefly retain the launch-log handle
            # after it exits. Rotation is best-effort here and must not prevent
            # the replacement supervisor from starting.
            return False
    return True


class LockedSizeRotatingFileHandler(logging.Handler):
    """A multi-process-safe rotating handler with no persistent file stream."""

    terminator = "\n"

    def __init__(
        self,
        filename: str | Path,
        *,
        max_bytes: int,
        backup_count: int,
        encoding: str = "utf-8",
        errors: str = "backslashreplace",
    ) -> None:
        super().__init__()
        self.baseFilename = str(Path(filename).resolve())
        self.max_bytes = max(1, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        self.encoding = encoding
        self.errors = errors
        # Retained for FileHandler compatibility and to make the closed-handle
        # behavior explicit to diagnostics and tests.
        self.stream = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record) + self.terminator
            append_rotating_text(
                self.baseFilename,
                message,
                max_bytes=self.max_bytes,
                backup_count=self.backup_count,
                encoding=self.encoding,
                errors=self.errors,
            )
        except Exception:
            self.handleError(record)
