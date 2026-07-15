from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import quote

from clipper_app.application.log_tail import reverse_tail
from clipper_app.application.queue_repository import QueueStateRepository, queue_storage_mode
from clipper_app.application.read_cache import ReadCache
from clipper_app.application.settings import BROWSER_EDITABLE_SETTINGS, LegacyConfigProvider, SETTINGS_REGISTRY
from clipper_app.contracts.read_models import (
    ArtifactRef,
    ComplianceIndexPage,
    ComplianceRow,
    ComplianceViolationRow,
    DashboardSummary,
    LogLine,
    LogTail,
    ModuleLibraryPage,
    ModuleLibraryRow,
    ModuleDetail,
    ModuleReadiness,
    ModuleReadinessRow,
    OverviewCompliance,
    OverviewExport,
    OverviewScoreTrendPoint,
    OverviewSummary,
    OverviewTopClip,
    QueueDetail,
    QueueRunRow,
    QueueVodFile,
    QueueVodList,
    ScoreDetail,
    ScoreIndexPage,
    ScoreRow,
    ScoreStats,
    SettingsReadEntry,
    SettingsReadSnapshot,
    SourceSignature,
    SystemStats,
)


STAGES: tuple[tuple[str, str], ...] = (
    ("transcribe", "Transcription"),
    ("llm", "Sales Moment Detection"),
    ("yolo", "Product/Face Scan"),
    ("ffmpeg", "Clip Rendering"),
)
STAGE_LABELS = {key: label for key, label in STAGES}
MODULE_PRODUCTS: tuple[tuple[str, str], ...] = (
    ("cleanser", "Cleanser"),
    ("toner", "Toner"),
    ("serum", "Serum"),
    ("eye_cream", "Eye Cream"),
    ("mask", "Mask"),
    ("skin_cream", "Skin Cream"),
)
MODULE_ROLES = ("hook", "main", "cta")
MODULE_PRODUCT_LABELS = dict(MODULE_PRODUCTS)
MIN_SORT_TIMESTAMP = datetime(1970, 1, 1, tzinfo=datetime.now().astimezone().tzinfo)


@dataclass(frozen=True)
class ReadServiceResult:
    data: Any
    source_signatures: tuple[SourceSignature, ...] = ()
    warnings: tuple[str, ...] = ()
    revision: str | None = None


@dataclass(frozen=True)
class ResolvedArtifact:
    path: Path
    media_type: str | None = None


@dataclass(frozen=True)
class ScoreRecord:
    row: ScoreRow
    raw: dict[str, Any]
    base_raw: dict[str, Any]


@dataclass(frozen=True)
class _OverviewScoreCandidate:
    score_key: str
    clip_id: str
    product: str
    total_score: float
    scored_at: str
    source_date: str
    sort_timestamp: str
    output_dir: Path
    artifact_value: Any


@dataclass(frozen=True)
class _OverviewScoreCorpus:
    scored_count: int
    score_total: float
    score_value_count: int
    compliance_blocked_count: int
    trend: tuple[OverviewScoreTrendPoint, ...]
    top_clips: tuple[OverviewTopClip, ...]
    signatures: tuple[SourceSignature, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _OverviewComplianceCorpus:
    scanned: int
    passed: int
    blocked: int
    signatures: tuple[SourceSignature, ...]
    warnings: tuple[str, ...]


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone().isoformat(timespec="seconds")


def score_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def score_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def as_nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def split_output_folder_name(folder_name: str) -> tuple[str, str]:
    if "__" not in folder_name:
        return folder_name, ""
    source_video, run_tag = folder_name.rsplit("__", 1)
    return source_video, run_tag


def build_score_key(clip: dict[str, Any]) -> str:
    raw = str(clip.get("clip_path") or clip.get("output_file") or clip.get("clip_id") or "")
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def source_date_from_source_video(value: Any) -> str:
    text = str(value or "")
    import re

    match = re.search(r"(?P<date>\d{4}-\d{2}-\d{2})-\d{2}-\d{2}-\d{2}", text)
    return match.group("date") if match else ""


def source_video_filename(source_video: Any) -> str:
    if isinstance(source_video, dict):
        source_video = source_video.get("name") or source_video.get("path") or ""
    return Path(str(source_video or "")).name


def module_source_date_value(module: dict[str, Any]) -> str:
    for key in ("source_date", "date"):
        explicit = str(module.get(key) or "").strip()
        if explicit:
            return explicit
    return source_date_from_source_video(module.get("source_video"))


class ReadDashboardService:
    def __init__(
        self,
        settings_provider: LegacyConfigProvider | None = None,
        *,
        force_legacy: bool = False,
    ) -> None:
        self.settings_provider = settings_provider or LegacyConfigProvider()
        self.cfg = self.settings_provider.live_view()
        self._cache = ReadCache(max_entries=96)
        self.catalog_mode = "legacy" if force_legacy else (
            os.getenv("CLIPPER_CATALOG_MODE", "legacy").strip().casefold() or "legacy"
        )
        self._catalog = None
        if self.catalog_mode == "catalog":
            from clipper_app.application.catalog import CatalogDatabase, CatalogQueryService

            self._catalog = CatalogQueryService(CatalogDatabase.from_config(self.cfg), self.cfg)

    def invalidate(self, *domains: str) -> None:
        """Invalidate cached read corpora after a successful mutation."""
        aliases = {
            "queue": ("queue", "dashboard", "output_dirs", "scores", "compliance", "overview"),
            "settings": ("settings", "dashboard", "queue", "modules", "overview"),
            "scores": ("scores", "overview"),
            "compliance": ("compliance", "overview"),
            "modules": ("modules",),
            "outputs": ("output_dirs", "scores", "compliance", "overview"),
            "system": ("system",),
            "overview": ("overview",),
        }
        prefixes: list[str] = []
        for domain in domains:
            prefixes.extend(aliases.get(domain, (domain,)))
        self._cache.invalidate(*prefixes)

    def dashboard(self, state_path: str | None = None) -> ReadServiceResult:
        state, signature, warnings = self._read_queue_state(state_path)
        revision = self._signature_key(signature)
        summary = self._cache.get_or_load(
            "dashboard",
            revision,
            lambda: self._build_dashboard_summary(state, signature.path),
        )
        return ReadServiceResult(summary, (signature,), tuple(warnings))

    def queue_detail(self, state_path: str | None = None) -> ReadServiceResult:
        state, signature, warnings = self._read_queue_state(state_path)
        control, control_signature, control_warnings = self._read_queue_control()
        supervisor, supervisor_signature, supervisor_warnings = self._read_queue_forever()
        warnings.extend(control_warnings)
        warnings.extend(supervisor_warnings)
        revision = tuple(self._signature_key(item) for item in (signature, control_signature, supervisor_signature))

        def build() -> QueueDetail:
            active_launch = self._normalized_launch_config(state.get("launch_config"))
            stored_launch = self._normalized_launch_config(control.get("launch_config"))
            launch = active_launch or stored_launch
            rows = self._queue_rows(state)
            videos = [self._aggregate_video_entry(video) for video in self._state_videos(state)]
            return QueueDetail(
                state_path=signature.path,
                updated_at=str(state.get("updated_at") or "") or None,
                queue_status=str(state.get("queue_status") or "unknown"),
                queue_health=self._queue_health(state),
                control_status=self._effective_control_status(control, supervisor, launch),
                launch_config=launch,
                active_launch_config=active_launch,
                stored_launch_config=stored_launch,
                launch_summary=self._launch_summary(launch),
                stage_waiting=self._stage_waiting_counts(state, videos),
                waiting_videos=self._waiting_video_count(state, videos),
                stage_admission_limit=self._stage_admission_limit(state),
                rows=tuple(rows),
            )

        data = self._cache.get_or_load("queue:detail", revision, build)
        return ReadServiceResult(data, (signature, control_signature, supervisor_signature), tuple(warnings))

    def queue_vods(self) -> ReadServiceResult:
        input_dir = Path(str(getattr(self.cfg, "QUEUE_INPUT_DIR", r"D:\VOD") or r"D:\VOD"))
        if not input_dir.is_absolute():
            input_dir = (Path.cwd() / input_dir).resolve()
        else:
            input_dir = input_dir.resolve()
        signature = self._source_signature(input_dir)
        files: list[QueueVodFile] = []
        warnings: list[str] = []
        if input_dir.exists() and input_dir.is_dir():
            try:
                from video_queue import VIDEO_EXTS
            except Exception:
                VIDEO_EXTS = {".mp4", ".mkv", ".mov"}
            for path in sorted(input_dir.iterdir(), key=lambda item: item.name.casefold()):
                if not path.is_file() or path.suffix.casefold() not in {suffix.casefold() for suffix in VIDEO_EXTS}:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                files.append(
                    QueueVodFile(
                        name=path.name,
                        path=str(path.resolve()),
                        size=max(0, int(stat.st_size)),
                        modified_at=datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                    )
                )
        else:
            warnings.append(f"Queue input folder not found: {input_dir}")
        return ReadServiceResult(
            QueueVodList(input_dir=str(input_dir), exists=input_dir.exists() and input_dir.is_dir(), files=tuple(files)),
            (signature,),
            tuple(warnings),
        )

    def scores(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        status: str | None = None,
        product: str | None = None,
        sort: str = "scored_at",
        direction: Literal["asc", "desc"] = "desc",
    ) -> ReadServiceResult:
        limit, offset = self._bounded_page(limit, offset)
        if self._catalog is not None and self._catalog.ready("score_records"):
            data = self._catalog.scores(
                limit=limit,
                offset=offset,
                search=search,
                status=status,
                product=product,
                sort=sort,
                direction=direction,
            )
            return ReadServiceResult(data, revision=self._catalog.revision("scores"))
        records, signatures, warnings, stats = self._score_records()
        filter_options = {
            "product": tuple(sorted({record.row.product for record in records if record.row.product})),
            "status": tuple(sorted({record.row.status for record in records if record.row.status})),
        }
        records = self._filter_score_records(records, search=search, status=status, product=product)
        records = self._sort_score_records(records, sort=sort, direction=direction)
        total = len(records)
        page = records[offset : offset + limit]
        data = ScoreIndexPage(
            rows=tuple(record.row for record in page),
            total=total,
            limit=limit,
            offset=offset,
            stats=stats,
            filter_options=filter_options,
        )
        return ReadServiceResult(data, tuple(signatures), tuple(warnings))

    def score_detail(self, score_key: str) -> ReadServiceResult:
        if self._catalog is not None and self._catalog.ready("score_records"):
            return ReadServiceResult(
                self._catalog.score_detail(score_key),
                revision=self._catalog.revision("scores"),
            )
        records, signatures, warnings, _stats = self._score_records()
        selected = next((record for record in records if record.row.score_key == score_key), None)
        variants: list[ScoreRow] = []
        if selected is not None:
            variants = [
                record.row
                for record in records
                if record.row.base_score_key == selected.row.base_score_key
            ]
        data = ScoreDetail(
            selected=selected.row if selected else None,
            variants=tuple(variants),
            raw=selected.raw if selected else {},
            base_raw=selected.base_raw if selected else {},
        )
        return ReadServiceResult(data, tuple(signatures), tuple(warnings))

    def compliance(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        status: str | None = None,
        product: str | None = None,
        sort: str = "checked_at",
        direction: Literal["asc", "desc"] = "desc",
    ) -> ReadServiceResult:
        limit, offset = self._bounded_page(limit, offset)
        if self._catalog is not None and self._catalog.ready("compliance_results"):
            data = self._catalog.compliance(
                limit=limit,
                offset=offset,
                search=search,
                status=status,
                product=product,
                sort=sort,
                direction=direction,
            )
            return ReadServiceResult(data, revision=self._catalog.revision("compliance"))
        rows, violations, signatures, warnings = self._compliance_records()
        filter_options = {
            "product": tuple(sorted({row.product for row in rows if row.product})),
            "status": tuple(
                status_name
                for status_name, present in (
                    ("passed", any(row.passed for row in rows)),
                    ("blocked", any(row.blocked for row in rows)),
                    ("auto_fixed", any(row.auto_fixed for row in rows)),
                )
                if present
            ),
        }
        rows = self._filter_compliance_rows(rows, search=search, status=status, product=product)
        rows = self._sort_compliance_rows(rows, sort=sort, direction=direction)
        total = len(rows)
        page = rows[offset : offset + limit]
        summary = {
            "scanned": len(rows),
            "passed": sum(1 for row in rows if row.passed),
            "blocked": sum(1 for row in rows if row.blocked),
            "auto_fixed": sum(1 for row in rows if row.auto_fixed),
            "violation_count": sum(row.violation_count for row in rows),
        }
        data = ComplianceIndexPage(
            rows=tuple(page),
            violations=tuple(violations[: min(200, len(violations))]),
            total=total,
            limit=limit,
            offset=offset,
            summary=summary,
            filter_options=filter_options,
        )
        return ReadServiceResult(data, tuple(signatures), tuple(warnings))

    def compliance_detail(self, output_dir: str) -> ReadServiceResult:
        if self._catalog is not None and self._catalog.ready("compliance_results"):
            data = self._catalog.compliance_detail(output_dir)
            return ReadServiceResult(data, revision=self._catalog.revision("compliance"))
        rows, violations, signatures, warnings = self._compliance_records((output_dir,))
        data = ComplianceIndexPage(
            rows=tuple(rows),
            violations=tuple(violations),
            total=len(rows),
            limit=max(1, len(rows) or 1),
            offset=0,
            summary={
                "scanned": len(rows),
                "passed": sum(1 for row in rows if row.passed),
                "blocked": sum(1 for row in rows if row.blocked),
                "auto_fixed": sum(1 for row in rows if row.auto_fixed),
                "violation_count": sum(row.violation_count for row in rows),
            },
        )
        return ReadServiceResult(data, tuple(signatures), tuple(warnings))

    def overview(self, latest_export: Any | None = None) -> ReadServiceResult:
        del latest_export
        if self._catalog is not None and self._catalog.ready("score_records"):
            export_status_path = self._export_status_path()
            export_signature = self._source_signature(export_status_path)
            export_warnings: list[str] = []
            export_payload = self._load_json_dict(export_status_path, export_warnings, optional=True)
            data = self._catalog.overview(
                queue_active=None,
                export_payload=export_payload,
            )
            return ReadServiceResult(
                data,
                (export_signature,),
                tuple(export_warnings),
                revision=data.revision,
            )
        score_corpus = self._overview_score_corpus()
        compliance_corpus = self._overview_compliance_corpus()
        queue_state, queue_signature, queue_warnings = self._read_queue_state(None, include_history=False)
        export_status_path = self._export_status_path()
        export_signature = self._source_signature(export_status_path)

        def load_export_status() -> tuple[dict[str, Any], tuple[str, ...]]:
            status_warnings: list[str] = []
            payload = self._load_json_dict(export_status_path, status_warnings, optional=True)
            return payload, tuple(status_warnings)

        export_payload, export_warnings = self._cache.get_or_load(
            "overview:export-status",
            self._signature_key(export_signature),
            load_export_status,
        )

        revision_source = [
            *(str(self._signature_key(signature)) for signature in score_corpus.signatures),
            *(str(self._signature_key(signature)) for signature in compliance_corpus.signatures),
            str(self._signature_key(queue_signature)),
            str(self._signature_key(export_signature)),
        ]
        revision = hashlib.sha256("|".join(revision_source).encode("utf-8")).hexdigest()
        cached = self._cache.get("overview", revision)
        if cached is not None:
            return cached

        scanned = compliance_corpus.scanned
        passed = compliance_corpus.passed
        blocked = compliance_corpus.blocked
        if scanned == 0 and score_corpus.scored_count:
            scanned = score_corpus.scored_count
            blocked = score_corpus.compliance_blocked_count
            passed = scanned - blocked

        actionable = as_nonnegative_int(export_payload.get("actionable_count"))
        packaged = as_nonnegative_int(export_payload.get("packaged_count"))
        batch_size = as_nonnegative_int(export_payload.get("batch_size"))
        pending = as_nonnegative_int(export_payload.get("pending_count"))
        packaged_total = as_nonnegative_int(export_payload.get("packaged_total"))
        available = bool(export_signature.exists and export_payload.get("pending_count") is not None)
        queue_status = str(queue_state.get("queue_status") or "").casefold()
        queue_active = queue_status in {"running", "processing", "starting", "queued", "pausing", "stopping"}

        warnings = list(dict.fromkeys([
            *score_corpus.warnings,
            *compliance_corpus.warnings,
            *queue_warnings,
            *export_warnings,
        ]))
        if len(warnings) > 20:
            omitted = len(warnings) - 19
            warnings = [*warnings[:19], f"{omitted} additional warning(s) omitted."]
        data = OverviewSummary(
            revision=revision,
            queue_active=queue_active,
            scored_count=score_corpus.scored_count,
            average_score=(
                round(score_corpus.score_total / score_corpus.score_value_count, 3)
                if score_corpus.score_value_count
                else None
            ),
            export_ready_count=pending if available else 0,
            score_trend=score_corpus.trend,
            top_clips=score_corpus.top_clips,
            compliance=OverviewCompliance(
                scanned=scanned,
                passed=passed,
                blocked=blocked,
                rate=round((passed / scanned) * 100.0, 3) if scanned else 0.0,
            ),
            export=OverviewExport(
                available=available,
                actionable=actionable,
                ready=actionable,
                packaged_last_run=packaged,
                packaged=packaged,
                pending=pending,
                packaged_total=packaged_total,
                error_count=as_nonnegative_int(export_payload.get("error_count")),
                batch_size=batch_size,
                progress=round((packaged / actionable) * 100) if actionable else 0,
                status=str(export_payload.get("status") or ""),
                updated_at=str(export_payload.get("updated_at") or ""),
                trigger=str(export_payload.get("trigger") or ""),
                dry_run=bool(export_payload.get("dry_run")),
            ),
        )
        signatures = tuple([
            queue_signature,
            export_signature,
            *score_corpus.signatures[:4],
            *compliance_corpus.signatures[:4],
        ])
        result = ReadServiceResult(data, signatures, tuple(warnings))
        return self._cache.set("overview", revision, result)

    def _overview_score_corpus(self) -> _OverviewScoreCorpus:
        output_dirs = self._collect_output_dirs()
        signatures = tuple(
            self._source_signature(Path(output_dir) / "scores_summary.json")
            for output_dir in output_dirs
        )
        revision = tuple(self._signature_key(signature) for signature in signatures)

        def load() -> _OverviewScoreCorpus:
            warnings: list[str] = []
            scored_count = 0
            score_total = 0.0
            score_value_count = 0
            compliance_blocked_count = 0
            trend_groups: dict[str, tuple[float, int]] = {}
            candidates: list[_OverviewScoreCandidate] = []
            earliest = (datetime.now().astimezone() - timedelta(days=13)).date()

            def add_candidate(
                *,
                identity: dict[str, Any],
                clip_id: Any,
                product: str,
                total_score: float | None,
                scored_at: str,
                source_date: str,
                blocked: bool,
                output_dir: Path,
                artifact_value: Any,
            ) -> None:
                nonlocal scored_count, score_total, score_value_count
                nonlocal compliance_blocked_count
                scored_count += 1
                if blocked:
                    compliance_blocked_count += 1
                if total_score is None:
                    return
                score_total += total_score
                score_value_count += 1
                parsed = parse_timestamp(scored_at or source_date)
                if parsed is not None and parsed.date() >= earliest:
                    key = parsed.date().isoformat()
                    running_total, count = trend_groups.get(key, (0.0, 0))
                    trend_groups[key] = (running_total + total_score, count + 1)
                candidates.append(
                    _OverviewScoreCandidate(
                        score_key=build_score_key(identity),
                        clip_id=str(clip_id or ""),
                        product=product,
                        total_score=total_score,
                        scored_at=scored_at,
                        source_date=source_date,
                        sort_timestamp=scored_at,
                        output_dir=output_dir,
                        artifact_value=artifact_value,
                    )
                )

            for output_dir, signature in zip(output_dirs, signatures):
                payload = self._load_json_dict(Path(signature.path), warnings, optional=True)
                if not payload:
                    continue
                folder = Path(output_dir)
                source_video, _run_tag = split_output_folder_name(folder.name)
                source_date = source_date_from_source_video(source_video)
                for group in self._score_groups_from_summary(payload):
                    product = str(group.get("product", "general") or "general")
                    total_score = score_float(group.get("total_score"))
                    scored_at = str(group.get("scored_at") or "")
                    blocked = bool(group.get("compliance_blocked", False))
                    base_clip_id = group.get("base_clip_id") or group.get("clip_id")
                    artifact_value = group.get("representative_clip_path") or group.get(
                        "representative_output_file"
                    )
                    add_candidate(
                        identity={"clip_id": base_clip_id, "clip_path": artifact_value},
                        clip_id=base_clip_id,
                        product=product,
                        total_score=total_score,
                        scored_at=scored_at,
                        source_date=source_date,
                        blocked=blocked,
                        output_dir=folder,
                        artifact_value=artifact_value,
                    )
                    variants = group.get("variants", [])
                    if not isinstance(variants, list):
                        continue
                    for variant in (item for item in variants if isinstance(item, dict)):
                        variant_artifact = variant.get("clip_path") or variant.get("output_file")
                        add_candidate(
                            identity=variant,
                            clip_id=variant.get("clip_id"),
                            product=product,
                            total_score=total_score,
                            scored_at=str(variant.get("scored_at") or scored_at),
                            source_date=source_date,
                            blocked=bool(variant.get("compliance_blocked", blocked)),
                            output_dir=folder,
                            artifact_value=variant_artifact,
                        )

            trend = tuple(
                OverviewScoreTrendPoint(
                    date=key,
                    average_score=round(total / count, 3),
                    scored_count=count,
                )
                for key, (total, count) in sorted(trend_groups.items())[-14:]
                if count
            )
            newest_first = sorted(
                candidates,
                key=lambda candidate: parse_timestamp(candidate.sort_timestamp) or MIN_SORT_TIMESTAMP,
                reverse=True,
            )
            top_candidates = sorted(
                newest_first,
                key=lambda candidate: candidate.total_score,
                reverse=True,
            )[:5]
            top_clips = tuple(
                OverviewTopClip(
                    score_key=candidate.score_key,
                    clip_id=candidate.clip_id,
                    product=candidate.product,
                    total_score=candidate.total_score,
                    scored_at=candidate.scored_at,
                    source_date=candidate.source_date,
                    artifact=self._artifact_for_output(
                        candidate.output_dir,
                        candidate.artifact_value,
                    ),
                )
                for candidate in top_candidates
            )
            return _OverviewScoreCorpus(
                scored_count=scored_count,
                score_total=score_total,
                score_value_count=score_value_count,
                compliance_blocked_count=compliance_blocked_count,
                trend=trend,
                top_clips=top_clips,
                signatures=signatures,
                warnings=tuple(warnings),
            )

        return self._cache.get_or_load("scores:overview", revision, load)

    def _overview_compliance_corpus(self) -> _OverviewComplianceCorpus:
        output_dirs = self._collect_output_dirs()
        signatures = tuple(
            self._source_signature(Path(output_dir) / "manifest.json")
            for output_dir in output_dirs
        )
        revision = tuple(self._signature_key(signature) for signature in signatures)

        def load() -> _OverviewComplianceCorpus:
            warnings: list[str] = []
            scanned = passed = blocked = 0
            for signature in signatures:
                for row in self._manifest_rows(Path(signature.path), warnings):
                    if not self._manifest_row_has_compliance_fields(row):
                        continue
                    scanned += 1
                    if bool(row.get("compliance_passed", False)):
                        passed += 1
                    if bool(row.get("compliance_blocked", False)):
                        blocked += 1
            return _OverviewComplianceCorpus(
                scanned=scanned,
                passed=passed,
                blocked=blocked,
                signatures=signatures,
                warnings=tuple(warnings),
            )

        return self._cache.get_or_load("compliance:overview", revision, load)

    def module_readiness(self) -> ReadServiceResult:
        if self._catalog is not None and self._catalog.ready("modules"):
            return ReadServiceResult(
                self._catalog.module_readiness(),
                revision=self._catalog.revision("modules"),
            )
        index_payload, signature, warnings = self._module_index_payload()
        modules = index_payload.get("modules", []) if isinstance(index_payload, dict) else []
        modules = [module for module in modules if isinstance(module, dict)]
        min_hook = int(getattr(self.cfg, "MODULAR_ASSEMBLY_READY_MIN_HOOK", 5) or 5)
        min_main = int(getattr(self.cfg, "MODULAR_ASSEMBLY_READY_MIN_MAIN", 3) or 3)
        min_cta = int(getattr(self.cfg, "MODULAR_ASSEMBLY_READY_MIN_CTA", 3) or 3)
        min_events = max(1, int(getattr(self.cfg, "MODULE_ASSEMBLY_ZOOM_READY_MIN_EVENTS", 1) or 1))
        revision = (self._signature_key(signature), min_hook, min_main, min_cta, min_events)
        cached = self._cache.get("modules:readiness", revision)
        if cached is not None:
            return cached
        role_counts = {product: {role: 0 for role in MODULE_ROLES} for product, _label in MODULE_PRODUCTS}
        visual_counts = {
            product: {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "not_run": 0,
                "zoom_ready_candidates": 0,
            }
            for product, _label in MODULE_PRODUCTS
        }
        for module in modules:
            product_key = str(module.get("product") or "")
            role = str(module.get("role") or "")
            if product_key in role_counts and role in role_counts[product_key]:
                role_counts[product_key][role] += 1
            if product_key in visual_counts:
                visual = visual_counts[product_key]
                visual["total"] += 1
                status = self._module_visual_status(module.get("visual_validation_status"))
                visual[status] += 1
                hits = as_nonnegative_int(module.get("visual_product_hits"))
                approved = str(module.get("quality_status") or "") in {"approved", "no_visual_events"}
                if approved and status == "passed" and hits >= min_events:
                    visual["zoom_ready_candidates"] += 1

        rows: list[ModuleReadinessRow] = []
        for product_key, label in MODULE_PRODUCTS:
            counts = role_counts[product_key]
            total = sum(counts.values())
            if counts["hook"] >= min_hook and counts["main"] >= min_main and counts["cta"] >= min_cta:
                readiness = "ready"
            elif total > 0:
                readiness = "partial"
            else:
                readiness = "empty"
            visual = visual_counts[product_key]
            rows.append(
                ModuleReadinessRow(
                    product=label,
                    product_key=product_key,
                    hook=counts["hook"],
                    main=counts["main"],
                    cta=counts["cta"],
                    total=total,
                    readiness=readiness,
                    visual_total=visual["total"],
                    visual_passed=visual["passed"],
                    visual_failed=visual["failed"],
                    visual_not_run=visual["not_run"],
                    zoom_ready_candidates=visual["zoom_ready_candidates"],
                )
            )
        data = ModuleReadiness(
            library_dir=str(self._module_library_dir()),
            index_path=signature.path,
            index_exists=signature.exists,
            index_updated_at=str(index_payload.get("updated_at") or "") if isinstance(index_payload, dict) else "",
            index_module_count=self._module_index_count(index_payload, modules),
            thresholds={"hook": min_hook, "main": min_main, "cta": min_cta, "zoom_ready_events": min_events},
            rows=tuple(rows),
        )
        return self._cache.set(
            "modules:readiness",
            revision,
            ReadServiceResult(data, (signature,), tuple(warnings)),
        )

    def module_library(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        status: str | None = None,
        quality_status: str | None = None,
        review_status: str | None = None,
        visual_status: str | None = None,
        product: str | None = None,
        sort: str = "product",
        direction: Literal["asc", "desc"] = "asc",
    ) -> ReadServiceResult:
        limit, offset = self._bounded_page(limit, offset)
        if self._catalog is not None and self._catalog.ready("modules"):
            data = self._catalog.modules(
                limit=limit,
                offset=offset,
                search=search,
                status=status,
                quality_status=quality_status,
                review_status=review_status,
                visual_status=visual_status,
                product=product,
                sort=sort,
                direction=direction,
            )
            return ReadServiceResult(data, revision=self._catalog.revision("modules"))
        rows, modules_by_id, signature, warnings = self._module_corpus()
        filter_options = {
            "product": tuple(sorted({row.product for row in rows if row.product})),
            "source_date": tuple(sorted({row.source_date for row in rows if row.source_date})),
            "quality_status": tuple(sorted({row.quality_status for row in rows if row.quality_status})),
            "visual_validation_status": tuple(sorted({row.visual_validation_status for row in rows if row.visual_validation_status})),
            "review_status": tuple(sorted({row.review_status for row in rows if row.review_status})),
        }
        rows = self._filter_module_rows(
            rows,
            search=search,
            status=status,
            quality_status=quality_status,
            review_status=review_status,
            visual_status=visual_status,
            product=product,
        )
        rows = self._sort_module_rows(rows, sort=sort, direction=direction)
        total = len(rows)
        page = [
            self._module_row(modules_by_id[row.module_id], include_artifact=True)
            if row.module_id in modules_by_id else row
            for row in rows[offset : offset + limit]
        ]
        data = ModuleLibraryPage(
            library_dir=str(self._module_library_dir()),
            rows=tuple(page),
            total=total,
            limit=limit,
            offset=offset,
            filter_options=filter_options,
        )
        return ReadServiceResult(data, (signature,), tuple(warnings))

    def module_detail(self, module_id: str) -> ReadServiceResult:
        if self._catalog is not None and self._catalog.ready("modules"):
            return ReadServiceResult(
                self._catalog.module_detail(module_id),
                revision=self._catalog.revision("modules"),
            )
        _rows, modules_by_id, signature, warnings = self._module_corpus()
        module = modules_by_id.get(str(module_id))
        if module is None:
            return ReadServiceResult(ModuleDetail(), (signature,), tuple(warnings))
        data = ModuleDetail(
            selected=self._module_row(module, include_artifact=True),
            transcript_text=str(module.get("transcript_text") or ""),
        )
        return ReadServiceResult(data, (signature,), tuple(warnings))

    def settings_snapshot(self) -> ReadServiceResult:
        snapshot = self.settings_provider.snapshot()
        entries_by_name = {entry.name: entry for entry in snapshot.entries}
        groups: dict[str, list[SettingsReadEntry]] = {}
        for name, definition in sorted(SETTINGS_REGISTRY.items()):
            entry = entries_by_name.get(name)
            if entry is None:
                continue
            read_entry = SettingsReadEntry(
                name=name,
                value=entry.value,
                source=entry.source,
                value_type=definition.value_type.__name__,
                category=definition.category,
                minimum=definition.minimum,
                maximum=definition.maximum,
                editable=name in BROWSER_EDITABLE_SETTINGS,
                read_only_reason="" if name in BROWSER_EDITABLE_SETTINGS else "Operator-managed; restart required.",
            )
            groups.setdefault(definition.category, []).append(read_entry)
        data = SettingsReadSnapshot(
            revision=snapshot.revision,
            groups={key: tuple(value) for key, value in sorted(groups.items())},
        )
        overrides_path = getattr(self.settings_provider, "overrides_path", None)
        signatures = (self._source_signature(Path(overrides_path)),) if overrides_path is not None else ()
        return ReadServiceResult(data, signatures)

    def log_tail(self, path: str | None = None, *, lines: int = 200) -> ReadServiceResult:
        lines = max(1, min(int(lines or 200), 1000))
        target = Path(path) if path else Path("pipeline.log")
        if not target.is_absolute():
            target = Path.cwd() / target
        target = target.resolve()
        signature = self._source_signature(target)
        if target.name != "pipeline.log":
            return ReadServiceResult(
                LogTail(path=str(target), exists=False, lines=()),
                (signature,),
                ("Only pipeline.log can be tailed in the app.",),
            )
        if not target.exists():
            return ReadServiceResult(LogTail(path=str(target), exists=False), (signature,), ("pipeline.log was not found.",))
        try:
            tail = reverse_tail(target, line_limit=lines)
        except OSError as exc:
            return ReadServiceResult(
                LogTail(path=str(target), exists=True),
                (signature,),
                (f"Could not read log: {exc}",),
            )
        payload = tuple(
            LogLine(line_number=line.line_number, text=line.text)
            for line in tail.lines
        )
        data = LogTail(
            path=str(target),
            exists=True,
            total_lines=tail.total_lines,
            returned_lines=len(payload),
            lines=payload,
        )
        warnings = ()
        if tail.partial_oldest_line:
            warnings = (
                "Log tail reached the 4 MiB read limit; the partial oldest line was omitted.",
            )
        return ReadServiceResult(data, (signature,), warnings)

    def system_stats(self) -> ReadServiceResult:
        bucket = int(time.monotonic() // 2)
        return self._cache.get_or_load("system", bucket, self._build_system_stats, max_age=2.0)

    def _build_system_stats(self) -> ReadServiceResult:
        warnings: list[str] = []
        try:
            import psutil  # type: ignore
        except Exception:
            disk_root = Path.cwd().anchor or str(Path.cwd())
            try:
                disk = os.statvfs(disk_root)  # type: ignore[attr-defined]
                disk_label = f"{(disk.f_bavail * disk.f_frsize) / (1024**4):.1f} TB free"
            except Exception:
                disk_label = "Unavailable"
            warnings.append("psutil is not installed; CPU/RAM metrics are unavailable.")
            return ReadServiceResult(SystemStats(disk_label=disk_label, gpu_label=self._gpu_stats()["label"]), warnings=tuple(warnings))

        cpu_percent = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        disk_root = Path.cwd().anchor or str(Path.cwd())
        disk = psutil.disk_usage(disk_root)
        gpu = self._gpu_stats()
        data = SystemStats(
            cpu_percent=float(cpu_percent),
            ram_percent=float(ram.percent),
            ram_label=f"{ram.used / (1024**3):.1f}/{ram.total / (1024**3):.1f} GB",
            disk_percent=float(disk.percent),
            disk_label=f"{disk.free / (1024**4):.1f} TB free",
            gpu_percent=gpu.get("utilization"),
            gpu_mem_percent=gpu.get("memory_percent"),
            gpu_label=str(gpu.get("label") or "Unavailable"),
        )
        return ReadServiceResult(data, warnings=tuple(warnings))

    def resolve_artifact(self, requested_path: str) -> ResolvedArtifact:
        if not requested_path or "\x00" in requested_path:
            raise PermissionError("Invalid artifact path.")
        path = Path(requested_path)
        if not path.is_absolute():
            raise PermissionError("Artifact paths must be absolute.")
        path = path.resolve()
        allowed = [root for root in self._allowed_artifact_roots() if root.exists()]
        if not any(self._is_relative_to(path, root) for root in allowed):
            raise PermissionError("Artifact path is outside configured read roots.")
        if path.suffix.casefold() not in {
            ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi",
            ".jpg", ".jpeg", ".png", ".webp", ".gif",
        }:
            raise PermissionError("Artifact type is not allowed.")
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(str(path))
        return ResolvedArtifact(path=path, media_type=self._media_type(path))

    def _read_queue_state(
        self,
        state_path: str | None,
        *,
        include_history: bool = True,
    ) -> tuple[dict[str, Any], SourceSignature, list[str]]:
        path = Path(state_path) if state_path else self._default_state_path()
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        signature = self._source_signature(path)
        storage_mode = queue_storage_mode() if state_path is None else "json"
        if storage_mode != "json":
            repository = QueueStateRepository(path, mode=storage_mode)
            try:
                payload = repository.load(include_history=include_history)
            except Exception as exc:
                return (
                    {"schema_version": 3, "videos": {}, "updated_at": None},
                    signature,
                    [f"Failed to read authoritative queue state: {exc}"],
                )
            if payload:
                return payload, signature, []
        if not path.exists():
            return {"schema_version": 2, "videos": {}, "updated_at": None}, signature, [f"State file not found: {path}"]

        def load() -> tuple[dict[str, Any], tuple[str, ...]]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                return {"schema_version": 2, "videos": {}, "updated_at": None}, (f"Failed to read state file: {exc}",)
            if not isinstance(payload, dict):
                return {"schema_version": 2, "videos": {}, "updated_at": None}, ("Queue state JSON was not an object.",)
            return payload, ()

        payload, cached_warnings = self._cache.get_or_load(
            f"queue:state:{signature.path}",
            self._signature_key(signature),
            load,
        )
        return payload, signature, list(cached_warnings)

    def _read_queue_control(self) -> tuple[dict[str, Any], SourceSignature, list[str]]:
        path = Path(str(getattr(self.cfg, "QUEUE_CONTROL_FILE", Path(getattr(self.cfg, "WORKING_DIR", "working")) / "queue_control.json")))
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        signature = self._source_signature(path)
        if not path.exists():
            return {}, signature, []

        def load() -> tuple[dict[str, Any], tuple[str, ...]]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                return {}, (f"Failed to read queue control file: {exc}",)
            if not isinstance(payload, dict):
                return {}, ("Queue control JSON was not an object.",)
            return payload, ()

        payload, cached_warnings = self._cache.get_or_load(
            f"queue:control:{signature.path}",
            self._signature_key(signature),
            load,
        )
        return payload, signature, list(cached_warnings)

    def _read_queue_forever(self) -> tuple[dict[str, Any], SourceSignature, list[str]]:
        path = Path(str(getattr(self.cfg, "QUEUE_FOREVER_STATE_FILE", Path(getattr(self.cfg, "WORKING_DIR", "working")) / "queue_forever_state.json")))
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        signature = self._source_signature(path)
        if not path.exists():
            return {}, signature, []

        def load() -> tuple[dict[str, Any], tuple[str, ...]]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                return {}, (f"Failed to read queue supervisor state file: {exc}",)
            if not isinstance(payload, dict):
                return {}, ("Queue supervisor state JSON was not an object.",)
            return payload, ()

        payload, cached_warnings = self._cache.get_or_load(
            f"queue:supervisor:{signature.path}",
            self._signature_key(signature),
            load,
        )
        return payload, signature, list(cached_warnings)

    @staticmethod
    def _effective_control_status(
        control: dict[str, Any],
        supervisor: dict[str, Any],
        launch: dict[str, Any],
    ) -> str:
        control_status = str(control.get("status") or "unknown")
        supervisor_status = str(supervisor.get("status") or "").strip().lower()
        run_mode = str(launch.get("run_mode") or "").strip().lower()
        if run_mode not in {"single_video", "folder_once"}:
            return control_status
        if supervisor_status not in {"completed", "stopped", "failed"}:
            return control_status

        control_run_tag = str(control.get("current_run_tag") or "").strip()
        supervisor_run_tag = str(supervisor.get("current_run_tag") or "").strip()
        if control_run_tag and supervisor_run_tag and control_run_tag != supervisor_run_tag:
            return control_status
        return supervisor_status

    @staticmethod
    def _normalized_launch_config(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        try:
            import queue_control

            return queue_control.normalize_launch_config(value)
        except Exception:
            return {}

    @staticmethod
    def _launch_summary(value: dict[str, Any]) -> str:
        if not value:
            return ""
        try:
            import queue_control

            return queue_control.launch_summary(value)
        except Exception:
            return ""

    def _build_dashboard_summary(self, state: dict[str, Any], state_path: str) -> DashboardSummary:
        rows = self._queue_rows(state)
        queue_health = self._queue_health(state)
        statuses = Counter(row.status for row in rows)
        stage_running: Counter[str] = Counter()
        stage_queued: Counter[str] = Counter()
        videos = [self._aggregate_video_entry(video) for video in self._state_videos(state)]
        stage_waiting = self._stage_waiting_counts(state, videos)
        for video in videos:
            if str(video.get("status") or "").strip().lower() in {"completed", "failed", "paused", "stopped"}:
                continue
            stages = video.get("stages") if isinstance(video.get("stages"), dict) else {}
            for stage_key, _label in STAGES:
                stage_state = stages.get(stage_key) if isinstance(stages.get(stage_key), dict) else {}
                stage_status = str(stage_state.get("status") or "pending").strip().lower()
                if stage_status == "running":
                    stage_running[stage_key] += 1
                if stage_status == "queued" or (stage_state.get("queued") and stage_status not in {"done", "failed", "paused", "skipped", "running"}):
                    stage_queued[stage_key] += 1
        clip_events = self._clip_events(videos)
        now = datetime.now().astimezone()
        today = now.date()
        clips_today = sum(count for timestamp, count in clip_events if timestamp.astimezone().date() == today)
        clips_last_24h = sum(count for timestamp, count in clip_events if timestamp >= now - timedelta(days=1))
        clips_per_hour = self._average_completed_bucket(clip_events, "hour")
        production_dates = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
        production_keys = {value.isoformat() for value in production_dates}
        production_counts: Counter[str] = Counter()
        for timestamp, count in clip_events:
            key = timestamp.astimezone().date().isoformat()
            if key in production_keys:
                production_counts[key] += count
        return DashboardSummary(
            state_path=state_path,
            updated_at=str(state.get("updated_at") or "") or None,
            queue_status=str(state.get("queue_status") or "unknown"),
            queue_health=queue_health,
            status_counts=dict(statuses),
            stage_running=dict(stage_running),
            stage_queued=dict(stage_queued),
            stage_waiting=stage_waiting,
            waiting_videos=self._waiting_video_count(state, videos),
            stage_admission_limit=self._stage_admission_limit(state),
            total_videos=len(rows),
            total_clips=sum(row.clips_generated for row in rows),
            clips_today=int(clips_today),
            clips_last_24h=int(clips_last_24h),
            clips_per_hour=float(clips_per_hour),
            production_days=tuple(
                {"date": value.isoformat(), "clips": int(production_counts[value.isoformat()])}
                for value in production_dates
            ),
            rows=tuple(rows[:50]),
        )

    def _stage_waiting_counts(self, state: dict[str, Any], videos: list[dict[str, Any]]) -> dict[str, int]:
        active_stages = self._active_stage_keys(state)
        counts: Counter[str] = Counter()
        for video in videos:
            status = str(video.get("status") or "").strip().lower()
            if status in {"completed", "failed", "paused", "stopped"}:
                continue
            stages = video.get("stages") if isinstance(video.get("stages"), dict) else {}
            for stage_key in active_stages:
                stage_state = stages.get(stage_key) if isinstance(stages.get(stage_key), dict) else {}
                stage_status = str(stage_state.get("status") or "pending").strip().lower()
                if stage_status in {"done", "skipped"}:
                    continue
                if stage_status in {"failed", "paused"}:
                    break
                if self._stage_is_admitted(video, stage_key, stage_state):
                    break
                counts[stage_key] += 1
                break
        return dict(counts)

    def _waiting_video_count(self, state: dict[str, Any], videos: list[dict[str, Any]]) -> int:
        active_stages = self._active_stage_keys(state)
        count = 0
        for video in videos:
            status = str(video.get("status") or "").strip().lower()
            if status in {"completed", "failed", "paused", "stopped"}:
                continue
            if not self._video_has_admitted_stage(video, active_stages):
                count += 1
        return count

    def _active_stage_keys(self, state: dict[str, Any]) -> tuple[str, ...]:
        known = {key for key, _label in STAGES}
        raw = state.get("active_stages") if isinstance(state.get("active_stages"), list) else []
        active = tuple(str(stage) for stage in raw if str(stage) in known)
        return active or tuple(key for key, _label in STAGES)

    @staticmethod
    def _stage_is_admitted(video: dict[str, Any], stage_key: str, stage_state: dict[str, Any]) -> bool:
        return (
            str(stage_state.get("status") or "").strip().lower() in {"queued", "running"}
            or bool(stage_state.get("queued"))
            or video.get("current_stage") == stage_key
        )

    def _video_has_admitted_stage(self, video: dict[str, Any], active_stages: tuple[str, ...]) -> bool:
        stages = video.get("stages") if isinstance(video.get("stages"), dict) else {}
        for stage_key in active_stages:
            stage_state = stages.get(stage_key) if isinstance(stages.get(stage_key), dict) else {}
            if self._stage_is_admitted(video, stage_key, stage_state):
                return True
        return False

    def _stage_admission_limit(self, state: dict[str, Any]) -> int:
        value = state.get("stage_admission_limit")
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return max(1, int(getattr(self.cfg, "QUEUE_STAGE_ADMISSION_LIMIT", 3) or 3))

    def _queue_rows(self, state: dict[str, Any]) -> list[QueueRunRow]:
        queue_health = self._queue_health(state)
        attention_by_video = queue_health.get("attention_by_video", {}) if isinstance(queue_health, dict) else {}
        rows: list[QueueRunRow] = []
        now = datetime.now().astimezone()
        for video in [self._aggregate_video_entry(item) for item in self._state_videos(state)]:
            created_at = parse_timestamp(video.get("created_at"))
            completed_at = self._infer_run_completed_at(video)
            duration = "-"
            if created_at:
                duration = format_duration(((completed_at or now) - created_at).total_seconds())
            rows.append(
                QueueRunRow(
                    run_id=str(video.get("operation_id") or video.get("run_id") or f"{video.get('path') or video.get('name') or '-'}|{format_datetime(created_at)}"),
                    video_name=str(video.get("name") or "-"),
                    video_path=str(video.get("path") or "") or None,
                    status=self._infer_video_status(video, attention_by_video),
                    current_step=self._infer_current_step(video, attention_by_video),
                    progress=self._compute_progress(video, attention_by_video),
                    attention=self._attention_text(video, attention_by_video),
                    clips_generated=as_nonnegative_int(video.get("clips_generated_total")),
                    runs=as_nonnegative_int(video.get("run_count"), 1),
                    redos=as_nonnegative_int(video.get("redo_count")),
                    duration=duration,
                    started_at=format_datetime(created_at),
                    completed_at=format_datetime(completed_at),
                    output_dir=str(video.get("output_dir") or "") or None,
                    working_dir=str(video.get("working_dir") or "") or None,
                    current_stage=str(video.get("current_stage") or "") or None,
                )
            )
        status_rank = {"Needs Attention": 0, "Processing": 1, "Waiting": 2, "Failed": 3, "Stopped": 4, "Completed": 5, "Paused": 6}
        rows.sort(key=lambda row: (status_rank.get(row.status, 9), row.started_at, row.video_name))
        return rows

    def _queue_health(self, state: dict[str, Any]) -> dict[str, Any]:
        try:
            from clipper_app.application.services import HealthService
            import queue_state_health as qh

            return HealthService().snapshot(
                state,
                stage_labels=STAGE_LABELS,
                running_stall_seconds=float(getattr(self.cfg, "QUEUE_DASHBOARD_RUNNING_STALL_SECONDS", qh.DEFAULT_RUNNING_STALL_SECONDS)),
                queued_stall_seconds=float(getattr(self.cfg, "QUEUE_DASHBOARD_QUEUED_STALL_SECONDS", qh.DEFAULT_QUEUED_STALL_SECONDS)),
            )
        except Exception as exc:
            return {"status": "needs_attention", "severity": "warning", "summary": f"Could not derive queue health: {exc}"}

    def _state_videos(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        raw_videos = state.get("videos") if isinstance(state.get("videos"), dict) else {}
        return [video for video in raw_videos.values() if isinstance(video, dict)]

    def _aggregate_video_entry(self, video: dict[str, Any]) -> dict[str, Any]:
        runs = [run for run in video.get("run_history", []) if isinstance(run, dict)]
        runs.append(
            {
                "name": video.get("name", "-"),
                "path": video.get("path"),
                "working_dir": video.get("working_dir"),
                "output_dir": video.get("output_dir"),
                "status": video.get("status"),
                "current_stage": video.get("current_stage"),
                "created_at": video.get("created_at"),
                "completed_at": video.get("completed_at"),
                "failed_at": video.get("failed_at"),
                "stages": video.get("stages", {}),
            }
        )
        aggregate = dict(video)
        aggregate["runs"] = runs
        aggregate["redo_count"] = max(0, len(runs) - 1)
        aggregate["run_count"] = len(runs)
        aggregate["clips_generated_total"] = sum(self._run_clip_count(run) for run in runs)
        return aggregate

    def _run_clip_count(self, run: dict[str, Any]) -> int:
        stages = run.get("stages") if isinstance(run.get("stages"), dict) else {}
        ffmpeg = stages.get("ffmpeg") if isinstance(stages.get("ffmpeg"), dict) else {}
        live_count = as_nonnegative_int(ffmpeg.get("clips_created"))
        if live_count:
            return live_count
        output_dir = run.get("output_dir")
        if not output_dir:
            return 0
        return self._manifest_clip_count(Path(str(output_dir)))

    def _infer_run_completed_at(self, run: dict[str, Any]) -> datetime | None:
        explicit = parse_timestamp(run.get("completed_at"))
        if explicit:
            return explicit
        stages = run.get("stages") if isinstance(run.get("stages"), dict) else {}
        ffmpeg = stages.get("ffmpeg") if isinstance(stages.get("ffmpeg"), dict) else {}
        return parse_timestamp(ffmpeg.get("finished_at"))

    def _clip_events(self, videos: list[dict[str, Any]]) -> list[tuple[datetime, int]]:
        events: list[tuple[datetime, int]] = []
        for video in videos:
            for run in video.get("runs", []):
                if not isinstance(run, dict):
                    continue
                timestamp = self._infer_run_completed_at(run)
                count = self._run_clip_count(run)
                if timestamp and count:
                    events.append((timestamp, count))
        return events

    def _average_completed_bucket(self, events: list[tuple[datetime, int]], bucket: Literal["hour"]) -> float:
        if not events:
            return 0.0
        counters: Counter[datetime] = Counter()
        for timestamp, count in events:
            if bucket == "hour":
                key = timestamp.replace(minute=0, second=0, microsecond=0)
            else:
                key = timestamp
            counters[key] += count
        if not counters:
            return 0.0
        return sum(counters.values()) / len(counters)

    def _infer_video_status(self, video: dict[str, Any], attention_by_video: dict[str, Any]) -> str:
        if self._attention_items(video, attention_by_video):
            return "Needs Attention"
        status = str(video.get("status") or "").lower()
        if status == "completed":
            return "Completed"
        if status == "failed":
            return "Failed"
        if status == "stopped":
            return "Stopped"
        if status == "paused":
            return "Paused"
        stages = video.get("stages") if isinstance(video.get("stages"), dict) else {}
        if video.get("current_stage") or any(isinstance(stage, dict) and stage.get("status") == "running" for stage in stages.values()):
            return "Processing"
        return "Waiting"

    def _infer_current_step(self, video: dict[str, Any], attention_by_video: dict[str, Any]) -> str:
        issues = self._attention_items(video, attention_by_video)
        if issues:
            stage_key = issues[0].get("stage")
            if stage_key:
                return STAGE_LABELS.get(str(stage_key), str(stage_key).title())
        current_stage = str(video.get("current_stage") or "")
        if current_stage:
            return STAGE_LABELS.get(current_stage, current_stage.title())
        stages = video.get("stages") if isinstance(video.get("stages"), dict) else {}
        for stage_key, label in STAGES:
            stage_state = stages.get(stage_key) if isinstance(stages.get(stage_key), dict) else {}
            if stage_state.get("status") == "failed":
                return label
            if stage_state.get("status") != "done":
                return label
        return "Completed"

    def _compute_progress(self, video: dict[str, Any], attention_by_video: dict[str, Any]) -> int:
        stages = video.get("stages") if isinstance(video.get("stages"), dict) else {}
        done = sum(1 for key, _label in STAGES if isinstance(stages.get(key), dict) and stages[key].get("status") == "done")
        progress = (done / len(STAGES)) * 100
        status = self._infer_video_status(video, attention_by_video)
        if status == "Processing":
            progress = min(progress + 12.5, 98.0)
        if status == "Completed":
            progress = 100.0
        return int(round(progress))

    def _attention_items(self, video: dict[str, Any], attention_by_video: dict[str, Any]) -> list[dict[str, Any]]:
        key = str(video.get("path") or video.get("video_path") or video.get("name") or "")
        items = attention_by_video.get(key, []) if isinstance(attention_by_video, dict) else []
        return [item for item in items if isinstance(item, dict)]

    def _attention_text(self, video: dict[str, Any], attention_by_video: dict[str, Any]) -> str:
        issues = self._attention_items(video, attention_by_video)
        if not issues:
            return ""
        first = issues[0]
        stage = str(first.get("stage_label") or "Queue")
        message = str(first.get("message") or "")
        return f"{stage}: {message}" if message else stage

    def _score_records(self) -> tuple[list[ScoreRecord], list[SourceSignature], list[str], ScoreStats]:
        output_dirs = self._collect_output_dirs()
        signatures = [self._source_signature(Path(output_dir) / "scores_summary.json") for output_dir in output_dirs]
        revision = tuple(self._signature_key(signature) for signature in signatures)

        def load() -> tuple[tuple[ScoreRecord, ...], tuple[SourceSignature, ...], tuple[str, ...], ScoreStats]:
            warnings: list[str] = []
            records: list[ScoreRecord] = []
            stats = self._empty_score_stats()
            for output_dir, signature in zip(output_dirs, signatures):
                payload = self._load_json_dict(Path(signature.path), warnings, optional=True)
                if not payload:
                    continue
                self._accumulate_score_stats(stats, payload)
                folder = Path(output_dir)
                source_video, run_tag = split_output_folder_name(folder.name)
                for group in self._score_groups_from_summary(payload):
                    records.extend(self._score_records_from_group(group, folder, source_video, run_tag))
            records.sort(key=lambda record: parse_timestamp(record.row.sort_timestamp) or MIN_SORT_TIMESTAMP, reverse=True)
            return tuple(records), tuple(signatures), tuple(warnings), stats

        records, cached_signatures, warnings, stats = self._cache.get_or_load("scores:corpus", revision, load)
        return list(records), list(cached_signatures), list(warnings), stats

    def _score_records_from_group(
        self,
        group: dict[str, Any],
        output_dir: Path,
        source_video: str,
        run_tag: str,
    ) -> list[ScoreRecord]:
        base_key = build_score_key(
            {
                "clip_id": group.get("base_clip_id") or group.get("clip_id"),
                "clip_path": group.get("representative_clip_path") or group.get("representative_output_file"),
            }
        )
        scored_at = str(group.get("scored_at") or "")
        flags = self._score_flags_list(group.get("flags", []))
        flag_severity = self._score_flag_severity(flags)
        total_score = score_float(group.get("total_score"))
        quality_score = score_float(group.get("quality_score"))
        blocked = bool(group.get("compliance_blocked", False))
        base_row = ScoreRow(
            score_key=base_key,
            base_score_key=base_key,
            row_type="base",
            source_video=source_video,
            run_tag=run_tag,
            source_date=source_date_from_source_video(source_video),
            clip_id=str(group.get("base_clip_id") or group.get("clip_id") or ""),
            product=str(group.get("product", "general") or "general"),
            total_score=total_score,
            content_score=score_float(group.get("content_score")),
            host_focus_score=score_float(group.get("host_focus_score")),
            hook_score=score_float(group.get("hook_score")),
            quality_score=quality_score,
            engagement_score=score_float(group.get("engagement_score")),
            similarity_score=score_float(group.get("average_similarity_score")),
            variants=as_nonnegative_int(group.get("variant_count")),
            flags=tuple(flags),
            flag_count=len(flags),
            flag_severity=flag_severity,
            status=self._score_status_label(total_score, flag_severity, blocked),
            compliance_blocked=blocked,
            summary=str(group.get("summary") or ""),
            output_file=str(group.get("representative_output_file") or ""),
            clip_path=str(group.get("representative_clip_path") or ""),
            artifact=self._artifact_for_output(output_dir, group.get("representative_clip_path") or group.get("representative_output_file")),
            scored_at=scored_at,
            sort_timestamp=scored_at,
        )
        records = [ScoreRecord(base_row, group, group)]
        variants = group.get("variants", [])
        if not isinstance(variants, list):
            return records
        for variant in sorted(
            (item for item in variants if isinstance(item, dict)),
            key=lambda item: (
                int(score_float(item.get("variant_index")) or 0),
                str(item.get("variant_id") or ""),
                str(item.get("clip_id") or ""),
            ),
        ):
            variant_flags = self._score_flags_list(variant.get("flags") or variant.get("similarity_flags", []))
            variant_severity = self._score_flag_severity(variant_flags)
            variant_blocked = bool(variant.get("compliance_blocked", blocked))
            variant_scored_at = str(variant.get("scored_at") or scored_at or "")
            row = ScoreRow(
                score_key=build_score_key(variant),
                base_score_key=base_key,
                row_type="variant",
                source_video=source_video,
                run_tag=run_tag,
                source_date=source_date_from_source_video(source_video),
                clip_id=str(variant.get("clip_id") or ""),
                product=str(group.get("product", "general") or "general"),
                total_score=total_score,
                content_score=score_float(group.get("content_score")),
                host_focus_score=score_float(group.get("host_focus_score")),
                hook_score=score_float(group.get("hook_score")),
                quality_score=quality_score,
                engagement_score=score_float(group.get("engagement_score")),
                similarity_score=score_float(variant.get("similarity_score")),
                variants=None,
                flags=tuple(variant_flags),
                flag_count=len(variant_flags),
                flag_severity=variant_severity,
                status=self._score_status_label(total_score, variant_severity, variant_blocked),
                compliance_blocked=variant_blocked,
                summary=str(group.get("summary") or ""),
                output_file=str(variant.get("output_file") or ""),
                clip_path=str(variant.get("clip_path") or ""),
                artifact=self._artifact_for_output(output_dir, variant.get("clip_path") or variant.get("output_file")),
                scored_at=variant_scored_at,
                sort_timestamp=variant_scored_at,
            )
            records.append(ScoreRecord(row, variant, group))
        return records

    def _filter_score_records(
        self,
        records: list[ScoreRecord],
        *,
        search: str | None,
        status: str | None,
        product: str | None,
    ) -> list[ScoreRecord]:
        search_key = str(search or "").casefold().strip()
        status_key = str(status or "").casefold().strip()
        product_key = str(product or "").casefold().strip()
        filtered = records
        if search_key:
            filtered = [
                record
                for record in filtered
                if search_key
                in " ".join(
                    [
                        record.row.source_video,
                        record.row.run_tag,
                        record.row.clip_id,
                        record.row.product,
                        record.row.summary,
                        " ".join(record.row.flags),
                    ]
                ).casefold()
            ]
        if status_key:
            filtered = [record for record in filtered if record.row.status.casefold() == status_key]
        if product_key:
            filtered = [record for record in filtered if record.row.product.casefold() == product_key]
        return filtered

    def _sort_score_records(self, records: list[ScoreRecord], *, sort: str, direction: str) -> list[ScoreRecord]:
        reverse = direction == "desc"
        sorters = {
            "scored_at": lambda record: parse_timestamp(record.row.sort_timestamp) or MIN_SORT_TIMESTAMP,
            "total_score": lambda record: record.row.total_score if record.row.total_score is not None else -1,
            "quality_score": lambda record: record.row.quality_score if record.row.quality_score is not None else -1,
            "similarity_score": lambda record: record.row.similarity_score if record.row.similarity_score is not None else -1,
            "source_video": lambda record: record.row.source_video.casefold(),
            "product": lambda record: record.row.product.casefold(),
            "status": lambda record: record.row.status.casefold(),
        }
        if sort not in sorters:
            raise ValueError(f"Unsupported score sort: {sort}")
        return sorted(records, key=sorters[sort], reverse=reverse)

    def _score_groups_from_summary(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        groups = payload.get("groups", [])
        if isinstance(groups, list) and groups:
            return [group for group in groups if isinstance(group, dict)]
        clips = payload.get("clips", [])
        if isinstance(clips, list):
            return self._synthesize_score_groups_from_clips([clip for clip in clips if isinstance(clip, dict)])
        return []

    def _synthesize_score_groups_from_clips(self, clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for clip in clips:
            clip_id = str(clip.get("clip_id") or "")
            base_clip_id = str(clip.get("base_clip_id") or self._base_clip_id_for_scores(clip_id))
            grouped.setdefault(base_clip_id, []).append(clip)
        groups = []
        for base_clip_id, variants in grouped.items():
            representative = sorted(variants, key=lambda item: str(item.get("clip_id") or ""))[0]
            groups.append(
                {
                    **representative,
                    "score_level": "base",
                    "clip_id": base_clip_id,
                    "base_clip_id": base_clip_id,
                    "representative_clip_id": representative.get("clip_id"),
                    "representative_output_file": representative.get("output_file", ""),
                    "representative_clip_path": representative.get("clip_path", ""),
                    "variant_count": len(variants),
                    "variants": variants,
                }
            )
        return groups

    def _base_clip_id_for_scores(self, clip_id: str) -> str:
        import re

        for pattern in (r"^(clip_\d+)(?:_v\d+(?:_|$).*)?$", r"^(.+?)_v\d+(?:_|$).*$"):
            match = re.match(pattern, str(clip_id or ""), flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return clip_id

    def _score_flags_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, Iterable):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value)]

    def _score_flag_severity(self, flags: list[str]) -> str:
        severities = {self._score_single_flag_severity(flag) for flag in flags}
        if "high" in severities:
            return "high"
        if "medium" in severities:
            return "medium"
        return "none"

    def _score_single_flag_severity(self, flag: Any) -> str:
        text = str(flag or "").casefold()
        if any(token in text for token in ("blocked", "unsafe", "policy", "violation", "missing_file")):
            return "high"
        if any(token in text for token in ("low", "blur", "short", "similar")):
            return "medium"
        return "none"

    def _score_status_label(self, total_score: Any, flag_severity: str = "none", compliance_blocked: bool = False) -> str:
        numeric = score_float(total_score)
        if compliance_blocked:
            return "Blocked"
        if numeric is not None and numeric < 5:
            return "Review"
        if str(flag_severity or "").casefold() == "high":
            return "Review"
        if numeric is not None and numeric >= 7:
            return "Strong"
        return "Okay"

    def _empty_score_stats(self) -> ScoreStats:
        return ScoreStats()

    def _accumulate_score_stats(self, totals: ScoreStats, payload: dict[str, Any]) -> None:
        stats = payload.get("scoring_optimization", {}) if isinstance(payload, dict) else {}
        if not isinstance(stats, dict):
            return
        vision_stats = stats.get("vision_scoring", {})
        if not isinstance(vision_stats, dict):
            vision_stats = {}
        object.__setattr__(totals, "summary_count", totals.summary_count + 1)
        for field, source in (
            ("previous_text_qwen_calls", stats),
            ("actual_text_qwen_calls", stats),
            ("saved_text_qwen_calls", stats),
            ("actual_vision_qwen_calls", vision_stats if "actual_vision_qwen_calls" in vision_stats else stats),
            ("vision_base_group_count", vision_stats),
            ("vision_contact_sheet_groups", vision_stats),
            ("vision_contact_sheet_fallbacks", vision_stats),
        ):
            object.__setattr__(totals, field, getattr(totals, field) + as_nonnegative_int(source.get(field)))

    def _compliance_records(
        self,
        output_dirs: tuple[str, ...] | None = None,
    ) -> tuple[list[ComplianceRow], list[ComplianceViolationRow], list[SourceSignature], list[str]]:
        dirs = output_dirs or self._collect_output_dirs()
        deep = output_dirs is not None
        manifest_signatures = [self._source_signature(Path(output_dir) / "manifest.json") for output_dir in dirs]
        revision = tuple(self._signature_key(signature) for signature in manifest_signatures)
        cache_key = "compliance:global" if not deep else f"compliance:detail:{'|'.join(dirs)}"
        cached = self._cache.get(cache_key, revision, max_age=10.0 if deep else None)
        if cached is not None:
            cached_rows, cached_violations, cached_signatures, cached_warnings = cached
            return list(cached_rows), list(cached_violations), list(cached_signatures), list(cached_warnings)
        warnings: list[str] = []
        signatures: list[SourceSignature] = list(manifest_signatures)
        rows: list[ComplianceRow] = []
        violations: list[ComplianceViolationRow] = []
        for output_dir, manifest_signature in zip(dirs, manifest_signatures):
            folder = Path(output_dir)
            manifest = folder / "manifest.json"
            source_video, run_tag = split_output_folder_name(folder.name)
            seen_compliance_files: set[str] = set()
            for manifest_row in self._manifest_rows(manifest, warnings):
                if not deep and not self._manifest_row_has_compliance_fields(manifest_row):
                    continue
                # The global index and Overview use only the fields already
                # denormalized into manifest rows. Resolving every sidecar here
                # performs thousands of filesystem probes without reading it;
                # reserve that work for the explicit detail endpoint.
                compliance_path = self._resolve_compliance_path(folder, manifest_row) if deep else None
                result = self._load_json_dict(compliance_path, warnings, optional=True) if deep and compliance_path else {}
                if deep and compliance_path:
                    signatures.append(self._source_signature(compliance_path))
                    seen_compliance_files.add(os.path.normcase(str(compliance_path.resolve())))
                row = self._compliance_row(folder, source_video, run_tag, manifest_row, result)
                rows.append(row)
                for violation in result.get("violations", []) if isinstance(result, dict) else []:
                    if isinstance(violation, dict):
                        violations.append(self._violation_row(row, violation))
            if not deep:
                continue
            for compliance_path in self._iter_compliance_files(folder):
                key = os.path.normcase(str(compliance_path.resolve()))
                if key in seen_compliance_files:
                    continue
                signatures.append(self._source_signature(compliance_path))
                result = self._load_json_dict(compliance_path, warnings, optional=True)
                if not result:
                    continue
                row = self._compliance_row(
                    folder,
                    source_video,
                    run_tag,
                    {"clip_id": compliance_path.stem.removesuffix("_compliance"), "product": "general"},
                    result,
                )
                rows.append(row)
                for violation in result.get("violations", []):
                    if isinstance(violation, dict):
                        violations.append(self._violation_row(row, violation))
        rows.sort(key=lambda row: parse_timestamp(row.checked_at) or MIN_SORT_TIMESTAMP, reverse=True)
        violations.sort(key=lambda row: parse_timestamp(row.checked_at) or MIN_SORT_TIMESTAMP, reverse=True)
        self._cache.set(
            cache_key,
            revision,
            (tuple(rows), tuple(violations), tuple(signatures), tuple(warnings)),
        )
        return rows, violations, signatures, warnings

    def _manifest_row_has_compliance_fields(self, row: dict[str, Any]) -> bool:
        return any(
            key in row
            for key in (
                "compliance_passed",
                "compliance_blocked",
                "violation_count",
                "auto_fixed",
                "compliance_summary",
                "compliance_file",
                "compliance_json",
            )
        )

    def _filter_compliance_rows(
        self,
        rows: list[ComplianceRow],
        *,
        search: str | None,
        status: str | None,
        product: str | None,
    ) -> list[ComplianceRow]:
        search_key = str(search or "").casefold().strip()
        status_key = str(status or "").casefold().strip()
        product_key = str(product or "").casefold().strip()
        filtered = rows
        if search_key:
            filtered = [
                row
                for row in filtered
                if search_key
                in " ".join([row.source_video, row.run_tag, row.clip_id, row.product, row.summary]).casefold()
            ]
        if status_key:
            if status_key == "passed":
                filtered = [row for row in filtered if row.passed and not row.blocked]
            elif status_key == "blocked":
                filtered = [row for row in filtered if row.blocked]
            elif status_key == "auto_fixed":
                filtered = [row for row in filtered if row.auto_fixed]
            else:
                filtered = [row for row in filtered if row.status.casefold() == status_key]
        if product_key:
            filtered = [row for row in filtered if row.product.casefold() == product_key]
        return filtered

    def _sort_compliance_rows(self, rows: list[ComplianceRow], *, sort: str, direction: str) -> list[ComplianceRow]:
        reverse = direction == "desc"
        sorters = {
            "checked_at": lambda row: parse_timestamp(row.checked_at) or MIN_SORT_TIMESTAMP,
            "source_video": lambda row: row.source_video.casefold(),
            "product": lambda row: row.product.casefold(),
            "violation_count": lambda row: row.violation_count,
            "status": lambda row: row.status.casefold(),
        }
        if sort not in sorters:
            raise ValueError(f"Unsupported compliance sort: {sort}")
        return sorted(rows, key=sorters[sort], reverse=reverse)

    def _compliance_row(
        self,
        folder: Path,
        source_video: str,
        run_tag: str,
        row: dict[str, Any],
        result: dict[str, Any] | None,
    ) -> ComplianceRow:
        result = result or {}
        checked_at = str(
            result.get("checked_at")
            or row.get("compliance_checked_at")
            or row.get("checked_at")
            or row.get("completed_at")
            or ""
        )
        violation_count = as_nonnegative_int(result.get("violation_count", row.get("violation_count") or 0))
        return ComplianceRow(
            source_video=source_video,
            run_tag=run_tag,
            clip_id=str(row.get("clip_id") or result.get("clip_id") or ""),
            product=str(row.get("product") or result.get("product") or "general"),
            status=str(row.get("status") or ""),
            passed=bool(result.get("passed", row.get("compliance_passed", False))),
            blocked=bool(result.get("blocked", row.get("compliance_blocked", False))),
            auto_fixed=bool(result.get("auto_fixed", row.get("auto_fixed", False))),
            violation_count=violation_count,
            summary=str(result.get("compliance_summary", row.get("compliance_summary", "")) or ""),
            compliance_file=str(row.get("compliance_file") or row.get("compliance_json") or ""),
            output_dir=str(folder),
            checked_at=checked_at,
        )

    def _violation_row(self, clip_record: ComplianceRow, violation: dict[str, Any]) -> ComplianceViolationRow:
        position = violation.get("position") if isinstance(violation.get("position"), dict) else {}
        return ComplianceViolationRow(
            source_video=clip_record.source_video,
            run_tag=clip_record.run_tag,
            clip_id=clip_record.clip_id,
            product=clip_record.product,
            field=str(violation.get("source_field") or "transcript"),
            severity=str(violation.get("severity") or ""),
            violation_type=str(violation.get("violation_type") or ""),
            original_text=str(violation.get("original_text") or ""),
            suggested_replacement=str(violation.get("suggested_replacement") or ""),
            start=score_int(position.get("start")),
            end=score_int(position.get("end")),
            compliance_file=clip_record.compliance_file,
            output_dir=clip_record.output_dir,
            checked_at=clip_record.checked_at,
        )

    def _manifest_rows(self, manifest_path: Path, warnings: list[str]) -> list[dict[str, Any]]:
        payload = self._load_json(manifest_path, warnings, optional=True)
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for key in ("clips", "items"):
                rows = payload.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
        return []

    def _resolve_compliance_path(self, folder: Path, row: dict[str, Any]) -> Path | None:
        candidates: list[Path] = []
        compliance_file = str(row.get("compliance_file") or row.get("compliance_json") or "").strip()
        if compliance_file:
            path = Path(compliance_file)
            candidates.append(path if path.is_absolute() else folder / path)
        clip_id = str(row.get("clip_id") or "").strip()
        output_file = str(row.get("output_file") or "").strip()
        if clip_id and output_file:
            output_path = Path(output_file)
            if not output_path.is_absolute():
                output_path = folder / output_path
            candidates.append(output_path.parent / f"{clip_id}_compliance.json")
            candidates.append(folder / "compliance" / f"{clip_id}_compliance.json")
        for candidate in candidates:
            try:
                if candidate.exists():
                    return candidate
            except OSError:
                continue
        if clip_id:
            try:
                return next(folder.glob(f"**/{clip_id}_compliance.json"), None)
            except OSError:
                return None
        return None

    def _iter_compliance_files(self, folder: Path) -> Iterable[Path]:
        for pattern in ("*_compliance.json", "v*/*_compliance.json", "compliance/*_compliance.json"):
            try:
                yield from folder.glob(pattern)
            except OSError:
                continue

    def _module_index_payload(self) -> tuple[dict[str, Any], SourceSignature, list[str]]:
        path = self._module_library_dir() / "index.json"
        signature = self._source_signature(path)

        def load() -> tuple[dict[str, Any], tuple[str, ...]]:
            warnings: list[str] = []
            payload = self._load_json_dict(path, warnings, optional=True)
            if not signature.exists:
                warnings.append(f"No module index found at {path}")
            modules = payload.get("modules", []) if isinstance(payload, dict) else []
            if modules is not None and not isinstance(modules, list):
                warnings.append("Module index 'modules' field was not a list.")
                payload["modules"] = []
            return payload, tuple(warnings)

        payload, warnings = self._cache.get_or_load(
            "modules:index",
            self._signature_key(signature),
            load,
        )
        return payload, signature, list(warnings)

    def _module_corpus(
        self,
    ) -> tuple[list[ModuleLibraryRow], dict[str, dict[str, Any]], SourceSignature, list[str]]:
        payload, signature, warnings = self._module_index_payload()

        def load() -> tuple[tuple[ModuleLibraryRow, ...], dict[str, dict[str, Any]]]:
            rows: list[ModuleLibraryRow] = []
            modules_by_id: dict[str, dict[str, Any]] = {}
            for module in payload.get("modules", []) if isinstance(payload, dict) else []:
                if not isinstance(module, dict):
                    continue
                row = self._module_row(module, include_artifact=False)
                rows.append(row)
                modules_by_id[row.module_id] = module
            return tuple(rows), modules_by_id

        rows, modules_by_id = self._cache.get_or_load(
            "modules:corpus",
            self._signature_key(signature),
            load,
        )
        return list(rows), modules_by_id, signature, warnings

    def _module_row(self, module: dict[str, Any], *, include_artifact: bool = False) -> ModuleLibraryRow:
        product_key = str(module.get("product") or "")
        file_path = str(module.get("file_path") or "")
        return ModuleLibraryRow(
            module_id=str(module.get("module_id") or Path(file_path).stem),
            product=MODULE_PRODUCT_LABELS.get(product_key, product_key),
            product_key=product_key,
            role=str(module.get("role") or ""),
            source_date=module_source_date_value(module),
            source_video=source_video_filename(module.get("source_video")),
            duration=round(score_float(module.get("duration")) or 0.0, 2),
            confidence=round(score_float(module.get("confidence")) or 0.0, 3),
            quality_status=str(module.get("quality_status") or ""),
            review_status=str(module.get("review_status") or ""),
            boundary_mode=str(module.get("boundary_mode") or ""),
            visual_validation_status=self._module_visual_status(module.get("visual_validation_status")),
            visual_product_hits=as_nonnegative_int(module.get("visual_product_hits")),
            visual_product_confidence_max=round(score_float(module.get("visual_product_confidence_max")) or 0.0, 3),
            visual_validation_reason=str(module.get("visual_validation_reason") or ""),
            file_artifact=self._artifact_for_output(self._module_library_dir(), file_path) if include_artifact else None,
        )

    def _filter_module_rows(
        self,
        rows: list[ModuleLibraryRow],
        *,
        search: str | None,
        status: str | None,
        quality_status: str | None,
        review_status: str | None,
        visual_status: str | None,
        product: str | None,
    ) -> list[ModuleLibraryRow]:
        search_key = str(search or "").casefold().strip()
        status_key = str(status or "").casefold().strip()
        quality_key = str(quality_status or "").casefold().strip()
        review_key = str(review_status or "").casefold().strip()
        visual_key = str(visual_status or "").casefold().strip()
        product_key = str(product or "").casefold().strip()
        filtered = rows
        if search_key:
            filtered = [
                row
                for row in filtered
                if search_key
                in " ".join([row.module_id, row.source_video, row.transcript_text, row.product, row.role]).casefold()
            ]
        if status_key:
            filtered = [
                row
                for row in filtered
                if row.quality_status.casefold() == status_key
                or row.review_status.casefold() == status_key
                or row.visual_validation_status.casefold() == status_key
            ]
        if quality_key:
            filtered = [row for row in filtered if row.quality_status.casefold() == quality_key]
        if review_key:
            filtered = [row for row in filtered if row.review_status.casefold() == review_key]
        if visual_key:
            filtered = [row for row in filtered if row.visual_validation_status.casefold() == visual_key]
        if product_key:
            filtered = [row for row in filtered if row.product_key.casefold() == product_key or row.product.casefold() == product_key]
        return filtered

    def _sort_module_rows(self, rows: list[ModuleLibraryRow], *, sort: str, direction: str) -> list[ModuleLibraryRow]:
        reverse = direction == "desc"
        sorters = {
            "product": lambda row: (row.product.casefold(), row.source_date, row.role, row.module_id),
            "source_date": lambda row: row.source_date,
            "duration": lambda row: row.duration,
            "confidence": lambda row: row.confidence,
            "role": lambda row: row.role.casefold(),
            "status": lambda row: (row.quality_status.casefold(), row.review_status.casefold()),
        }
        if sort not in sorters:
            raise ValueError(f"Unsupported module sort: {sort}")
        return sorted(rows, key=sorters[sort], reverse=reverse)

    def _module_visual_status(self, value: Any) -> str:
        status = str(value or "not_run").strip().lower()
        return status if status in {"passed", "failed", "not_run"} else "not_run"

    def _module_index_count(self, payload: dict[str, Any], modules: list[Any]) -> int:
        try:
            return int(payload.get("module_count") or len(modules) or 0) if isinstance(payload, dict) else len(modules)
        except (TypeError, ValueError):
            return len(modules)

    def _collect_output_dirs(self) -> tuple[str, ...]:
        max_dirs = max(1, int(getattr(self.cfg, "READ_APP_MAX_OUTPUT_DIRS", 200) or 200))
        output_dirs: dict[str, Path] = {}

        def add_output_dir(value: Any) -> None:
            raw = str(value or "").strip()
            if not raw:
                return
            path = Path(raw)
            key = os.path.normcase(str(path))
            output_dirs.setdefault(key, path)

        state, _signature, _warnings = self._read_queue_state(None)
        for video in [self._aggregate_video_entry(item) for item in self._state_videos(state)]:
            for run in video.get("runs", []):
                if not isinstance(run, dict) or not run.get("output_dir"):
                    continue
                add_output_dir(run["output_dir"])

        root = self._output_root()
        root_signature = self._source_signature(root)

        def scan_root() -> tuple[str, ...]:
            discovered: list[Path] = []
            if not root.exists():
                return ()
            try:
                with os.scandir(root) as entries:
                    for entry in entries:
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        folder = Path(entry.path)
                        if not (
                            (folder / "scores_summary.json").exists()
                            or (folder / "manifest.json").exists()
                            or (folder / "compliance").exists()
                        ):
                            continue
                        discovered.append(folder)
            except OSError:
                return ()
            discovered.sort(key=self._safe_mtime_ns, reverse=True)
            return tuple(str(path) for path in discovered[:max_dirs])

        discovered = self._cache.get_or_load(
            f"output_dirs:{root_signature.path}:{max_dirs}",
            self._signature_key(root_signature),
            scan_root,
            max_age=30.0,
        )
        for folder in discovered:
            add_output_dir(folder)

        sorted_dirs = sorted(output_dirs.values(), key=lambda path: self._safe_mtime_ns(path), reverse=True)
        return tuple(str(path) for path in sorted_dirs[:max_dirs])

    def _safe_mtime_ns(self, path: Path) -> int:
        try:
            return int(path.stat().st_mtime_ns)
        except OSError:
            return 0

    def _manifest_clip_count(self, output_dir: Path) -> int:
        payload = self._load_json(output_dir / "manifest.json", [], optional=True)
        if isinstance(payload, list):
            return len([row for row in payload if isinstance(row, dict)])
        if isinstance(payload, dict):
            for key in ("clips", "items"):
                rows = payload.get(key)
                if isinstance(rows, list):
                    return len(rows)
        return 0

    def _artifact_for_output(self, base_dir: Path, value: Any) -> ArtifactRef | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = base_dir / path
        path = path.resolve()
        return ArtifactRef(
            path=str(path),
            url=f"/api/artifacts?path={quote(str(path), safe='')}",
            kind=self._artifact_kind(path),
            exists=path.exists() and path.is_file(),
        )

    def _load_json(self, path: Path | None, warnings: list[str], *, optional: bool) -> Any:
        if path is None:
            return None
        try:
            if not path.exists():
                if not optional:
                    warnings.append(f"Missing JSON file: {path}")
                return None
            for attempt in range(2):
                before = self._source_signature(path)
                text = path.read_text(encoding="utf-8")
                after = self._source_signature(path)
                if self._signature_key(before) == self._signature_key(after):
                    return json.loads(text)
                if attempt == 0:
                    continue
                warnings.append(f"Could not read stable snapshot of {path}; file changed during read.")
                return None
        except Exception as exc:
            warnings.append(f"Could not read {path}: {exc}")
            return None

    def _load_json_dict(self, path: Path | None, warnings: list[str], *, optional: bool) -> dict[str, Any]:
        payload = self._load_json(path, warnings, optional=optional)
        return payload if isinstance(payload, dict) else {}

    def _source_signature(self, path: Path) -> SourceSignature:
        normalized = os.path.normcase(os.path.abspath(os.fspath(path)))
        try:
            stat = path.stat()
        except OSError:
            return SourceSignature(path=normalized, exists=False)
        return SourceSignature(path=normalized, exists=True, mtime_ns=int(stat.st_mtime_ns), size=int(stat.st_size))

    @staticmethod
    def _signature_key(signature: SourceSignature) -> tuple[str, bool, int, int]:
        return (signature.path, signature.exists, signature.mtime_ns, signature.size)

    def _default_state_path(self) -> Path:
        return Path(getattr(self.cfg, "QUEUE_STATE_FILE", Path(getattr(self.cfg, "WORKING_DIR", "working")) / "video_queue_state.json"))

    def _output_root(self) -> Path:
        return Path(getattr(self.cfg, "OUTPUT_DIR", r"D:\output_clips")).resolve()

    def _export_status_path(self) -> Path:
        batch_dir_name = str(getattr(self.cfg, "EXPORT_BATCH_DIR_NAME", "export_batches") or "export_batches")
        return self._output_root() / batch_dir_name / "_status.json"

    def _module_library_dir(self) -> Path:
        return Path(getattr(self.cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules")).resolve()

    def _allowed_artifact_roots(self) -> tuple[Path, ...]:
        working_dir = Path(getattr(self.cfg, "WORKING_DIR", "working"))
        product_broll = Path(getattr(self.cfg, "PRODUCT_BROLL_DIR", "assets/product_broll"))
        roots = [
            Path(getattr(self.cfg, "OUTPUT_DIR", r"D:\output_clips")),
            Path(getattr(self.cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules")),
            product_broll,
            working_dir / "variation_previews",
            Path.cwd() / "assets" / "variation_preview",
        ]
        return tuple((Path.cwd() / root if not root.is_absolute() else root).resolve() for root in roots)

    def _is_relative_to(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _artifact_kind(self, path: Path) -> Literal["video", "image", "json", "text", "unknown"]:
        suffix = path.suffix.lower()
        if suffix in {".mp4", ".mov", ".mkv", ".webm"}:
            return "video"
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return "image"
        if suffix == ".json":
            return "json"
        if suffix in {".txt", ".log", ".csv", ".tsv"}:
            return "text"
        return "unknown"

    def _media_type(self, path: Path) -> str | None:
        suffix = path.suffix.lower()
        return {
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".mov": "video/quicktime",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".json": "application/json",
            ".txt": "text/plain",
            ".log": "text/plain",
            ".csv": "text/csv",
            ".tsv": "text/tab-separated-values",
        }.get(suffix)

    def _gpu_stats(self) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.total,name",
                    "--format=csv,noheader,nounits",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:
            return {"utilization": None, "memory_percent": None, "label": "Unavailable"}
        if result.returncode != 0 or not result.stdout.strip():
            return {"utilization": None, "memory_percent": None, "label": "Unavailable"}
        rows = []
        for line in result.stdout.strip().splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 4:
                continue
            try:
                util = float(parts[0])
                mem_used = float(parts[1])
                mem_total = float(parts[2])
            except ValueError:
                continue
            rows.append((util, mem_used, mem_total, parts[3]))
        if not rows:
            return {"utilization": None, "memory_percent": None, "label": "Unavailable"}
        avg_util = sum(row[0] for row in rows) / len(rows)
        total_used = sum(row[1] for row in rows)
        total_mem = sum(row[2] for row in rows)
        label = f"{rows[0][3]} | {int(total_used)}/{int(total_mem)} MB" if len(rows) == 1 else f"{len(rows)} GPU(s) | {int(total_used)}/{int(total_mem)} MB"
        return {
            "utilization": avg_util,
            "memory_percent": (total_used / total_mem * 100.0) if total_mem else None,
            "label": label,
        }

    def _bounded_page(self, limit: int, offset: int) -> tuple[int, int]:
        return max(1, min(int(limit or 50), 500)), max(0, int(offset or 0))
