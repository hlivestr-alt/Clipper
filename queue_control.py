#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONTROL_SCHEMA_VERSION = 1
RUN_ACTION = "run"
STOP_GRACEFUL_ACTION = "stop_graceful"
PAUSE_GRACEFUL_ACTION = "pause_graceful"
PAUSED_STATUS = "paused"

RUN_MODE_LABELS = {
    "single_video": "Single Video",
    "folder_once": "Folder Once",
    "folder_repeat": "Folder Repeat",
}
PIPELINE_MODE_LABELS = {
    "full": "Full Pipeline",
    "clips_only": "Clips Only",
    "modules_only": "Modules Only",
    "raw_cuts_only": "Raw Cuts Only",
}
VARIANT_MODE_LABELS = {
    "all": "All Variants",
    "original": "Original Only",
    "custom": "Custom Variants",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_control_path(cfg: Any | None = None) -> Path:
    if cfg is None:
        try:
            import config as cfg  # type: ignore
        except Exception:
            cfg = None
    if cfg is not None:
        value = getattr(cfg, "QUEUE_CONTROL_FILE", None)
        if value:
            return Path(value)
        working_dir = getattr(cfg, "WORKING_DIR", "working")
        return Path(working_dir) / "queue_control.json"
    return Path("working") / "queue_control.json"


def default_forever_state_path(cfg: Any | None = None) -> Path:
    if cfg is None:
        try:
            import config as cfg  # type: ignore
        except Exception:
            cfg = None
    if cfg is not None:
        value = getattr(cfg, "QUEUE_FOREVER_STATE_FILE", None)
        if value:
            return Path(value)
        working_dir = getattr(cfg, "WORKING_DIR", "working")
        return Path(working_dir) / "queue_forever_state.json"
    return Path("working") / "queue_forever_state.json"


def read_json(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json_atomic(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.1 * attempt)
        except OSError as exc:
            last_error = exc
            time.sleep(0.1 * attempt)
    if last_error is not None:
        raise last_error


def read_control_state(control_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(control_path) if control_path else default_control_path()
    state = read_json(path)
    state.setdefault("schema_version", CONTROL_SCHEMA_VERSION)
    state.setdefault("requested_action", RUN_ACTION)
    state.setdefault("status", "idle")
    state.setdefault("updated_at", now_iso())
    return state


def normalize_launch_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = dict(config or {})
    run_mode = str(raw.get("run_mode") or "folder_repeat").strip().lower()
    pipeline_mode = str(raw.get("pipeline_mode") or "full").strip().lower()
    variant_mode = str(raw.get("variant_mode") or "all").strip().lower()
    if run_mode not in RUN_MODE_LABELS:
        raise ValueError(f"Unsupported run_mode: {run_mode}")
    if pipeline_mode not in PIPELINE_MODE_LABELS:
        raise ValueError(f"Unsupported pipeline_mode: {pipeline_mode}")
    if variant_mode not in VARIANT_MODE_LABELS:
        raise ValueError(f"Unsupported variant_mode: {variant_mode}")

    try:
        variant_count = int(raw.get("variant_count") or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("variant_count must be an integer from 1 to 6") from exc
    variant_count = max(1, min(6, variant_count))

    max_clips_raw = raw.get("max_clips")
    max_clips: int | None
    if max_clips_raw in (None, ""):
        max_clips = None
    else:
        try:
            max_clips_value = int(max_clips_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_clips must be 0 or a positive integer") from exc
        if max_clips_value < 0:
            raise ValueError("max_clips must be 0 or a positive integer")
        max_clips = None if max_clips_value == 0 else max_clips_value

    video_path = str(raw.get("video_path") or "").strip()
    if run_mode == "single_video" and not video_path:
        raise ValueError("video_path is required for single_video run mode")
    if run_mode != "single_video" and video_path:
        raise ValueError("video_path is only valid for single_video run mode")

    if pipeline_mode == "raw_cuts_only":
        variant_mode = "original"
        variant_count = 1
    elif variant_mode != "custom":
        variant_count = 1

    normalized = {
        "run_mode": run_mode,
        "pipeline_mode": pipeline_mode,
        "variant_mode": variant_mode,
        "variant_count": variant_count,
        "max_clips": max_clips,
        "video_path": video_path or None,
    }
    return normalized


def launch_summary(config: dict[str, Any] | None = None) -> str:
    launch = normalize_launch_config(config)
    parts = [
        RUN_MODE_LABELS.get(str(launch.get("run_mode")), str(launch.get("run_mode") or "")),
        PIPELINE_MODE_LABELS.get(str(launch.get("pipeline_mode")), str(launch.get("pipeline_mode") or "")),
    ]
    variant_mode = str(launch.get("variant_mode") or "")
    if variant_mode == "custom":
        parts.append(f"{int(launch.get('variant_count') or 1)} Variants")
    else:
        parts.append(VARIANT_MODE_LABELS.get(variant_mode, variant_mode))
    max_clips = launch.get("max_clips")
    parts.append("Unlimited" if max_clips is None else f"{max_clips} clip{'s' if int(max_clips) != 1 else ''}")
    if launch.get("video_path"):
        parts.append(Path(str(launch["video_path"])).name)
    return " • ".join(part for part in parts if part)


def write_control_state(control_path: str | Path | None, state: dict[str, Any]) -> dict[str, Any]:
    path = Path(control_path) if control_path else default_control_path()
    payload = dict(state)
    payload["schema_version"] = CONTROL_SCHEMA_VERSION
    payload["updated_at"] = now_iso()
    write_json_atomic(path, payload)
    return payload


def pause_requested(control_path: str | Path | None = None) -> bool:
    state = read_control_state(control_path)
    return state.get("requested_action") in {STOP_GRACEFUL_ACTION, PAUSE_GRACEFUL_ACTION}


def stop_requested(control_path: str | Path | None = None) -> bool:
    state = read_control_state(control_path)
    return state.get("requested_action") == STOP_GRACEFUL_ACTION


def request_stop(control_path: str | Path | None = None) -> dict[str, Any]:
    state = read_control_state(control_path)
    state["requested_action"] = STOP_GRACEFUL_ACTION
    state["status"] = "stop_requested"
    state["requested_at"] = now_iso()
    return write_control_state(control_path, state)


def request_pause(control_path: str | Path | None = None) -> dict[str, Any]:
    state = read_control_state(control_path)
    state["requested_action"] = PAUSE_GRACEFUL_ACTION
    state["status"] = "pause_requested"
    state["requested_at"] = now_iso()
    return write_control_state(control_path, state)


def request_continue(control_path: str | Path | None = None) -> dict[str, Any]:
    state = read_control_state(control_path)
    state["requested_action"] = RUN_ACTION
    state["status"] = "continue_requested"
    state["continued_at"] = now_iso()
    return write_control_state(control_path, state)


def request_start(
    control_path: str | Path | None = None,
    launch_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = read_control_state(control_path)
    launch = normalize_launch_config(launch_config)
    state["requested_action"] = RUN_ACTION
    state["status"] = "start_requested"
    state["started_at"] = now_iso()
    state["launch_config"] = launch
    state["launch_summary"] = launch_summary(launch)
    return write_control_state(control_path, state)


def update_control_status(
    control_path: str | Path | None,
    status: str,
    **fields: Any,
) -> dict[str, Any]:
    state = read_control_state(control_path)
    state["status"] = status
    for key, value in fields.items():
        if value is not None:
            state[key] = value
    return write_control_state(control_path, state)


def read_status_snapshot(
    control_path: str | Path | None = None,
    forever_state_path: str | Path | None = None,
    queue_state_path: str | Path | None = None,
) -> dict[str, Any]:
    control = read_control_state(control_path)
    forever_state = read_json(forever_state_path or default_forever_state_path())
    queue_state = read_json(queue_state_path) if queue_state_path else {}
    return {
        "control": control,
        "supervisor": forever_state,
        "queue": queue_state,
    }


def _print_status(snapshot: dict[str, Any]) -> None:
    control = snapshot.get("control") or {}
    supervisor = snapshot.get("supervisor") or {}
    queue = snapshot.get("queue") or {}
    print(f"Control status: {control.get('status', 'unknown')}")
    print(f"Requested action: {control.get('requested_action', RUN_ACTION)}")
    if supervisor:
        print(f"Current run: {supervisor.get('current_run_tag', '-')}")
        print(f"Supervisor status: {supervisor.get('status', '-')}")
        summary = supervisor.get("queue_summary") or {}
        if isinstance(summary, dict) and summary:
            print(f"Run summary: {summary.get('reason', summary)}")
    launch = control.get("launch_config") if isinstance(control.get("launch_config"), dict) else {}
    if launch:
        print(f"Launch: {control.get('launch_summary') or launch_summary(launch)}")
    videos = queue.get("videos") if isinstance(queue, dict) else None
    if isinstance(videos, dict):
        running = [
            entry
            for entry in videos.values()
            if isinstance(entry, dict) and str(entry.get("status", "")).lower() in {"running", "paused"}
        ]
        print(f"Tracked videos: {len(videos)}")
        if running:
            names = ", ".join(str(item.get("name", "-")) for item in running[:5])
            print(f"Active/paused: {names}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Control the PROYA queue supervisor")
    parser.add_argument("action", choices=["status", "stop", "pause", "continue", "start"])
    parser.add_argument("--control-file", default=None)
    parser.add_argument("--forever-state-file", default=None)
    parser.add_argument("--queue-state-file", default=None)
    parser.add_argument("--json", action="store_true", help="Print raw JSON for status")
    args = parser.parse_args()

    if os.environ.get("CLIPPER_SERVICE_BOUNDARY", "service").casefold() != "legacy":
        from clipper_app.bootstrap import build_queue_control_service
        from clipper_app.contracts import QueueAction, QueueControlCommand

        snapshot_model = build_queue_control_service().execute(
            QueueControlCommand(
                action=QueueAction(args.action),
                control_path=args.control_file,
                forever_state_path=args.forever_state_file,
                queue_state_path=args.queue_state_file,
            )
        )
        snapshot = snapshot_model.model_dump()
        control = snapshot["control"]
        if args.action == "stop":
            print(f"Graceful stop requested at {control.get('requested_at')}")
            return 0
        if args.action == "pause":
            print(f"Graceful pause requested at {control.get('requested_at')}")
            return 0
        if args.action == "continue":
            print(f"Continue requested at {control.get('continued_at')}")
            return 0
        if args.action == "start":
            print(f"Start requested at {control.get('started_at')}")
            return 0
        if args.json:
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        else:
            _print_status(snapshot)
        return 0

    if args.action == "stop":
        state = request_stop(args.control_file)
        print(f"Graceful stop requested at {state.get('requested_at')}")
        return 0
    if args.action == "pause":
        state = request_pause(args.control_file)
        print(f"Graceful pause requested at {state.get('requested_at')}")
        return 0
    if args.action == "continue":
        state = request_continue(args.control_file)
        print(f"Continue requested at {state.get('continued_at')}")
        return 0
    if args.action == "start":
        state = request_start(args.control_file)
        print(f"Start requested at {state.get('started_at')}")
        return 0

    snapshot = read_status_snapshot(
        control_path=args.control_file,
        forever_state_path=args.forever_state_file,
        queue_state_path=args.queue_state_file,
    )
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        _print_status(snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
