#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONTROL_SCHEMA_VERSION = 1
RUN_ACTION = "run"
STOP_GRACEFUL_ACTION = "stop_graceful"
PAUSED_STATUS = "paused"


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
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def read_control_state(control_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(control_path) if control_path else default_control_path()
    state = read_json(path)
    state.setdefault("schema_version", CONTROL_SCHEMA_VERSION)
    state.setdefault("requested_action", RUN_ACTION)
    state.setdefault("status", "idle")
    state.setdefault("updated_at", now_iso())
    return state


def write_control_state(control_path: str | Path | None, state: dict[str, Any]) -> dict[str, Any]:
    path = Path(control_path) if control_path else default_control_path()
    payload = dict(state)
    payload["schema_version"] = CONTROL_SCHEMA_VERSION
    payload["updated_at"] = now_iso()
    write_json_atomic(path, payload)
    return payload


def pause_requested(control_path: str | Path | None = None) -> bool:
    state = read_control_state(control_path)
    return state.get("requested_action") == STOP_GRACEFUL_ACTION


def request_stop(control_path: str | Path | None = None) -> dict[str, Any]:
    state = read_control_state(control_path)
    state["requested_action"] = STOP_GRACEFUL_ACTION
    state["status"] = "stop_requested"
    state["requested_at"] = now_iso()
    return write_control_state(control_path, state)


def request_continue(control_path: str | Path | None = None) -> dict[str, Any]:
    state = read_control_state(control_path)
    state["requested_action"] = RUN_ACTION
    state["status"] = "continue_requested"
    state["continued_at"] = now_iso()
    return write_control_state(control_path, state)


def request_start(control_path: str | Path | None = None) -> dict[str, Any]:
    state = read_control_state(control_path)
    state["requested_action"] = RUN_ACTION
    state["status"] = "start_requested"
    state["started_at"] = now_iso()
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
    parser.add_argument("action", choices=["status", "stop", "continue", "start"])
    parser.add_argument("--control-file", default=None)
    parser.add_argument("--forever-state-file", default=None)
    parser.add_argument("--queue-state-file", default=None)
    parser.add_argument("--json", action="store_true", help="Print raw JSON for status")
    args = parser.parse_args()

    if args.action == "stop":
        state = request_stop(args.control_file)
        print(f"Graceful stop requested at {state.get('requested_at')}")
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
