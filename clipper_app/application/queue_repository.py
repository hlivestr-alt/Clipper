from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import portalocker

from clipper_app.application.catalog import CatalogDatabase, ChangeEventRepository, utc_now


QUEUE_SCHEMA_VERSION = 3
QUEUE_STORAGE_MODES = frozenset({"json", "dual", "sqlite"})


def queue_storage_mode() -> str:
    mode = os.getenv("CLIPPER_QUEUE_STORAGE_MODE", "json").strip().casefold() or "json"
    return mode if mode in QUEUE_STORAGE_MODES else "json"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _run_id(video_key: str, run: Mapping[str, Any]) -> str:
    identity = {
        "video_key": video_key,
        "working_dir": run.get("working_dir"),
        "output_dir": run.get("output_dir"),
        "created_at": run.get("created_at"),
    }
    return hashlib.sha256(_canonical_bytes(identity)).hexdigest()[:32]


class QueueStateRepository:
    """Active SQLite queue state with append-only, checksummed history journal."""

    def __init__(
        self,
        state_path: str | Path,
        *,
        database: CatalogDatabase | None = None,
        mode: str | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        selected = (mode or queue_storage_mode()).strip().casefold()
        self.mode = selected if selected in QUEUE_STORAGE_MODES else "json"
        self.working_dir = self.state_path.resolve().parent
        self.database = database or CatalogDatabase(self.working_dir / "catalog" / "clipper.sqlite3")
        self.history_dir = self.working_dir / "queue_history"
        self._last_snapshot_at = 0.0
        self._last_database_write_at = 0.0
        self._last_history_verification: dict[str, Any] = {
            "ok": None,
            "checked": 0,
            "errors": [],
        }
        self._journal_run_ids: dict[str, set[str]] = {}

    def load(
        self,
        fallback: Mapping[str, Any] | None = None,
        *,
        include_history: bool = True,
    ) -> dict[str, Any]:
        legacy = copy.deepcopy(dict(fallback or {}))
        if not legacy and self.state_path.exists():
            legacy = self._read_json(self.state_path)
        if self.mode == "json":
            return legacy
        self.database.ensure_schema()
        self._recover_journal_orphans()
        if legacy and not self._has_active_state():
            self.import_legacy(legacy)
        loaded = self._load_database_state(include_history=include_history)
        return loaded or legacy

    def save(self, state: Mapping[str, Any], *, lifecycle: bool = False) -> None:
        if self.mode == "json":
            self._write_json_atomic(self.state_path, dict(state))
            return
        if not lifecycle and time.monotonic() - self._last_database_write_at < 0.25:
            return
        self.database.ensure_schema()
        self._persist_database_state(state)
        self._set_journal_recovery_marker()
        self._last_database_write_at = time.monotonic()
        if self.mode == "dual":
            snapshot = copy.deepcopy(dict(state))
            snapshot["schema_version"] = 2
            self._write_json_atomic(self.state_path, snapshot)
        elif lifecycle or time.monotonic() - self._last_snapshot_at >= 10.0:
            self._write_compatibility_snapshot(state)
            self._last_snapshot_at = time.monotonic()
        if self.mode == "sqlite":
            for value in dict(state.get("videos") or {}).values():
                if isinstance(value, dict):
                    value["run_history"] = []
        ChangeEventRepository(self.database).publish(("queue",))

    def import_legacy(self, state: Mapping[str, Any]) -> dict[str, int]:
        self.database.ensure_schema()
        histories = 0
        for video_key, value in dict(state.get("videos") or {}).items():
            if not isinstance(value, Mapping):
                continue
            for run in value.get("run_history") or ():
                if isinstance(run, Mapping) and self._append_history(str(video_key), run):
                    histories += 1
        self._persist_database_state(state)
        self._set_journal_recovery_marker()
        return {"active_runs": len(dict(state.get("videos") or {})), "history_runs": histories}

    def export_legacy(self, destination: str | Path) -> Path:
        state = self._load_database_state(include_history=True)
        state["schema_version"] = 2
        target = Path(destination)
        self._write_json_atomic(target, state)
        return target

    def status(self) -> dict[str, Any]:
        if self.mode == "json":
            return {
                "mode": self.mode,
                "state_path": str(self.state_path),
                "active_runs": 0,
                "history_runs": 0,
            }
        return {
            "mode": self.mode,
            "state_path": str(self.state_path),
            "active_runs": int(self.database.scalar("SELECT COUNT(*) FROM queue_active_runs", default=0) or 0),
            "history_runs": int(self.database.scalar("SELECT COUNT(*) FROM queue_run_history", default=0) or 0),
            "history_dir": str(self.history_dir),
            "history_integrity": self._last_history_verification,
        }

    def verify_history(self) -> dict[str, Any]:
        if self.mode == "json":
            return {"ok": True, "checked": 0, "errors": []}
        errors: list[str] = []
        checked = 0
        for row in self.database.query(
            "SELECT run_id, checksum, payload_json, journal_segment FROM queue_run_history ORDER BY run_id"
        ):
            checked += 1
            payload = json.loads(row["payload_json"])
            actual = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
            if actual != row["checksum"]:
                errors.append(f"checksum mismatch: {row['run_id']}")
            if not (self.history_dir / str(row["journal_segment"])).exists():
                errors.append(f"missing journal segment: {row['journal_segment']}")
        result = {"ok": not errors, "checked": checked, "errors": errors[:100]}
        self._last_history_verification = result
        return result

    def _has_active_state(self) -> bool:
        return bool(self.database.scalar("SELECT COUNT(*) FROM queue_active_runs", default=0))

    def _persist_database_state(self, state: Mapping[str, Any]) -> None:
        videos = dict(state.get("videos") or {})
        meta = {key: copy.deepcopy(value) for key, value in state.items() if key != "videos"}
        meta["schema_version"] = QUEUE_SCHEMA_VERSION
        meta["updated_at"] = str(state.get("updated_at") or utc_now())
        active_keys: set[str] = set()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO queue_meta(key, value_json, updated_at) VALUES('state', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (json.dumps(meta, ensure_ascii=False), utc_now()),
            )
            for raw_key, raw_value in videos.items():
                if not isinstance(raw_value, Mapping):
                    continue
                video_key = str(raw_key)
                active_keys.add(video_key)
                value = copy.deepcopy(dict(raw_value))
                histories = value.pop("run_history", [])
                stages = dict(value.pop("stages", {}) or {})
                connection.execute(
                    "INSERT INTO queue_active_runs(video_key, payload_json, updated_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(video_key) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                    (video_key, json.dumps(value, ensure_ascii=False), utc_now()),
                )
                connection.execute("DELETE FROM queue_active_stages WHERE video_key=?", (video_key,))
                connection.executemany(
                    "INSERT INTO queue_active_stages(video_key, stage, payload_json) VALUES(?, ?, ?)",
                    [
                        (video_key, str(stage), json.dumps(payload, ensure_ascii=False))
                        for stage, payload in stages.items()
                    ],
                )
                for run in histories:
                    if isinstance(run, Mapping):
                        self._append_history(video_key, run, connection=connection)
            if active_keys:
                placeholders = ",".join("?" for _ in active_keys)
                connection.execute(
                    f"DELETE FROM queue_active_runs WHERE video_key NOT IN ({placeholders})",
                    tuple(sorted(active_keys)),
                )
            else:
                connection.execute("DELETE FROM queue_active_runs")

    def _load_database_state(self, *, include_history: bool = True) -> dict[str, Any]:
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM queue_meta WHERE key='state'"
            ).fetchone()
            if row is None:
                return {}
            active_rows = connection.execute(
                "SELECT video_key, payload_json FROM queue_active_runs ORDER BY video_key"
            ).fetchall()
            stage_rows = connection.execute(
                "SELECT video_key, stage, payload_json FROM queue_active_stages ORDER BY video_key, stage"
            ).fetchall()
            history_rows = (
                connection.execute(
                    "SELECT video_key, payload_json FROM queue_run_history ORDER BY video_key, archived_at, run_id"
                ).fetchall()
                if include_history else []
            )
        state = json.loads(row["value_json"])
        state["schema_version"] = QUEUE_SCHEMA_VERSION
        videos: dict[str, Any] = {}
        stages_by_video: dict[str, dict[str, Any]] = {}
        for stage in stage_rows:
            stages_by_video.setdefault(str(stage["video_key"]), {})[str(stage["stage"])] = json.loads(
                stage["payload_json"]
            )
        history_by_video: dict[str, list[Any]] = {}
        if include_history:
            for history in history_rows:
                history_by_video.setdefault(str(history["video_key"]), []).append(
                    json.loads(history["payload_json"])
                )
        for row in active_rows:
            video_key = str(row["video_key"])
            payload = json.loads(row["payload_json"])
            payload["stages"] = stages_by_video.get(video_key, {})
            payload["run_history"] = history_by_video.get(video_key, []) if include_history else []
            videos[video_key] = payload
        state["videos"] = videos
        return state

    def _append_history(
        self,
        video_key: str,
        run: Mapping[str, Any],
        *,
        connection: Any | None = None,
    ) -> bool:
        payload = copy.deepcopy(dict(run))
        payload.setdefault("archived_at", utc_now())
        run_id = _run_id(video_key, payload)
        payload["run_id"] = run_id
        checksum = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
        archived_at = str(payload["archived_at"])
        segment_name = self._segment_name(archived_at)
        exists = (
            connection.execute("SELECT 1 FROM queue_run_history WHERE run_id=?", (run_id,)).fetchone()
            if connection is not None
            else self.database.query("SELECT 1 FROM queue_run_history WHERE run_id=?", (run_id,))
        )
        if exists:
            return False
        self._append_journal(segment_name, run_id, checksum, video_key, payload)
        params = (
            run_id,
            video_key,
            archived_at,
            segment_name,
            checksum,
            json.dumps(payload, ensure_ascii=False),
        )
        if connection is not None:
            connection.execute(
                "INSERT OR IGNORE INTO queue_run_history(run_id, video_key, archived_at, journal_segment, checksum, payload_json) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                params,
            )
        else:
            self.database.execute(
                "INSERT OR IGNORE INTO queue_run_history(run_id, video_key, archived_at, journal_segment, checksum, payload_json) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                params,
            )
        return True

    def _append_journal(
        self,
        segment_name: str,
        run_id: str,
        checksum: str,
        video_key: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.history_dir.mkdir(parents=True, exist_ok=True)
        segment = self.history_dir / segment_name
        record = {
            "schema_version": QUEUE_SCHEMA_VERSION,
            "run_id": run_id,
            "video_key": video_key,
            "archived_at": payload.get("archived_at"),
            "checksum": checksum,
            "payload": payload,
        }
        lock_path = Path(f"{segment}.lock")
        with portalocker.Lock(str(lock_path), mode="a", timeout=10):
            existing = self._journal_run_ids.get(segment_name)
            if existing is None:
                existing = set()
            if segment.exists() and segment_name not in self._journal_run_ids:
                with segment.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        try:
                            existing.add(str(json.loads(line).get("run_id") or ""))
                        except (ValueError, TypeError):
                            continue
                self._journal_run_ids[segment_name] = existing
            if run_id in existing:
                return
            with segment.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            existing.add(run_id)
            self._journal_run_ids[segment_name] = existing

    def _recover_journal_orphans(self) -> int:
        if not self.history_dir.exists():
            return 0
        fingerprint = self._journal_fingerprint()
        marker_rows = self.database.query(
            "SELECT value_json FROM queue_meta WHERE key='journal_recovery_fingerprint'"
        )
        if marker_rows and json.loads(marker_rows[0]["value_json"]) == fingerprint:
            return 0
        existing = {
            str(row["run_id"])
            for row in self.database.query("SELECT run_id FROM queue_run_history")
        }
        pending: list[tuple[str, str, str, str, str, str]] = []
        recovered = 0
        for segment in sorted(self.history_dir.glob("????-??.jsonl")):
            with segment.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                        payload = record["payload"]
                        run_id = str(record["run_id"])
                        checksum = str(record["checksum"])
                        video_key = str(record["video_key"])
                        if hashlib.sha256(_canonical_bytes(payload)).hexdigest() != checksum:
                            continue
                        if run_id in existing:
                            continue
                        pending.append(
                            (
                                run_id, video_key,
                                str(record.get("archived_at") or payload.get("archived_at") or utc_now()),
                                segment.name, checksum, json.dumps(payload, ensure_ascii=False),
                            )
                        )
                        existing.add(run_id)
                        recovered += 1
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
        with self.database.transaction(immediate=True) as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO queue_run_history(run_id, video_key, archived_at, journal_segment, checksum, payload_json) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                pending,
            )
            connection.execute(
                "INSERT INTO queue_meta(key, value_json, updated_at) VALUES('journal_recovery_fingerprint', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (json.dumps(fingerprint, sort_keys=True), utc_now()),
            )
        return recovered

    def _journal_fingerprint(self) -> list[list[Any]]:
        return [
            [segment.name, int(stat.st_size), int(stat.st_mtime_ns)]
            for segment in sorted(self.history_dir.glob("????-??.jsonl"))
            for stat in (segment.stat(),)
        ]

    def _set_journal_recovery_marker(self) -> None:
        fingerprint = self._journal_fingerprint()
        self.database.execute(
            "INSERT INTO queue_meta(key, value_json, updated_at) VALUES('journal_recovery_fingerprint', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (json.dumps(fingerprint, sort_keys=True), utc_now()),
        )

    def _write_compatibility_snapshot(self, state: Mapping[str, Any]) -> None:
        snapshot = copy.deepcopy(dict(state))
        snapshot["schema_version"] = QUEUE_SCHEMA_VERSION
        for value in dict(snapshot.get("videos") or {}).values():
            if isinstance(value, dict):
                value.pop("run_history", None)
        snapshot["history_journal"] = str(self.history_dir)
        snapshot["catalog_revision"] = int(
            self.database.scalar(
                "SELECT revision FROM catalog_revisions WHERE domain='queue'", default=0
            )
            or 0
        )
        self._write_json_atomic(self.state_path, snapshot)

    @staticmethod
    def _segment_name(archived_at: str) -> str:
        try:
            parsed = datetime.fromisoformat(archived_at.replace("Z", "+00:00"))
            return f"{parsed.year:04d}-{parsed.month:02d}.jsonl"
        except ValueError:
            now = datetime.now().astimezone()
            return f"{now.year:04d}-{now.month:02d}.jsonl"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        for attempt in range(3):
            try:
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                return
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.2 * (attempt + 1))
