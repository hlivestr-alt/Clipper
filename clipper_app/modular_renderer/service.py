from __future__ import annotations

import hashlib
import json
import queue
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from clipper_app.contracts.modular_renderer_models import ModularRenderRunCreateRequest
from clipper_app.modular_planner import ModularPlannerService
from clipper_app.modular_scanner.service import production_is_active

from .constants import RENDERER_VERSION, WAIT_POLL_SECONDS
from .media import FFmpegPipeline, RenderMediaError, verify_manifest_source
from .repository import RendererRepository


class ModularRendererConflict(RuntimeError):
    pass


def _safe_name(value: str, maximum: int = 48) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-._")[:maximum] or "composition"


class ModularRendererService:
    """One-worker, restart-safe materializer for immutable approved planner manifests."""

    def __init__(
        self,
        cfg: Any,
        *,
        planner: ModularPlannerService | None = None,
        repository: RendererRepository | None = None,
        media: FFmpegPipeline | None = None,
        production_active: Callable[[], bool] | None = None,
        wait_poll_seconds: float = WAIT_POLL_SECONDS,
        start_worker: bool = True,
    ):
        self.cfg = cfg
        working = Path(str(getattr(cfg, "WORKING_DIR", "working") or "working"))
        self.storage_root = (working / "modular_renders").resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.planner = planner or ModularPlannerService(cfg)
        self.repository = repository or RendererRepository(working / "modular_renderer.sqlite3")
        self.media = media or FFmpegPipeline()
        self._production_active = production_active or (lambda: production_is_active(cfg))
        self.wait_poll_seconds = wait_poll_seconds
        self._tasks: queue.Queue[str | None] = queue.Queue()
        self._queued: set[str] = set()
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.repository.recover_incomplete()
        for run_id in self.repository.pending_run_ids():
            self._enqueue(run_id)
        if start_worker:
            self.start_worker()

    def start_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="modular-renderer", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self._stop.set()
        self._tasks.put(None)
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=2.0)

    def create_run(self, request: ModularRenderRunCreateRequest) -> tuple[dict[str, Any], bool]:
        try:
            planner_run = self.planner.get_run(request.planner_run_id)
        except KeyError:
            raise
        if planner_run["status"] != "approved":
            raise ModularRendererConflict("Only an approved planner run can be rendered")
        manifest = self.planner.manifest(request.planner_run_id, public=False)
        payload = manifest["payload"]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != manifest["checksum_sha256"]:
            raise ModularRendererConflict("Approved planner manifest checksum does not match")

        requested = set(request.composition_ids)
        approved = [row for row in payload.get("compositions", []) if row.get("composition_id") in requested]
        found = {str(row.get("composition_id")) for row in approved}
        missing = requested - found
        if missing:
            raise ValueError(f"Selected compositions are not in the approved manifest: {sorted(missing)}")
        if len(approved) != len(request.composition_ids):
            raise ValueError("Every selected composition must be unique and approved")
        ordered_ids = [str(row["composition_id"]) for row in approved]
        request_key = hashlib.sha256(
            f"{manifest['manifest_id']}|{RENDERER_VERSION}|{'|'.join(ordered_ids)}".encode("utf-8")
        ).hexdigest()
        reusable = self.repository.find_reusable(request_key)
        if reusable and (not request.manual_rerender or reusable["status"] in {"queued", "waiting_for_production", "rendering"}):
            return self._public_run(reusable), True
        active_overlap = self.repository.find_composition_overlap(
            manifest["manifest_id"], RENDERER_VERSION, ordered_ids,
            ("queued", "waiting_for_production", "rendering"),
        )
        if active_overlap:
            raise ModularRendererConflict(
                f"A selected composition is already active in render run {active_overlap['render_run_id']}"
            )
        if not request.manual_rerender:
            completed_overlap = self.repository.find_composition_overlap(
                manifest["manifest_id"], RENDERER_VERSION, ordered_ids,
                ("completed", "partial_failure"), item_status="completed",
            )
            if completed_overlap:
                raise ModularRendererConflict(
                    f"A selected composition already has a completed render in run {completed_overlap['render_run_id']}; "
                    "open that render or request an explicit rerender"
                )

        run_id = uuid.uuid4().hex
        output_dir = self.storage_root / run_id
        items = []
        for composition in approved:
            expected = sum(
                float(item["end_seconds"]) - float(item["start_seconds"])
                for item in sorted(composition["items"], key=lambda row: int(row["position"]))
            )
            ordinal = int(composition["ordinal"])
            filename = f"{ordinal:03d}_{_safe_name(str(composition['composition_id']))}_{_safe_name(str(payload['product']))}.mp4"
            items.append({
                "composition_id": composition["composition_id"],
                "product": payload["product"],
                "template": composition["actual_template"],
                "ordinal": ordinal,
                "expected_duration": expected,
                "output_path": str((output_dir / filename).resolve()),
            })
        created = self.repository.create_run({
            "render_run_id": run_id,
            "planner_run_id": request.planner_run_id,
            "planner_manifest_id": manifest["manifest_id"],
            "planner_manifest_checksum": manifest["checksum_sha256"],
            "renderer_version": RENDERER_VERSION,
            "request_key": request_key,
            "selected_composition_ids": ordered_ids,
            "output_directory": str(output_dir),
            "rerender_of_run_id": reusable["render_run_id"] if reusable else None,
        }, items)
        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_report(run_id)
        self._enqueue(run_id)
        return self._public_run(created), False

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._public_run(self.repository.get_run(run_id))

    def list_runs(self, planner_run_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return [self._public_run(run) for run in self.repository.list_runs(planner_run_id, limit)]

    def media_path(self, run_id: str, composition_id: str) -> Path:
        item = self.repository.item(run_id, composition_id)
        if item["status"] != "completed":
            raise ModularRendererConflict("Modular base video is not completed")
        path = Path(str(item["output_path"])).resolve(strict=False)
        try:
            path.relative_to(self.storage_root)
        except ValueError as exc:
            raise PermissionError("Render output is outside modular render storage") from exc
        if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
            raise FileNotFoundError("Completed modular base video is missing")
        return path

    def process_pending_once(self) -> bool:
        pending = self.repository.pending_run_ids()
        if not pending:
            return False
        self._process_run(pending[0])
        return True

    def _enqueue(self, run_id: str) -> None:
        with self._guard:
            if run_id in self._queued:
                return
            self._queued.add(run_id)
        self._tasks.put(run_id)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            run_id = self._tasks.get()
            if run_id is None:
                return
            with self._guard:
                self._queued.discard(run_id)
            try:
                self._process_run(run_id)
            except Exception:
                # Item-level failures are persisted below. A process-level failure stays queued for recovery.
                try:
                    self.repository.set_run_state(run_id, "queued")
                except Exception:
                    pass

    def _process_run(self, run_id: str) -> None:
        run = self.repository.get_run(run_id)
        if run["status"] not in {"queued", "waiting_for_production", "rendering"}:
            return
        manifest = self.planner.manifest(run["planner_run_id"], public=False)
        compositions = {
            str(row["composition_id"]): row for row in manifest["payload"].get("compositions", [])
        }
        verification_cache: dict[tuple[str, int, int, str], Path] = {}
        source_probe_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
        for item in run["items"]:
            if item["status"] in {"completed", "failed"}:
                continue
            composition_id = str(item["composition_id"])
            if self._stop.is_set():
                self.repository.set_item_state(run_id, composition_id, "queued")
                self.repository.set_run_state(run_id, "queued")
                return
            if not self._wait_for_production(run_id, composition_id):
                return
            composition = compositions.get(composition_id)
            started = time.perf_counter()
            if composition is None:
                self.repository.fail_item(run_id, composition_id, "manifest_item_missing", "Composition is no longer present in its approved manifest", 0.0)
                continue
            try:
                self.repository.set_run_state(run_id, "rendering", composition_id)
                self.repository.set_item_state(run_id, composition_id, "rendering")
                internal = self.repository.item(run_id, composition_id)
                final_path = Path(str(internal["output_path"]))
                expected = float(internal["expected_duration"])
                if final_path.exists():
                    validation = self.media.validate_output(final_path, expected, len(composition["items"]))
                    result = {
                        "rendered_duration": validation["duration"],
                        "duration_delta": validation["duration"] - expected,
                        "source_verification_seconds": 0.0,
                        "extraction_seconds": 0.0,
                        "concat_encode_seconds": 0.0,
                        "total_seconds": time.perf_counter() - started,
                        "normalization": {"recovered_valid_atomic_output": True},
                        "diagnostics": {"recovered_valid_atomic_output": True, "segments": []},
                    }
                    self.repository.complete_item(run_id, composition_id, result)
                    self._write_report(run_id)
                    continue

                verify_started = time.perf_counter()
                verified: dict[int, Path] = {}
                for source_item in sorted(composition["items"], key=lambda row: int(row["position"])):
                    verified[int(source_item["position"])] = verify_manifest_source(
                        source_item, getattr(self.cfg, "QUEUE_INPUT_DIR"), verification_cache,
                    )
                verification_seconds = time.perf_counter() - verify_started
                work_dir = Path(str(internal["output_directory"])) / "temp" / _safe_name(composition_id)
                result = self.media.render(
                    composition, work_dir, final_path, verified,
                    source_probe_cache=source_probe_cache,
                )
                result["source_verification_seconds"] = verification_seconds
                result["total_seconds"] = time.perf_counter() - started
                self.repository.complete_item(run_id, composition_id, result)
                shutil.rmtree(work_dir, ignore_errors=True)
            except RenderMediaError as exc:
                self.repository.fail_item(run_id, composition_id, exc.code, str(exc), time.perf_counter() - started)
            except Exception as exc:
                self.repository.fail_item(run_id, composition_id, "render_unexpected", str(exc), time.perf_counter() - started)
            self._write_report(run_id)
        self.repository.finish_run(run_id)
        self._write_report(run_id)

    def _wait_for_production(self, run_id: str, composition_id: str) -> bool:
        waiting = False
        while self._production_active():
            waiting = True
            self.repository.set_run_state(run_id, "waiting_for_production", composition_id)
            self.repository.set_item_state(run_id, composition_id, "waiting_for_production")
            self._write_report(run_id)
            if self._stop.wait(self.wait_poll_seconds):
                self.repository.set_item_state(run_id, composition_id, "queued")
                self.repository.set_run_state(run_id, "queued")
                return False
        if waiting:
            self.repository.set_item_state(run_id, composition_id, "queued")
            self.repository.set_run_state(run_id, "queued")
        return True

    def _write_report(self, run_id: str) -> None:
        try:
            run = self.repository.get_run(run_id)
            directory = Path(str(run["output_directory"]))
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / "render_report.json"
            temporary = directory / "render_report.partial.json"
            temporary.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(target)
        except OSError:
            pass

    @staticmethod
    def _public_run(run: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(json.dumps(run))
        payload.pop("output_directory", None)
        payload.pop("request_key", None)
        payload.pop("planner_manifest_checksum", None)
        return payload
