from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any


STAGES = ("transcribe", "llm", "yolo", "ffmpeg")
PRE_EDIT_STAGES = STAGES[:-1]
EDIT_STAGE = "ffmpeg"
TERMINAL_VIDEO_STATUSES = {"completed", "failed"}
DEFAULT_RUNNING_STALL_SECONDS = 2 * 60 * 60
DEFAULT_QUEUED_STALL_SECONDS = 24 * 60 * 60
SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}
LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed


def format_age(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown age"
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} sec"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hr"
    days = hours // 24
    return f"{days} day" if days == 1 else f"{days} days"


def coerce_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def video_key(video: dict[str, Any]) -> str:
    return str(video.get("path") or video.get("video_path") or video.get("name") or "")


def stage_label(stage_key: str, stage_labels: dict[str, str] | None = None) -> str:
    if stage_labels and stage_key in stage_labels:
        return stage_labels[stage_key]
    return stage_key.replace("_", " ").title()


def _stage_state(video: dict[str, Any], stage_key: str) -> dict[str, Any]:
    stages = video.get("stages") if isinstance(video.get("stages"), dict) else {}
    stage_state = stages.get(stage_key)
    return stage_state if isinstance(stage_state, dict) else {}


def _stage_status(stage_state: dict[str, Any]) -> str:
    return str(stage_state.get("status") or "pending").strip().lower()


def _is_stage_queued(stage_state: dict[str, Any]) -> bool:
    return bool(stage_state.get("queued")) or _stage_status(stage_state) == "queued"


def _has_queue_marker(stage_state: dict[str, Any]) -> bool:
    return bool(stage_state.get("queued")) or parse_timestamp(stage_state.get("queued_at")) is not None


def _active_clip_renders(stage_state: dict[str, Any]) -> int:
    return coerce_nonnegative_int(stage_state.get("active_clip_renders"))


def _latest_timestamp(*values: Any) -> datetime | None:
    timestamps = [parsed for parsed in (parse_timestamp(value) for value in values) if parsed]
    return max(timestamps) if timestamps else None


def _previous_stage_finished_at(video: dict[str, Any], stage_key: str) -> datetime | None:
    try:
        index = STAGES.index(stage_key)
    except ValueError:
        return None
    if index <= 0:
        return parse_timestamp(video.get("created_at"))
    previous_stage = STAGES[index - 1]
    return parse_timestamp(_stage_state(video, previous_stage).get("finished_at"))


def _stage_queue_since(video: dict[str, Any], stage_key: str, stage_state: dict[str, Any]) -> datetime | None:
    return (
        parse_timestamp(stage_state.get("queued_at"))
        or _previous_stage_finished_at(video, stage_key)
        or parse_timestamp(video.get("created_at"))
    )


def _stage_activity_at(video: dict[str, Any], stage_key: str, stage_state: dict[str, Any]) -> datetime | None:
    if stage_key == EDIT_STAGE:
        return _latest_timestamp(
            stage_state.get("last_progress_at"),
            stage_state.get("started_at"),
            stage_state.get("queued_at"),
            video.get("updated_at"),
        )
    return _latest_timestamp(stage_state.get("started_at"), video.get("updated_at"))


def _all_previous_stages_done(video: dict[str, Any], stage_key: str) -> bool:
    try:
        index = STAGES.index(stage_key)
    except ValueError:
        return False
    return all(_stage_status(_stage_state(video, previous)) == "done" for previous in STAGES[:index])


def _make_issue(
    *,
    kind: str,
    severity: str,
    video: dict[str, Any] | None,
    stage_key: str | None,
    stage_labels: dict[str, str] | None,
    message: str,
    now: datetime,
    since: datetime | None = None,
) -> dict[str, Any]:
    age_seconds = None
    if since:
        age_seconds = max(0, int((now - since).total_seconds()))
    return {
        "kind": kind,
        "severity": severity,
        "stage": stage_key,
        "stage_label": stage_label(stage_key, stage_labels) if stage_key else "Queue",
        "video_key": video_key(video) if video else "",
        "name": str((video or {}).get("name") or "Queue"),
        "message": message,
        "since": since.isoformat(timespec="seconds") if since else None,
        "age_seconds": age_seconds,
        "age_label": format_age(age_seconds) if age_seconds is not None else "",
    }


def video_attention_items(
    video: dict[str, Any],
    *,
    now: datetime | None = None,
    queue_status: str = "",
    stage_labels: dict[str, str] | None = None,
    running_stall_seconds: float = DEFAULT_RUNNING_STALL_SECONDS,
    queued_stall_seconds: float = DEFAULT_QUEUED_STALL_SECONDS,
) -> list[dict[str, Any]]:
    now = now or datetime.now().astimezone()
    status = str(video.get("status") or "").strip().lower()
    if status == "failed":
        return []

    issues: list[dict[str, Any]] = []
    current_stage = str(video.get("current_stage") or "").strip().lower()

    if status == "completed":
        for stage_key in STAGES:
            stage_state = _stage_state(video, stage_key)
            queue_marker_since = parse_timestamp(stage_state.get("queued_at"))
            if _stage_status(stage_state) != "done":
                issues.append(
                    _make_issue(
                        kind="completed_with_open_stage",
                        severity="warning",
                        video=video,
                        stage_key=stage_key,
                        stage_labels=stage_labels,
                        message=f"{stage_label(stage_key, stage_labels)} is still open on a completed video.",
                        now=now,
                        since=parse_timestamp(stage_state.get("finished_at")),
                    )
                )
            if _has_queue_marker(stage_state):
                issues.append(
                    _make_issue(
                        kind="completed_with_queue_marker",
                        severity="warning",
                        video=video,
                        stage_key=stage_key,
                        stage_labels=stage_labels,
                        message=f"{stage_label(stage_key, stage_labels)} still has queue markers on a completed video.",
                        now=now,
                        since=queue_marker_since or parse_timestamp(stage_state.get("finished_at")),
                    )
                )
            if _active_clip_renders(stage_state) > 0:
                issues.append(
                    _make_issue(
                        kind="completed_with_active_renders",
                        severity="warning",
                        video=video,
                        stage_key=stage_key,
                        stage_labels=stage_labels,
                        message=f"{stage_label(stage_key, stage_labels)} still reports active clip renders.",
                        now=now,
                        since=parse_timestamp(stage_state.get("last_progress_at")),
                    )
                )
        return issues

    for stage_key in STAGES:
        stage_state = _stage_state(video, stage_key)
        stage_status = _stage_status(stage_state)
        label = stage_label(stage_key, stage_labels)
        queue_marker_since = parse_timestamp(stage_state.get("queued_at"))

        if stage_status == "failed" and status not in TERMINAL_VIDEO_STATUSES:
            message = f"{label} failed, but the video is still marked {status or 'non-terminal'}."
            if stage_state.get("queued"):
                message = f"{label} failed, but the queue flag is still set."
            issues.append(
                _make_issue(
                    kind="failed_stage_nonterminal",
                    severity="critical",
                    video=video,
                    stage_key=stage_key,
                    stage_labels=stage_labels,
                    message=message,
                    now=now,
                    since=parse_timestamp(stage_state.get("finished_at"))
                    or parse_timestamp(stage_state.get("last_progress_at")),
                )
            )
            continue

        if (
            stage_status not in {"queued", "running"}
            and _has_queue_marker(stage_state)
            and queue_status != "paused"
        ):
            issues.append(
                _make_issue(
                    kind="inactive_stage_queue_marker",
                    severity="warning",
                    video=video,
                    stage_key=stage_key,
                    stage_labels=stage_labels,
                    message=f"{label} is {stage_status}, but queue markers are still set.",
                    now=now,
                    since=queue_marker_since or _stage_activity_at(video, stage_key, stage_state),
                )
            )

        if (
            current_stage == stage_key
            and status in {"queued", "running"}
            and queue_status != "paused"
            and stage_status != "running"
        ):
            issues.append(
                _make_issue(
                    kind="current_stage_inactive",
                    severity="warning",
                    video=video,
                    stage_key=stage_key,
                    stage_labels=stage_labels,
                    message=f"{label} is the current step, but it is {stage_status}.",
                    now=now,
                    since=_stage_activity_at(video, stage_key, stage_state),
                )
            )

        if stage_status == "running":
            since = _stage_activity_at(video, stage_key, stage_state)
            if since is None:
                issues.append(
                    _make_issue(
                        kind="running_without_heartbeat",
                        severity="warning",
                        video=video,
                        stage_key=stage_key,
                        stage_labels=stage_labels,
                        message=f"{label} is running without a progress timestamp.",
                        now=now,
                    )
                )
                continue
            age_seconds = max(0, (now - since).total_seconds())
            if age_seconds >= running_stall_seconds:
                issues.append(
                    _make_issue(
                        kind="running_stalled",
                        severity="critical",
                        video=video,
                        stage_key=stage_key,
                        stage_labels=stage_labels,
                        message=f"{label} has not reported progress for {format_age(age_seconds)}.",
                        now=now,
                        since=since,
                )
            )

        if _is_stage_queued(stage_state) and stage_status == "queued" and queue_status != "paused":
            since = _stage_queue_since(video, stage_key, stage_state)
            if since is None:
                issues.append(
                    _make_issue(
                        kind="queued_without_timestamp",
                        severity="warning",
                        video=video,
                        stage_key=stage_key,
                        stage_labels=stage_labels,
                        message=f"{label} is queued without a queue timestamp.",
                        now=now,
                    )
                )
                continue
            age_seconds = max(0, (now - since).total_seconds())
            if age_seconds >= queued_stall_seconds:
                issues.append(
                    _make_issue(
                        kind="queued_stalled",
                        severity="warning",
                        video=video,
                        stage_key=stage_key,
                        stage_labels=stage_labels,
                        message=f"{label} has waited in queue for {format_age(age_seconds)}.",
                        now=now,
                        since=since,
                    )
                )

        if (
            stage_key == EDIT_STAGE
            and stage_status == "pending"
            and status in {"queued", "running"}
            and queue_status != "paused"
            and _all_previous_stages_done(video, EDIT_STAGE)
        ):
            since = _stage_queue_since(video, EDIT_STAGE, stage_state)
            age_seconds = max(0, (now - since).total_seconds()) if since else None
            if current_stage == EDIT_STAGE or age_seconds is None or age_seconds >= queued_stall_seconds:
                wait = format_age(age_seconds) if age_seconds is not None else "unknown time"
                issues.append(
                    _make_issue(
                        kind="ready_not_enqueued",
                        severity="warning",
                        video=video,
                        stage_key=EDIT_STAGE,
                        stage_labels=stage_labels,
                        message=f"{stage_label(EDIT_STAGE, stage_labels)} is ready but not actively queued after {wait}.",
                        now=now,
                        since=since,
                    )
                )

    return sorted(issues, key=lambda item: (SEVERITY_RANK.get(item["severity"], 0), item.get("age_seconds") or 0), reverse=True)


def _video_is_nonterminal(video: dict[str, Any]) -> bool:
    return str(video.get("status") or "").strip().lower() not in TERMINAL_VIDEO_STATUSES


def derive_queue_health(
    state: dict[str, Any],
    *,
    now: datetime | None = None,
    stage_labels: dict[str, str] | None = None,
    running_stall_seconds: float = DEFAULT_RUNNING_STALL_SECONDS,
    queued_stall_seconds: float = DEFAULT_QUEUED_STALL_SECONDS,
) -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    queue_status = str(state.get("queue_status") or "unknown").strip().lower()
    raw_videos = state.get("videos") if isinstance(state.get("videos"), dict) else {}
    videos = [video for video in raw_videos.values() if isinstance(video, dict)]

    top_issues: list[dict[str, Any]] = []
    paused_at = parse_timestamp(state.get("paused_at"))
    if paused_at and queue_status != "paused":
        age_seconds = max(0, int((now - paused_at).total_seconds()))
        top_issues.append(
            _make_issue(
                kind="stale_paused_at",
                severity="warning",
                video=None,
                stage_key=None,
                stage_labels=stage_labels,
                message=f"paused_at is still set from {format_age(age_seconds)} ago while queue_status is {queue_status or 'unknown'}.",
                now=now,
                since=paused_at,
            )
        )

    attention_by_video: dict[str, list[dict[str, Any]]] = {}
    flat_video_issues: list[dict[str, Any]] = []
    running_stage_count = 0
    queued_stage_count = 0
    active_clip_renders = 0

    for video in videos:
        for stage_key in STAGES:
            stage_state = _stage_state(video, stage_key)
            stage_status = _stage_status(stage_state)
            if stage_status == "running":
                running_stage_count += 1
            if _is_stage_queued(stage_state):
                queued_stage_count += 1
            active_clip_renders += _active_clip_renders(stage_state)

        issues = video_attention_items(
            video,
            now=now,
            queue_status=queue_status,
            stage_labels=stage_labels,
            running_stall_seconds=running_stall_seconds,
            queued_stall_seconds=queued_stall_seconds,
        )
        if issues:
            key = video_key(video)
            attention_by_video[key] = issues
            flat_video_issues.extend(issues)

    nonterminal_count = sum(1 for video in videos if _video_is_nonterminal(video))
    if queue_status in {"completed", "idle"} and nonterminal_count:
        top_issues.append(
            _make_issue(
                kind="queue_status_nonterminal",
                severity="warning",
                video=None,
                stage_key=None,
                stage_labels=stage_labels,
                message=f"queue_status is {queue_status}, but {nonterminal_count} video(s) are still non-terminal.",
                now=now,
                since=parse_timestamp(state.get("updated_at")),
            )
        )

    if queue_status == "running" and nonterminal_count and running_stage_count == 0 and queued_stage_count == 0:
        top_issues.append(
            _make_issue(
                kind="running_without_work",
                severity="warning",
                video=None,
                stage_key=None,
                stage_labels=stage_labels,
                message="queue_status is running, but no stages are running or queued.",
                now=now,
                since=parse_timestamp(state.get("updated_at")),
            )
        )

    sort_key = lambda item: (SEVERITY_RANK.get(item["severity"], 0), item.get("age_seconds") or 0)
    top_video_issues = sorted(flat_video_issues, key=sort_key, reverse=True)
    all_issues = sorted(top_issues + flat_video_issues, key=sort_key, reverse=True)
    issue_counts = Counter(issue["kind"] for issue in all_issues)
    severity = "ok"
    if any(issue["severity"] == "critical" for issue in all_issues):
        severity = "critical"
    elif all_issues:
        severity = "warning"

    if severity == "critical":
        status_label = "Stalled"
    elif severity == "warning":
        status_label = "Needs Attention"
    else:
        status_label = queue_status.title() if queue_status else "Healthy"

    summary_parts = []
    if attention_by_video:
        summary_parts.append(f"{len(attention_by_video)} video(s) need attention")
    if issue_counts.get("running_stalled"):
        summary_parts.append(f"{issue_counts['running_stalled']} stalled running stage(s)")
    if issue_counts.get("queued_stalled") or issue_counts.get("ready_not_enqueued"):
        stale_waiting = issue_counts.get("queued_stalled", 0) + issue_counts.get("ready_not_enqueued", 0)
        summary_parts.append(f"{stale_waiting} stale waiting stage(s)")
    stale_queue_markers = issue_counts.get("inactive_stage_queue_marker", 0) + issue_counts.get("completed_with_queue_marker", 0)
    if stale_queue_markers:
        summary_parts.append(f"{stale_queue_markers} stale queue marker(s)")
    if issue_counts.get("stale_paused_at"):
        summary_parts.append("stale pause marker")
    if not summary_parts:
        summary_parts.append("No stalled or inconsistent queue state detected")

    return {
        "status": "ok" if severity == "ok" else "needs_attention",
        "severity": severity,
        "status_label": status_label,
        "queue_status": queue_status,
        "summary": ", ".join(summary_parts),
        "issues": all_issues,
        "top_issues": top_issues,
        "video_issues": flat_video_issues,
        "attention_by_video": attention_by_video,
        "attention_video_count": len(attention_by_video),
        "stalled_stage_count": issue_counts.get("running_stalled", 0),
        "stale_waiting_stage_count": issue_counts.get("queued_stalled", 0) + issue_counts.get("ready_not_enqueued", 0),
        "stale_queue_marker_count": stale_queue_markers,
        "failed_stage_count": issue_counts.get("failed_stage_nonterminal", 0),
        "running_stage_count": running_stage_count,
        "queued_stage_count": queued_stage_count,
        "active_clip_renders": active_clip_renders,
        "issue_counts": dict(issue_counts),
        "top_videos": top_video_issues[:8],
    }
