from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from clipper_app.contracts.modular_variant_pilot_models import ModularVariantPilotCreateRequest
from clipper_app.modular_planner import ModularPlannerService
from clipper_app.modular_renderer import ModularRendererService
from clipper_app.modular_scanner.service import production_is_active
from clipper_app.variant_generation import BaseClipArtifact, generate_base_clip_variants, safe_clip_id
from product_broll import canonical_product
from variation_profile import load_active_profile, load_preset

from .repository import ModularVariantPilotRepository
from .transcript_bridge import BRIDGE_VERSION, SourceTranscriptResolver, bridge_composition_words


class ModularVariantPilotConflict(RuntimeError):
    pass


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-._")[:64] or "base"


def _file_identity(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_words(composition: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    words: list[dict[str, Any]] = []
    cursor = 0.0
    hook = ""
    for item in sorted(composition.get("items", []), key=lambda row: int(row["position"])):
        text = " ".join(str(item.get("transcript_text") or "").split())
        duration = max(0.1, float(item["end_seconds"]) - float(item["start_seconds"]))
        tokens = text.split()
        if not hook and str(item.get("role")) == "hook":
            hook = text
        for index, token in enumerate(tokens):
            start = cursor + duration * index / max(1, len(tokens))
            end = cursor + duration * (index + 1) / max(1, len(tokens))
            words.append({"word": token, "start": round(start, 3), "end": round(end, 3), "probability": 1.0})
        cursor += duration
    return words, hook


class ModularVariantPilotService:
    """Pilot-only adapter from durable modular render IDs to the ordinary Variants boundary."""

    def __init__(
        self,
        cfg: Any,
        *,
        renderer: ModularRendererService,
        planner: ModularPlannerService,
        repository: ModularVariantPilotRepository | None = None,
        generator: Callable[..., list[dict[str, Any]]] = generate_base_clip_variants,
        production_active: Callable[[], bool] | None = None,
        wait_poll_seconds: float = 5.0,
        start_worker: bool = True,
    ):
        self.cfg = cfg
        working = Path(str(getattr(cfg, "WORKING_DIR", "working") or "working"))
        self.storage_root = (working / "modular_variant_pilot").resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.renderer = renderer
        self.planner = planner
        self.repository = repository or ModularVariantPilotRepository(working / "modular_variant_pilot.sqlite3")
        self.transcript_resolver = SourceTranscriptResolver(working / "modular_library.sqlite3")
        self.generator = generator
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
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="modular-variant-pilot", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self._stop.set(); self._tasks.put(None)
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2)

    def profiles(self) -> dict[str, Any]:
        from variation_profile import list_presets
        active = load_active_profile(self.cfg)
        presets = []
        for row in list_presets(self.cfg):
            profile = load_preset(self.cfg, row["preset_id"])
            presets.append({
                "profile_id": row["preset_id"], "name": row["name"], "revision": row["revision"],
                "variant_count": len(profile.get("variants", [])),
            })
        return {
            "profiles": [{"profile_id": "active", "name": "Active variation profile", "revision": active["revision"], "variant_count": len(active.get("variants", []))}]
            + presets,
            "required_variant_count": 6,
        }

    def eligible(self, planner_run_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for run in self.renderer.repository.list_runs(planner_run_id, 100):
            for item in run["items"]:
                if item["status"] != "completed" or not self._compatible_renderer(run["renderer_version"]):
                    continue
                try:
                    path = self.renderer.media_path(run["render_run_id"], item["composition_id"])
                except (KeyError, OSError, PermissionError, RuntimeError):
                    continue
                result.append({
                    "render_run_id": run["render_run_id"], "composition_id": item["composition_id"],
                    "product": item["product"], "ordinal": item["ordinal"], "renderer_version": run["renderer_version"],
                    "rendered_duration": item.get("rendered_duration"), "base_identity": _file_identity(path),
                })
        return result

    def create_run(self, request: ModularVariantPilotCreateRequest) -> tuple[dict[str, Any], bool]:
        profile = load_active_profile(self.cfg) if request.profile_id == "active" else load_preset(self.cfg, request.profile_id)
        if len(profile.get("variants", [])) != 6:
            raise ModularVariantPilotConflict("The pilot requires an existing six-variant profile")
        items: list[dict[str, Any]] = []
        request_parts: list[str] = []
        for ref in request.bases:
            item = self._adapt(ref.render_run_id, ref.composition_id)
            items.append(item); request_parts.append(f"{ref.render_run_id}:{ref.composition_id}:{item['base_identity']}")
        request_key = hashlib.sha256(("|".join(sorted(request_parts)) + "|" + str(profile["revision"]) + "|" + BRIDGE_VERSION).encode()).hexdigest()
        reusable = self.repository.find_reusable(request_key)
        if reusable and (not request.manual_rerun or reusable["status"] in {"queued", "waiting_for_production", "generating"}):
            return self._public_run(reusable), True
        run_id = uuid.uuid4().hex
        root = self.storage_root / run_id
        for item in items:
            item["output_directory"] = str((root / f"{item['ordinal']:03d}_{_safe_name(item['composition_id'])}_{item['product']}").resolve())
        created = self.repository.create_run({
            "run_id": run_id, "request_key": request_key, "profile_id": request.profile_id,
            "profile_revision": profile["revision"], "profile": profile, "output_directory": str(root),
            "requested_variant_count": 6, "rerun_of_run_id": reusable["run_id"] if reusable else None,
            "transcript_bridge_version": BRIDGE_VERSION,
        }, items)
        root.mkdir(parents=True, exist_ok=True); self._write_report(run_id); self._enqueue(run_id)
        return self._public_run(created), False

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._public_run(self.repository.get_run(run_id))

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [self._public_run(run) for run in self.repository.list_runs(limit)]

    def media_path(self, media_id: str) -> Path:
        path = self.repository.media_path(media_id)
        try: path.relative_to(self.storage_root)
        except ValueError as exc: raise PermissionError("Variant output is outside pilot storage") from exc
        if not path.is_file() or path.stat().st_size <= 0: raise FileNotFoundError("Variant media is missing")
        return path

    def process_pending_once(self) -> bool:
        pending = self.repository.pending_run_ids()
        if not pending: return False
        self._process_run(pending[0]); return True

    def _adapt(self, render_run_id: str, composition_id: str) -> dict[str, Any]:
        run = self.renderer.repository.get_run(render_run_id)
        if not self._compatible_renderer(str(run["renderer_version"])):
            raise ModularVariantPilotConflict("Render item was not produced by modular-renderer-v1.1 or a compatible version")
        internal = self.renderer.repository.item(render_run_id, composition_id)
        if internal["status"] != "completed": raise ModularVariantPilotConflict("Only completed modular render items are eligible")
        diagnostics = json.loads(str(internal.get("diagnostics_json") or "{}"))
        if not diagnostics and str(run["renderer_version"]) == "modular-renderer-v1.1":
            raise ModularVariantPilotConflict("Renderer validation diagnostics are missing")
        planner_run = self.planner.get_run(run["planner_run_id"])
        if planner_run["status"] != "approved": raise ModularVariantPilotConflict("Planner lineage is not approved")
        manifest = self.planner.manifest(run["planner_run_id"], public=False)
        composition = next((row for row in manifest["payload"].get("compositions", []) if row["composition_id"] == composition_id), None)
        if composition is None: raise ModularVariantPilotConflict("Composition is not in the approved manifest")
        path = self.renderer.media_path(render_run_id, composition_id)
        product = canonical_product(internal["product"])
        if product is None or product != canonical_product(manifest["payload"]["product"]):
            raise ModularVariantPilotConflict("Render product does not match approved composition metadata")
        words, hook, transcript_diagnostics = bridge_composition_words(
            composition, self.transcript_resolver,
            rendered_duration=float(internal["rendered_duration"]) if internal.get("rendered_duration") is not None else None,
        )
        return {
            "render_run_id": render_run_id, "modular_render_item_id": f"{render_run_id}:{composition_id}",
            "planner_run_id": run["planner_run_id"], "composition_id": composition_id, "product": product,
            "renderer_version": run["renderer_version"], "base_path": str(path), "base_identity": _file_identity(path),
            "ordinal": int(internal["ordinal"]), "transcript_words": words, "hook_text": hook,
            "transcript_diagnostics": transcript_diagnostics,
        }

    @staticmethod
    def _compatible_renderer(version: str) -> bool:
        match = re.fullmatch(r"modular-renderer-v(\d+)(?:\.(\d+))?", version)
        return bool(match and (int(match.group(1)), int(match.group(2) or 0)) >= (1, 1))

    def _enqueue(self, run_id: str) -> None:
        with self._guard:
            if run_id in self._queued: return
            self._queued.add(run_id)
        self._tasks.put(run_id)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            run_id = self._tasks.get()
            if run_id is None: return
            with self._guard: self._queued.discard(run_id)
            try: self._process_run(run_id)
            except Exception:
                try: self.repository.set_run_state(run_id, "queued")
                except Exception: pass

    def _process_run(self, run_id: str) -> None:
        run = self.repository.get_run(run_id)
        if run["status"] not in {"queued", "waiting_for_production", "generating"}: return
        for public_item in run["items"]:
            if public_item["status"] in {"completed", "failed"}: continue
            item_id = public_item["modular_render_item_id"]
            if self._stop.is_set(): self.repository.set_run_state(run_id, "queued"); return
            while self._production_active():
                self.repository.set_run_state(run_id, "waiting_for_production", item_id)
                self.repository.set_item_state(run_id, item_id, "waiting_for_production"); self._write_report(run_id)
                if self._stop.wait(self.wait_poll_seconds): self.repository.set_run_state(run_id, "queued"); return
            item = self.repository.item(run_id, item_id)
            started = time.perf_counter()
            try:
                self.repository.set_run_state(run_id, "generating", item_id); self.repository.set_item_state(run_id, item_id, "generating")
                outputs = self.generator(BaseClipArtifact(
                    clip_id=safe_clip_id(item["composition_id"]), path=Path(item["base_path"]), product=item["product"],
                    transcript_words=tuple(item["transcript_words"]), hook=item["hook_text"],
                ), item["output_directory"], self.cfg, run["profile"], expected_count=6)
                for row in outputs: row["media_id"] = uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{item_id}:{row['variant_index']}").hex
                self.repository.replace_outputs(run_id, item_id, outputs)
                self.repository.complete_item(run_id, item_id, len(outputs), time.perf_counter() - started)
            except Exception as exc:
                self.repository.fail_item(run_id, item_id, str(exc), time.perf_counter() - started)
            self._write_report(run_id)
        self.repository.finish_run(run_id); self._write_report(run_id)

    def _write_report(self, run_id: str) -> None:
        try:
            run = self.repository.get_run(run_id); root = Path(run["output_directory"]); root.mkdir(parents=True, exist_ok=True)
            for item in run.get("items", []):
                internal = self.repository.item(run_id, item["modular_render_item_id"])
                item["base_path"] = internal["base_path"]
            temporary = root / "pilot_report.partial.json"; temporary.write_text(json.dumps(run, indent=2), encoding="utf-8"); temporary.replace(root / "pilot_report.json")
        except OSError: pass

    @staticmethod
    def _public_run(run: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(json.dumps(run)); payload.pop("request_key", None); payload.pop("output_directory", None); payload.pop("profile", None)
        for item in payload.get("items", []):
            for output in item.get("outputs", []):
                output.pop("output_path", None); output["url"] = f"/api/modular-variant-pilot/media/{output['media_id']}"
        return payload
