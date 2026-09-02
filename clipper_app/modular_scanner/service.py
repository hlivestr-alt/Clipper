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
    MINIMUM_DURATION_SECONDS,
    PRODUCTS,
    PROMPT_VERSION,
    ROLES,
    VIDEO_SUFFIXES,
    WAIT_POLL_SECONDS,
    WINDOW_OVERLAP_SECONDS,
)
from .media import revalidate_source, source_record
from .repository import ScannerRepository, utc_now
from .transcripts import (
    build_windows,
    load_transcript,
    transcript_fingerprint,
    transcribe_fresh,
    subdivide_window,
)
from .validation import (
    build_product_context,
    compose_candidates,
    deduplicate,
    product_evidence,
    resolve_cross_window_product_conflicts,
    validate_candidate,
)

log = logging.getLogger("clipper.modular_scanner")

ACTIVE_PRODUCTION_STATUSES = frozenset({
    "running", "processing", "starting", "start_requested", "continue_requested",
    "queued", "pausing", "pause_requested", "stopping", "stop_requested", "retrying",
})


def _scan_is_active(scan: dict[str, Any]) -> bool:
    return str(scan.get("status") or "") not in {"completed", "failed"}


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
        self._tasks: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._queued_ids: set[tuple[str, str]] = set()
        self._guard = threading.Lock()
        self._scan_start_guard = threading.RLock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.repository.recover_incomplete()
        for batch in self.repository.pending_batches():
            self._enqueue_batch(batch["batch_id"])
        for scan in self.repository.pending_scans():
            self._enqueue_scan(scan["scan_id"])
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
        return [self._public_source(record) for record in self._discover_records()]

    def _discover_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in self._eligible_paths():
            try:
                records.append(self._source_for_path(path, include_duration=True))
            except (OSError, ValueError):
                continue
        return records

    def _eligible_paths(self) -> list[Path]:
        root = Path(getattr(self.cfg, "QUEUE_INPUT_DIR")).resolve()
        if not root.is_dir():
            return []
        return [
            path for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
            if path.is_file() and path.suffix.casefold() in VIDEO_SUFFIXES
        ]

    def _source_for_path(self, path: Path, *, include_duration: bool) -> dict[str, Any]:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
        known = self.repository.source_by_metadata(str(resolved), stat.st_size, stat.st_mtime_ns)
        if known is not None and (not include_duration or known.get("duration_seconds") is not None):
            return self.repository.upsert_source(known)
        if known is not None:
            refreshed = dict(known)
            from .media import probe_duration
            refreshed["duration_seconds"] = probe_duration(resolved)
            return self.repository.upsert_source(refreshed)
        return self.repository.upsert_source(source_record(resolved, include_duration=include_duration))

    def start_scan(self, source_id: str, *, rescan: bool = False) -> tuple[dict[str, Any], bool]:
        with self._scan_start_guard:
            return self._start_scan_locked(source_id, rescan=rescan)

    def _start_scan_locked(self, source_id: str, *, rescan: bool = False) -> tuple[dict[str, Any], bool]:
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
        self._enqueue_scan(scan["scan_id"])
        return self._public_scan(scan), False

    def batch_preview(self) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Modular Scanner is disabled")
        started = time.monotonic()
        sources: list[dict[str, Any]] = []
        for path in self._eligible_paths():
            try:
                resolved = path.resolve(strict=True)
                stat = resolved.stat()
                source = self.repository.source_by_metadata(str(resolved), stat.st_size, stat.st_mtime_ns)
                if source is None:
                    disposition = "needs_check"
                    source_id = None
                else:
                    source_id = source["source_id"]
                    active = self.repository.active_scan(source_id)
                    transcript = self.repository.compatible_transcript(source_id)
                    compatible = self.repository.compatible_scan(
                        source_id, transcript["transcript_fingerprint"],
                        ANALYZER_VERSION, PROMPT_VERSION, self.model_id,
                    ) if transcript is not None else None
                    disposition = (
                        "already_active" if active is not None
                        else "already_current" if compatible is not None
                        else "would_queue"
                    )
                sources.append({
                    "source_id": source_id, "filename": path.name, "disposition": disposition,
                })
            except OSError:
                sources.append({"source_id": None, "filename": path.name, "disposition": "needs_check"})
        summary = self._batch_plan_summary(sources)
        log.info("Batch preview inspected %d metadata records in %.3fs", len(sources), time.monotonic() - started)
        return summary

    def start_batch(self) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Modular Scanner is disabled")
        with self._scan_start_guard:
            batch = self.repository.active_batch()
            reused = batch is not None
            if batch is None:
                batch = self.repository.create_batch()
                self._enqueue_batch(batch["batch_id"])
        return {"launched": True, "reused": reused, "batch": self.batch_status(batch["batch_id"])}

    def prepare_batch(self, batch_id: str) -> None:
        batch = self.repository.get_batch(batch_id)
        if batch is None or batch.get("status") != "preparing":
            return
        started = time.monotonic()
        try:
            paths = self._eligible_paths()
            self.repository.update_batch(batch_id, "preparing", discovered_count=len(paths), error=None)
            prepared_paths = self.repository.batch_prepared_paths(batch_id)
            for path in paths:
                if self._stop.is_set():
                    return
                item_key = str(path.resolve()).casefold()
                if item_key in prepared_paths:
                    continue
                item_started = time.monotonic()
                try:
                    source = self._source_for_path(path, include_duration=True)
                    with self._scan_start_guard:
                        scan, reused = self._start_scan_locked(source["source_id"])
                    disposition = (
                        "already_active" if reused and _scan_is_active(scan)
                        else "already_current" if reused
                        else "queued"
                    )
                    self.repository.add_batch_item(batch_id, {
                        "source_id": source["source_id"], "scan_id": scan["scan_id"],
                        "disposition": disposition,
                    })
                except Exception as exc:
                    self.repository.add_batch_failure(batch_id, str(path.resolve()), path.name, str(exc))
                    log.warning("Batch %s could not prepare %s: %s", batch_id, path.name, exc)
                finally:
                    log.info(
                        "Batch %s prepared %s in %.3fs",
                        batch_id, path.name, time.monotonic() - item_started,
                    )
            self.repository.update_batch(batch_id, "running")
            self.batch_status(batch_id)
            log.info("Batch %s preparation finished in %.3fs", batch_id, time.monotonic() - started)
        except Exception as exc:
            self.repository.update_batch(batch_id, "failed", error=str(exc), completed_at=utc_now())
            log.exception("Batch %s preparation failed", batch_id)

    def batch_status(self, batch_id: str) -> dict[str, Any]:
        batch = self.repository.get_batch(batch_id)
        if batch is None:
            raise KeyError("Unknown scan batch")
        items = self.repository.batch_items(batch_id)
        preparation_failures = self.repository.batch_failures(batch_id)
        queued = [item for item in items if item["disposition"] == "queued"]
        completed = sum(item.get("scan_status") == "completed" for item in queued)
        failed_scans = sum(item.get("scan_status") == "failed" for item in queued)
        failed_to_queue = sum(item["disposition"] == "failed_to_queue" for item in items)
        queued_remaining = sum(item.get("scan_status") not in {"completed", "failed"} for item in queued)
        unchecked = max(int(batch.get("discovered_count") or 0) - len(items) - len(preparation_failures), 0)
        remaining = queued_remaining + (unchecked if batch.get("status") == "preparing" else 0)
        running = next((
            item for item in items
            if item.get("scan_status") in {
                "waiting_for_production", "transcribing", "analyzing", "validating"
            }
        ), None)
        failure_count = failed_scans + failed_to_queue + len(preparation_failures)
        if batch.get("status") == "running" and remaining == 0 and batch.get("completed_at") is None:
            self.repository.complete_batch(batch_id, with_failures=failure_count > 0)
            batch = self.repository.get_batch(batch_id) or batch
        status = str(batch.get("status") or "preparing")
        return {
            "batch_id": batch_id,
            "created_at": batch["created_at"],
            "completed_at": batch.get("completed_at"),
            "status": status,
            "total_eligible": int(batch.get("discovered_count") or 0),
            "discovered": int(batch.get("discovered_count") or 0),
            "checked": len(items) + len(preparation_failures),
            "checking": 1 if status == "preparing" and unchecked > 0 else 0,
            "already_current": sum(item["disposition"] == "already_current" for item in items),
            "already_active": sum(item["disposition"] == "already_active" for item in items),
            "queued": len(queued),
            "completed": completed,
            "failed": failure_count,
            "remaining": remaining,
            "currently_running": ({
                "source_id": running["source_id"],
                "filename": running["filename"],
                "status": running["scan_status"],
            } if running else None),
        }

    @staticmethod
    def _batch_plan_summary(plan: list[dict[str, Any]]) -> dict[str, Any]:
        needs_check = sum(item["disposition"] == "needs_check" for item in plan)
        return {
            "total_eligible": len(plan),
            "already_current": sum(item["disposition"] == "already_current" for item in plan),
            "already_active": sum(item["disposition"] == "already_active" for item in plan),
            "would_queue": sum(item["disposition"] == "would_queue" for item in plan),
            "needs_check": needs_check,
            "will_evaluate": sum(item["disposition"] in {"would_queue", "needs_check"} for item in plan),
            "sources": [
                {key: item[key] for key in ("source_id", "filename", "disposition")}
                for item in plan
            ],
        }

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
            processed: list[dict[str, Any]] = []
            rejected_count = 0
            order = 0
            product_context = build_product_context(transcript["segments"])
            chunk_rows = {row["chunk_index"]: row for row in self.repository.chunks(scan_id)}
            duration = float(source.get("duration_seconds") or 0)
            if duration <= 0:
                raise RuntimeError("VOD duration is unavailable; ffprobe is required for safe bounds validation")
            for window in windows:
                raw_candidates = json.loads(chunk_rows[window["index"]]["response_json"] or "[]")
                normalized: list[dict[str, Any]] = []
                repair_options: dict[int, dict[str, Any]] = {}
                repair_rejections: dict[int, Any] = {}
                raw_by_order: dict[int, Any] = {}
                for candidate in raw_candidates:
                    candidate_order = order
                    raw_by_order[candidate_order] = candidate
                    validated, rejection = validate_candidate(
                        candidate, window, duration, order=candidate_order,
                        product_context=product_context, allow_short=True,
                        attempt_duration_repair=False, enforce_ownership=False,
                        defer_product_validation=True,
                    )
                    order += 1
                    if validated is not None:
                        normalized.append(validated)
                        if validated["duration_seconds"] < MINIMUM_DURATION_SECONDS:
                            repaired, repair_rejection = validate_candidate(
                                candidate, window, duration, order=candidate_order,
                                product_context=product_context, enforce_ownership=False,
                                defer_product_validation=True,
                            )
                            if repaired is not None:
                                repaired["_chunk_index"] = window["index"]
                                repair_options[candidate_order] = repaired
                            else:
                                repair_rejections[candidate_order] = repair_rejection
                    else:
                        rejected_count += 1
                        self.repository.add_rejection(scan_id, window["index"], rejection.code, rejection.detail, candidate)
                    if validated is not None:
                        validated["_chunk_index"] = window["index"]
                normalized, composition_diagnostics = compose_candidates(
                    normalized, window, product_context, repair_options,
                )
                for diagnostic in composition_diagnostics:
                    self.repository.add_rejection(
                        scan_id, window["index"], "composed_into_segment",
                        diagnostic["reason"], diagnostic,
                    )
                for validated in normalized:
                    if validated["duration_seconds"] >= MINIMUM_DURATION_SECONDS:
                        processed.append(validated)
                        continue
                    candidate_order = int(validated.get("_order", -1))
                    repaired = repair_options.get(candidate_order)
                    if repaired is not None:
                        processed.append(repaired)
                        continue
                    rejection = repair_rejections[candidate_order]
                    rejected_count += 1
                    self.repository.add_rejection(
                        scan_id, window["index"], rejection.code, rejection.detail,
                        raw_by_order[candidate_order],
                    )
            processed, conflict_diagnostics = resolve_cross_window_product_conflicts(
                processed, transcript["segments"], product_context,
            )
            for diagnostic in conflict_diagnostics:
                reason_code = diagnostic["status"]
                self.repository.add_rejection(
                    scan_id, None, reason_code,
                    f"{diagnostic['resolution']}: {diagnostic['evidence']}", diagnostic,
                )
                rejected_count += int(diagnostic["discarded_candidate_count"])

            windows_by_index = {window["index"]: window for window in windows}
            for candidate in processed:
                chunk_index = int(candidate.get("_chunk_index", -1))
                window = windows_by_index[chunk_index]
                prior_diagnostics = candidate.get("validation_diagnostics") or {}
                validated, rejection = validate_candidate(
                    candidate, window, duration, order=int(candidate.get("_order", 0)),
                    product_context=product_context, enforce_ownership=False,
                )
                if validated is None:
                    rejected_count += 1
                    self.repository.add_rejection(
                        scan_id, chunk_index, rejection.code, rejection.detail, candidate,
                    )
                    continue
                for key in ("composition", "cross_window_product_conflict"):
                    if key in prior_diagnostics:
                        validated["validation_diagnostics"][key] = prior_diagnostics[key]
                prior_repair = prior_diagnostics.get("duration_repair") or {}
                if prior_repair.get("outcome") == "expanded":
                    validated["validation_diagnostics"]["duration_repair"] = prior_repair
                if not candidate.get("_cross_window_resolution_winner") and not _candidate_owned_by_window(validated, window):
                    rejected_count += 1
                    self.repository.add_rejection(
                        scan_id, chunk_index, "overlap_ownership",
                        "Neighboring window owns this candidate", candidate,
                    )
                    continue
                accepted.append(validated)
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
        from clipper_app.storage.transcripts import resolve_effective_transcript_path

        path = resolve_effective_transcript_path(target_dir)
        if path is None:
            raise RuntimeError("Canonical scanner transcript was not committed")
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
        from .transcripts import find_production_transcript

        production_path = find_production_transcript(source, self.cfg)
        if production_path is None:
            return None
        transcript = load_transcript(production_path)
        return self.repository.add_transcript(
            source["source_id"], "production", str(production_path), transcript_fingerprint(transcript),
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

    def _enqueue_scan(self, scan_id: str) -> None:
        self._enqueue_task("scan", scan_id)

    def _enqueue_batch(self, batch_id: str) -> None:
        self._enqueue_task("batch", batch_id)

    def _enqueue_task(self, kind: str, identifier: str) -> None:
        key = (kind, identifier)
        with self._guard:
            if key in self._queued_ids:
                return
            self._queued_ids.add(key)
        self._tasks.put(key)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            task = self._tasks.get()
            if task is None:
                return
            kind, identifier = task
            with self._guard:
                self._queued_ids.discard(task)
            if kind == "batch":
                self.prepare_batch(identifier)
            else:
                self.run_scan(identifier)

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
