from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from clipper_app.application.events import EventSink, NullEventSink
from clipper_app.application.logging_utils import (
    SUPERVISOR_LOG_BACKUP_COUNT,
    SUPERVISOR_LOG_MAX_BYTES,
    rotate_file_if_oversize,
)
from clipper_app.application.settings import LegacyConfigProvider
from clipper_app.contracts.events import OperationKind, ProgressEvent
from clipper_app.contracts.models import (
    ComplianceScanCommand,
    ComplianceScanResult,
    ExportPackagingCommand,
    ExportPackagingResult,
    ModuleAssemblyCommand,
    ModuleOperationResult,
    ModuleReportCommand,
    ModuleReviewCommand,
    ModuleValidationCommand,
    PipelineResult,
    PipelineRunCommand,
    QueueAction,
    QueueControlCommand,
    QueueRunCommand,
    QueueRunResult,
    QueueSnapshot,
    QueueSupervisorCommand,
    ScoringCommand,
    ScoringResult,
)


def _progress_callback(
    sink: EventSink,
    operation_id: str,
    operation: OperationKind,
    video_path: str | None = None,
) -> Callable[..., None]:
    def callback(stage: str, percent: int, message: str, **payload: Any) -> None:
        sink.publish(
            ProgressEvent.from_legacy(
                stage,
                percent,
                message,
                operation_id=operation_id,
                operation=operation,
                video_path=video_path,
                **payload,
            )
        )
    return callback


@dataclass
class PipelineService:
    executor: Callable[..., dict[str, Any]]
    settings_provider: LegacyConfigProvider

    def run(self, command: PipelineRunCommand, sink: EventSink | None = None) -> PipelineResult:
        sink = sink or NullEventSink()
        operation_id = uuid4().hex
        snapshot = self.settings_provider.snapshot(command.settings_overrides)
        runtime_cfg = self.settings_provider.runtime_view(snapshot)
        result = self.executor(
            command=command,
            runtime_cfg=runtime_cfg,
            progress_callback=_progress_callback(
                sink,
                operation_id,
                OperationKind.PIPELINE,
                command.video_path,
            ),
        )
        normalized = dict(result or {})
        normalized["export_batches"] = normalized.get("export_batches") or {}
        normalized["module_extraction"] = normalized.get("module_extraction") or {}
        normalized["modular_assembly"] = normalized.get("modular_assembly") or {}
        return PipelineResult.model_validate(normalized)


@dataclass
class QueueService:
    runner_factory: Callable[[QueueRunCommand], Any]

    def run(self, command: QueueRunCommand, sink: EventSink | None = None) -> QueueRunResult:
        del sink
        runner = self.runner_factory(command)
        return QueueRunResult(exit_code=int(runner.run()))


@dataclass
class QueueSupervisorService:
    executor: Callable[[QueueSupervisorCommand], int]

    def run(self, command: QueueSupervisorCommand) -> QueueRunResult:
        return QueueRunResult(exit_code=int(self.executor(command)))


class QueueControlService:
    def __init__(self, settings_provider: LegacyConfigProvider | None = None) -> None:
        self.settings_provider = settings_provider or LegacyConfigProvider()

    def execute(self, command: QueueControlCommand) -> QueueSnapshot:
        import queue_control

        command = self._with_effective_paths(command)
        launch_info: dict[str, Any] | None = None
        if command.action == QueueAction.START:
            launch_payload = (
                command.launch_config.model_dump(mode="json")
                if command.launch_config is not None else None
            )
            queue_control.request_start(command.control_path, launch_payload)
            snapshot, snapshot_path = self._new_run_settings_snapshot(command.control_path)
            launch_info = self._ensure_supervisor_running(command, snapshot, snapshot_path)
        elif command.action == QueueAction.CONTINUE:
            queue_control.request_continue(command.control_path)
            snapshot, snapshot_path = self._continued_run_settings_snapshot(command.control_path)
            launch_info = self._ensure_supervisor_running(command, snapshot, snapshot_path)
        elif command.action == QueueAction.PAUSE:
            queue_control.request_pause(command.control_path)
        elif command.action == QueueAction.STOP:
            queue_control.request_stop(command.control_path)
            self._clear_pending_queue_state(command)
        payload = queue_control.read_status_snapshot(
            control_path=command.control_path,
            forever_state_path=command.forever_state_path,
            queue_state_path=command.queue_state_path,
        )
        if command.action in {QueueAction.START, QueueAction.CONTINUE}:
            payload = self._refresh_started_queue_snapshot(command, payload)
        if launch_info:
            payload.setdefault("control", {})["supervisor_launch"] = launch_info
        return QueueSnapshot.model_validate(payload)

    def _with_effective_paths(self, command: QueueControlCommand) -> QueueControlCommand:
        snapshot = self.settings_provider.snapshot()
        cfg = self.settings_provider.runtime_view(snapshot)
        return command.model_copy(update={
            "control_path": command.control_path or str(getattr(cfg, "QUEUE_CONTROL_FILE", "working/queue_control.json")),
            "forever_state_path": command.forever_state_path or str(getattr(cfg, "QUEUE_FOREVER_STATE_FILE", "working/queue_forever_state.json")),
            "queue_state_path": command.queue_state_path or str(getattr(cfg, "QUEUE_STATE_FILE", "working/video_queue_state.json")),
        })

    def _new_run_settings_snapshot(self, control_path: str | None):
        import queue_control

        snapshot = self.settings_provider.snapshot()
        working_dir = Path(str(control_path or "working/queue_control.json")).resolve().parent
        snapshot_path = working_dir / "settings_snapshots" / f"{snapshot.revision}.json"
        self.settings_provider.write_snapshot_file(snapshot, snapshot_path)
        state = queue_control.read_control_state(control_path)
        state["settings_revision"] = snapshot.revision
        state["settings_snapshot_file"] = str(snapshot_path)
        queue_control.write_control_state(control_path, state)
        return snapshot, snapshot_path

    def _continued_run_settings_snapshot(self, control_path: str | None):
        import queue_control

        state = queue_control.read_control_state(control_path)
        raw_path = str(state.get("settings_snapshot_file") or "").strip()
        if raw_path and Path(raw_path).exists():
            return self.settings_provider.snapshot_from_file(raw_path), Path(raw_path)
        return self._new_run_settings_snapshot(control_path)

    def _clear_pending_queue_state(self, command: QueueControlCommand) -> None:
        try:
            import config as cfg
            from video_queue import clear_pending_queue_state

            state_path = command.queue_state_path or getattr(cfg, "QUEUE_STATE_FILE", "working/video_queue_state.json")
            clear_pending_queue_state(state_path)
        except FileNotFoundError:
            return

    def _ensure_supervisor_running(self, command: QueueControlCommand, snapshot, snapshot_path: Path) -> dict[str, Any]:
        cfg = self.settings_provider.runtime_view(snapshot)

        repo_root = Path(__file__).resolve().parents[2]
        supervisor_script = repo_root / "queue_supervisor.py"
        control_path = self._resolve_project_path(
            command.control_path or getattr(cfg, "QUEUE_CONTROL_FILE", "working/queue_control.json"),
            repo_root,
        )
        state_path = self._resolve_project_path(
            command.queue_state_path or getattr(cfg, "QUEUE_STATE_FILE", "working/video_queue_state.json"),
            repo_root,
        )
        forever_state_path = self._resolve_project_path(
            command.forever_state_path or getattr(cfg, "QUEUE_FOREVER_STATE_FILE", "working/queue_forever_state.json"),
            repo_root,
        )
        input_dir = self._resolve_project_path(getattr(cfg, "QUEUE_INPUT_DIR", r"D:\VOD"), repo_root)
        launch_config = self._launch_config_for_command(command, control_path)
        if launch_config.get("video_path"):
            launch_config["video_path"] = str(
                self._validate_single_video_path(str(launch_config["video_path"]), input_dir)
            )

        try:
            processes = self._process_command_lines()
        except Exception:
            processes = []
        supervisor_pids = self._matching_supervisor_pids(
            supervisor_script,
            state_path,
            control_path,
            processes=processes,
        )
        if supervisor_pids and self._video_queue_process_running(
            state_path,
            control_path,
            processes=processes,
        ):
            return {
                "started": False,
                "reason": "supervisor_already_running",
                "pids": supervisor_pids,
            }
        replaced_pids: list[int] = []
        if supervisor_pids:
            replaced_pids = self._terminate_processes(supervisor_pids)

        launch_log = self._resolve_project_path(getattr(cfg, "WORKING_DIR", "working"), repo_root) / "queue_supervisor_launch.log"
        launch_log.parent.mkdir(parents=True, exist_ok=True)
        rotate_file_if_oversize(
            launch_log,
            max_bytes=SUPERVISOR_LOG_MAX_BYTES,
            backup_count=SUPERVISOR_LOG_BACKUP_COUNT,
        )
        command_line = [
            sys.executable,
            str(supervisor_script),
            "--input-dir",
            str(input_dir),
            "--state-file",
            str(state_path),
            "--forever-state-file",
            str(forever_state_path),
            "--control-file",
            str(control_path),
            "--start-run-number",
            str(getattr(cfg, "QUEUE_START_RUN_NUMBER", 1)),
            "--run-mode",
            str(launch_config["run_mode"]),
            "--pipeline-mode",
            str(launch_config["pipeline_mode"]),
            "--variant-mode",
            str(launch_config["variant_mode"]),
            "--variant-count",
            str(launch_config["variant_count"]),
            "--stage-admission-limit",
            str(getattr(cfg, "QUEUE_STAGE_ADMISSION_LIMIT", 3)),
            "--max-retries",
            str(getattr(cfg, "QUEUE_MAX_RETRIES", 2)),
            "--max-inflight-videos",
            str(getattr(cfg, "QUEUE_MAX_INFLIGHT_VIDEOS", 1)),
            "--ffmpeg-max-parallel-clips",
            str(getattr(cfg, "QUEUE_FFMPEG_MAX_PARALLEL_CLIPS", 2)),
            "--poll-interval",
            str(getattr(cfg, "QUEUE_POLL_INTERVAL", 2.0)),
            "--scan-interval",
            str(getattr(cfg, "QUEUE_RESCAN_INTERVAL_SECONDS", 300.0)),
            "--stable-seconds",
            str(getattr(cfg, "QUEUE_STABLE_SECONDS", 60.0)),
            "--restart-delay-seconds",
            str(getattr(cfg, "QUEUE_RESTART_DELAY_SECONDS", 30.0)),
            "--between-runs-delay-seconds",
            str(getattr(cfg, "QUEUE_BETWEEN_RUNS_DELAY_SECONDS", 10.0)),
            "--settings-snapshot-file",
            str(snapshot_path),
        ]
        if launch_config.get("max_clips") is not None:
            command_line.extend(["--max-clips", str(launch_config["max_clips"])])
        if launch_config.get("video_path"):
            command_line.extend(["--video-path", str(launch_config["video_path"])])

        creationflags = 0
        startupinfo = None
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        with launch_log.open("a", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                command_line,
                cwd=str(repo_root),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                close_fds=os.name != "nt",
                creationflags=creationflags,
                startupinfo=startupinfo,
            )

        return {
            "started": True,
            "pid": process.pid,
            "log_path": str(launch_log),
            "command": command_line,
            "replaced_pids": replaced_pids,
            "settings_revision": snapshot.revision,
            "settings_snapshot_file": str(snapshot_path),
        }

    def _launch_config_for_command(
        self,
        command: QueueControlCommand,
        control_path: Path,
    ) -> dict[str, Any]:
        import queue_control

        if command.launch_config is not None:
            return queue_control.normalize_launch_config(command.launch_config.model_dump(mode="json"))
        control = queue_control.read_control_state(control_path)
        raw = control.get("launch_config") if isinstance(control.get("launch_config"), dict) else None
        return queue_control.normalize_launch_config(raw)

    @staticmethod
    def _validate_single_video_path(video_path: str, input_dir: Path) -> Path:
        try:
            from video_queue import VIDEO_EXTS
        except Exception:
            VIDEO_EXTS = {".mp4", ".mkv", ".mov"}

        target = Path(video_path)
        if not target.is_absolute():
            target = input_dir / target
        target = target.resolve()
        root = input_dir.resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("single_video video_path must be inside QUEUE_INPUT_DIR") from exc
        if not target.exists() or not target.is_file():
            raise ValueError(f"single_video video_path was not found: {target}")
        if target.suffix.casefold() not in {suffix.casefold() for suffix in VIDEO_EXTS}:
            raise ValueError(f"Unsupported VOD file type: {target.suffix}")
        return target

    def _refresh_started_queue_snapshot(
        self,
        command: QueueControlCommand,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        import queue_control

        queue_state_path = Path(
            command.queue_state_path
            or payload.get("queue", {}).get("state_path")
            or "working/video_queue_state.json"
        )
        if not queue_state_path.is_absolute():
            queue_state_path = Path.cwd() / queue_state_path
        if not queue_state_path.exists():
            return payload

        refreshed = payload
        for _ in range(10):
            refreshed = queue_control.read_status_snapshot(
                control_path=command.control_path,
                forever_state_path=command.forever_state_path,
                queue_state_path=str(queue_state_path),
            )
            queue = refreshed.get("queue") if isinstance(refreshed.get("queue"), dict) else {}
            if str(queue.get("queue_status") or "").strip().lower() == "running":
                return self._write_running_summary(command, refreshed)
            time.sleep(0.5)
        return refreshed

    def _write_running_summary(
        self,
        command: QueueControlCommand,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        import queue_control
        import queue_state_health as qh

        queue = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
        control = payload.get("control") if isinstance(payload.get("control"), dict) else {}
        supervisor = payload.get("supervisor") if isinstance(payload.get("supervisor"), dict) else {}
        health = qh.derive_queue_health(queue)
        run_tag = (
            health.get("active_run_tag")
            or supervisor.get("current_run_tag")
            or control.get("current_run_tag")
            or ""
        )
        running_count = int(health.get("running_stage_count") or 0)
        queued_count = int(health.get("queued_stage_count") or 0)
        summary = {
            "is_terminal": False,
            "run_tag": run_tag,
            "paused": False,
            "pending": 1,
            "pending_stages": running_count + queued_count,
            "active_clip_renders": int(health.get("active_clip_renders") or 0),
            "reason": (
                f"Queue process is running for {run_tag or 'current run'}; "
                f"{running_count} running stage(s), {queued_count} queued stage(s)."
            ),
        }
        queue_control.update_control_status(
            command.control_path,
            "running",
            current_run_number=control.get("current_run_number") or supervisor.get("current_run_number"),
            current_run_tag=run_tag or None,
            queue_summary=summary,
        )
        forever_path = Path(command.forever_state_path) if command.forever_state_path else queue_control.default_forever_state_path()
        forever = queue_control.read_json(forever_path)
        if forever:
            forever.update(
                {
                    "status": "running",
                    "current_run_tag": run_tag or forever.get("current_run_tag"),
                    "queue_summary": summary,
                    "updated_at": queue_control.now_iso(),
                }
            )
            queue_control.write_json_atomic(forever_path, forever)
        return queue_control.read_status_snapshot(
            control_path=command.control_path,
            forever_state_path=command.forever_state_path,
            queue_state_path=command.queue_state_path,
        )

    @staticmethod
    def _resolve_project_path(path_text: str | Path, repo_root: Path) -> Path:
        path = Path(path_text)
        if path.is_absolute():
            return path
        return repo_root / path

    def _supervisor_process_running(
        self,
        supervisor_script: Path,
        state_path: Path,
        control_path: Path,
    ) -> bool:
        return bool(self._matching_supervisor_pids(supervisor_script, state_path, control_path))

    def _matching_supervisor_pids(
        self,
        supervisor_script: Path,
        state_path: Path,
        control_path: Path,
        *,
        processes: list[tuple[int, str]] | None = None,
    ) -> list[int]:
        script_name = supervisor_script.name.casefold()
        script_path = str(supervisor_script).casefold()
        state_text = str(state_path).casefold()
        control_text = str(control_path).casefold()
        if processes is None:
            try:
                processes = self._process_command_lines()
            except Exception:
                processes = []
        pids: list[int] = []
        for pid, command_line in processes:
            if pid == os.getpid() or not command_line:
                continue
            normalized = command_line.casefold()
            if "python" not in normalized:
                continue
            if script_name not in normalized and script_path not in normalized:
                continue
            if state_text in normalized or control_text in normalized:
                pids.append(pid)
                continue
            if script_path in normalized:
                pids.append(pid)
        return sorted(set(pids))

    def _video_queue_process_running(
        self,
        state_path: Path,
        control_path: Path,
        *,
        processes: list[tuple[int, str]] | None = None,
    ) -> bool:
        state_text = str(state_path).casefold()
        control_text = str(control_path).casefold()
        if processes is None:
            try:
                processes = self._process_command_lines()
            except Exception:
                processes = []
        for pid, command_line in processes:
            if pid == os.getpid() or not command_line:
                continue
            normalized = command_line.casefold()
            if "python" not in normalized or "video_queue.py" not in normalized:
                continue
            if state_text in normalized or control_text in normalized:
                return True
        return False

    @staticmethod
    def _terminate_processes(pids: list[int]) -> list[int]:
        terminated: list[int] = []
        for pid in pids:
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue
            terminated.append(pid)
        if terminated:
            time.sleep(0.5)
        return terminated

    def _process_command_lines(self) -> list[tuple[int, str]]:
        if os.name == "nt":
            return self._windows_process_command_lines()
        return self._posix_process_command_lines()

    @staticmethod
    def _windows_process_command_lines() -> list[tuple[int, str]]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [
                "powershell",
                "-NoLogo",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=flags,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return []
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return []
        if isinstance(payload, dict):
            payload = [payload]
        rows: list[tuple[int, str]] = []
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                pid = int(item.get("ProcessId"))
            except (TypeError, ValueError):
                continue
            rows.append((pid, str(item.get("CommandLine") or "")))
        return rows

    @staticmethod
    def _posix_process_command_lines() -> list[tuple[int, str]]:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if completed.returncode != 0:
            return []
        rows: list[tuple[int, str]] = []
        for line in completed.stdout.splitlines():
            text = line.strip()
            if not text:
                continue
            pid_text, _, command_line = text.partition(" ")
            try:
                pid = int(pid_text)
            except ValueError:
                continue
            rows.append((pid, command_line.strip()))
        return rows


class ScoringService:
    def __init__(self, settings_provider: LegacyConfigProvider | None = None) -> None:
        self.settings_provider = settings_provider or LegacyConfigProvider()

    def rescore(self, command: ScoringCommand, sink: EventSink | None = None) -> ScoringResult:
        del sink
        from clip_scorer import score_output_tree

        snapshot = self.settings_provider.snapshot(command.settings_overrides)
        cfg = self.settings_provider.runtime_view(snapshot)
        if command.force_rescore:
            cfg.SCORER_FORCE_RESCORE = True
        scores = score_output_tree(
            command.output_dir,
            working_root=command.working_dir,
            cfg=cfg,
            limit=command.limit,
            include_failed=command.include_failed,
            flush_every=command.flush_every,
        )
        return ScoringResult(scores=tuple(scores))


class ComplianceService:
    def __init__(self, settings_provider: LegacyConfigProvider | None = None) -> None:
        self.settings_provider = settings_provider or LegacyConfigProvider()

    def scan(self, command: ComplianceScanCommand, sink: EventSink | None = None) -> ComplianceScanResult:
        del sink
        from compliance_checker import scan_output_dir

        snapshot = self.settings_provider.snapshot(command.settings_overrides)
        cfg = self.settings_provider.runtime_view(snapshot)
        result = scan_output_dir(
            command.output_dir,
            working_dir=command.working_dir,
            cfg=cfg,
            force=command.force,
        )
        return ComplianceScanResult.model_validate(result)


class ExportPackagingService:
    def __init__(self, settings_provider: LegacyConfigProvider | None = None) -> None:
        self.settings_provider = settings_provider or LegacyConfigProvider()

    def package(self, command: ExportPackagingCommand, sink: EventSink | None = None) -> ExportPackagingResult:
        del sink
        from export_packager import package_export_batches

        snapshot = self.settings_provider.snapshot(command.settings_overrides)
        cfg = self.settings_provider.runtime_view(snapshot)
        output_root = command.output_root or str(getattr(cfg, "OUTPUT_DIR", r"D:\output_clips"))
        result = package_export_batches(
            output_root,
            cfg=cfg,
            batch_size=command.batch_size,
            dry_run=command.dry_run,
        )
        return ExportPackagingResult(payload=result or {})


class ModuleService:
    def __init__(self, settings_provider: LegacyConfigProvider | None = None) -> None:
        self.settings_provider = settings_provider or LegacyConfigProvider()

    def assemble(self, command: ModuleAssemblyCommand, sink: EventSink | None = None) -> ModuleOperationResult:
        from main import run_module_assembly

        cfg = self.settings_provider.runtime_view(self.settings_provider.snapshot())

        kwargs: dict[str, Any] = {
            "assembly_date": command.assembly_date,
            "module_assembly_limit": command.module_assembly_limit,
            "module_product_zoom": command.module_product_zoom,
        }
        if command.product:
            kwargs["product"] = command.product
        if sink is not None:
            kwargs["progress_callback"] = _progress_callback(
                sink,
                uuid4().hex,
                OperationKind.MODULE_ASSEMBLY,
            )
        result = run_module_assembly(runtime_cfg=cfg, **kwargs)
        return ModuleOperationResult(payload=result or {})

    def validate(self, command: ModuleValidationCommand) -> ModuleOperationResult:
        from module_visual_validator import validate_module_library_visual

        cfg = self.settings_provider.runtime_view(self.settings_provider.snapshot())
        result = validate_module_library_visual(
            cfg,
            product=command.product,
            limit=command.limit,
            force=command.force,
            visual_status=command.visual_status,
            role=command.role,
            approved_only=command.approved_only,
            priority=command.priority,
        )
        return ModuleOperationResult(payload=result or {})

    def review(self, command: ModuleReviewCommand) -> ModuleOperationResult:
        from module_review import update_module_review

        cfg = self.settings_provider.runtime_view(self.settings_provider.snapshot())
        result = update_module_review(
            command.identifier,
            command.status,
            cfg,
            note=command.note,
            reviewer=command.reviewer,
        )
        return ModuleOperationResult(payload=result or {})

    def report(self, command: ModuleReportCommand) -> ModuleOperationResult:
        cfg = self.settings_provider.runtime_view(self.settings_provider.snapshot())
        payload: dict[str, Any] = {}
        if command.include_library_report:
            from module_report import build_module_library_report

            payload["report"] = build_module_library_report(cfg)
        if command.include_review_queue:
            from module_review import build_module_review_queue

            payload["review_queue"] = build_module_review_queue(
                cfg,
                status=command.review_filter,
                limit=command.review_limit,
            )
        return ModuleOperationResult(payload=payload)


class HealthService:
    def snapshot(self, state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        from queue_state_health import derive_queue_health

        return derive_queue_health(state, **kwargs)
