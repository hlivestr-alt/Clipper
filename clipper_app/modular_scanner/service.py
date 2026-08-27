from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .analyzer import ScannerAnalyzer
from .constants import (
    ANALYZER_VERSION,
    EMPTY_FALLBACK_MAX_DEPTH,
    EMPTY_FALLBACK_MINIMUM_SECONDS,
    EMPTY_FALLBACK_TARGET_SECONDS,
    MAX_ANALYSIS_RETRIES,
    PRODUCTS,
    PROMPT_VERSION,
    ROLES,
    WAIT_POLL_SECONDS,
    WINDOW_OVERLAP_SECONDS,
)
from .media import discover_sources, revalidate_source
from .repository import ScannerRepository, utc_now
from .transcripts import (
    build_windows,
    copy_production_transcript,
    load_transcript,
    transcript_fingerprint,
    transcribe_fresh,
    subdivide_window,
)
from .validation import deduplicate, product_evidence, validate_candidate

log = logging.getLogger("clipper.modular_scanner")

ACTIVE_PRODUCTION_STATUSES = frozenset({
    "running", "processing", "starting", "start_requested", "continue_requested",
    "queued", "pausing", "pause_requested", "stopping", "stop_requested", "retrying",
})


def _candidate_owned_by_window(candidate: Any, window: dict[str, Any]) -> bool:
    """Apply child overlap ownership while preserving malformed candidates for validation diagnostics."""
    if not isinstance(candidate, dict):
        return True
    start = candidate.get("start_seconds")
    end = candidate.get("end_seconds")
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return True
    midpoint = (float(start) + float(end)) / 2.0
    final_window = abs(float(window["ownership_end"]) - float(window["end"])) <= 1e-6
    return midpoint >= window["ownership_start"] and (midpoint < window["ownership_end"] or final_window)


def production_is_active(cfg: Any) -> bool:
    """Read the existing queue/control/supervisor snapshot without mutating it."""
    import queue_control

    snapshot = queue_control.read_status_snapshot(
        control_path=getattr(cfg, "QUEUE_CONTROL_FILE", None),
        forever_state_path=getattr(cfg, "QUEUE_FOREVER_STATE_FILE", None),
        queue_state_path=getattr(cfg, "QUEUE_STATE_FILE", None),
    )
    for section_name in ("control", "supervisor", "queue"):
        section = snapshot.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in ("status", "queue_status"):
            if str(section.get(key) or "").strip().casefold() in ACTIVE_PRODUCTION_STATUSES:
                return True
    videos = (snapshot.get("queue") or {}).get("videos")
    if isinstance(videos, dict):
        for video in videos.values():
            if not isinstance(video, dict):
                continue
            if str(video.get("status") or "").strip().casefold() in {"running", "queued"}:
                return True
            stages = video.get("stages")
            if isinstance(stages, dict) and any(
                isinstance(stage, dict)
                and str(stage.get("status") or "").strip().casefold() in {"running", "queued"}
                for stage in stages.values()
            ):
                return True
    return False


class ModularScannerService:
    """One-worker scanner with durable scan and per-window checkpoints."""

    def __init__(
        self,
        cfg: Any,
        *,
        repository: ScannerRepository | None = None,
        analyzer_factory: Callable[[], Any] | None = None,
        production_active: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        wait_poll_seconds: float = WAIT_POLL_SECONDS,
        start_worker: bool = True,
    ):
        self.cfg = cfg
        working = Path(getattr(cfg, "WORKING_DIR", "working"))
        self.storage_root = working / "modular_scanner"
        self.repository = repository or ScannerRepository(working / "modular_library.sqlite3")
        self.analyzer_factory = analyzer_factory or (lambda: ScannerAnalyzer(cfg))
        self._production_active = production_active or (lambda: production_is_active(cfg))
        self._sleep = sleep
        self.wait_poll_seconds = wait_poll_seconds
        self._tasks: queue.Queue[str | None] = queue.Queue()
        self._queued_ids: set[str] = set()
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.repository.recover_incomplete()
        for scan in self.repository.pending_scans():
            self._enqueue(scan["scan_id"])
        if start_worker:
            self.start_worker()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "MODSCAN_ENABLED", True))

    def start_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._worker_loop, name="modular-scanner", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self._stop.set()
        self._tasks.put(None)
        if self._worker is not None:
            self._worker.join(timeout=5)

    def discover(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        records = []
        for source in discover_sources(getattr(self.cfg, "QUEUE_INPUT_DIR"), include_duration=True):
            record = self.repository.upsert_source(source)
            records.append(self._public_source(record))
        return records

    def start_scan(self, source_id: str, *, rescan: bool = False) -> tuple[dict[str, Any], bool]:
        if not self.enabled:
            raise RuntimeError("Modular Scanner is disabled")
        source = self.repository.get_source(source_id)
        if source is None:
            raise KeyError("Unknown source")
        revalidate_source(source, getattr(self.cfg, "QUEUE_INPUT_DIR"))
        if not rescan:
            active = self.repository.active_scan(source_id)
            if active is not None:
                return self._public_scan(active), True
            transcript_record = self._reuse_available_transcript(source)
            if transcript_record is not None:
                compatible = self.repository.compatible_scan(
                    source_id,
                    transcript_record["transcript_fingerprint"],
                    ANALYZER_VERSION,
                    PROMPT_VERSION,
                    self.model_id,
                )
                if compatible is not None:
                    return self._public_scan(compatible), True
        scan = self.repository.create_scan(
            source_id,
            "rescan" if rescan else "scan",
            ANALYZER_VERSION,
            PROMPT_VERSION,
            self.model_id,
        )
        self._enqueue(scan["scan_id"])
        return self._public_scan(scan), False

    @property
    def model_id(self) -> str:
        return str(getattr(self.cfg, "LM_STUDIO_MOMENT_MODEL_ID"))

    def get_scan(self, scan_id: str) -> dict[str, Any]:
        scan = self.repository.get_scan(scan_id)
        if scan is None:
            raise KeyError("Unknown scan")
        return self._public_scan(scan)

    def history(self, source_id: str) -> list[dict[str, Any]]:
        if self.repository.get_source(source_id) is None:
            raise KeyError("Unknown source")
        return [self._public_scan(scan) for scan in self.repository.list_scans(source_id)]

    def segments(
        self,
        *,
        source_id: str,
        scan_id: str | None = None,
        product: str | None = None,
        role: str | None = None,
        minimum_confidence: float = 0.0,
        search: str = "",
        sort: str = "timestamp",
    ) -> list[dict[str, Any]]:
        if product is not None and product not in PRODUCTS:
            raise ValueError("Invalid product filter")
        if role is not None and role not in ROLES:
            raise ValueError("Invalid role filter")
        selected = self.repository.get_scan(scan_id) if scan_id else self.repository.current_scan(source_id)
        if selected is None or selected["source_id"] != source_id:
            return []
        return self.repository.list_segments(
            selected["scan_id"], product, role, minimum_confidence, search, sort,
        )

    def media_path(self, source_id: str) -> Path:
        source = self.repository.get_source(source_id)
        if source is None:
            raise KeyError("Unknown source")
        return revalidate_source(source, getattr(self.cfg, "QUEUE_INPUT_DIR"))

    def run_scan(self, scan_id: str) -> None:
        """Execute one persisted scan. Public for deterministic focused tests."""
        scan = self.repository.get_scan(scan_id)
        if scan is None:
            raise KeyError("Unknown scan")
        if scan["status"] in {"completed", "failed"}:
            return
        source = self.repository.get_source(scan["source_id"])
        if source is None:
            self.repository.update_scan(scan_id, "failed", error="Source metadata is missing", completed_at=utc_now())
            return
        try:
            source_path = revalidate_source(source, getattr(self.cfg, "QUEUE_INPUT_DIR"))
            transcript_record, transcript = self._resolve_transcript(scan_id, source)
            self.repository.update_scan(scan_id, "queued", transcript_id=transcript_record["transcript_id"])
            windows = build_windows(transcript)
            self.repository.upsert_chunks(scan_id, windows)
            self.repository.update_scan(scan_id, "queued", progress_total=len(windows))
            analyzer = self.analyzer_factory()
            saved_chunks = {item["chunk_index"]: item for item in self.repository.chunks(scan_id)}
            completed = 0
            for window in windows:
                chunk = saved_chunks[window["index"]]
                if chunk["status"] == "completed" and chunk["response_json"] is not None:
                    completed += 1
                    continue
                self._wait_for_production(scan_id)
                self.repository.update_scan(scan_id, "analyzing", progress_current=completed, progress_total=len(windows))
                candidates = self._analyze_with_empty_recovery(analyzer, window, scan_id)
                self.repository.complete_chunk(scan_id, window["index"], candidates)
                completed += 1
                self.repository.update_scan(scan_id, "analyzing", progress_current=completed, progress_total=len(windows))
                # An in-flight request is allowed to finish. Checkpoint first, then yield
                # cooperatively if production became active during that request.
                self._wait_for_production(scan_id)
            self.repository.update_scan(scan_id, "validating", progress_current=len(windows), progress_total=len(windows))
            accepted: list[dict[str, Any]] = []
            rejected_count = 0
            order = 0
            chunk_rows = {row["chunk_index"]: row for row in self.repository.chunks(scan_id)}
            duration = float(source.get("duration_seconds") or 0)
            if duration <= 0:
                raise RuntimeError("VOD duration is unavailable; ffprobe is required for safe bounds validation")
            for window in windows:
                raw_candidates = json.loads(chunk_rows[window["index"]]["response_json"] or "[]")
                for candidate in raw_candidates:
                    validated, rejection = validate_candidate(candidate, window, duration, order=order)
                    order += 1
                    if validated is not None:
                        accepted.append(validated)
                    else:
                        rejected_count += 1
                        self.repository.add_rejection(scan_id, window["index"], rejection.code, rejection.detail, candidate)
            final_segments = deduplicate(accepted)
            self.repository.replace_segments(scan_id, source, final_segments)
            self.repository.update_scan(
                scan_id,
                "completed",
                accepted_count=len(final_segments),
                rejected_count=rejected_count,
                progress_current=len(windows),
                progress_total=len(windows),
                error=None,
                completed_at=utc_now(),
            )
        except Exception as exc:
            log.exception("Modular scan %s failed", scan_id)
            self.repository.update_scan(scan_id, "failed", error=str(exc)[:2000], completed_at=utc_now())

    def _resolve_transcript(self, scan_id: str, source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        scan = self.repository.get_scan(scan_id)
        if scan and scan.get("transcript_id"):
            record = self.repository.get_transcript(scan["transcript_id"])
            if record is not None:
                return record, load_transcript(record["cache_path"])
        record = self._reuse_available_transcript(source)
        if record is not None:
            return record, load_transcript(record["cache_path"])
        self._wait_for_production(scan_id)
        self.repository.update_scan(scan_id, "transcribing", started_at=utc_now())
        target_dir = self.storage_root / "transcripts" / source["source_id"]
        transcript = transcribe_fresh(source, self.cfg, target_dir)
        path = target_dir / "transcript.json"
        record = self.repository.add_transcript(
            source["source_id"], "scanner", str(path), transcript_fingerprint(transcript),
        )
        return record, transcript

    def _reuse_available_transcript(self, source: dict[str, Any]) -> dict[str, Any] | None:
        record = self.repository.compatible_transcript(source["source_id"])
        if record is not None:
            try:
                transcript = load_transcript(record["cache_path"])
                if transcript_fingerprint(transcript) == record["transcript_fingerprint"]:
                    return record
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        target = self.storage_root / "transcripts" / source["source_id"] / "transcript.json"
        transcript = copy_production_transcript(source, self.cfg, target)
        if transcript is None:
            return None
        return self.repository.add_transcript(
            source["source_id"], "production", str(target), transcript_fingerprint(transcript),
        )

    def _wait_for_production(self, scan_id: str) -> None:
        waiting = False
        while self._production_active():
            waiting = True
            self.repository.update_scan(scan_id, "waiting_for_production")
            if self._stop.is_set():
                raise RuntimeError("Scanner is stopping")
            self._sleep(self.wait_poll_seconds)
        if waiting:
            self.repository.update_scan(scan_id, "queued")

    @staticmethod
    def _analyze_with_retry(analyzer: Any, window: dict[str, Any]) -> list[Any]:
        error: Exception | None = None
        for _attempt in range(MAX_ANALYSIS_RETRIES + 1):
            try:
                return analyzer.analyze(window)
            except Exception as exc:
                error = exc
        raise RuntimeError(f"LM Studio analysis failed after retries: {error}") from error

    def _analyze_with_empty_recovery(
        self,
        analyzer: Any,
        window: dict[str, Any],
        scan_id: str,
        *,
        depth: int = 0,
    ) -> list[Any]:
        candidates = self._analyze_with_retry(analyzer, window)
        duration = float(window["end"]) - float(window["start"])
        if candidates or not product_evidence(window["text"]):
            return candidates
        if depth >= EMPTY_FALLBACK_MAX_DEPTH or duration < EMPTY_FALLBACK_MINIMUM_SECONDS:
            return candidates

        child_limit = min(EMPTY_FALLBACK_TARGET_SECONDS, duration / 2.0 + WINDOW_OVERLAP_SECONDS)
        children = subdivide_window(window, maximum_seconds=child_limit)
        if len(children) < 2:
            return candidates
        recovered: list[Any] = []
        for child in children:
            self._wait_for_production(scan_id)
            child_candidates = self._analyze_with_empty_recovery(
                analyzer, child, scan_id, depth=depth + 1,
            )
            recovered.extend(
                candidate for candidate in child_candidates if _candidate_owned_by_window(candidate, child)
            )
        return recovered

    def _enqueue(self, scan_id: str) -> None:
        with self._guard:
            if scan_id in self._queued_ids:
                return
            self._queued_ids.add(scan_id)
        self._tasks.put(scan_id)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            scan_id = self._tasks.get()
            if scan_id is None:
                return
            with self._guard:
                self._queued_ids.discard(scan_id)
            self.run_scan(scan_id)

    def _public_source(self, source: dict[str, Any]) -> dict[str, Any]:
        current = self.repository.current_scan(source["source_id"])
        active = self.repository.active_scan(source["source_id"])
        return {
            "source_id": source["source_id"],
            "filename": source["filename"],
            "file_size": source["file_size"],
            "mtime_ns": source["mtime_ns"],
            "duration_seconds": source["duration_seconds"],
            "current_scan": self._public_scan(current) if current else None,
            "active_scan": self._public_scan(active) if active else None,
        }

    def _public_scan(self, scan: dict[str, Any]) -> dict[str, Any]:
        current = self.repository.current_scan(scan["source_id"])
        return {
            key: scan.get(key) for key in (
                "scan_id", "source_id", "generation", "trigger", "status", "analyzer_version",
                "prompt_version", "model_id", "progress_current", "progress_total",
                "accepted_count", "rejected_count", "error", "created_at", "started_at", "completed_at",
            )
        } | {"is_current": bool(current and current["scan_id"] == scan["scan_id"])}
