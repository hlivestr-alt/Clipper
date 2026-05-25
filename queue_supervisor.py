#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import queue_control

try:
    from video_queue import STAGES, TERMINAL_VIDEO_STATUSES, VIDEO_EXTS
except Exception:
    STAGES = ("transcribe", "llm", "yolo", "ffmpeg")
    TERMINAL_VIDEO_STATUSES = {"completed", "failed"}
    VIDEO_EXTS = {".mp4", ".mkv", ".mov"}


SUPERVISOR_SCHEMA_VERSION = 1
PAUSED_EXIT_CODE = 10
TERMINAL_STAGE_STATUSES = {"done", "failed", "skipped"}
NON_TERMINAL_STAGE_STATUSES = {"pending", "queued", "running", "paused"}


@dataclass
class RunTerminalSummary:
    is_terminal: bool
    run_tag: str
    video_count: int
    completed: int = 0
    failed: int = 0
    pending: int = 0
    missing: int = 0
    active_clip_renders: int = 0
    pending_stages: int = 0
    paused: bool = False
    reason: str = ""


def format_run_tag(run_number: int) -> str:
    return f"_run_{int(run_number):03d}"


def discover_stable_videos(
    input_dir: str | Path,
    stable_seconds: float,
    now: float | None = None,
) -> list[Path]:
    root = Path(input_dir)
    if not root.exists():
        return []
    now_value = time.time() if now is None else now
    stable_age = max(0.0, float(stable_seconds))
    videos: list[Path] = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if now_value - stat.st_mtime >= stable_age:
            videos.append(path)
    return videos


def load_json(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_supervisor_state(
    path: str | Path,
    run_number: int,
    run_tag: str,
    status: str,
    last_exit_code: int = 0,
    queue_summary: RunTerminalSummary | dict[str, Any] | None = None,
) -> None:
    summary = asdict(queue_summary) if isinstance(queue_summary, RunTerminalSummary) else queue_summary
    payload = {
        "schema_version": SUPERVISOR_SCHEMA_VERSION,
        "current_run_number": int(run_number),
        "current_run_tag": run_tag,
        "status": status,
        "last_exit_code": int(last_exit_code),
        "queue_summary": summary or {},
        "updated_at": queue_control.now_iso(),
    }
    queue_control.write_json_atomic(path, payload)


def load_run_number(path: str | Path, start_run_number: int) -> int:
    state = load_json(path)
    run_number = max(1, int(start_run_number))
    try:
        saved = int(state.get("current_run_number"))
    except (TypeError, ValueError):
        saved = 0
    return max(run_number, saved)


def queue_run_terminal(
    queue_state_file: str | Path,
    input_dir: str | Path,
    run_tag: str,
    stable_seconds: float,
) -> RunTerminalSummary:
    stable_videos = discover_stable_videos(input_dir, stable_seconds)
    summary = RunTerminalSummary(
        is_terminal=False,
        run_tag=run_tag,
        video_count=len(stable_videos),
    )
    if not stable_videos:
        summary.reason = "No stable VOD files found yet."
        return summary

    queue_state = load_json(queue_state_file)
    if str(queue_state.get("queue_status", "")).lower() == queue_control.PAUSED_STATUS:
        summary.paused = True
        summary.reason = "Queue state is paused."
        return summary

    videos = queue_state.get("videos")
    if not isinstance(videos, dict):
        summary.missing = len(stable_videos)
        summary.reason = "Queue state does not exist for this run yet."
        return summary

    entries = {str(key).casefold(): value for key, value in videos.items() if isinstance(value, dict)}
    for video in stable_videos:
        key = str(video.resolve()).casefold()
        entry = entries.get(key)
        if not entry:
            summary.missing += 1
            continue
        if entry.get("working_tag") != run_tag or entry.get("output_tag") != run_tag:
            summary.pending += 1
            continue

        status = str(entry.get("status") or "").lower()
        if status == "completed":
            summary.completed += 1
        elif status == "failed":
            summary.failed += 1
        elif status == queue_control.PAUSED_STATUS:
            summary.paused = True
            summary.pending += 1
        else:
            summary.pending += 1

        stages = entry.get("stages") if isinstance(entry.get("stages"), dict) else {}
        for stage in STAGES:
            stage_state = stages.get(stage) if isinstance(stages.get(stage), dict) else {}
            stage_status = str(stage_state.get("status") or "pending").lower()
            if stage_status in NON_TERMINAL_STAGE_STATUSES:
                summary.pending_stages += 1
            if stage == "ffmpeg":
                try:
                    summary.active_clip_renders += max(0, int(stage_state.get("active_clip_renders") or 0))
                except (TypeError, ValueError):
                    pass

    terminal_count = summary.completed + summary.failed
    summary.is_terminal = (
        summary.video_count > 0
        and summary.missing == 0
        and summary.pending == 0
        and summary.pending_stages == 0
        and summary.active_clip_renders == 0
        and terminal_count == summary.video_count
        and not summary.paused
    )
    summary.reason = (
        f"completed={summary.completed}, failed={summary.failed}, pending={summary.pending}, "
        f"missing={summary.missing}, pending_stages={summary.pending_stages}, "
        f"active_clip_renders={summary.active_clip_renders}, paused={summary.paused}"
    )
    return summary


def build_queue_command(args, run_tag: str) -> list[str]:
    command = [
        args.python_exe,
        str(Path(__file__).resolve().with_name("video_queue.py")),
        "--input-dir",
        str(args.input_dir),
        "--state-file",
        str(args.state_file),
        "--max-retries",
        str(args.max_retries),
        "--max-inflight-videos",
        str(args.max_inflight_videos),
        "--ffmpeg-max-parallel-clips",
        str(args.ffmpeg_max_parallel_clips),
        "--poll-interval",
        str(args.poll_interval),
        "--redo-tag",
        run_tag,
        "--control-file",
        str(args.control_file),
        "--stable-seconds",
        str(args.stable_seconds),
        "--scan-interval",
        str(args.scan_interval),
    ]
    if args.max_clips:
        command.extend(["--max-clips", str(args.max_clips)])
    if args.min_score is not None:
        command.extend(["--min-score", str(args.min_score)])
    if args.force_rescore:
        command.append("--force-rescore")
    if args.force_modules:
        command.append("--force-modules")
    if args.retry_failed:
        command.append("--retry-failed")
    return command


def wait_for_continue(control_file: str | Path, sleep_seconds: float) -> None:
    while queue_control.pause_requested(control_file):
        time.sleep(max(1.0, sleep_seconds))


def run_supervisor(args) -> int:
    run_number = load_run_number(args.forever_state_file, args.start_run_number)
    print("PROYA queue supervisor is active.")
    print(f"Input: {args.input_dir}")
    print(f"Queue state: {args.state_file}")
    print(f"Supervisor state: {args.forever_state_file}")
    print(f"Control file: {args.control_file}")

    while True:
        run_tag = format_run_tag(run_number)
        if queue_control.pause_requested(args.control_file):
            summary = queue_run_terminal(args.state_file, args.input_dir, run_tag, args.stable_seconds)
            write_supervisor_state(
                args.forever_state_file,
                run_number,
                run_tag,
                queue_control.PAUSED_STATUS,
                last_exit_code=PAUSED_EXIT_CODE,
                queue_summary=summary,
            )
            queue_control.update_control_status(
                args.control_file,
                queue_control.PAUSED_STATUS,
                current_run_number=run_number,
                current_run_tag=run_tag,
                queue_summary=asdict(summary),
            )
            print(f"Run {run_tag} is paused. Waiting for continue.")
            wait_for_continue(args.control_file, args.scan_interval)
            continue

        before = queue_run_terminal(args.state_file, args.input_dir, run_tag, args.stable_seconds)
        if before.is_terminal:
            print(f"Run {run_tag} is terminal ({before.reason}). Advancing.")
            run_number += 1
            write_supervisor_state(
                args.forever_state_file,
                run_number,
                format_run_tag(run_number),
                "waiting",
                queue_summary=before,
            )
            time.sleep(max(0.0, args.between_runs_delay_seconds))
            continue

        queue_control.update_control_status(
            args.control_file,
            "running",
            current_run_number=run_number,
            current_run_tag=run_tag,
            queue_summary=asdict(before),
        )
        write_supervisor_state(
            args.forever_state_file,
            run_number,
            run_tag,
            "running",
            queue_summary=before,
        )

        command = build_queue_command(args, run_tag)
        print(f"Launching run {run_tag}.")
        if args.dry_run:
            print(" ".join(command))
            return 0

        completed = subprocess.run(command, cwd=str(Path(__file__).resolve().parent), check=False)
        exit_code = completed.returncode
        after = queue_run_terminal(args.state_file, args.input_dir, run_tag, args.stable_seconds)

        if after.is_terminal:
            print(f"Finished run {run_tag} ({after.reason}).")
            write_supervisor_state(
                args.forever_state_file,
                run_number + 1,
                format_run_tag(run_number + 1),
                "waiting",
                last_exit_code=exit_code,
                queue_summary=after,
            )
            queue_control.update_control_status(
                args.control_file,
                "waiting",
                current_run_number=run_number + 1,
                current_run_tag=format_run_tag(run_number + 1),
                queue_summary=asdict(after),
            )
            run_number += 1
            time.sleep(max(0.0, args.between_runs_delay_seconds))
            continue

        if exit_code == PAUSED_EXIT_CODE or queue_control.pause_requested(args.control_file) or after.paused:
            print(f"Run {run_tag} paused ({after.reason}).")
            write_supervisor_state(
                args.forever_state_file,
                run_number,
                run_tag,
                queue_control.PAUSED_STATUS,
                last_exit_code=exit_code,
                queue_summary=after,
            )
            queue_control.update_control_status(
                args.control_file,
                queue_control.PAUSED_STATUS,
                current_run_number=run_number,
                current_run_tag=run_tag,
                queue_summary=asdict(after),
            )
            wait_for_continue(args.control_file, args.scan_interval)
            continue

        print(
            f"Queue exited before run {run_tag} was terminal ({after.reason}). "
            f"Restarting same run after {args.restart_delay_seconds} seconds."
        )
        write_supervisor_state(
            args.forever_state_file,
            run_number,
            run_tag,
            "restart_pending",
            last_exit_code=exit_code,
            queue_summary=after,
        )
        queue_control.update_control_status(
            args.control_file,
            "restart_pending",
            current_run_number=run_number,
            current_run_tag=run_tag,
            queue_summary=asdict(after),
        )
        time.sleep(max(0.0, args.restart_delay_seconds))


def parse_args(argv: list[str] | None = None):
    import config as cfg

    working_dir = getattr(cfg, "WORKING_DIR", "working")
    parser = argparse.ArgumentParser(description="Supervise resumable PROYA queue runs")
    parser.add_argument("--input-dir", default=getattr(cfg, "QUEUE_INPUT_DIR", r"D:\VOD"))
    parser.add_argument(
        "--state-file",
        default=getattr(cfg, "QUEUE_STATE_FILE", str(Path(working_dir) / "video_queue_state.json")),
    )
    parser.add_argument(
        "--forever-state-file",
        default=getattr(cfg, "QUEUE_FOREVER_STATE_FILE", str(Path(working_dir) / "queue_forever_state.json")),
    )
    parser.add_argument(
        "--control-file",
        default=getattr(cfg, "QUEUE_CONTROL_FILE", str(Path(working_dir) / "queue_control.json")),
    )
    parser.add_argument("--start-run-number", type=int, default=getattr(cfg, "QUEUE_START_RUN_NUMBER", 1))
    parser.add_argument("--max-retries", type=int, default=getattr(cfg, "QUEUE_MAX_RETRIES", 2))
    parser.add_argument(
        "--max-inflight-videos",
        type=int,
        default=getattr(cfg, "QUEUE_MAX_INFLIGHT_VIDEOS", 1),
    )
    parser.add_argument(
        "--ffmpeg-max-parallel-clips",
        type=int,
        default=getattr(cfg, "QUEUE_FFMPEG_MAX_PARALLEL_CLIPS", 2),
    )
    parser.add_argument("--poll-interval", type=float, default=getattr(cfg, "QUEUE_POLL_INTERVAL", 2.0))
    parser.add_argument(
        "--scan-interval",
        type=float,
        default=getattr(cfg, "QUEUE_SCAN_INTERVAL_SECONDS", 10.0),
    )
    parser.add_argument(
        "--stable-seconds",
        type=float,
        default=getattr(cfg, "QUEUE_STABLE_SECONDS", 60.0),
    )
    parser.add_argument(
        "--restart-delay-seconds",
        type=float,
        default=getattr(cfg, "QUEUE_RESTART_DELAY_SECONDS", 30.0),
    )
    parser.add_argument(
        "--between-runs-delay-seconds",
        type=float,
        default=getattr(cfg, "QUEUE_BETWEEN_RUNS_DELAY_SECONDS", 10.0),
    )
    parser.add_argument("--max-clips", type=int, default=None)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--python-exe", default=sys.executable)
    parser.add_argument("--force-rescore", action="store_true")
    parser.add_argument("--force-modules", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_supervisor(args)


if __name__ == "__main__":
    raise SystemExit(main())
