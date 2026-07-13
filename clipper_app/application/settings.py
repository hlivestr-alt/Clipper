from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from clipper_app.contracts.models import SettingEntry, SettingsSnapshot


DEPRECATED_SETTINGS_OVERRIDES = frozenset({
    "BEFORE_AFTER_ENABLED",
    "VARIANT_FFMPEG_BAKE",
    "VARIANTS_PER_CLIP",
})

LEGACY_SETTINGS_ALIASES = {
    "LM_STUDIO_MODEL": "LM_STUDIO_MOMENT_MODEL_ID",
    "SCORER_VISION_MODEL": "SCORER_VISION_MODEL_ID",
    "QUEUE_SCAN_INTERVAL_SECONDS": "QUEUE_RESCAN_INTERVAL_SECONDS",
}


def normalize_setting_aliases(values: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(values)
    for alias, canonical in LEGACY_SETTINGS_ALIASES.items():
        if alias in normalized and canonical not in normalized:
            normalized[canonical] = normalized[alias]
        normalized.pop(alias, None)
    return normalized

@dataclass(frozen=True)
class SettingDefinition:
    name: str
    value_type: type
    category: str
    minimum: float | None = None
    maximum: float | None = None


# Operation-local controls are accepted from typed commands but are deliberately
# excluded from SETTINGS_REGISTRY so they are not exposed as persistent Settings
# page values.
RUNTIME_SETTINGS_REGISTRY = {
    "BEFORE_AFTER_ENABLED": SettingDefinition("BEFORE_AFTER_ENABLED", bool, "runtime"),
    "VARIANT_FFMPEG_BAKE": SettingDefinition("VARIANT_FFMPEG_BAKE", bool, "runtime"),
    "VARIANTS_PER_CLIP": SettingDefinition("VARIANTS_PER_CLIP", int, "runtime", 1, 6),
    "VARIANT_SELECTION_MODE": SettingDefinition("VARIANT_SELECTION_MODE", str, "runtime"),
    "SETTINGS_REVISION": SettingDefinition("SETTINGS_REVISION", str, "runtime"),
}


def _definitions() -> tuple[SettingDefinition, ...]:
    bool_keys = {
        "QUEUE_YOLO_IN_SUBPROCESS": "queue",
        "LM_STUDIO_QWEN_THINKING_ENABLED": "models",
        "LM_STUDIO_MODEL_MANAGEMENT_ENABLED": "models",
        "WHISPERX_ALIGN_IN_SUBPROCESS": "models",
        "WHISPERX_FALLBACK_TO_RAW_ON_OOM": "models",
        "WHISPERX_FALLBACK_TO_RAW_ON_ALIGNMENT_CRASH": "models",
        "WHISPERX_ACCEPT_RAW_FALLBACK_CACHE": "models",
        "BGM_ENABLED": "render",
        "BGM_DUCKING_ENABLED": "render",
        "EXPORT_BATCHES_ENABLED": "render",
        "HOST_FACE_ZOOM_ENABLED": "render",
        "SFX_ENABLED": "render",
        "SILENCE_TRIM_ENABLED": "render",
        "SCORER_ENABLED": "scoring",
        "SCORER_FORCE_RESCORE": "scoring",
        "SCORER_AUTO_SORT_ENABLED": "scoring",
        "SCORER_VISION_ENABLED": "scoring",
        "COMPLIANCE_ENABLED": "compliance",
        "COMPLIANCE_AUTO_FIX": "compliance",
        "COMPLIANCE_BLOCK_HIGH": "compliance",
        "MODULE_EXTRACTION_ENABLED": "modules",
        "MODULE_ASSEMBLY_ENABLED": "modules",
        "MODULE_ASSEMBLY_REQUIRE_APPROVED": "modules",
        "MODULE_ASSEMBLY_SAME_DATE_ONLY": "modules",
        "MODULE_ASSEMBLY_REQUIRE_ZOOM_READY": "modules",
        "MODULE_REBUILD_INDEX_BEFORE_ASSEMBLY": "modules",
        "MODULE_VALIDATE_ON_EXTRACT": "modules",
        "MODULE_WORD_FALLBACK_REVIEW_REQUIRED": "modules",
        "MODULE_PRODUCT_EVIDENCE_REQUIRED": "modules",
        "MODULE_PRODUCT_ZOOM_ENABLED": "modules",
    }
    int_keys = {
        "QUEUE_START_RUN_NUMBER": ("queue", 1, None),
        "QUEUE_MAX_RETRIES": ("queue", 0, None),
        "QUEUE_MAX_INFLIGHT_VIDEOS": ("queue", 1, None),
        "QUEUE_FFMPEG_MAX_PARALLEL_CLIPS": ("queue", 1, None),
        "QUEUE_RESTART_DELAY_SECONDS": ("queue", 0, None),
        "QUEUE_BETWEEN_RUNS_DELAY_SECONDS": ("queue", 0, None),
        "WHISPER_BEAM_SIZE": ("models", 1, None),
        "WHISPER_BEST_OF": ("models", 1, None),
        "CHUNK_DURATION": ("selection", 1, None),
        "CHUNK_OVERLAP": ("selection", 0, None),
        "MAX_PARALLEL_CLIPS": ("render", 1, None),
        "OUTPUT_FPS": ("render", 1, 120),
        "OUTPUT_CQ": ("render", 0, 63),
        "SCORER_BATCH_FLUSH_EVERY": ("scoring", 1, None),
        "SCORER_FRAME_SAMPLE_RATE": ("scoring", 1, None),
        "SCORER_TOP_VARIANTS_PER_CLIP": ("scoring", 0, None),
        "MODULE_ASSEMBLY_RENDER_LIMIT": ("modules", 0, None),
        "MODULE_ASSEMBLY_MAX_PER_PRODUCT": ("modules", 1, None),
        "MODULE_ASSEMBLY_CANDIDATE_POOL": ("modules", 1, None),
        "MODULE_ASSEMBLY_MIN_SOURCE_VIDEOS": ("modules", 1, None),
        "MODULE_CLASSIFIER_WORKERS": ("modules", 1, None),
    }
    float_keys = {
        "QUEUE_POLL_INTERVAL": ("queue", 0.5, None),
        "QUEUE_RESCAN_INTERVAL_SECONDS": ("queue", 0.5, None),
        "QUEUE_STABLE_SECONDS": ("queue", 0, None),
        "QUEUE_DASHBOARD_RUNNING_STALL_SECONDS": ("queue", 1, None),
        "QUEUE_DASHBOARD_QUEUED_STALL_SECONDS": ("queue", 1, None),
        "LM_STUDIO_TIMEOUT": ("models", 1, None),
        "LM_STUDIO_TEMPERATURE": ("models", 0, 2),
        "LM_STUDIO_MODEL_UNLOAD_TIMEOUT": ("models", 1, None),
        "LM_STUDIO_MODEL_UNLOAD_LOG_INTERVAL": ("models", 1, None),
        "MIN_CLIP_DURATION": ("selection", 0, None),
        "MAX_CLIP_DURATION": ("selection", 0, None),
        "MIN_SCORE": ("selection", 0, 10),
        "PAD_START": ("selection", 0, None),
        "PAD_END": ("selection", 0, None),
        "BROLL_INTRO_MIN_VARIANT_RATE": ("render", 0, 1),
        "BROLL_INTRO_MAX_VARIANT_RATE": ("render", 0, 1),
        "FACE_ZOOM_SCALE_MIN": ("render", 1, 3),
        "FACE_ZOOM_SCALE_MAX": ("render", 1, 3),
        "FACE_ZOOM_DUR_MIN": ("render", 0, None),
        "FACE_ZOOM_DUR_MAX": ("render", 0, None),
        "BGM_VOLUME": ("render", 0, 1),
        "SFX_VOLUME_PRODUCT": ("render", 0, 1),
        "SILENCE_TRIM_MIN_GAP": ("render", 0, None),
        "SILENCE_TRIM_KEEP_GAP": ("render", 0, None),
        "SILENCE_TRIM_EDGE_KEEP": ("render", 0, None),
        "SILENCE_TRIM_MAX_REMOVAL_FRACTION": ("render", 0, 1),
        "SCORER_EXPORT_READY_THRESHOLD": ("scoring", 0, 10),
        "SCORER_REVIEW_THRESHOLD": ("scoring", 0, 10),
        "COMPLIANCE_LM_TIMEOUT": ("compliance", 1, None),
        "SCORER_VISION_TIMEOUT": ("scoring", 1, None),
        "MODULE_CLASSIFICATION_MIN_CONFIDENCE": ("modules", 0, 1),
        "MODULE_HOOK_MIN_DURATION": ("modules", 0, None),
        "MODULE_HOOK_MAX_DURATION": ("modules", 0, None),
        "MODULE_MAIN_MIN_DURATION": ("modules", 0, None),
        "MODULE_MAIN_MAX_DURATION": ("modules", 0, None),
        "MODULE_CTA_MIN_DURATION": ("modules", 0, None),
        "MODULE_CTA_MAX_DURATION": ("modules", 0, None),
        "MODULE_VISUAL_VALIDATION_MIN_CONFIDENCE": ("modules", 0, 1),
    }
    string_keys = {
        "OUTPUT_DIR": "paths",
        "WORKING_DIR": "paths",
        "YOLO_WEIGHTS": "paths",
        "QUEUE_INPUT_DIR": "paths",
        "QUEUE_STATE_FILE": "paths",
        "QUEUE_FOREVER_STATE_FILE": "paths",
        "QUEUE_CONTROL_FILE": "paths",
        "MODULE_LIBRARY_DIR": "paths",
        "LM_STUDIO_BASE_URL": "models",
        "LM_STUDIO_MOMENT_MODEL_ID": "models",
        "SCORER_VISION_BASE_URL": "models",
        "SCORER_VISION_MODEL_ID": "models",
        "WHISPER_MODEL_SIZE": "models",
        "WHISPER_DEVICE": "models",
        "WHISPERX_DEVICE": "models",
        "WHISPER_COMPUTE": "models",
        "WHISPER_LANGUAGE": "models",
        "OUTPUT_CODEC": "render",
        "OUTPUT_PRESET": "render",
        "OUTPUT_NVENC_PRESET": "render",
    }
    definitions: list[SettingDefinition] = []
    definitions.extend(SettingDefinition(key, bool, category) for key, category in bool_keys.items())
    definitions.extend(
        SettingDefinition(key, int, category, minimum, maximum)
        for key, (category, minimum, maximum) in int_keys.items()
    )
    definitions.extend(
        SettingDefinition(key, float, category, minimum, maximum)
        for key, (category, minimum, maximum) in float_keys.items()
    )
    definitions.extend(SettingDefinition(key, str, category) for key, category in string_keys.items())
    return tuple(sorted(definitions, key=lambda definition: definition.name))


SETTINGS_REGISTRY = {definition.name: definition for definition in _definitions()}

PRIVILEGED_SETTINGS = frozenset({
    "OUTPUT_DIR",
    "WORKING_DIR",
    "YOLO_WEIGHTS",
    "QUEUE_INPUT_DIR",
    "QUEUE_STATE_FILE",
    "QUEUE_FOREVER_STATE_FILE",
    "QUEUE_CONTROL_FILE",
    "MODULE_LIBRARY_DIR",
    "LM_STUDIO_BASE_URL",
    "SCORER_VISION_BASE_URL",
})
BROWSER_EDITABLE_SETTINGS = frozenset(SETTINGS_REGISTRY) - PRIVILEGED_SETTINGS


def validate_setting_relationships(values: Mapping[str, Any]) -> None:
    errors: list[str] = []

    def ordered(low: str, high: str, label: str) -> None:
        if low in values and high in values and values[low] > values[high]:
            errors.append(f"{label}: {low} must be <= {high}")

    ordered("MIN_CLIP_DURATION", "MAX_CLIP_DURATION", "Clip duration range")
    ordered("SCORER_REVIEW_THRESHOLD", "SCORER_EXPORT_READY_THRESHOLD", "Scoring thresholds")
    ordered("BROLL_INTRO_MIN_VARIANT_RATE", "BROLL_INTRO_MAX_VARIANT_RATE", "B-roll variant rate")
    ordered("FACE_ZOOM_SCALE_MIN", "FACE_ZOOM_SCALE_MAX", "Face zoom scale")
    ordered("FACE_ZOOM_DUR_MIN", "FACE_ZOOM_DUR_MAX", "Face zoom duration")
    ordered("MODULE_HOOK_MIN_DURATION", "MODULE_HOOK_MAX_DURATION", "Hook module duration")
    ordered("MODULE_MAIN_MIN_DURATION", "MODULE_MAIN_MAX_DURATION", "Main module duration")
    ordered("MODULE_CTA_MIN_DURATION", "MODULE_CTA_MAX_DURATION", "CTA module duration")

    if (
        "CHUNK_OVERLAP" in values
        and "CHUNK_DURATION" in values
        and values["CHUNK_OVERLAP"] >= values["CHUNK_DURATION"]
    ):
        errors.append("Chunking: CHUNK_OVERLAP must be < CHUNK_DURATION")

    whisper_device = str(values.get("WHISPER_DEVICE") or "").strip().casefold()
    whisper_compute = str(values.get("WHISPER_COMPUTE") or "").strip().casefold()
    if whisper_device == "cpu" and whisper_compute == "float16":
        errors.append("Whisper: WHISPER_COMPUTE=float16 is not valid with WHISPER_DEVICE=cpu")

    codec = str(values.get("OUTPUT_CODEC") or "").strip().casefold()
    nvenc_preset = str(values.get("OUTPUT_NVENC_PRESET") or "").strip().casefold()
    if (
        codec
        and codec.endswith("_nvenc")
        and "OUTPUT_NVENC_PRESET" in values
        and nvenc_preset not in {f"p{index}" for index in range(1, 8)}
    ):
        errors.append("Encoder: OUTPUT_NVENC_PRESET must be p1 through p7 for an NVENC codec")
    if (
        codec
        and not codec.endswith("_nvenc")
        and "OUTPUT_PRESET" in values
        and not str(values.get("OUTPUT_PRESET") or "").strip()
    ):
        errors.append("Encoder: OUTPUT_PRESET is required for a non-NVENC codec")

    if errors:
        raise ValueError("; ".join(errors))


class LegacyConfigProvider:
    def __init__(
        self,
        config_module: Any | None = None,
        *,
        overrides_path: str | Path | None = None,
        include_persisted_overrides: bool = True,
    ) -> None:
        if config_module is None:
            import config as config_module  # type: ignore
        self.config_module = config_module
        self.include_persisted_overrides = include_persisted_overrides
        self.overrides_path = Path(overrides_path) if overrides_path is not None else self._default_overrides_path()
        self._snapshot_lock = threading.RLock()
        self._snapshot_cache_key: tuple[Any, ...] | None = None
        self._snapshot_cache: SettingsSnapshot | None = None

    def snapshot(self, overrides: Mapping[str, Any] | None = None) -> SettingsSnapshot:
        cache_key = self._base_snapshot_cache_key() if not overrides else None
        if cache_key is not None:
            with self._snapshot_lock:
                if self._snapshot_cache_key == cache_key and self._snapshot_cache is not None:
                    return self._snapshot_cache
        persisted = self._load_persisted_overrides() if self.include_persisted_overrides else {}
        command_overrides = normalize_setting_aliases(overrides or {})
        stale_persisted = set(persisted) & DEPRECATED_SETTINGS_OVERRIDES
        active_persisted = {key: value for key, value in persisted.items() if key not in stale_persisted}
        accepted_command_names = set(SETTINGS_REGISTRY) | set(RUNTIME_SETTINGS_REGISTRY)
        unknown = sorted(
            (set(active_persisted) - set(SETTINGS_REGISTRY))
            | (set(command_overrides) - accepted_command_names)
        )
        if unknown:
            raise ValueError(f"Unsupported settings override(s): {', '.join(unknown)}")
        entries: list[SettingEntry] = []
        definitions = dict(SETTINGS_REGISTRY)
        definitions.update(
            (name, definition)
            for name, definition in RUNTIME_SETTINGS_REGISTRY.items()
            if name in command_overrides
        )
        for name, definition in definitions.items():
            if name in command_overrides:
                value = self._validate(definition, command_overrides[name])
                source = "runtime_override"
            elif name in active_persisted:
                value = self._validate(definition, active_persisted[name])
                source = "settings_override"
            elif hasattr(self.config_module, name):
                value = self._validate(definition, getattr(self.config_module, name))
                source = "legacy_config"
            else:
                continue
            entries.append(SettingEntry(name=name, value=value, source=source))
        normalized = [(entry.name, entry.value, entry.source) for entry in entries]
        validate_setting_relationships({entry.name: entry.value for entry in entries})
        revision = hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        snapshot = SettingsSnapshot(entries=tuple(entries), revision=revision)
        if cache_key is not None:
            with self._snapshot_lock:
                self._snapshot_cache_key = cache_key
                self._snapshot_cache = snapshot
        return snapshot

    def invalidate(self) -> None:
        with self._snapshot_lock:
            self._snapshot_cache_key = None
            self._snapshot_cache = None

    def _base_snapshot_cache_key(self) -> tuple[Any, ...]:
        path = self.overrides_path
        try:
            stat_key = (str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_size) if path is not None else ("", 0, 0)
        except OSError:
            stat_key = (str(path.resolve()) if path is not None else "", 0, 0)
        config_values = tuple(
            (name, repr(getattr(self.config_module, name)))
            for name in SETTINGS_REGISTRY
            if hasattr(self.config_module, name)
        )
        return (self.include_persisted_overrides, stat_key, config_values)

    def _default_overrides_path(self) -> Path:
        working_dir = Path(str(getattr(self.config_module, "WORKING_DIR", "working") or "working"))
        if not working_dir.is_absolute():
            working_dir = Path.cwd() / working_dir
        return working_dir / "settings_overrides.json"

    def _load_persisted_overrides(self) -> dict[str, Any]:
        path = self.overrides_path
        if path is None or not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Could not read settings overrides: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("settings_overrides.json must contain a JSON object")
        overrides = payload.get("overrides", {})
        if overrides is None:
            return {}
        if not isinstance(overrides, dict):
            raise ValueError("settings_overrides.json overrides must be an object")
        return normalize_setting_aliases(overrides)

    @staticmethod
    def _validate(definition: SettingDefinition, value: Any) -> Any:
        if definition.value_type is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if definition.value_type is int and isinstance(value, bool):
            raise ValueError(f"{definition.name} must be an integer")
        if not isinstance(value, definition.value_type):
            raise ValueError(f"{definition.name} must be {definition.value_type.__name__}")
        if definition.minimum is not None and value < definition.minimum:
            raise ValueError(f"{definition.name} must be >= {definition.minimum}")
        if definition.maximum is not None and value > definition.maximum:
            raise ValueError(f"{definition.name} must be <= {definition.maximum}")
        return value

    def runtime_view(
        self,
        snapshot: SettingsSnapshot | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> "RuntimeSettingsView":
        snapshot = snapshot or self.snapshot()
        validated = self.snapshot(overrides).as_dict() if overrides else {}
        return RuntimeSettingsView(self.config_module, snapshot, validated)

    def live_view(self) -> "LiveSettingsView":
        return LiveSettingsView(self)

    def snapshot_from_file(self, path: str | Path) -> SettingsSnapshot:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("values"), dict):
            raise ValueError("Settings snapshot file must contain a values object")
        values = payload["values"]
        sources = payload.get("sources", {}) if isinstance(payload.get("sources"), dict) else {}
        unknown = sorted(set(values) - set(SETTINGS_REGISTRY))
        if unknown:
            raise ValueError(f"Unsupported settings snapshot value(s): {', '.join(unknown)}")
        entries = tuple(
            SettingEntry(
                name=name,
                value=self._validate(SETTINGS_REGISTRY[name], value),
                source=str(sources.get(name) or "legacy_config"),
            )
            for name, value in sorted(values.items())
        )
        normalized = [(entry.name, entry.value, entry.source) for entry in entries]
        revision = str(payload.get("revision") or "")
        calculated = hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if revision and revision != calculated:
            raise ValueError("Settings snapshot revision does not match its values")
        return SettingsSnapshot(entries=entries, revision=calculated)

    def write_snapshot_file(self, snapshot: SettingsSnapshot, path: str | Path) -> Path:
        target = Path(path)
        payload = {
            "schema_version": 1,
            "revision": snapshot.revision,
            "values": snapshot.as_dict(),
            "sources": {entry.name: entry.source for entry in snapshot.entries},
        }
        if target.exists():
            existing = self.snapshot_from_file(target)
            if existing.revision != snapshot.revision:
                raise ValueError(f"Settings snapshot path already contains a different revision: {target}")
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(target)
        return target


class RuntimeSettingsView:
    """Attribute-compatible, operation-local overlay over legacy config."""

    def __init__(self, base: Any, snapshot: SettingsSnapshot, overrides: Mapping[str, Any] | None = None) -> None:
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_snapshot", MappingProxyType(snapshot.as_dict()))
        object.__setattr__(self, "_overrides", dict(overrides or {}))
        object.__setattr__(self, "_settings_revision", snapshot.revision)

    def __getattr__(self, name: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        canonical_name = LEGACY_SETTINGS_ALIASES.get(name)
        if canonical_name:
            if canonical_name in overrides:
                return overrides[canonical_name]
            snapshot = object.__getattribute__(self, "_snapshot")
            if canonical_name in snapshot:
                return snapshot[canonical_name]
        if name in overrides:
            return overrides[name]
        snapshot = object.__getattribute__(self, "_snapshot")
        if name in snapshot:
            return snapshot[name]
        return getattr(object.__getattribute__(self, "_base"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        object.__getattribute__(self, "_overrides")[name] = value

    def with_overrides(self, overrides: Mapping[str, Any]) -> "RuntimeSettingsView":
        combined = dict(object.__getattribute__(self, "_overrides"))
        combined.update(overrides)
        snapshot = SettingsSnapshot(
            entries=tuple(
                SettingEntry(name=name, value=value)
                for name, value in object.__getattribute__(self, "_snapshot").items()
            ),
            revision="runtime",
        )
        return RuntimeSettingsView(object.__getattribute__(self, "_base"), snapshot, combined)


class LiveSettingsView:
    """Read-through view that reflects the latest persisted settings revision."""

    def __init__(self, provider: LegacyConfigProvider) -> None:
        object.__setattr__(self, "_provider", provider)

    def __getattr__(self, name: str) -> Any:
        provider = object.__getattribute__(self, "_provider")
        return getattr(provider.runtime_view(provider.snapshot()), name)
