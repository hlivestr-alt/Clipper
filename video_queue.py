#!/usr/bin/env python3
"""
Simple threaded queue runner for the PROYA video pipeline.

Pipeline per video:
  1. Transcription (GPU shared lane)
  2. LLM moment detection (GPU shared lane)
  3. YOLO scan (parallel lane)
  4. FFmpeg clip render (parallel CPU lane/backlog)

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
CLIP_PROGRESS_DEFAULTS = {
    "progress_pct": 0,
    "message": None,
    "last_progress_at": None,
    "clips_total": 0,
    "clips_completed": 0,
    "clips_created": 0,
    "clips_failed": 0,
    "clips_skipped": 0,
    "last_clip_id": None,
    "last_clip_status": None,
    "last_event": None,
}


@dataclass(frozen=True)
class StageJob:
    video_path: str
    stage: str


def _build_versioned_stem(stem: str, tag: str | None) -> str:
    if not tag:
        return stem
    safe_tag = re.sub(r'[<>:"/\\|?*\n\r]', '', str(tag)).strip()
    safe_tag = re.sub(r"\s+", "_", safe_tag)
    return f"{stem}__{safe_tag}" if safe_tag else stem


class VideoQueueRunner:
    def __init__(
        self,
        input_dir: str,
        state_path: str,
        max_retries: int,
        max_inflight_videos: int,
        ffmpeg_max_parallel_clips: int | None = None,
        output_tag: str | None = None,
        working_tag: str | None = None,
        poll_interval: float = 2.0,
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
        self.poll_interval = max(0.5, float(poll_interval))
        self.output_tag = output_tag
        self.working_tag = working_tag
        self.state_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.state = self._load_state()
        self.job_counter = 0
        self.active_video_keys: set[str] = set()
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
            log.info(f"No supported videos found in {self.input_dir}")
            return 0

        self._sync_videos(videos)
        self._start_workers()
        with self.state_lock:
            self._schedule_locked("bootstrap")

        try:
            while True:
                with self.state_lock:
                    done = self._all_videos_terminal_locked()
                if done:
                    break
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
            "videos": {},
        }

    def _migrate_state(self, state: dict) -> dict:
        state.setdefault("created_at", self._now_iso())
        state.setdefault("updated_at", self._now_iso())
        state.setdefault("input_dir", str(self.input_dir))
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
        videos = [
            p for p in sorted(self.input_dir.iterdir())
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS
        ]
        log.info(f"Discovered {len(videos)} video(s) in {self.input_dir}")
        return videos

    def _sync_videos(self, videos: list[Path]) -> None:
        with self.state_lock:
            known = self.state["videos"]
            self.active_video_keys = {str(video.resolve()) for video in videos}
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
                    self._refresh_stage_status_from_disk(entry)

            self._save_state_locked()

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

        working_dir, _ = self._video_dirs(Path(video_path))
        transcript = load_cached_transcript(str(working_dir))
        if transcript is None or not transcript_cache_is_compatible(transcript, self.cfg):
            raise RuntimeError("Transcript cache missing or outdated before LLM stage")

        chunks = build_text_chunks(transcript, self.cfg.CHUNK_DURATION, self.cfg.CHUNK_OVERLAP)
        detect_moments(chunks, str(working_dir), self.cfg)
        write_stage_fingerprint(working_dir / "moments.json", video_path, self.cfg, "llm")

    def _stage_yolo(self, video_path: str) -> None:
        from vision_scanner import build_scan_ranges_from_moments, scan_video_for_products

        working_dir, _ = self._video_dirs(Path(video_path))
        moments_path = working_dir / "moments.json"
        if not moments_path.exists():
            raise RuntimeError("Moments cache missing before YOLO stage")

        with open(moments_path, "r", encoding="utf-8") as f:
            moments = json.load(f)

        scan_ranges = build_scan_ranges_from_moments(moments, self.cfg)
        scan_video_for_products(
            video_path,
            str(working_dir),
            self.cfg,
            scan_ranges=scan_ranges,
        )
        write_stage_fingerprint(
            working_dir / "product_detections.json",
            video_path,
            self.cfg,
            "yolo",
            extra={"scan_ranges": scan_ranges},
        )

    def _stage_ffmpeg(self, video_path: str) -> None:
        from main import run_pipeline

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
            run_pipeline(
                video_path=video_path,
                skip_transcribe=True,
                skip_moments=True,
                skip_vision=True,
                cut_only=False,
                output_tag=self.output_tag,
                working_tag=self.working_tag,
                progress_callback=lambda stage, pct, message, **payload: self._handle_ffmpeg_progress(
                    video_path,
                    stage,
                    pct,
                    message,
                    **payload,
                ),
            )
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
            ):
                if field in payload:
                    stage_state[field] = self._coerce_nonnegative_int(payload.get(field))

            if payload.get("clip_id"):
                stage_state["last_clip_id"] = str(payload["clip_id"])
            if payload.get("clip_status"):
                stage_state["last_clip_status"] = str(payload["clip_status"])
            if payload.get("event"):
                stage_state["last_event"] = str(payload["event"])
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
            if stage_state["status"] == "done" and not path.exists():
                stage_state["status"] = "pending"
                stage_state["queued"] = False
                stage_state["finished_at"] = None
                stage_state["duration_sec"] = None

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
        try:
            if stage == "transcribe":
                from transcriber import load_cached_transcript, transcript_cache_is_compatible

                transcript = load_cached_transcript(str(path.parent))
                return bool(
                    transcript
                    and transcript_cache_is_compatible(transcript, self.cfg)
                    and stage_fingerprint_matches(path, video_path, self.cfg, "transcribe")
                )
            if stage == "llm":
                from moment_detector import _cached_moments_are_current

                with open(path, "r", encoding="utf-8") as f:
                    moments = json.load(f)
                return bool(
                    _cached_moments_are_current(moments)
                    and stage_fingerprint_matches(path, video_path, self.cfg, "llm")
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
                scan_ranges = build_scan_ranges_from_moments(moments, self.cfg)
                return bool(
                    _vision_cache_fingerprint_matches(path, video_path, self.cfg, scan_ranges)
                    and stage_fingerprint_matches(
                        path,
                        video_path,
                        self.cfg,
                        "yolo",
                        extra={"scan_ranges": scan_ranges},
                    )
                )
            if stage == "ffmpeg":
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
                        self.cfg,
                        "ffmpeg",
                        extra={"max_clips": None, "cut_only": False},
                    )
                )
        except Exception as exc:
            log.warning(f"Ignoring invalid {stage} cache for {Path(video_path).name}: {exc}")
            return False
        return False

    def _next_stage_locked(self, entry: dict) -> Optional[str]:
        for stage in STAGES:
            if entry["stages"][stage]["status"] != "done":
                return stage
        return None

    def _schedule_locked(self, reason: str) -> None:
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
        if stage_state.get("queued"):
            return
        if stage_state["status"] == "done":
            return

        queue_name = self._queue_name_for_stage(stage)
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
            return True
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
    parser = argparse.ArgumentParser(description="Simple queue runner for the PROYA video pipeline")
    parser.add_argument("--input-dir", default=r"D:\VOD", help="Folder containing input videos")
    parser.add_argument(
        "--state-file",
        default=str(Path("working") / "video_queue_state.json"),
        help="JSON progress file used for resume",
    )
    parser.add_argument("--max-retries", type=int, default=2, help="Retries per stage before marking failed")
    parser.add_argument(
        "--max-inflight-videos",
        "--max-analysis-videos",
        dest="max_inflight_videos",
        type=int,
        default=1,
        help=(
            "How many videos may be active in GPU analysis stages "
            "(transcription/LLM) at once. YOLO and FFmpeg have their own queues "
            "and do not block new analysis."
        ),
    )
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Monitor loop interval in seconds")
    parser.add_argument(
        "--ffmpeg-max-parallel-clips",
        type=int,
        default=2,
        help=(
            "Clip jobs allowed inside the queue FFmpeg worker. "
            "Lower values leave CPU/GPU room for the next transcription to advance."
        ),
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
        _run_stage_once(args.run_stage, args.video_path, output_tag=output_tag, working_tag=working_tag)
        return 0

    runner = VideoQueueRunner(
        input_dir=args.input_dir,
        state_path=args.state_file,
        max_retries=args.max_retries,
        max_inflight_videos=args.max_inflight_videos,
        ffmpeg_max_parallel_clips=args.ffmpeg_max_parallel_clips,
        output_tag=output_tag,
        working_tag=working_tag,
        poll_interval=args.poll_interval,
    )
    return runner.run()


def _run_stage_once(
    stage: str,
    video_path: str,
    output_tag: str | None = None,
    working_tag: str | None = None,
) -> None:
    import config as cfg

    video_path = str(Path(video_path))
    stem = Path(video_path).stem
    working_dir = Path(cfg.WORKING_DIR) / _build_versioned_stem(stem, working_tag)

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
        if transcript is None or not transcript_cache_is_compatible(transcript, cfg):
            raise RuntimeError("Transcript cache missing or outdated before LLM stage")
        chunks = build_text_chunks(transcript, cfg.CHUNK_DURATION, cfg.CHUNK_OVERLAP)
        detect_moments(chunks, str(working_dir), cfg)
        write_stage_fingerprint(working_dir / "moments.json", video_path, cfg, "llm")
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

    prefix = f"{stem}__"
    try:
        siblings = [
            path for path in working_root.iterdir()
            if path.is_dir()
            and path.name.startswith(prefix)
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
