#!/usr/bin/env python3
"""
Simple threaded queue runner for the PROYA video pipeline.

Pipeline per video:
  1. Transcription (GPU shared lane)
  2. LLM moment detection (GPU shared lane)
  3. YOLO scan (isolated parallel lane)
  4. Module extraction + normal FFmpeg clip render (parallel CPU lane/backlog)

State is persisted to JSON so interrupted runs can resume.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, PriorityQueue, Queue
from typing import Optional

import queue_control
from stage_cache import stage_fingerprint_matches, write_stage_fingerprint


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("proya.queue")


VIDEO_EXTS = {".mp4", ".mkv", ".mov"}
STAGES = ("transcribe", "llm", "yolo", "ffmpeg")
GPU_ANALYSIS_STAGES = ("transcribe", "llm")
PRE_EDIT_STAGES = STAGES[:-1]
EDIT_STAGE = "ffmpeg"
TERMINAL_VIDEO_STATUSES = {"completed", "failed"}
STATE_SCHEMA_VERSION = 2
PAUSED_EXIT_CODE = 10
CLIP_PROGRESS_DEFAULTS = {
    "progress_pct": 0,
    "message": None,
    "last_progress_at": None,
    "clips_total": 0,
    "clips_completed": 0,
    "clips_created": 0,
    "clips_failed": 0,
    "clips_skipped": 0,
    "clips_blocked": 0,
    "clips_scored": 0,
    "modules_accepted": 0,
    "modules_existing": 0,
    "modules_rejected": 0,
    "last_clip_id": None,
    "last_clip_status": None,
    "last_event": None,
    "manifest_path": None,
    "render_state_path": None,
    "active_clip_renders": 0,
    "render_paused": False,
}


@dataclass(frozen=True)
class StageJob:
    video_path: str
    stage: str


class QueuePaused(RuntimeError):
    pass


class _RuntimeConfig:
    def __init__(self, base, overrides: dict | None = None):
        self._base = base
        self._overrides = dict(overrides or {})

    def __getattr__(self, name: str):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)


def _build_versioned_stem(stem: str, tag: str | None) -> str:
    if not tag:
        return stem
    safe_tag = re.sub(r'[<>:"/\\|?*\n\r]', '', str(tag)).strip()
    safe_tag = re.sub(r"\s+", "_", safe_tag)
    if not safe_tag:
        return stem
    separator = "" if safe_tag.startswith("_") else "__"
    return f"{stem}{separator}{safe_tag}"


class VideoQueueRunner:
    def __init__(
        self,
        input_dir: str,
        state_path: str,
        max_retries: int,
        max_inflight_videos: int,
        ffmpeg_max_parallel_clips: int | None = None,
        max_clips: int | None = None,
        min_score: float | None = None,
        force_rescore: bool = False,
        force_modules: bool = False,
        output_tag: str | None = None,
        working_tag: str | None = None,
        poll_interval: float = 2.0,
        scan_interval: float | None = None,
        stable_seconds: float | None = None,
        control_path: str | None = None,
        yolo_in_subprocess: bool | None = None,
        retry_failed: bool = False,
    ) -> None:
        import config as cfg

        self.cfg = cfg
        self.input_dir = Path(input_dir)
        self.state_path = Path(state_path)
        self.max_retries = max(0, int(max_retries))
        self.max_active_analysis_videos = max(1, int(max_inflight_videos))
        self.ffmpeg_max_parallel_clips = (
            max(1, int(ffmpeg_max_parallel_clips))
            if ffmpeg_max_parallel_clips is not None
            else None
        )
        self.max_clips = max(1, int(max_clips)) if max_clips else None
        self.min_score = float(min_score) if min_score is not None else None
        self.force_rescore = bool(force_rescore)
        self.force_modules = bool(force_modules)
        self.poll_interval = max(0.5, float(poll_interval))
        if scan_interval is None:
            scan_interval = getattr(
                cfg,
                "QUEUE_RESCAN_INTERVAL_SECONDS",
                getattr(cfg, "QUEUE_SCAN_INTERVAL_SECONDS", 300.0),
            )
        if stable_seconds is None:
            stable_seconds = getattr(cfg, "QUEUE_STABLE_SECONDS", 60.0)
        self.scan_interval = max(self.poll_interval, float(scan_interval))
        self.stable_seconds = max(0.0, float(stable_seconds))
        self.control_path = str(control_path or getattr(
            cfg,
            "QUEUE_CONTROL_FILE",
            str(Path(getattr(cfg, "WORKING_DIR", "working")) / "queue_control.json"),
        ))
        if yolo_in_subprocess is None:
            yolo_in_subprocess = getattr(cfg, "QUEUE_YOLO_IN_SUBPROCESS", True)
        self.yolo_in_subprocess = bool(yolo_in_subprocess)
        self.retry_failed = bool(retry_failed)
        self.output_tag = output_tag
        self.working_tag = working_tag
        self.state_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.state = self._load_state()
        self.job_counter = 0
        self.active_video_keys: set[str] = set()
        self._file_observations: dict[str, tuple[int, float, float]] = {}
        self.queues = {
            "gpu": PriorityQueue(),
            "yolo": Queue(),
            "ffmpeg": Queue(),
        }
        self.workers: list[threading.Thread] = []
        self._install_thread_exception_hook()

    def run(self) -> int:
        if not self.input_dir.exists():
            raise FileNotFoundError(f"Input folder not found: {self.input_dir}")

        videos = self._discover_videos()
        if not videos:
            log.info(f"No stable supported videos found in {self.input_dir}; waiting for new VODs")

        self._sync_videos(videos)
        if self.retry_failed:
            with self.state_lock:
                self._reset_failed_active_videos_locked()
                self._save_state_locked()
        if self._pause_requested():
            with self.state_lock:
                self._mark_queue_paused_locked()
            return PAUSED_EXIT_CODE
        self._start_workers()
        with self.state_lock:
            self.state["queue_status"] = "running"
            self._schedule_locked("bootstrap")

        try:
            last_scan = 0.0
            while True:
                with self.state_lock:
                    done = self._all_videos_terminal_locked()
                if done:
                    break

                now = time.time()
                if now - last_scan >= self.scan_interval:
                    last_scan = now
                    self._sync_videos(
                        self._discover_videos(),
                        refresh_existing_from_disk=False,
                    )
                    with self.state_lock:
                        self._schedule_locked("rescan")

                if self._pause_requested():
                    with self.state_lock:
                        if not self._has_active_work_locked():
                            self._mark_queue_paused_locked()
                            return PAUSED_EXIT_CODE
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            log.warning("Interrupted by user. Current progress is already saved to JSON.")
            self.stop_event.set()
            return 1
        except BaseException as exc:
            log.error(f"Queue main loop crashed: {exc}\n{traceback.format_exc()}")
            self.stop_event.set()
            return 2
        finally:
            self._stop_workers()

        with self.state_lock:
            self.state["queue_status"] = "completed"
            self._save_state_locked()
            active_video_keys = self._active_video_keys_locked()
            completed = sum(
                1
                for video_path, v in self.state["videos"].items()
                if video_path in active_video_keys and v["status"] == "completed"
            )
            failed = sum(
                1
                for video_path, v in self.state["videos"].items()
                if video_path in active_video_keys and v["status"] == "failed"
            )
        log.info("=" * 70)
        log.info(f"Queue finished | completed={completed} | failed={failed}")
        log.info(f"State file: {self.state_path}")
        log.info("=" * 70)
        return 0 if failed == 0 else 2

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except Exception as exc:
                log.warning(f"Ignoring unreadable queue state {self.state_path}: {exc}")
            else:
                if isinstance(state.get("videos"), dict):
                    return self._migrate_state(state)

        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "created_at": self._now_iso(),
            "updated_at": self._now_iso(),
            "input_dir": str(self.input_dir),
            "queue_status": "idle",
            "control_file": self.control_path,
            "videos": {},
        }

    def _migrate_state(self, state: dict) -> dict:
        state.setdefault("created_at", self._now_iso())
        state.setdefault("updated_at", self._now_iso())
        state.setdefault("input_dir", str(self.input_dir))
        state.setdefault("queue_status", "idle")
        state.setdefault("control_file", self.control_path)
        state.setdefault("videos", {})
        for entry in state["videos"].values():
            if isinstance(entry, dict):
                entry.setdefault("run_history", [])
                self._ensure_stage_shapes(entry)
        state["schema_version"] = STATE_SCHEMA_VERSION
        return state

    def _install_thread_exception_hook(self) -> None:
        previous_hook = getattr(threading, "excepthook", None)

        def _hook(args):
            try:
                log.error(
                    f"[{args.thread.name}] Thread crashed with {args.exc_type.__name__}: {args.exc_value}\n"
                    f"{''.join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))}"
                )
            finally:
                if previous_hook is not None:
                    previous_hook(args)

        try:
            threading.excepthook = _hook
        except Exception:
            pass

    def _save_state_locked(self) -> None:
        self.state["updated_at"] = self._now_iso()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        for attempt in range(1, 4):
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(self.state, f, ensure_ascii=False, indent=2)
                os.replace(temp_path, self.state_path)
                return
            except Exception as exc:
                if attempt >= 3:
                    log.exception(f"Failed to persist queue state to {self.state_path}: {exc}")
                    raise
                time.sleep(0.2 * attempt)

    def _discover_videos(self) -> list[Path]:
        videos = []
        now = time.time()
        for path in sorted(self.input_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            key = str(path.resolve())
            observation = self._file_observations.get(key)
            current = (int(stat.st_size), float(stat.st_mtime))
            if observation and observation[0] == current[0] and observation[1] == current[1]:
                first_seen = observation[2]
            else:
                first_seen = now
                self._file_observations[key] = (current[0], current[1], first_seen)

            old_enough = now - float(stat.st_mtime) >= self.stable_seconds
            observed_stable = now - first_seen >= self.stable_seconds
            if old_enough or observed_stable:
                videos.append(path)

        log.debug(
            f"Discovered {len(videos)} stable video(s) in {self.input_dir} "
            f"(stable_seconds={self.stable_seconds:g})"
        )
        return videos

    def _sync_videos(self, videos: list[Path], refresh_existing_from_disk: bool = True) -> None:
        with self.state_lock:
            known = self.state["videos"]
            previous_active_keys = set(self.active_video_keys)
            stable_keys = {str(video.resolve()) for video in videos}
            newly_stable_names = [
                video.name
                for video in videos
                if str(video.resolve()) not in previous_active_keys
            ]
            self.active_video_keys = stable_keys
            for video in videos:
                key = str(video.resolve())
                working_dir, output_dir = self._video_dirs(video)
                entry = known.get(key)
                if entry is None:
                    entry = self._new_video_entry(video, working_dir, output_dir)
                    known[key] = entry
                else:
                    rerun_target_changed = (
                        entry.get("working_dir") != str(working_dir)
                        or entry.get("output_dir") != str(output_dir)
                        or entry.get("working_tag") != self.working_tag
                        or entry.get("output_tag") != self.output_tag
                    )
                    entry["name"] = video.name
                    if rerun_target_changed:
                        self._reset_entry_for_new_run(entry)
                    entry["working_dir"] = str(working_dir)
                    entry["output_dir"] = str(output_dir)
                    entry["working_tag"] = self.working_tag
                    entry["output_tag"] = self.output_tag
                    self._ensure_stage_shapes(entry)
                    if refresh_existing_from_disk or rerun_target_changed:
                        self._refresh_stage_status_from_disk(entry)

            if newly_stable_names:
                preview = ", ".join(newly_stable_names[:5])
                suffix = "" if len(newly_stable_names) <= 5 else f", +{len(newly_stable_names) - 5} more"
                log.info(
                    "New stable video(s) discovered in %s: %s%s",
                    self.input_dir,
                    preview,
                    suffix,
                )
            self._save_state_locked()

    def _reset_failed_active_videos_locked(self) -> int:
        active_video_keys = self._active_video_keys_locked()
        reset_count = 0
        for video_path in sorted(active_video_keys):
            entry = self.state["videos"].get(video_path)
            if not entry or entry.get("status") != "failed":
                continue

            failed_index = None
            for index, stage in enumerate(STAGES):
                if entry["stages"][stage].get("status") == "failed":
                    failed_index = index
                    break
            if failed_index is None:
                failed_index = 0

            for stage in STAGES[failed_index:]:
                entry["stages"][stage] = self._new_stage_entry()

            entry["status"] = "queued"
            entry["current_stage"] = None
            entry["failed_at"] = None
            entry["completed_at"] = None
            reset_count += 1

        if reset_count:
            log.info(f"Reset {reset_count} failed active video(s) for retry")
        return reset_count

    def _start_workers(self) -> None:
        worker_specs = [
            ("gpu-worker", self.queues["gpu"]),
            ("yolo-worker", self.queues["yolo"]),
            ("ffmpeg-worker", self.queues["ffmpeg"]),
        ]
        for name, queue_obj in worker_specs:
            thread = threading.Thread(
                target=self._worker_loop,
                name=name,
                args=(name, queue_obj),
                daemon=False,
            )
            thread.start()
            self.workers.append(thread)

    def _stop_workers(self) -> None:
        self.stop_event.set()
        for queue_name, queue_obj in self.queues.items():
            queue_obj.put(self._make_queue_payload(queue_name, None))
        for thread in self.workers:
            thread.join(timeout=5.0)

    def _worker_loop(self, worker_name: str, queue_obj: Queue) -> None:
        while not self.stop_event.is_set():
            try:
                payload = queue_obj.get(timeout=0.5)
            except Empty:
                continue

            _, _, job = payload
            if job is None:
                queue_obj.task_done()
                break

            try:
                self._run_job(worker_name, job)
            except BaseException as exc:
                log.error(
                    f"[{worker_name}] Unhandled queue error for {Path(job.video_path).name} "
                    f"stage={job.stage}: {exc}\n{traceback.format_exc()}"
                )
                self._mark_job_crashed(job, exc)
            finally:
                queue_obj.task_done()

    def _run_job(self, worker_name: str, job: StageJob) -> None:
        with self.state_lock:
            entry = self.state["videos"].get(job.video_path)
            if entry is None:
                return
            stage_state = entry["stages"][job.stage]
            if self._pause_requested():
                stage_state["queued"] = False
                if stage_state.get("status") == "queued":
                    stage_state["status"] = "pending"
                if entry.get("status") == "queued":
                    entry["current_stage"] = None
                self._save_state_locked()
                return
            if stage_state["status"] == "done":
                return
            if entry["status"] in TERMINAL_VIDEO_STATUSES:
                return

            attempt = int(stage_state.get("attempts", 0)) + 1
            stage_state["attempts"] = attempt
            stage_state["queued"] = False
            stage_state["status"] = "running"
            stage_state["started_at"] = self._now_iso()
            stage_state["last_error"] = None
            if job.stage == EDIT_STAGE:
                self._reset_clip_progress_locked(stage_state)
            entry["status"] = "running"
            entry["current_stage"] = job.stage
            if job.stage in {"yolo", EDIT_STAGE}:
                self._schedule_locked(f"{job.stage}-start")
            self._save_state_locked()

        start = time.perf_counter()
        log.info(
            f"[{worker_name}] ENTER {job.stage.upper()} | {Path(job.video_path).name} | "
            f"attempt {attempt}/{self.max_retries + 1}"
        )

        try:
            self._execute_stage(job)
        except QueuePaused as exc:
            duration = time.perf_counter() - start
            self._handle_stage_paused(job, duration, exc)
            return
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            duration = time.perf_counter() - start
            self._handle_stage_failure(job, duration, exc)
            return

        duration = time.perf_counter() - start
        with self.state_lock:
            entry = self.state["videos"][job.video_path]
            stage_state = entry["stages"][job.stage]
            stage_state["status"] = "done"
            stage_state["finished_at"] = self._now_iso()
            stage_state["duration_sec"] = round(duration, 3)
            stage_state["last_error"] = None
            entry["current_stage"] = None
            next_stage = self._next_stage_locked(entry)
            if next_stage is None:
                entry["status"] = "completed"
                entry["completed_at"] = self._now_iso()
            else:
                entry["status"] = "queued"
            self._schedule_locked(f"{job.stage}-complete")
            self._save_state_locked()

        log.info(
            f"[{worker_name}] EXIT  {job.stage.upper()} | {Path(job.video_path).name} | "
            f"took {self._fmt_time(duration)}"
        )

    def _handle_stage_failure(self, job: StageJob, duration: float, exc: Exception) -> None:
        error_text = f"{type(exc).__name__}: {exc}"
        retry_job = False

        with self.state_lock:
            entry = self.state["videos"][job.video_path]
            stage_state = entry["stages"][job.stage]
            attempts = int(stage_state.get("attempts", 0))
            stage_state["status"] = "failed"
            stage_state["finished_at"] = self._now_iso()
            stage_state["duration_sec"] = round(duration, 3)
            stage_state["last_error"] = error_text
            entry["current_stage"] = None

            if attempts <= self.max_retries:
                retry_job = True
                entry["status"] = "queued"
                self._enqueue_stage_locked(job.video_path, job.stage, reason="retry")
            else:
                entry["status"] = "failed"
                entry["failed_at"] = self._now_iso()
                self._mark_downstream_pending_skipped_locked(entry, job.stage)
                self._schedule_locked(f"{job.stage}-failed")
            self._save_state_locked()

        if retry_job:
            log.error(
                f"Stage failed, retrying {job.stage} for {Path(job.video_path).name} "
                f"after {self._fmt_time(duration)} | {error_text}"
            )
        else:
            log.error(
                f"Stage failed permanently: {job.stage} | {Path(job.video_path).name} | "
                f"after {self._fmt_time(duration)} | {error_text}"
            )

    def _handle_stage_paused(self, job: StageJob, duration: float, exc: Exception) -> None:
        with self.state_lock:
            entry = self.state["videos"][job.video_path]
            stage_state = entry["stages"][job.stage]
            stage_state["status"] = "paused"
            stage_state["queued"] = False
            stage_state["finished_at"] = self._now_iso()
            stage_state["duration_sec"] = round(duration, 3)
            stage_state["last_error"] = None
            if job.stage == EDIT_STAGE:
                stage_state["render_paused"] = True
                stage_state["active_clip_renders"] = 0
            entry["current_stage"] = None
            entry["status"] = "paused"
            self._mark_queue_paused_locked(save=False)
            self._save_state_locked()

        log.info(
            f"Stage paused: {job.stage} | {Path(job.video_path).name} | "
            f"after {self._fmt_time(duration)} | {exc}"
        )

    def _mark_downstream_pending_skipped_locked(self, entry: dict, failed_stage: str) -> None:
        try:
            failed_index = STAGES.index(failed_stage)
        except ValueError:
            return
        for stage in STAGES[failed_index + 1:]:
            stage_state = entry["stages"].get(stage)
            if not isinstance(stage_state, dict):
                continue
            if stage_state.get("status") in {"pending", "queued", "running", "paused"}:
                stage_state["status"] = "skipped"
                stage_state["queued"] = False
                stage_state["finished_at"] = stage_state.get("finished_at") or self._now_iso()

    def _mark_job_crashed(self, job: StageJob, exc: Exception) -> None:
        with self.state_lock:
            entry = self.state["videos"].get(job.video_path)
            if entry is None:
                return
            stage_state = entry["stages"].get(job.stage)
            if stage_state is None:
                return
            stage_state["queued"] = False
            stage_state["status"] = "failed"
            stage_state["finished_at"] = self._now_iso()
            stage_state["last_error"] = f"{type(exc).__name__}: {exc}"
            entry["current_stage"] = None
            entry["status"] = "failed"
            entry["failed_at"] = self._now_iso()
            self._mark_downstream_pending_skipped_locked(entry, job.stage)
            self._save_state_locked()

    def _execute_stage(self, job: StageJob) -> None:
        if job.stage == "transcribe":
            self._stage_transcribe(job.video_path)
            return
        if job.stage == "llm":
            self._stage_llm(job.video_path)
            return
        if job.stage == "yolo":
            self._stage_yolo(job.video_path)
            return
        if job.stage == "ffmpeg":
            self._stage_ffmpeg(job.video_path)
            return
        raise ValueError(f"Unknown stage: {job.stage}")

    def _stage_transcribe(self, video_path: str) -> None:
        self._run_stage_subprocess("transcribe", video_path)

    def _stage_llm(self, video_path: str) -> None:
        from moment_detector import detect_moments
        from transcriber import build_text_chunks, load_cached_transcript, transcript_cache_is_compatible

        stage_cfg = self._stage_cache_cfg("llm")
        working_dir, _ = self._video_dirs(Path(video_path))
        transcript = load_cached_transcript(str(working_dir))
        if transcript is None or not transcript_cache_is_compatible(transcript, stage_cfg):
            raise RuntimeError("Transcript cache missing or outdated before LLM stage")

        chunks = build_text_chunks(transcript, stage_cfg.CHUNK_DURATION, stage_cfg.CHUNK_OVERLAP)
        detect_moments(chunks, str(working_dir), stage_cfg)
        write_stage_fingerprint(working_dir / "moments.json", video_path, stage_cfg, "llm")

    def _stage_yolo(self, video_path: str) -> None:
        if self.yolo_in_subprocess:
            self._run_stage_subprocess("yolo", video_path)
            return
        self._stage_yolo_inline(video_path)

    def _stage_yolo_inline(self, video_path: str) -> None:
        from vision_scanner import build_scan_ranges_from_moments, scan_video_for_products

        stage_cfg = self._stage_cache_cfg("yolo")
        working_dir, _ = self._video_dirs(Path(video_path))
        moments_path = working_dir / "moments.json"
        if not moments_path.exists():
            raise RuntimeError("Moments cache missing before YOLO stage")

        with open(moments_path, "r", encoding="utf-8") as f:
            moments = json.load(f)

        scan_ranges = build_scan_ranges_from_moments(moments, stage_cfg)
        scan_video_for_products(
            video_path,
            str(working_dir),
            stage_cfg,
            scan_ranges=scan_ranges,
        )
        write_stage_fingerprint(
            working_dir / "product_detections.json",
            video_path,
            stage_cfg,
            "yolo",
            extra={"scan_ranges": scan_ranges},
        )

    def _stage_ffmpeg(self, video_path: str) -> None:
        from main import run_pipeline
        try:
            from main import PipelinePaused
        except Exception:
            PipelinePaused = QueuePaused

        original_max_parallel_clips = getattr(self.cfg, "MAX_PARALLEL_CLIPS", None)
        original_ffmpeg_priority_flag = os.environ.get("PROYA_QUEUE_FFMPEG_BELOW_NORMAL")
        if self.ffmpeg_max_parallel_clips is not None:
            log.info(
                "Queue FFmpeg throttle active: "
                f"MAX_PARALLEL_CLIPS {original_max_parallel_clips} -> {self.ffmpeg_max_parallel_clips}"
            )
            self.cfg.MAX_PARALLEL_CLIPS = self.ffmpeg_max_parallel_clips
            os.environ["PROYA_QUEUE_FFMPEG_BELOW_NORMAL"] = "1"

        try:
            try:
                run_pipeline(
                    video_path=video_path,
                    skip_transcribe=True,
                    skip_moments=True,
                    skip_vision=True,
                    cut_only=False,
                    max_clips=self.max_clips,
                    min_score=self.min_score,
                    force_rescore=self.force_rescore,
                    force_modules=self.force_modules,
                    output_tag=self.output_tag,
                    working_tag=self.working_tag,
                    control_path=self.control_path,
                    progress_callback=lambda stage, pct, message, **payload: self._handle_ffmpeg_progress(
                        video_path,
                        stage,
                        pct,
                        message,
                        **payload,
                    ),
                )
            except PipelinePaused as exc:
                raise QueuePaused(str(exc)) from exc
        finally:
            if self.ffmpeg_max_parallel_clips is not None and original_max_parallel_clips is not None:
                self.cfg.MAX_PARALLEL_CLIPS = original_max_parallel_clips
            if original_ffmpeg_priority_flag is None:
                os.environ.pop("PROYA_QUEUE_FFMPEG_BELOW_NORMAL", None)
            else:
                os.environ["PROYA_QUEUE_FFMPEG_BELOW_NORMAL"] = original_ffmpeg_priority_flag

    def _handle_ffmpeg_progress(self, video_path: str, stage: str, pct: int, message: str, **payload) -> None:
        with self.state_lock:
            entry = self.state["videos"].get(video_path)
            if entry is None:
                return

            stage_state = entry["stages"].setdefault(EDIT_STAGE, self._new_stage_entry())
            stage_state["progress_pct"] = self._coerce_nonnegative_int(pct)
            stage_state["message"] = message
            stage_state["last_progress_at"] = self._now_iso()

            for field in (
                "clips_total",
                "clips_completed",
                "clips_created",
                "clips_failed",
                "clips_skipped",
                "clips_blocked",
                "clips_scored",
                "modules_accepted",
                "modules_existing",
                "modules_rejected",
                "active_clip_renders",
            ):
                if field in payload:
                    stage_state[field] = self._coerce_nonnegative_int(payload.get(field))

            if payload.get("clip_id"):
                stage_state["last_clip_id"] = str(payload["clip_id"])
            if payload.get("clip_status"):
                stage_state["last_clip_status"] = str(payload["clip_status"])
            if payload.get("event"):
                stage_state["last_event"] = str(payload["event"])
            if payload.get("manifest_path"):
                stage_state["manifest_path"] = str(payload["manifest_path"])
            if payload.get("render_state_path"):
                stage_state["render_state_path"] = str(payload["render_state_path"])
            if "render_paused" in payload:
                stage_state["render_paused"] = bool(payload.get("render_paused"))
            if payload.get("output_dir"):
                entry["output_dir"] = str(payload["output_dir"])

            if entry.get("status") not in TERMINAL_VIDEO_STATUSES:
                entry["status"] = "running"
                entry["current_stage"] = EDIT_STAGE

            self._save_state_locked()

    def _reset_clip_progress_locked(self, stage_state: dict) -> None:
        for key, value in CLIP_PROGRESS_DEFAULTS.items():
            stage_state[key] = value
        stage_state["last_event"] = None

    def _pause_requested(self) -> bool:
        return queue_control.pause_requested(self.control_path)

    def _mark_queue_paused_locked(self, save: bool = True) -> None:
        self.state["queue_status"] = "paused"
        self.state["paused_at"] = self._now_iso()
        if save:
            self._save_state_locked()

    def _has_active_work_locked(self) -> bool:
        if any(not queue.empty() for queue in self.queues.values()):
            return True
        for video_path in self._active_video_keys_locked():
            entry = self.state["videos"].get(video_path)
            if not entry:
                continue
            for stage_state in entry.get("stages", {}).values():
                if not isinstance(stage_state, dict):
                    continue
                if stage_state.get("status") in {"running", "queued"}:
                    return True
                try:
                    if int(stage_state.get("active_clip_renders") or 0) > 0:
                        return True
                except (TypeError, ValueError):
                    pass
        return False

    @staticmethod
    def _coerce_nonnegative_int(value) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    def _run_stage_subprocess(self, stage: str, video_path: str) -> None:
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--run-stage",
            stage,
            "--video-path",
            video_path,
        ]
        if self.output_tag:
            cmd.extend(["--output-tag", self.output_tag])
        if self.working_tag:
            cmd.extend(["--working-tag", self.working_tag])
        if self.max_clips:
            cmd.extend(["--max-clips", str(self.max_clips)])
        if self.min_score is not None:
            cmd.extend(["--min-score", str(self.min_score)])
        if self.force_rescore:
            cmd.append("--force-rescore")
        if self.force_modules:
            cmd.append("--force-modules")
        log.info(f"Launching isolated subprocess for {stage}: {Path(video_path).name}")
        completed = subprocess.run(
            cmd,
            cwd=str(Path(__file__).resolve().parent),
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"Isolated {stage} subprocess failed for {Path(video_path).name} "
                f"with exit code {completed.returncode}"
            )

    def _video_dirs(self, video_path: Path) -> tuple[Path, Path]:
        stem = video_path.stem
        working_dir = Path(self.cfg.WORKING_DIR) / _build_versioned_stem(stem, self.working_tag)
        output_dir = Path(self.cfg.OUTPUT_DIR) / _build_versioned_stem(stem, self.output_tag)
        return working_dir, output_dir

    def _new_video_entry(self, video_path: Path, working_dir: Path, output_dir: Path) -> dict:
        return {
            "name": video_path.name,
            "path": str(video_path.resolve()),
            "working_dir": str(working_dir),
            "output_dir": str(output_dir),
            "working_tag": self.working_tag,
            "output_tag": self.output_tag,
            "run_history": [],
            "status": "queued",
            "current_stage": None,
            "created_at": self._now_iso(),
            "completed_at": None,
            "failed_at": None,
            "stages": {
                stage: self._new_stage_entry() for stage in STAGES
            },
        }

    def _new_stage_entry(self) -> dict:
        stage_entry = {
            "status": "pending",
            "attempts": 0,
            "started_at": None,
            "finished_at": None,
            "duration_sec": None,
            "last_error": None,
            "queued": False,
        }
        stage_entry.update(CLIP_PROGRESS_DEFAULTS)
        return stage_entry

    def _ensure_stage_shapes(self, entry: dict) -> None:
        entry.setdefault("run_history", [])
        stages = entry.setdefault("stages", {})
        for stage in STAGES:
            if stage not in stages:
                stages[stage] = self._new_stage_entry()
            else:
                stages[stage].setdefault("status", "pending")
                stages[stage].setdefault("attempts", 0)
                stages[stage].setdefault("started_at", None)
                stages[stage].setdefault("finished_at", None)
                stages[stage].setdefault("duration_sec", None)
                stages[stage].setdefault("last_error", None)
                stages[stage].setdefault("queued", False)
                for key, value in CLIP_PROGRESS_DEFAULTS.items():
                    stages[stage].setdefault(key, value)

    def _reset_entry_for_new_run(self, entry: dict) -> None:
        self._archive_current_run(entry)
        entry["status"] = "queued"
        entry["current_stage"] = None
        entry["created_at"] = self._now_iso()
        entry["completed_at"] = None
        entry["failed_at"] = None
        entry["stages"] = {stage: self._new_stage_entry() for stage in STAGES}

    def _archive_current_run(self, entry: dict) -> None:
        history = entry.setdefault("run_history", [])
        if not self._entry_has_meaningful_progress(entry):
            return

        snapshot = {
            "working_dir": entry.get("working_dir"),
            "output_dir": entry.get("output_dir"),
            "working_tag": entry.get("working_tag"),
            "output_tag": entry.get("output_tag"),
            "status": entry.get("status"),
            "current_stage": entry.get("current_stage"),
            "created_at": entry.get("created_at"),
            "completed_at": entry.get("completed_at"),
            "failed_at": entry.get("failed_at"),
            "stages": copy.deepcopy(entry.get("stages", {})),
            "archived_at": self._now_iso(),
        }
        history.append(snapshot)

    def _entry_has_meaningful_progress(self, entry: dict) -> bool:
        stages = entry.get("stages", {})
        if entry.get("status") in {"completed", "failed"}:
            return True
        for stage_state in stages.values():
            if stage_state.get("status") in {"done", "running", "queued", "failed"}:
                return True
            if int(stage_state.get("attempts", 0)) > 0:
                return True
        return False

    def _refresh_stage_status_from_disk(self, entry: dict) -> None:
        working_dir = Path(entry["working_dir"])
        output_dir = Path(entry["output_dir"])
        cache_checks = {
            "transcribe": working_dir / "transcript.json",
            "llm": working_dir / "moments.json",
            "yolo": working_dir / "product_detections.json",
            "ffmpeg": output_dir / "manifest.json",
        }

        stages = entry["stages"]
        for stage, path in cache_checks.items():
            stage_state = stages[stage]
            stage_state["queued"] = False
            if self._stage_output_current(entry, stage, path):
                stage_state["status"] = "done"
                stage_state["finished_at"] = stage_state.get("finished_at") or self._now_iso()
                continue
            if stage_state["status"] in {"queued", "running"}:
                stage_state["status"] = "pending"
            if stage_state["status"] == "done":
                stage_state["status"] = "pending"
                stage_state["queued"] = False
                stage_state["finished_at"] = None
                stage_state["duration_sec"] = None
                stage_state["last_error"] = None

        if stages["ffmpeg"]["status"] == "done":
            entry["status"] = "completed"
        elif any(stages[s]["status"] == "running" for s in STAGES):
            entry["status"] = "queued"
        elif entry.get("status") == "completed" and stages["ffmpeg"]["status"] != "done":
            entry["status"] = "queued"
        elif entry.get("status") not in TERMINAL_VIDEO_STATUSES:
            entry["status"] = "queued"

    def _stage_output_current(self, entry: dict, stage: str, path: Path) -> bool:
        video_path = entry.get("path") or entry.get("video_path")
        if not video_path or not path.exists():
            return False
        stage_cfg = self._stage_cache_cfg(stage)
        try:
            if stage == "transcribe":
                from transcriber import load_cached_transcript, transcript_cache_is_compatible

                transcript = load_cached_transcript(str(path.parent))
                return bool(
                    transcript
                    and transcript_cache_is_compatible(transcript, stage_cfg)
                    and stage_fingerprint_matches(path, video_path, stage_cfg, "transcribe")
                )
            if stage == "llm":
                from moment_detector import _cached_moments_are_current

                with open(path, "r", encoding="utf-8") as f:
                    moments = json.load(f)
                return bool(
                    _cached_moments_are_current(moments)
                    and stage_fingerprint_matches(path, video_path, stage_cfg, "llm")
                )
            if stage == "yolo":
                from vision_scanner import (
                    _is_valid_cached_events,
                    _vision_cache_fingerprint_matches,
                    build_scan_ranges_from_moments,
                )

                with open(path, "r", encoding="utf-8") as f:
                    events = json.load(f)
                if not _is_valid_cached_events(events):
                    return False
                moments_path = Path(entry["working_dir"]) / "moments.json"
                if not moments_path.exists():
                    return False
                with open(moments_path, "r", encoding="utf-8") as f:
                    moments = json.load(f)
                scan_ranges = build_scan_ranges_from_moments(moments, stage_cfg)
                return bool(
                    _vision_cache_fingerprint_matches(path, video_path, stage_cfg, scan_ranges)
                    and stage_fingerprint_matches(
                        path,
                        video_path,
                        stage_cfg,
                        "yolo",
                        extra={"scan_ranges": scan_ranges},
                    )
                )
            if stage == "ffmpeg":
                if self.force_rescore or self.force_modules:
                    return False
                with open(path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                valid_manifest = isinstance(manifest, list) or (
                    isinstance(manifest, dict)
                    and isinstance(manifest.get("clips") or manifest.get("items"), list)
                )
                return bool(
                    valid_manifest
                    and stage_fingerprint_matches(
                        path,
                        video_path,
                        stage_cfg,
                        "ffmpeg",
                        extra={"max_clips": self.max_clips, "cut_only": False},
                    )
                )
        except Exception as exc:
            log.warning(f"Ignoring invalid {stage} cache for {Path(video_path).name}: {exc}")
            return False
        return False

    def _stage_cache_cfg(self, stage: str):
        overrides = {}
        if stage in {"llm", EDIT_STAGE} and self.min_score is not None:
            overrides["MIN_SCORE"] = self.min_score
        if stage == EDIT_STAGE:
            overrides.update(
                {
                    "MODULE_ASSEMBLY_ENABLED": False,
                    "MODULE_ASSEMBLY_RENDER_LIMIT": 0,
                    "MODULE_PRODUCT_ZOOM_ENABLED": False,
                }
            )
            if self.ffmpeg_max_parallel_clips is not None:
                overrides["MAX_PARALLEL_CLIPS"] = self.ffmpeg_max_parallel_clips
            if self.force_rescore:
                overrides["SCORER_FORCE_RESCORE"] = True
        if not overrides:
            return self.cfg
        return _RuntimeConfig(self.cfg, overrides)

    def _next_stage_locked(self, entry: dict) -> Optional[str]:
        for stage in STAGES:
            if entry["stages"][stage]["status"] != "done":
                return stage
        return None

    def _schedule_locked(self, reason: str) -> None:
        if self._pause_requested():
            return
        active_video_keys = self._active_video_keys_locked()
        ordered_items = sorted(
            (
                (video_path, entry)
                for video_path, entry in self.state["videos"].items()
                if video_path in active_video_keys
            ),
            key=lambda item: (self._stage_priority_for_video(item[1]), item[1]["name"]),
        )

        # First advance anything that has already entered the pipeline. Once a
        # video moves beyond the GPU stages, it no longer holds an analysis
        # slot, so YOLO/FFmpeg can run while the next source video transcribes.
        for video_path, entry in ordered_items:
            if entry["status"] in TERMINAL_VIDEO_STATUSES:
                continue
            if not self._video_has_pipeline_progress(entry):
                continue
            next_stage = self._next_stage_locked(entry)
            if next_stage and self._stage_ready_locked(entry, next_stage):
                self._enqueue_stage_locked(video_path, next_stage, reason=reason)

        active_analysis = sum(
            1
            for video_path, entry in self.state["videos"].items()
            if video_path in active_video_keys
            and entry["status"] not in TERMINAL_VIDEO_STATUSES
            and self._video_is_active_analysis(entry)
        )

        if active_analysis >= self.max_active_analysis_videos:
            return

        # Backfill the GPU analysis lane with fresh videos only. YOLO and
        # FFmpeg queued/running videos are deliberately ignored here because
        # they have their own workers.
        for video_path, entry in ordered_items:
            if active_analysis >= self.max_active_analysis_videos:
                break
            if entry["status"] in TERMINAL_VIDEO_STATUSES:
                continue
            if self._video_has_pipeline_progress(entry):
                continue
            next_stage = self._next_stage_locked(entry)
            if next_stage == "transcribe" and self._stage_ready_locked(entry, next_stage):
                self._enqueue_stage_locked(video_path, next_stage, reason=reason)
                active_analysis += 1

    def _video_has_pipeline_progress(self, entry: dict) -> bool:
        for stage in STAGES:
            stage_state = entry["stages"][stage]
            if self._stage_has_progress(stage_state):
                return True
        return False

    def _video_is_active_analysis(self, entry: dict) -> bool:
        for stage in GPU_ANALYSIS_STAGES:
            stage_state = entry["stages"][stage]
            if stage_state.get("status") != "done" and self._stage_has_progress(stage_state):
                return True
        return False

    @staticmethod
    def _stage_has_progress(stage_state: dict) -> bool:
        return (
            stage_state.get("status") in {"queued", "running", "done", "failed"}
            or int(stage_state.get("attempts", 0)) > 0
        )

    def _stage_ready_locked(self, entry: dict, stage: str) -> bool:
        stage_state = entry["stages"][stage]
        if stage_state["status"] in {"done", "queued", "running"}:
            return False
        stage_index = STAGES.index(stage)
        for prev_stage in STAGES[:stage_index]:
            if entry["stages"][prev_stage]["status"] != "done":
                return False
        return True

    def _stage_priority_for_video(self, entry: dict) -> int:
        next_stage = self._next_stage_locked(entry)
        if next_stage is None:
            return 999
        # Push videos that are closer to completion first so FFmpeg/YOLO keep
        # moving while newer videos wait their turn for transcription.
        return -STAGES.index(next_stage)

    def _enqueue_stage_locked(self, video_path: str, stage: str, reason: str) -> None:
        entry = self.state["videos"][video_path]
        stage_state = entry["stages"][stage]
        if (
            stage_state.get("queued")
            or stage_state.get("status") in {"queued", "running"}
            or entry.get("current_stage") == stage
        ):
            log.debug(
                "Skip duplicate enqueue for %s stage=%s reason=%s status=%s queued=%s current_stage=%s",
                Path(video_path).name,
                stage,
                reason,
                stage_state.get("status"),
                stage_state.get("queued"),
                entry.get("current_stage"),
            )
            return
        if stage_state["status"] == "done":
            return

        queue_name = self._queue_name_for_stage(stage)
        if self._stage_job_already_queued(queue_name, video_path, stage):
            stage_state["queued"] = True
            if stage_state["status"] == "pending":
                stage_state["status"] = "queued"
            log.debug(
                "Skip duplicate enqueue already present in %s queue for %s stage=%s reason=%s",
                queue_name,
                Path(video_path).name,
                stage,
                reason,
            )
            return
        self.queues[queue_name].put(
            self._make_queue_payload(queue_name, StageJob(video_path=video_path, stage=stage))
        )
        stage_state["queued"] = True
        if stage_state["status"] == "pending":
            stage_state["status"] = "queued"
        entry["status"] = "queued"
        self._save_state_locked()

        log.info(
            f"ENQUEUE {stage.upper():10s} | {Path(video_path).name} | reason={reason} | "
            f"queues gpu={self.queues['gpu'].qsize()} yolo={self.queues['yolo'].qsize()} ffmpeg={self.queues['ffmpeg'].qsize()}"
        )

    def _stage_job_already_queued(self, queue_name: str, video_path: str, stage: str) -> bool:
        queue_obj = self.queues.get(queue_name)
        if queue_obj is None:
            return False
        with queue_obj.mutex:
            for payload in queue_obj.queue:
                try:
                    job = payload[2]
                except Exception:
                    continue
                if (
                    isinstance(job, StageJob)
                    and job.video_path == video_path
                    and job.stage == stage
                ):
                    return True
        return False

    def _queue_name_for_stage(self, stage: str) -> str:
        if stage in {"transcribe", "llm"}:
            return "gpu"
        if stage == "yolo":
            return "yolo"
        if stage == "ffmpeg":
            return "ffmpeg"
        raise ValueError(f"Unknown stage: {stage}")

    def _all_videos_terminal_locked(self) -> bool:
        active_video_keys = self._active_video_keys_locked()
        if not active_video_keys:
            return False
        return all(
            self.state["videos"][video_path]["status"] in TERMINAL_VIDEO_STATUSES
            for video_path in active_video_keys
            if video_path in self.state["videos"]
        )

    def _active_video_keys_locked(self) -> set[str]:
        if self.active_video_keys:
            return set(self.active_video_keys)
        return set(self.state.get("videos", {}).keys())

    def _make_queue_payload(self, queue_name: str, job: Optional[StageJob]):
        self.job_counter += 1
        if job is None:
            return (9999, self.job_counter, None)
        return (self._queue_priority(queue_name, job.stage), self.job_counter, job)

    @staticmethod
    def _queue_priority(queue_name: str, stage: str) -> int:
        if queue_name != "gpu":
            return 100
        # Keep the single GPU lane busy, but always let downstream LLM work
        # advance before starting more fresh transcriptions.
        if stage == "llm":
            return 0
        if stage == "transcribe":
            return 10
        return 100

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        seconds = max(0.0, float(seconds))
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        if h > 0:
            return f"{h}h {m}m {s:.1f}s"
        if m > 0:
            return f"{m}m {s:.1f}s"
        return f"{s:.1f}s"

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def main() -> int:
    import config as cfg

    queue_state_file = getattr(
        cfg,
        "QUEUE_STATE_FILE",
        str(Path(getattr(cfg, "WORKING_DIR", "working")) / "video_queue_state.json"),
    )
    parser = argparse.ArgumentParser(description="Simple queue runner for the PROYA video pipeline")
    parser.add_argument(
        "--input-dir",
        default=getattr(cfg, "QUEUE_INPUT_DIR", r"D:\VOD"),
        help="Folder containing input videos",
    )
    parser.add_argument(
        "--state-file",
        default=queue_state_file,
        help="JSON progress file used for resume",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=getattr(cfg, "QUEUE_MAX_RETRIES", 2),
        help="Retries per stage before marking failed",
    )
    parser.add_argument(
        "--max-inflight-videos",
        "--max-analysis-videos",
        dest="max_inflight_videos",
        type=int,
        default=getattr(cfg, "QUEUE_MAX_INFLIGHT_VIDEOS", 1),
        help=(
            "How many videos may be active in GPU analysis stages "
            "(transcription/LLM) at once. YOLO and FFmpeg have their own queues "
            "and do not block new analysis."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=getattr(cfg, "QUEUE_POLL_INTERVAL", 2.0),
        help="Monitor loop interval in seconds",
    )
    parser.add_argument(
        "--scan-interval",
        type=float,
        default=getattr(
            cfg,
            "QUEUE_RESCAN_INTERVAL_SECONDS",
            getattr(cfg, "QUEUE_SCAN_INTERVAL_SECONDS", 300.0),
        ),
        help="How often to rescan the input folder for new stable videos",
    )
    parser.add_argument(
        "--stable-seconds",
        type=float,
        default=getattr(cfg, "QUEUE_STABLE_SECONDS", 60.0),
        help="Seconds a video file must remain unchanged before joining the active run",
    )
    parser.add_argument(
        "--control-file",
        default=getattr(
            cfg,
            "QUEUE_CONTROL_FILE",
            str(Path(getattr(cfg, "WORKING_DIR", "working")) / "queue_control.json"),
        ),
        help="JSON control file used for graceful stop/continue",
    )
    parser.add_argument(
        "--ffmpeg-max-parallel-clips",
        type=int,
        default=getattr(cfg, "QUEUE_FFMPEG_MAX_PARALLEL_CLIPS", 2),
        help=(
            "Clip jobs allowed inside the queue FFmpeg worker. "
            "Lower values leave CPU/GPU room for the next transcription to advance."
        ),
    )
    parser.add_argument(
        "--no-yolo-subprocess",
        dest="yolo_in_subprocess",
        action="store_false",
        default=getattr(cfg, "QUEUE_YOLO_IN_SUBPROCESS", True),
        help=(
            "Run YOLO inside the queue process instead of an isolated subprocess. "
            "Use only for debugging; isolation protects the queue after CUDA crashes."
        ),
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Reset failed active videos to retry from their failed stage onward",
    )
    parser.add_argument("--max-clips", type=int, default=None, help="Maximum rendered clip jobs per video")
    parser.add_argument("--min-score", type=float, default=None, help="Minimum LLM moment score for fresh detection")
    parser.add_argument("--force-rescore", action="store_true", help="Bypass post-render score cache")
    parser.add_argument(
        "--force-modules",
        action="store_true",
        help="Recut reusable module outputs even when existing module files are valid",
    )
    parser.add_argument("--output-tag", default=None, help="Write rendered clips to a new tagged output folder")
    parser.add_argument("--working-tag", default=None, help="Write caches to a new tagged working folder")
    parser.add_argument("--redo-tag", default=None, help="Apply the same tag to both working and output folders")
    parser.add_argument("--run-stage", choices=STAGES, help=argparse.SUPPRESS)
    parser.add_argument("--video-path", help=argparse.SUPPRESS)
    args = parser.parse_args()

    output_tag = args.output_tag
    working_tag = args.working_tag
    if args.redo_tag:
        output_tag = args.redo_tag
        working_tag = args.redo_tag

    if args.run_stage:
        if not args.video_path:
            raise SystemExit("--video-path is required with --run-stage")
        _run_stage_once(
            args.run_stage,
            args.video_path,
            output_tag=output_tag,
            working_tag=working_tag,
            max_clips=args.max_clips,
            min_score=args.min_score,
            force_rescore=args.force_rescore,
            force_modules=args.force_modules,
        )
        return 0

    runner = VideoQueueRunner(
        input_dir=args.input_dir,
        state_path=args.state_file,
        max_retries=args.max_retries,
        max_inflight_videos=args.max_inflight_videos,
        ffmpeg_max_parallel_clips=args.ffmpeg_max_parallel_clips,
        max_clips=args.max_clips,
        min_score=args.min_score,
        force_rescore=args.force_rescore,
        force_modules=args.force_modules,
        output_tag=output_tag,
        working_tag=working_tag,
        poll_interval=args.poll_interval,
        scan_interval=args.scan_interval,
        stable_seconds=args.stable_seconds,
        control_path=args.control_file,
        yolo_in_subprocess=args.yolo_in_subprocess,
        retry_failed=args.retry_failed,
    )
    return runner.run()


def _run_stage_once(
    stage: str,
    video_path: str,
    output_tag: str | None = None,
    working_tag: str | None = None,
    max_clips: int | None = None,
    min_score: float | None = None,
    force_rescore: bool = False,
    force_modules: bool = False,
) -> None:
    import config as cfg

    video_path = str(Path(video_path))
    stem = Path(video_path).stem
    working_dir = Path(cfg.WORKING_DIR) / _build_versioned_stem(stem, working_tag)
    llm_cfg = _RuntimeConfig(cfg, {"MIN_SCORE": float(min_score)}) if min_score is not None else cfg

    if stage == "transcribe":
        from transcriber import transcribe

        if _reuse_base_transcript_for_tagged_run(video_path, working_dir, working_tag, cfg):
            write_stage_fingerprint(working_dir / "transcript.json", video_path, cfg, "transcribe")
            return
        transcribe(video_path, str(working_dir), cfg)
        write_stage_fingerprint(working_dir / "transcript.json", video_path, cfg, "transcribe")
        return
    if stage == "llm":
        from moment_detector import detect_moments
        from transcriber import build_text_chunks, load_cached_transcript, transcript_cache_is_compatible

        transcript = load_cached_transcript(str(working_dir))
        if transcript is None or not transcript_cache_is_compatible(transcript, llm_cfg):
            raise RuntimeError("Transcript cache missing or outdated before LLM stage")
        chunks = build_text_chunks(transcript, llm_cfg.CHUNK_DURATION, llm_cfg.CHUNK_OVERLAP)
        detect_moments(chunks, str(working_dir), llm_cfg)
        write_stage_fingerprint(working_dir / "moments.json", video_path, llm_cfg, "llm")
        return
    if stage == "yolo":
        from vision_scanner import build_scan_ranges_from_moments, scan_video_for_products

        moments_path = working_dir / "moments.json"
        if not moments_path.exists():
            raise RuntimeError("Moments cache missing before YOLO stage")
        with open(moments_path, "r", encoding="utf-8") as f:
            moments = json.load(f)
        scan_ranges = build_scan_ranges_from_moments(moments, cfg)
        scan_video_for_products(video_path, str(working_dir), cfg, scan_ranges=scan_ranges)
        write_stage_fingerprint(
            working_dir / "product_detections.json",
            video_path,
            cfg,
            "yolo",
            extra={"scan_ranges": scan_ranges},
        )
        return
    if stage == "ffmpeg":
        from main import run_pipeline

        run_pipeline(
            video_path=video_path,
            skip_transcribe=True,
            skip_moments=True,
            skip_vision=True,
            cut_only=False,
            max_clips=max(1, int(max_clips)) if max_clips else None,
            min_score=float(min_score) if min_score is not None else None,
            force_rescore=force_rescore,
            force_modules=force_modules,
            output_tag=output_tag,
            working_tag=working_tag,
        )
        return

    raise RuntimeError(f"Unknown stage: {stage}")


def _reuse_base_transcript_for_tagged_run(
    video_path: str,
    tagged_working_dir: Path,
    working_tag: str | None,
    cfg,
) -> bool:
    if not working_tag:
        return False

    from transcriber import load_cached_transcript, transcript_cache_is_compatible

    stem = Path(video_path).stem
    tagged_transcript = load_cached_transcript(str(tagged_working_dir))
    if tagged_transcript is not None and transcript_cache_is_compatible(tagged_transcript, cfg):
        log.info(f"Loading cached transcript from {tagged_working_dir / 'transcript.json'}")
        return True

    for source_working_dir in _iter_transcript_reuse_candidates(stem, tagged_working_dir, cfg):
        source_transcript_path = source_working_dir / "transcript.json"
        source_transcript = load_cached_transcript(str(source_working_dir))
        if source_transcript is None:
            continue
        if not transcript_cache_is_compatible(source_transcript, cfg):
            log.info(
                "Prior transcript exists but is older, raw, or invalid; "
                f"redo cannot reuse it: {source_transcript_path}"
            )
            continue
        if not _transcript_source_matches_video(source_transcript, video_path):
            log.info(f"Prior transcript belongs to a different source video; skipping: {source_transcript_path}")
            continue

        tagged_working_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_transcript_path, tagged_working_dir / "transcript.json")

        source_raw_checkpoint = source_working_dir / "transcript.raw_checkpoint.json"
        if source_raw_checkpoint.exists():
            shutil.copy2(source_raw_checkpoint, tagged_working_dir / "transcript.raw_checkpoint.json")

        log.info(
            "Reusing compatible aligned transcript from prior run for redo: "
            f"{source_transcript_path} -> {tagged_working_dir / 'transcript.json'}"
        )
        return True

    return False


def _iter_transcript_reuse_candidates(stem: str, tagged_working_dir: Path, cfg) -> list[Path]:
    working_root = Path(cfg.WORKING_DIR)
    base_working_dir = working_root / _build_versioned_stem(stem, None)
    tagged_resolved = _safe_resolve(tagged_working_dir)
    candidates: list[Path] = []

    if _safe_resolve(base_working_dir) != tagged_resolved:
        candidates.append(base_working_dir)

    prefixes = (f"{stem}__", f"{stem}_")
    try:
        siblings = [
            path for path in working_root.iterdir()
            if path.is_dir()
            and path.name.startswith(prefixes)
            and _safe_resolve(path) != tagged_resolved
            and path not in candidates
        ]
    except FileNotFoundError:
        siblings = []

    candidates.extend(siblings)
    candidates.sort(key=_transcript_candidate_mtime, reverse=True)
    return candidates


def _transcript_source_matches_video(transcript: dict, video_path: str) -> bool:
    source_video_path = transcript.get("metadata", {}).get("source_video_path")
    if not source_video_path:
        return True
    try:
        return str(Path(source_video_path).resolve()).casefold() == str(Path(video_path).resolve()).casefold()
    except Exception:
        return False


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except Exception:
        return path


def _transcript_candidate_mtime(path: Path) -> float:
    transcript_path = path / "transcript.json"
    try:
        if transcript_path.exists():
            return transcript_path.stat().st_mtime
        return path.stat().st_mtime
    except OSError:
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
