from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


TAIL_CHUNK_BYTES = 64 * 1024
TAIL_MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ReverseTailLine:
    text: str
    line_number: int | None = None


@dataclass(frozen=True)
class ReverseTailResult:
    lines: tuple[ReverseTailLine, ...]
    total_lines: int | None
    bytes_read: int
    reached_start: bool
    partial_oldest_line: bool = False


class _FileChangedDuringTail(OSError):
    pass


def _reverse_tail_once(
    path: str | Path,
    *,
    line_limit: int,
    chunk_bytes: int = TAIL_CHUNK_BYTES,
    max_bytes: int = TAIL_MAX_BYTES,
) -> ReverseTailResult:
    """Read only enough of a file's end to return its newest complete lines."""
    target = Path(path)
    line_limit = max(1, int(line_limit))
    chunk_bytes = max(1, int(chunk_bytes))
    max_bytes = max(1, int(max_bytes))
    with target.open("rb") as handle:
        file_size = os.fstat(handle.fileno()).st_size
        if file_size == 0:
            return ReverseTailResult((), 0, 0, True)

        position = file_size
        bytes_read = 0
        newline_count = 0
        buffer = bytearray()
        handle.seek(file_size - 1)
        final_byte = handle.read(1)
        if len(final_byte) != 1:
            raise _FileChangedDuringTail("Log changed while preparing the tail read")
        ends_with_newline = final_byte == b"\n"

        # One delimiter before the oldest requested line is needed to prove
        # that line is complete. A trailing newline adds its own delimiter.
        required_newlines = line_limit + (1 if ends_with_newline else 0)
        while position > 0 and bytes_read < max_bytes:
            read_size = min(chunk_bytes, position, max_bytes - bytes_read)
            position -= read_size
            handle.seek(position)
            block = handle.read(read_size)
            if len(block) != read_size:
                raise _FileChangedDuringTail("Log changed during the tail read")
            buffer[:0] = block
            bytes_read += len(block)
            newline_count += block.count(b"\n")
            if newline_count >= required_newlines:
                break

    reached_start = position == 0
    partial_oldest_line = False
    if not reached_start:
        first_newline = buffer.find(b"\n")
        if first_newline >= 0:
            del buffer[: first_newline + 1]
        else:
            buffer.clear()
        partial_oldest_line = bytes_read >= max_bytes

    decoded_lines = bytes(buffer).decode("utf-8", errors="replace").splitlines()
    selected = decoded_lines[-line_limit:]

    if reached_start:
        total_lines = len(decoded_lines)
        start_number = total_lines - len(selected) + 1
        numbered = tuple(
            ReverseTailLine(text=text, line_number=start_number + index)
            for index, text in enumerate(selected)
        )
    else:
        total_lines = None
        numbered = tuple(ReverseTailLine(text=text) for text in selected)

    return ReverseTailResult(
        lines=tuple(reversed(numbered)),
        total_lines=total_lines,
        bytes_read=bytes_read,
        reached_start=reached_start,
        partial_oldest_line=partial_oldest_line,
    )


def reverse_tail(
    path: str | Path,
    *,
    line_limit: int,
    chunk_bytes: int = TAIL_CHUNK_BYTES,
    max_bytes: int = TAIL_MAX_BYTES,
) -> ReverseTailResult:
    """Read a bounded tail, retrying once if rotation truncates the open file."""
    for attempt in range(2):
        try:
            return _reverse_tail_once(
                path,
                line_limit=line_limit,
                chunk_bytes=chunk_bytes,
                max_bytes=max_bytes,
            )
        except _FileChangedDuringTail:
            if attempt == 1:
                raise
    raise AssertionError("unreachable")
