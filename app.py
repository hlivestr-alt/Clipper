from __future__ import annotations

import html
import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import psutil
import streamlit as st


STAGES = [
    ("transcribe", "Transcription", "audio", "#22c55e"),
    ("llm", "LLM Processing", "chip", "#3b82f6"),
    ("yolo", "YOLO Detection", "focus", "#fbbf24"),
    ("ffmpeg", "Video Editing", "scissors", "#8b5cf6"),
]
STAGE_LABELS = {key: label for key, label, _, _ in STAGES}
DEFAULT_STATE_CANDIDATES = [
    Path("state.json"),
    Path("working") / "video_queue_state.json",
]
TREND_PRODUCTS = ["Cleanser", "Serum", "Toner", "Eye Cream", "Sheet Mask", "Moisturizer"]


def resolve_default_state_path() -> Path:
    for candidate in DEFAULT_STATE_CANDIDATES:
        if candidate.exists():
            return candidate
    return DEFAULT_STATE_CANDIDATES[-1]


st.set_page_config(
    page_title="VOD Processing Dashboard",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    :root {
        --bg: #08111f;
        --panel: #101a29;
        --panel-soft: #111e30;
        --panel-rail: #0b1422;
        --line: rgba(148, 163, 184, 0.12);
        --text: #edf3ff;
        --muted: #94a3b8;
        --blue: #3b82f6;
        --green: #22c55e;
        --yellow: #fbbf24;
        --red: #ef4444;
        --violet: #8b5cf6;
    }

    .stApp {
        background:
            radial-gradient(circle at 20% 0%, rgba(59, 130, 246, 0.08), transparent 28%),
            radial-gradient(circle at 100% 0%, rgba(139, 92, 246, 0.07), transparent 24%),
            linear-gradient(180deg, #07101c 0%, #0a1220 100%);
        color: var(--text);
    }

    [data-testid="stSidebar"] {
        display: none;
    }

    header[data-testid="stHeader"] {
        background: transparent;
    }

    .block-container {
        padding-top: 1rem;
        padding-bottom: 1.5rem;
        max-width: 1600px;
    }

    h1, h2, h3, h4 {
        color: var(--text);
    }

    div[data-testid="stVerticalBlockBorderWrapper"] {
        background: linear-gradient(180deg, rgba(17, 26, 41, 0.98), rgba(14, 24, 38, 0.98));
        border: 1px solid var(--line);
        border-radius: 16px;
        box-shadow: 0 18px 48px rgba(0, 0, 0, 0.18);
    }

    div[data-testid="stProgressBar"] {
        padding-top: 0.15rem;
    }

    div[data-testid="stProgressBar"] > div {
        background-color: rgba(30, 41, 59, 0.78);
        border-radius: 999px;
    }

    div[data-testid="stProgressBar"] > div > div {
        background: linear-gradient(90deg, #3b82f6, #60a5fa);
        border-radius: 999px;
    }

    div[data-testid="stDataFrame"] {
        border: 1px solid var(--line);
        border-radius: 14px;
        overflow: hidden;
    }

    .topbar {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 1rem;
        padding: 0.2rem 0 0.4rem 0;
    }

    .topbar-left {
        display: flex;
        align-items: center;
        gap: 0.85rem;
    }

    .app-icon {
        width: 44px;
        height: 44px;
        border-radius: 12px;
        display: flex;
        align-items: center;
        justify-content: center;
        background: linear-gradient(180deg, rgba(25, 40, 66, 0.98), rgba(16, 26, 40, 0.98));
        border: 1px solid var(--line);
        font-size: 1.35rem;
    }

    .app-title {
        font-size: 1.9rem;
        font-weight: 700;
        color: var(--text);
        margin-bottom: 0.1rem;
    }

    .app-subtitle {
        color: var(--muted);
        font-size: 0.9rem;
    }

    .status-pill {
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        padding: 0.5rem 0.8rem;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: rgba(12, 20, 33, 0.72);
        color: var(--text);
        font-size: 0.88rem;
    }

    .status-dot {
        width: 8px;
        height: 8px;
        border-radius: 999px;
        background: var(--green);
        box-shadow: 0 0 10px rgba(34, 197, 94, 0.5);
    }

    .header-actions {
        display: flex;
        justify-content: flex-end;
        align-items: center;
        gap: 0.55rem;
        padding-top: 0.45rem;
    }

    .nav-shell {
        min-height: 100%;
    }

    .nav-item {
        display: flex;
        align-items: center;
        gap: 0.85rem;
        padding: 0.9rem 1rem;
        color: #d7e3f7;
        border-radius: 12px;
        margin-bottom: 0.35rem;
        border: 1px solid transparent;
        background: transparent;
        font-weight: 500;
    }

    .nav-item.active {
        background: linear-gradient(180deg, rgba(30, 64, 124, 0.92), rgba(25, 52, 102, 0.92));
        border-color: rgba(96, 165, 250, 0.16);
    }

    .nav-item.active .nav-icon {
        background: rgba(96, 165, 250, 0.18);
        border-color: rgba(96, 165, 250, 0.18);
    }

    .nav-icon {
        width: 28px;
        height: 28px;
        border-radius: 9px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        background: rgba(148, 163, 184, 0.08);
        border: 1px solid rgba(148, 163, 184, 0.08);
        color: #dbeafe;
        font-weight: 700;
        font-size: 0.85rem;
    }

    .icon-svg {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        line-height: 0;
    }

    .section-kicker {
        color: var(--muted);
        text-transform: uppercase;
        letter-spacing: 0.11em;
        font-size: 0.76rem;
        margin-bottom: 0.75rem;
    }

    .kpi-card {
        padding: 0.9rem 1rem;
        border-radius: 16px;
        border: 1px solid var(--line);
        background: linear-gradient(180deg, rgba(16, 26, 41, 0.98), rgba(14, 24, 38, 0.98));
        height: 112px;
        display: flex;
        align-items: center;
        gap: 0.9rem;
        overflow: hidden;
    }

    .kpi-icon {
        width: 56px;
        height: 56px;
        border-radius: 16px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 700;
        font-size: 1rem;
        border: 1px solid rgba(255, 255, 255, 0.06);
    }

    .kpi-label {
        font-size: 0.88rem;
        color: var(--text);
        margin-bottom: 0.25rem;
        line-height: 1.15;
        min-height: 2.05rem;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }

    .kpi-value {
        font-size: 2rem;
        line-height: 1;
        font-weight: 700;
        color: var(--text);
        margin-bottom: 0.25rem;
    }

    .kpi-sub {
        font-size: 0.88rem;
        color: var(--muted);
    }

    .panel-title {
        font-size: 1.05rem;
        font-weight: 700;
        margin-bottom: 0.85rem;
    }

    .page-title {
        font-size: 1.3rem;
        font-weight: 700;
        color: var(--text);
        margin-bottom: 0.2rem;
    }

    .page-subtitle {
        color: var(--muted);
        font-size: 0.9rem;
        margin-bottom: 1rem;
    }

    .pipeline-stage {
        text-align: center;
        display: flex;
        flex-direction: column;
        align-items: center;
    }

    .stage-ring {
        width: 76px;
        height: 76px;
        margin: 0 auto 0.85rem auto;
        border-radius: 999px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 700;
        font-size: 1rem;
        background: rgba(255, 255, 255, 0.02);
        box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.05);
    }

    .stage-name {
        color: var(--text);
        font-size: 0.95rem;
        line-height: 1.18;
        min-height: 2.25rem;
        margin-bottom: 0.45rem;
        display: flex;
        align-items: center;
        justify-content: center;
    }

    .stage-count {
        color: var(--text);
        font-size: 1.85rem;
        line-height: 1;
        font-weight: 700;
        margin-bottom: 0.25rem;
    }

    .stage-caption {
        color: var(--muted);
        font-size: 0.84rem;
    }

    .stage-arrow {
        text-align: center;
        color: rgba(148, 163, 184, 0.85);
        font-size: 1.6rem;
        padding-top: 1.65rem;
    }

    .queue-row {
        display: grid;
        grid-template-columns: 1.2fr 2.4fr auto;
        gap: 0.8rem;
        align-items: center;
        margin-bottom: 0.95rem;
    }

    .queue-label {
        color: #dce7f9;
        font-size: 0.92rem;
    }

    .queue-count {
        color: #dce7f9;
        font-size: 0.9rem;
        white-space: nowrap;
    }

    .mini-grid {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 1rem;
        margin-bottom: 0.8rem;
    }

    .mini-stat {
        padding: 0.35rem 0 0.8rem 0;
    }

    .mini-stat-label {
        color: #d8e4f6;
        font-size: 0.92rem;
        margin-bottom: 0.6rem;
    }

    .mini-stat-value {
        color: var(--text);
        font-size: 1.95rem;
        line-height: 1;
        font-weight: 700;
        margin-bottom: 0.35rem;
    }

    .mini-stat-sub {
        color: var(--muted);
        font-size: 0.86rem;
    }

    .system-row {
        margin-bottom: 1rem;
    }

    .system-line {
        display: flex;
        justify-content: space-between;
        align-items: center;
        color: #dce7f9;
        font-size: 0.95rem;
        margin-bottom: 0.35rem;
    }

    .system-left {
        display: flex;
        align-items: center;
        gap: 0.45rem;
    }

    .system-dot {
        width: 8px;
        height: 8px;
        border-radius: 999px;
        background: var(--green);
    }

    .small-muted {
        color: var(--muted);
        font-size: 0.84rem;
    }

    .table-shell {
        border: 1px solid var(--line);
        border-radius: 14px;
        overflow: hidden;
        background: rgba(9, 15, 25, 0.28);
    }

    .video-table {
        width: 100%;
        border-collapse: collapse;
    }

    .video-table thead th {
        text-align: left;
        font-size: 0.86rem;
        font-weight: 500;
        color: var(--muted);
        padding: 0.95rem 1rem;
        border-bottom: 1px solid var(--line);
        background: rgba(12, 19, 31, 0.35);
    }

    .video-table tbody td {
        padding: 0.95rem 1rem;
        border-bottom: 1px solid rgba(148, 163, 184, 0.08);
        color: #e2e8f0;
        font-size: 0.92rem;
        vertical-align: middle;
    }

    .video-table tbody tr:hover {
        background: rgba(59, 130, 246, 0.045);
    }

    .status-badge {
        display: inline-flex;
        align-items: center;
        padding: 0.3rem 0.65rem;
        border-radius: 10px;
        font-size: 0.82rem;
        font-weight: 600;
        border: 1px solid transparent;
    }

    .status-processing {
        color: #93c5fd;
        background: rgba(37, 99, 235, 0.16);
        border-color: rgba(59, 130, 246, 0.15);
    }

    .status-completed {
        color: #86efac;
        background: rgba(22, 163, 74, 0.14);
        border-color: rgba(34, 197, 94, 0.15);
    }

    .status-waiting {
        color: #fde68a;
        background: rgba(202, 138, 4, 0.16);
        border-color: rgba(251, 191, 36, 0.15);
    }

    .status-failed {
        color: #fda4af;
        background: rgba(220, 38, 38, 0.15);
        border-color: rgba(239, 68, 68, 0.15);
    }

    .progress-wrap {
        display: flex;
        align-items: center;
        gap: 0.7rem;
        min-width: 160px;
    }

    .progress-track {
        width: 102px;
        height: 10px;
        border-radius: 999px;
        background: rgba(30, 41, 59, 0.9);
        overflow: hidden;
    }

    .progress-fill {
        height: 100%;
        border-radius: 999px;
        background: linear-gradient(90deg, #3b82f6, #60a5fa);
    }

    .progress-fill.completed {
        background: linear-gradient(90deg, #16a34a, #22c55e);
    }

    .progress-value {
        color: #dbeafe;
        font-size: 0.86rem;
        min-width: 38px;
    }

    .row-action {
        color: #94a3b8;
        text-align: center;
        font-weight: 700;
        letter-spacing: 0.2em;
    }

    div[data-testid="stButton"] > button {
        border-radius: 10px;
        background: linear-gradient(180deg, rgba(19, 31, 50, 0.96), rgba(14, 22, 35, 0.96));
        border: 1px solid var(--line);
        color: var(--text);
        min-height: 2.5rem;
        box-shadow: none;
    }

    div[data-testid="stButton"] > button:hover {
        border-color: rgba(96, 165, 250, 0.22);
        color: white;
    }

    div[data-testid="stButton"] > button[kind="primary"] {
        background: linear-gradient(180deg, rgba(37, 99, 235, 0.95), rgba(29, 78, 216, 0.95));
        border-color: rgba(96, 165, 250, 0.28);
    }

    .nav-shell div[data-testid="stButton"] > button {
        text-align: left;
        justify-content: flex-start;
        padding-left: 0.95rem;
        font-weight: 500;
    }

    .nav-shell div[data-testid="stButton"] > button[kind="primary"] {
        box-shadow: 0 10px 24px rgba(37, 99, 235, 0.22);
    }

    div[data-baseweb="select"] > div,
    div[data-baseweb="input"] > div,
    div[data-baseweb="base-input"] > div {
        background: rgba(12, 20, 33, 0.72) !important;
        border-color: rgba(148, 163, 184, 0.14) !important;
    }

    .stTabs [data-baseweb="tab-list"] {
        gap: 0.35rem;
        border-bottom: 1px solid rgba(148, 163, 184, 0.12);
        margin-bottom: 1rem;
    }

    .stTabs [data-baseweb="tab"] {
        background: transparent;
        border-radius: 10px 10px 0 0;
        color: var(--muted);
        padding: 0.75rem 1rem 0.7rem 1rem;
    }

    .stTabs [aria-selected="true"] {
        color: var(--text) !important;
        background: rgba(59, 130, 246, 0.08) !important;
        box-shadow: inset 0 -2px 0 #3b82f6;
    }

    div[data-testid="stSelectbox"] label,
    div[data-testid="stTextInput"] label,
    div[data-testid="stToggle"] label,
    div[data-testid="stSlider"] label {
        color: var(--muted) !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    total_seconds = max(int(seconds), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_datetime(value: datetime | None) -> str:
    if not value:
        return "-"
    return value.strftime("%b %d, %I:%M %p")


def format_relative_time(value: datetime | None) -> str:
    if not value:
        return "never"
    now = datetime.now().astimezone()
    delta = now - value
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return f"{seconds} sec ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    return f"{days} day ago" if days == 1 else f"{days} days ago"


def infer_run_completed_at(run: dict[str, Any]) -> datetime | None:
    completed_at = parse_timestamp(run.get("completed_at"))
    if completed_at:
        return completed_at

    if (run.get("status") or "").lower() != "completed":
        return None

    finished_times = []
    for stage_state in (run.get("stages") or {}).values():
        if not isinstance(stage_state, dict):
            continue
        finished_at = parse_timestamp(stage_state.get("finished_at"))
        if finished_at:
            finished_times.append(finished_at)

    return max(finished_times) if finished_times else None


def average_completed_bucket(counter: Counter) -> float:
    if not counter:
        return 0.0
    return sum(counter.values()) / len(counter)


def floor_to_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def floor_to_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


@st.cache_data(ttl=2, show_spinner=False)
def load_state(state_path: str) -> dict[str, Any]:
    path = Path(state_path)
    if not path.exists():
        return {
            "schema_version": 2,
            "videos": {},
            "updated_at": None,
            "_error": f"State file not found: {path}",
        }

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "schema_version": 2,
            "videos": {},
            "updated_at": None,
            "_error": f"Failed to read state file: {exc}",
        }


@st.cache_data(ttl=10, show_spinner=False)
def load_manifest_clip_count(output_dir: str | None) -> int:
    if not output_dir:
        return 0

    manifest_path = Path(output_dir) / "manifest.json"
    if not manifest_path.exists():
        return 0

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return 0

    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        if isinstance(payload.get("clips"), list):
            return len(payload["clips"])
        if isinstance(payload.get("items"), list):
            return len(payload["items"])
    return 0


@st.cache_data(ttl=10, show_spinner=False)
def load_score_rows(output_dirs: tuple[str, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for output_dir in output_dirs:
        folder = Path(output_dir)
        summary_path = folder / "scores_summary.json"
        if not summary_path.exists():
            continue
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        source_video, run_tag = split_output_folder_name(folder.name)
        groups = payload.get("groups", []) if isinstance(payload, dict) else []
        if isinstance(groups, list) and groups:
            for group in groups:
                if isinstance(group, dict):
                    rows.extend(build_group_score_rows(group, source_video, run_tag))
            continue

        clips = payload.get("clips", []) if isinstance(payload, dict) else []
        if isinstance(clips, list):
            for group in synthesize_score_groups_from_clips(clips):
                rows.extend(build_group_score_rows(group, source_video, run_tag))
    rows.sort(key=lambda row: row["_scored_at_sort"], reverse=True)
    return rows


def split_output_folder_name(folder_name: str) -> tuple[str, str]:
    if "__" not in folder_name:
        return folder_name, ""
    source_video, run_tag = folder_name.rsplit("__", 1)
    return source_video, run_tag


def build_score_key(clip: dict[str, Any]) -> str:
    raw = str(clip.get("clip_path") or clip.get("output_file") or clip.get("clip_id") or "")
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def build_group_score_rows(group: dict[str, Any], source_video: str, run_tag: str) -> list[dict[str, Any]]:
    base_key = build_score_key(
        {
            "clip_id": group.get("base_clip_id") or group.get("clip_id"),
            "clip_path": group.get("representative_clip_path") or group.get("representative_output_file"),
        }
    )
    scored_at = group.get("scored_at", "")
    variant_search_text = " ".join(
        str(item.get("clip_id", "")) + " " + str(item.get("output_file", ""))
        for item in group.get("variants", [])
        if isinstance(item, dict)
    )
    base_row = {
        "Source Video": source_video,
        "Run Tag": run_tag,
        "Clip ID": group.get("base_clip_id") or group.get("clip_id", ""),
        "Product": group.get("product", "general") or "general",
        "Total Score": score_float(group.get("total_score")),
        "Content": score_float(group.get("content_score")),
        "Visual": score_float(group.get("visual_score")),
        "Host Focus": score_float(group.get("host_focus_score")),
        "Hook": score_float(group.get("hook_score")),
        "Quality": score_float(group.get("quality_score")),
        "Engagement": score_float(group.get("engagement_score")),
        "Similarity": score_float(group.get("average_similarity_score")),
        "Variants": int(score_float(group.get("variant_count")) or 0),
        "Flags": ", ".join(str(flag) for flag in group.get("flags", [])[:8]),
        "Summary": group.get("summary", ""),
        "Output File": group.get("representative_output_file", ""),
        "Clip Path": group.get("representative_clip_path", ""),
        "Variant Clips": variant_search_text,
        "Exported": bool(group.get("exported", True)),
        "Scored At": scored_at,
        "_scored_at_sort": parse_timestamp(scored_at) or datetime.min,
        "_score_key": base_key,
        "_base_score_key": base_key,
        "_base_clip_id": group.get("base_clip_id") or group.get("clip_id", ""),
        "_row_type": "base",
        "_variant_index": -1,
        "_raw": group,
        "_base_raw": group,
    }
    rows = [base_row]
    variants = group.get("variants", [])
    if not isinstance(variants, list):
        return rows

    for variant in sorted(
        (item for item in variants if isinstance(item, dict)),
        key=lambda item: (
            int(score_float(item.get("variant_index")) or 0),
            str(item.get("variant_id") or ""),
            str(item.get("clip_id") or ""),
        ),
    ):
        variant_scored_at = variant.get("scored_at") or scored_at
        variant_flags = variant.get("flags") or variant.get("similarity_flags", [])
        rows.append(
            {
                "Source Video": source_video,
                "Run Tag": run_tag,
                "Clip ID": variant.get("clip_id", ""),
                "Product": group.get("product", "general") or "general",
                "Total Score": score_float(group.get("total_score")),
                "Content": score_float(group.get("content_score")),
                "Visual": score_float(group.get("visual_score")),
                "Host Focus": score_float(group.get("host_focus_score")),
                "Hook": score_float(group.get("hook_score")),
                "Quality": score_float(group.get("quality_score")),
                "Engagement": score_float(group.get("engagement_score")),
                "Similarity": score_float(variant.get("similarity_score")),
                "Variants": None,
                "Flags": ", ".join(str(flag) for flag in variant_flags[:8]) or "inherits base",
                "Summary": group.get("summary", ""),
                "Output File": variant.get("output_file", ""),
                "Clip Path": variant.get("clip_path", ""),
                "Variant Clips": "",
                "Exported": bool(variant.get("exported", group.get("exported", True))),
                "Scored At": variant_scored_at,
                "_scored_at_sort": parse_timestamp(variant_scored_at) or datetime.min,
                "_score_key": build_score_key(variant),
                "_base_score_key": base_key,
                "_base_clip_id": group.get("base_clip_id") or group.get("clip_id", ""),
                "_row_type": "variant",
                "_variant_index": int(score_float(variant.get("variant_index")) or 0),
                "_raw": variant,
                "_base_raw": group,
            }
        )
    return rows


def synthesize_score_groups_from_clips(clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        base_clip_id = str(clip.get("base_clip_id") or base_clip_id_for_scores_ui(str(clip.get("clip_id") or "")))
        grouped.setdefault(base_clip_id, []).append(clip)

    groups = []
    for base_clip_id, variants in grouped.items():
        representative = sorted(
            variants,
            key=lambda item: (
                0 if "v0" in str(item.get("clip_id") or "").lower() or "original" in str(item.get("clip_id") or "").lower() else 1,
                str(item.get("clip_id") or ""),
            ),
        )[0]
        variant_rows = []
        for variant in variants:
            variant_rows.append(
                {
                    "clip_id": variant.get("clip_id"),
                    "base_clip_id": base_clip_id,
                    "variant_id": variant.get("variant_id") or variant_id_for_scores_ui(str(variant.get("clip_id") or ""), base_clip_id),
                    "variant_index": variant.get("variant_index"),
                    "version_dir": variant.get("version_dir", ""),
                    "output_file": variant.get("output_file", ""),
                    "clip_path": variant.get("clip_path", ""),
                    "similarity_score": variant.get("similarity_score"),
                    "similarity_flags": variant.get("similarity_flags", []),
                    "similarity_metrics": (variant.get("metrics") or {}).get("similarity", {}),
                    "exported": bool(variant.get("exported", True)),
                    "scored_at": variant.get("scored_at", ""),
                }
            )
        groups.append(
            {
                **representative,
                "score_level": "base",
                "clip_id": base_clip_id,
                "base_clip_id": base_clip_id,
                "representative_clip_id": representative.get("clip_id"),
                "representative_output_file": representative.get("output_file", ""),
                "representative_clip_path": representative.get("clip_path", ""),
                "variant_count": len(variant_rows),
                "average_similarity_score": average_score_value(
                    variant.get("similarity_score") for variant in variant_rows
                ),
                "variants": variant_rows,
            }
        )
    return groups


def base_clip_id_for_scores_ui(clip_id: str) -> str:
    match = re_match_score_id(r"^(clip_\d+)(?:_v\d+(?:_|$).*)?$", clip_id)
    if match:
        return match
    match = re_match_score_id(r"^(.+?)_v\d+(?:_|$).*$", clip_id)
    return match or clip_id


def variant_id_for_scores_ui(clip_id: str, base_clip_id: str) -> str:
    if base_clip_id and clip_id.startswith(base_clip_id + "_"):
        return clip_id[len(base_clip_id) + 1 :]
    return "original"


def re_match_score_id(pattern: str, text: str) -> str | None:
    import re

    match = re.match(pattern, str(text or ""), flags=re.IGNORECASE)
    return match.group(1) if match else None


def average_score_value(values: Any) -> float | None:
    numeric = [score_float(value) for value in values]
    numeric = [value for value in numeric if value is not None]
    if not numeric:
        return None
    return round(sum(numeric) / len(numeric), 2)


def score_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def scorer_thresholds() -> tuple[float, float]:
    try:
        import config as cfg

        export_threshold = float(getattr(cfg, "SCORER_EXPORT_READY_THRESHOLD", 7.0) or 7.0)
        review_threshold = float(getattr(cfg, "SCORER_REVIEW_THRESHOLD", 5.0) or 5.0)
        return export_threshold, review_threshold
    except Exception:
        return 7.0, 5.0


def scorer_vision_debug_enabled() -> bool:
    try:
        import config as cfg

        return bool(getattr(cfg, "SCORER_VISION_DEBUG", False))
    except Exception:
        return False


def score_tier_label(value: Any) -> str:
    total = score_float(value)
    export_threshold, review_threshold = scorer_thresholds()
    if total is None:
        return "Rejected"
    if total >= export_threshold:
        return "Export Ready"
    if total >= review_threshold:
        return "Review Needed"
    return "Rejected"


def trend_product_bucket(value: Any) -> str:
    text = str(value or "").lower()
    if "cleanser" in text or "clean" in text:
        return "Cleanser"
    if "serum" in text:
        return "Serum"
    if "toner" in text:
        return "Toner"
    if "eye" in text and "cream" in text:
        return "Eye Cream"
    if "sheet" in text or "mask" in text or "masker" in text:
        return "Sheet Mask"
    if "moist" in text or "cream" in text or "krim" in text:
        return "Moisturizer"
    return ""


def split_flags_for_trends(value: Any) -> list[str]:
    flags = []
    for part in str(value or "").split(","):
        clean = part.strip()
        if clean and clean != "inherits base":
            flags.append(clean)
    return flags


def _output_root_mtime_ns(root: Path) -> int:
    try:
        return root.stat().st_mtime_ns
    except OSError:
        return 0


@st.cache_data(ttl=10, show_spinner=False)
def _discover_score_output_dirs_cached(
    existing_dirs: tuple[str, ...],
    root_path: str,
    root_mtime_ns: int,
) -> tuple[str, ...]:
    output_dirs: list[str] = list(existing_dirs)
    seen = {item.casefold() for item in output_dirs}
    root = Path(root_path)
    if not root.exists():
        return tuple(output_dirs)
    for folder in root.iterdir():
        if not folder.is_dir() or not (folder / "scores_summary.json").exists():
            continue
        normalized = str(folder)
        key = normalized.casefold()
        if key not in seen:
            output_dirs.append(normalized)
            seen.add(key)
    return tuple(output_dirs)


def collect_score_output_dirs(summary: dict[str, Any]) -> tuple[str, ...]:
    output_dirs: list[str] = []
    seen = set()
    for video in summary.get("videos", []):
        for run in video.get("runs", []):
            output_dir = run.get("output_dir")
            if not output_dir:
                continue
            normalized = str(output_dir)
            key = normalized.casefold()
            if key not in seen:
                output_dirs.append(normalized)
                seen.add(key)

    root = resolve_output_root()
    return _discover_score_output_dirs_cached(
        tuple(output_dirs),
        str(root),
        _output_root_mtime_ns(root),
    )


def resolve_output_root() -> Path:
    try:
        import config as cfg

        return Path(getattr(cfg, "OUTPUT_DIR", r"D:\output_clips"))
    except Exception:
        return Path(r"D:\output_clips")


@st.cache_data(ttl=10, show_spinner=False)
def load_focus_debug_rows(output_dirs: tuple[str, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for output_dir in output_dirs:
        folder = Path(output_dir)
        if not folder.exists():
            continue
        source_video, run_tag = split_output_folder_name(folder.name)
        for json_path in sorted(folder.rglob("*_focus_debug.json")):
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, list):
                continue
            image_path = json_path.with_suffix(".jpg")
            clip_id = json_path.name.removesuffix("_focus_debug.json")
            rows.append(
                {
                    "Source Video": source_video,
                    "Run Tag": run_tag,
                    "Clip ID": clip_id,
                    "Image Path": str(image_path),
                    "JSON Path": str(json_path),
                    "Frames": len(payload),
                    "Breakdown": payload,
                    "_sort_key": json_path.stat().st_mtime if json_path.exists() else 0,
                }
            )
    rows.sort(key=lambda item: (item["_sort_key"], item["Clip ID"]), reverse=True)
    return rows


def as_nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def get_ffmpeg_stage(run: dict[str, Any]) -> dict[str, Any]:
    stage_state = (run.get("stages") or {}).get("ffmpeg", {})
    return stage_state if isinstance(stage_state, dict) else {}


def get_live_clip_count(run: dict[str, Any]) -> int:
    return as_nonnegative_int(get_ffmpeg_stage(run).get("clips_created"))


def get_run_clip_count(run: dict[str, Any]) -> int:
    manifest_count = load_manifest_clip_count(run.get("output_dir"))
    live_count = get_live_clip_count(run)
    return max(manifest_count, live_count)


def infer_run_clip_timestamp(run: dict[str, Any]) -> datetime | None:
    completed_at = infer_run_completed_at(run)
    if completed_at:
        return completed_at
    if get_live_clip_count(run) <= 0:
        return None
    return parse_timestamp(get_ffmpeg_stage(run).get("last_progress_at"))


@st.cache_data(ttl=3, show_spinner=False)
def get_gpu_stats() -> dict[str, Any]:
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
    memory_percent = (total_used / total_mem * 100.0) if total_mem else None
    label = (
        f"{rows[0][3]} | {int(total_used)}/{int(total_mem)} MB"
        if len(rows) == 1
        else f"{len(rows)} GPU(s) | {int(total_used)}/{int(total_mem)} MB"
    )
    return {"utilization": avg_util, "memory_percent": memory_percent, "label": label}


def get_system_stats() -> dict[str, Any]:
    cpu_percent = psutil.cpu_percent(interval=0.15)
    ram = psutil.virtual_memory()
    disk_root = Path.cwd().anchor or str(Path.cwd())
    disk = psutil.disk_usage(disk_root)
    gpu = get_gpu_stats()
    free_disk_tb = disk.free / (1024**4)
    return {
        "cpu_percent": cpu_percent,
        "ram_percent": ram.percent,
        "ram_label": f"{ram.used / (1024**3):.1f}/{ram.total / (1024**3):.1f} GB",
        "disk_percent": disk.percent,
        "disk_label": f"{free_disk_tb:.1f} TB free",
        "gpu_percent": gpu["utilization"],
        "gpu_label": gpu["label"],
        "gpu_mem_percent": gpu["memory_percent"],
    }


def infer_video_status(video: dict[str, Any]) -> str:
    status = (video.get("status") or "").lower()
    if status == "completed":
        return "Completed"
    if status == "failed":
        return "Failed"
    if video.get("current_stage") or any(
        stage.get("status") == "running"
        for stage in video.get("stages", {}).values()
    ):
        return "Processing"
    return "Waiting"


def infer_current_step(video: dict[str, Any]) -> str:
    current_stage = video.get("current_stage")
    if current_stage:
        return STAGE_LABELS.get(current_stage, current_stage.title())

    for stage_key, label, _, _ in STAGES:
        stage_state = video.get("stages", {}).get(stage_key, {})
        if stage_state.get("status") == "failed":
            return label
        if stage_state.get("status") not in {"done"}:
            return label

    return "Completed"


def compute_progress(video: dict[str, Any]) -> int:
    stages = video.get("stages", {})
    done = sum(1 for key, _, _, _ in STAGES if stages.get(key, {}).get("status") == "done")
    progress = (done / len(STAGES)) * 100
    status = infer_video_status(video)
    if status == "Processing":
        progress = min(progress + 12.5, 98.0)
    if status == "Completed":
        progress = 100.0
    return int(round(progress))


def build_run_snapshot(video: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": video.get("name", "-"),
        "path": video.get("path"),
        "working_dir": video.get("working_dir"),
        "output_dir": video.get("output_dir"),
        "working_tag": video.get("working_tag"),
        "output_tag": video.get("output_tag"),
        "status": video.get("status"),
        "current_stage": video.get("current_stage"),
        "created_at": video.get("created_at"),
        "completed_at": video.get("completed_at"),
        "failed_at": video.get("failed_at"),
        "stages": video.get("stages", {}),
    }


def collect_video_runs(video: dict[str, Any]) -> list[dict[str, Any]]:
    runs = [run for run in video.get("run_history", []) if isinstance(run, dict)]
    runs.append(build_run_snapshot(video))
    return runs


def aggregate_video_entry(video: dict[str, Any]) -> dict[str, Any]:
    runs = collect_video_runs(video)
    total_clips_all_runs = sum(get_run_clip_count(run) for run in runs)
    aggregate = dict(video)
    aggregate["runs"] = runs
    aggregate["redo_count"] = max(0, len(runs) - 1)
    aggregate["run_count"] = len(runs)
    aggregate["clips_generated_total"] = total_clips_all_runs
    return aggregate


def summarize_state(state: dict[str, Any]) -> dict[str, Any]:
    videos = [aggregate_video_entry(video) for video in state.get("videos", {}).values()]
    now = datetime.now().astimezone()
    fallback_sort_time = datetime(1970, 1, 1, tzinfo=now.tzinfo)

    statuses = Counter(infer_video_status(video) for video in videos)
    stage_running = Counter()
    stage_queued = Counter()
    stage_distribution = Counter()
    queue_items = {stage_key: [] for stage_key, _, _, _ in STAGES}
    throughput_counters = {stage_key: Counter() for stage_key, _, _, _ in STAGES}
    hourly_clips = Counter()
    minute_clip_buckets = Counter()
    hour_clip_buckets = Counter()
    day_clip_buckets = Counter()
    timeline_points: list[dict[str, Any]] = []
    table_rows = []
    top_video_rows = []
    total_clips = 0

    for video in videos:
        created_at = parse_timestamp(video.get("created_at"))
        completed_at = infer_run_completed_at(video)

        stages = video.get("stages", {})
        stage_bucket = video.get("current_stage")
        for stage_key, _, _, _ in STAGES:
            stage_state = stages.get(stage_key, {})
            if stage_state.get("status") == "running":
                stage_running[stage_key] += 1
            if stage_state.get("queued") or stage_state.get("status") == "queued":
                stage_queued[stage_key] += 1
            if stage_state.get("status") in {"queued", "running"}:
                queue_items[stage_key].append(
                    {
                        "name": video.get("name", "-"),
                        "status": stage_state.get("status", "queued").title(),
                    }
                )
            if stage_bucket is None and (
                stage_state.get("status") == "failed" or stage_state.get("status") not in {"done"}
            ):
                stage_bucket = stage_key

        for run in video.get("runs", []):
            run_clip_time = infer_run_clip_timestamp(run)
            run_clips = get_run_clip_count(run)
            if run_clip_time and run_clips:
                bucket = floor_to_hour(run_clip_time)
                timeline_points.append({"timestamp": bucket, "clips": run_clips})
                hourly_clips[run_clip_time.hour] += run_clips
                minute_clip_buckets[floor_to_minute(run_clip_time)] += run_clips
                hour_clip_buckets[bucket] += run_clips
                day_clip_buckets[run_clip_time.date()] += run_clips

            run_stages = run.get("stages", {}) or {}
            for stage_key, _, _, _ in STAGES:
                finished_at = parse_timestamp(run_stages.get(stage_key, {}).get("finished_at"))
                if finished_at:
                    throughput_counters[stage_key][floor_to_hour(finished_at)] += 1

        clips_generated = int(video.get("clips_generated_total", 0))
        total_clips += clips_generated

        duration = "-"
        if created_at:
            end_time = completed_at or now
            duration = format_duration((end_time - created_at).total_seconds())

        if infer_video_status(video) == "Completed":
            stage_bucket = "ffmpeg"
        stage_distribution[stage_bucket or "transcribe"] += 1

        table_rows.append(
            {
                "Video Name": video.get("name", "-"),
                "Status": infer_video_status(video),
                "Current Step": infer_current_step(video),
                "Progress": compute_progress(video),
                "Clips Generated": clips_generated,
                "Runs": int(video.get("run_count", 1)),
                "Redos": int(video.get("redo_count", 0)),
                "Duration": duration,
                "Started At": format_datetime(created_at),
                "Completed At": format_datetime(completed_at),
                "_sort_time": created_at or fallback_sort_time,
            }
        )
        top_video_rows.append(
            {
                "Video": video.get("name", "-"),
                "Clips": clips_generated,
                "Runs": int(video.get("run_count", 1)),
                "Duration": duration,
            }
        )

    if timeline_points:
        timeline_df = (
            pd.DataFrame(timeline_points)
            .groupby("timestamp", as_index=False)["clips"]
            .sum()
            .sort_values("timestamp")
        )
    else:
        timeline_df = pd.DataFrame(columns=["timestamp", "clips"])

    hourly_clips_df = pd.DataFrame(
        [{"hour": hour, "clips": hourly_clips.get(hour, 0)} for hour in range(24)]
    )

    stage_distribution_df = pd.DataFrame(
        [
            {
                "stage": STAGE_LABELS.get(stage_key, stage_key.title()),
                "count": stage_distribution.get(stage_key, 0),
            }
            for stage_key, _, _, _ in STAGES
        ]
    )

    throughput_frames: dict[str, pd.DataFrame] = {}
    for stage_key, label, _, _ in STAGES:
        points = [
            {"timestamp": timestamp, "count": count}
            for timestamp, count in sorted(throughput_counters[stage_key].items())
        ]
        if not points:
            points = [{"timestamp": now.replace(minute=0, second=0, microsecond=0), "count": 0}]
        throughput_frames[stage_key] = pd.DataFrame(points).sort_values("timestamp").tail(16)

    if top_video_rows:
        top_videos_df = (
            pd.DataFrame(top_video_rows)
            .sort_values(["Clips", "Video"], ascending=[False, True])
            .head(5)
            .reset_index(drop=True)
        )
    else:
        top_videos_df = pd.DataFrame(columns=["Video", "Status", "Clips"])

    clips_per_minute = average_completed_bucket(minute_clip_buckets)
    clips_per_hour = average_completed_bucket(hour_clip_buckets)
    clips_per_day = average_completed_bucket(day_clip_buckets)

    table_df = pd.DataFrame(table_rows)
    if not table_df.empty:
        status_rank = {"Processing": 0, "Waiting": 1, "Completed": 2, "Failed": 3}
        table_df["_status_rank"] = table_df["Status"].map(status_rank).fillna(9)
        table_df = (
            table_df.sort_values(
                by=["_status_rank", "_sort_time", "Video Name"],
                ascending=[True, False, True],
            )
            .drop(columns=["_status_rank", "_sort_time"])
            .reset_index(drop=True)
        )

    return {
        "videos": videos,
        "status_counts": statuses,
        "stage_running": stage_running,
        "stage_queued": stage_queued,
        "timeline_df": timeline_df,
        "hourly_clips_df": hourly_clips_df,
        "stage_distribution_df": stage_distribution_df,
        "throughput_frames": throughput_frames,
        "queue_items": queue_items,
        "top_videos_df": top_videos_df,
        "table_df": table_df,
        "total_clips": total_clips,
        "clips_per_minute": clips_per_minute,
        "clips_per_hour": clips_per_hour,
        "clips_per_day": clips_per_day,
    }


def svg_icon(name: str, color: str = "currentColor", size: int = 18, stroke: float = 1.9) -> str:
    paths = {
        "clapboard": (
            "<rect x='3.5' y='7.5' width='17' height='11' rx='2.2'></rect>"
            "<path d='M3.5 7.5h17'></path>"
            "<path d='M6 3.5l2.5 4'></path>"
            "<path d='M10.5 3.5l2.5 4'></path>"
            "<path d='M15 3.5l2.5 4'></path>"
        ),
        "home": (
            "<path d='M4 10.5 12 4l8 6.5'></path>"
            "<path d='M6.5 9.5V20h11V9.5'></path>"
        ),
        "video": (
            "<rect x='3.5' y='6' width='12.5' height='12' rx='2'></rect>"
            "<path d='M16 10l4-2.5v9L16 14'></path>"
        ),
        "chart": (
            "<path d='M5 19V11'></path>"
            "<path d='M10 19V7'></path>"
            "<path d='M15 19V13'></path>"
            "<path d='M20 19V5'></path>"
        ),
        "list": (
            "<path d='M9 7h11'></path>"
            "<path d='M9 12h11'></path>"
            "<path d='M9 17h11'></path>"
            "<circle cx='5.5' cy='7' r='1'></circle>"
            "<circle cx='5.5' cy='12' r='1'></circle>"
            "<circle cx='5.5' cy='17' r='1'></circle>"
        ),
        "gear": (
            "<circle cx='12' cy='12' r='3.2'></circle>"
            "<path d='M12 3.5v2.2'></path>"
            "<path d='M12 18.3v2.2'></path>"
            "<path d='M3.5 12h2.2'></path>"
            "<path d='M18.3 12h2.2'></path>"
            "<path d='M5.9 5.9l1.6 1.6'></path>"
            "<path d='M16.5 16.5l1.6 1.6'></path>"
            "<path d='M18.1 5.9l-1.6 1.6'></path>"
            "<path d='M7.5 16.5l-1.6 1.6'></path>"
        ),
        "grid": (
            "<rect x='4' y='4' width='6' height='6' rx='1.2'></rect>"
            "<rect x='14' y='4' width='6' height='6' rx='1.2'></rect>"
            "<rect x='4' y='14' width='6' height='6' rx='1.2'></rect>"
            "<rect x='14' y='14' width='6' height='6' rx='1.2'></rect>"
        ),
        "refresh": (
            "<path d='M20 7v5h-5'></path>"
            "<path d='M4 17v-5h5'></path>"
            "<path d='M6.5 9a7 7 0 0 1 12-2'></path>"
            "<path d='M17.5 15a7 7 0 0 1-12 2'></path>"
        ),
        "check-circle": (
            "<circle cx='12' cy='12' r='8.5'></circle>"
            "<path d='m8.5 12 2.4 2.5 4.9-5'></path>"
        ),
        "clock": (
            "<circle cx='12' cy='12' r='8.5'></circle>"
            "<path d='M12 7.5v4.8l3 1.9'></path>"
        ),
        "alert-circle": (
            "<circle cx='12' cy='12' r='8.5'></circle>"
            "<path d='M12 7.2v5.4'></path>"
            "<circle cx='12' cy='16.8' r='0.8' fill='currentColor' stroke='none'></circle>"
        ),
        "audio": (
            "<path d='M4.5 12h2'></path>"
            "<path d='M7.5 9v6'></path>"
            "<path d='M11 6v12'></path>"
            "<path d='M14.5 8v8'></path>"
            "<path d='M18 10v4'></path>"
        ),
        "chip": (
            "<rect x='7' y='7' width='10' height='10' rx='2'></rect>"
            "<path d='M9 3.8v2.4'></path>"
            "<path d='M12 3.8v2.4'></path>"
            "<path d='M15 3.8v2.4'></path>"
            "<path d='M9 17.8v2.4'></path>"
            "<path d='M12 17.8v2.4'></path>"
            "<path d='M15 17.8v2.4'></path>"
            "<path d='M3.8 9h2.4'></path>"
            "<path d='M3.8 12h2.4'></path>"
            "<path d='M3.8 15h2.4'></path>"
            "<path d='M17.8 9h2.4'></path>"
            "<path d='M17.8 12h2.4'></path>"
            "<path d='M17.8 15h2.4'></path>"
        ),
        "focus": (
            "<path d='M8 4.5H5.5V7'></path>"
            "<path d='M16 4.5h2.5V7'></path>"
            "<path d='M8 19.5H5.5V17'></path>"
            "<path d='M16 19.5h2.5V17'></path>"
            "<rect x='8' y='8' width='8' height='8' rx='1.5'></rect>"
        ),
        "scissors": (
            "<circle cx='8' cy='8' r='2'></circle>"
            "<circle cx='8' cy='16' r='2'></circle>"
            "<path d='M10 9.5 19 4.5'></path>"
            "<path d='M10 14.5 19 19.5'></path>"
        ),
    }
    inner = paths.get(name, paths["grid"])
    return (
        f"<span class='icon-svg' style='color:{color};'>"
        f"<svg width='{size}' height='{size}' viewBox='0 0 24 24' fill='none' "
        f"stroke='currentColor' stroke-width='{stroke}' stroke-linecap='round' stroke-linejoin='round'>"
        f"{inner}</svg></span>"
    )


def render_kpi_card(title: str, value: int, subtitle: str, icon: str, accent: str) -> None:
    st.markdown(
        f"""
        <div class="kpi-card">
            <div class="kpi-icon" style="color:{accent}; background: color-mix(in srgb, {accent} 16%, transparent);">
                {svg_icon(icon, accent, size=22)}
            </div>
            <div>
                <div class="kpi-label">{title}</div>
                <div class="kpi-value">{value:,}</div>
                <div class="kpi-sub" style="color:{accent};">{subtitle}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_system_metric(label: str, percent: float | None, trailing: str) -> None:
    pct_text = "N/A" if percent is None else f"{percent:.0f}%"
    progress = 0.0 if percent is None else max(0.0, min(percent / 100.0, 1.0))
    st.markdown(
        f"""
        <div class="system-row">
            <div class="system-line">
                <div class="system-left"><div class="system-dot"></div><div>{label}</div></div>
                <div>{trailing if label == 'Disk' else pct_text}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.progress(progress)
    if label != "Disk":
        st.markdown(f"<div class='small-muted'>{trailing}</div>", unsafe_allow_html=True)


def queue_fill_ratio(queued_count: int, running_count: int) -> float:
    active_count = queued_count + running_count
    if active_count <= 0:
        return 0.0
    return max(0.0, min(queued_count / active_count, 1.0))


def render_nav_controls(active_tab: str) -> None:
    items = [
        ("overview", "Overview", "home"),
        ("videos", "Videos", "video"),
        ("analytics", "Analytics", "chart"),
        ("scores", "Scores", "check-circle"),
        ("trends", "Trends", "chart"),
        ("queues", "Queues", "list"),
        ("settings", "Settings", "gear"),
    ]
    if scorer_vision_debug_enabled():
        items.insert(5, ("focus_debug", "Focus Debug", "focus"))
    for tab_key, label, icon_name in items:
        cols = st.columns([0.24, 1], gap="small")
        with cols[0]:
            st.markdown(f"<div class='nav-icon'>{svg_icon(icon_name, '#dbeafe', size=16, stroke=1.8)}</div>", unsafe_allow_html=True)
        with cols[1]:
            if st.button(
                label,
                key=f"nav_{tab_key}",
                use_container_width=True,
                type="primary" if active_tab == tab_key else "secondary",
            ):
                st.session_state.active_tab = tab_key
                st.rerun(scope="fragment")


def render_system_panel(system: dict[str, Any]) -> None:
    st.markdown("<div class='panel-title'>System Status</div>", unsafe_allow_html=True)
    render_system_metric("GPU", system["gpu_percent"], system["gpu_label"])
    render_system_metric("CPU", system["cpu_percent"], f"{system['cpu_percent']:.0f}%")
    render_system_metric("RAM", system["ram_percent"], system["ram_label"])
    render_system_metric("Disk", system["disk_percent"], system["disk_label"])
    st.markdown("<div style='height:0.75rem'></div>", unsafe_allow_html=True)
    st.markdown("<div class='small-muted'>VOD Pipeline v1.0.0</div>", unsafe_allow_html=True)


def render_header(updated_at: datetime | None) -> None:
    header_cols = st.columns([4.2, 1.35], gap="medium")
    with header_cols[0]:
        st.markdown(
            """
            <div class="topbar">
                <div class="topbar-left">
                    <div class="app-icon">"""
            + svg_icon("clapboard", "#edf3ff", size=24, stroke=1.8)
            + """</div>
                    <div>
                        <div class="app-title">VOD Processing Dashboard</div>
                        <div class="app-subtitle">Live monitoring for transcription, LLM, YOLO, and video editing.</div>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with header_cols[1]:
        action_cols = st.columns([2.2, 0.7, 0.7], gap="small")
        with action_cols[0]:
            st.markdown(
                f"""
                <div class="header-actions">
                    <div class="status-pill">
                        <div class="status-dot"></div>
                        <div>Last updated: {format_relative_time(updated_at)}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with action_cols[1]:
            if st.button("\u21bb", key="header_refresh", help="Refresh now", use_container_width=True):
                load_state.clear()
                load_manifest_clip_count.clear()
                load_score_rows.clear()
                load_focus_debug_rows.clear()
                st.rerun()
        with action_cols[2]:
            with st.popover("\u2699", use_container_width=True):
                pending_auto = st.toggle(
                    "Auto refresh",
                    value=st.session_state.auto_refresh_enabled,
                    key="settings_auto_refresh",
                )
                pending_seconds = st.slider(
                    "Refresh every (seconds)",
                    2,
                    5,
                    st.session_state.refresh_seconds_value,
                    key="settings_refresh_seconds",
                )
                pending_state_path = st.text_input(
                    "State JSON path",
                    value=st.session_state.state_path_value,
                    key="settings_state_path",
                )
                if st.button("Apply", key="settings_apply", use_container_width=True):
                    st.session_state.auto_refresh_enabled = pending_auto
                    st.session_state.refresh_seconds_value = pending_seconds
                    st.session_state.state_path_value = pending_state_path
                    load_state.clear()
                    load_manifest_clip_count.clear()
                    load_score_rows.clear()
                    load_focus_debug_rows.clear()
                    st.rerun()


def render_page_intro(title: str, subtitle: str) -> None:
    st.markdown(
        f"""
        <div class="page-title">{html.escape(title)}</div>
        <div class="page-subtitle">{html.escape(subtitle)}</div>
        """,
        unsafe_allow_html=True,
    )


def render_video_table_panel(summary: dict[str, Any], compact: bool = False) -> None:
    table_header_cols = st.columns([1.5, 1.5, 1.05, 1.05, 0.75], gap="small")
    with table_header_cols[0]:
        st.markdown("<div class='panel-title'>Videos</div>", unsafe_allow_html=True)
    with table_header_cols[1]:
        search_term = st.text_input(
            "Search videos",
            placeholder="Search videos...",
            label_visibility="collapsed",
            key=f"search_term_{'compact' if compact else 'full'}",
        )
    with table_header_cols[2]:
        status_filter = st.selectbox(
            "Status filter",
            ["All Statuses", "Processing", "Completed", "Waiting", "Failed"],
            label_visibility="collapsed",
            key=f"status_filter_{'compact' if compact else 'full'}",
        )
    with table_header_cols[3]:
        step_options = ["All Steps"] + [label for _, label, _, _ in STAGES] + ["Completed"]
        step_filter = st.selectbox(
            "Step filter",
            step_options,
            label_visibility="collapsed",
            key=f"step_filter_{'compact' if compact else 'full'}",
        )
    with table_header_cols[4]:
        if st.button("Refresh", key=f"table_refresh_{'compact' if compact else 'full'}", use_container_width=True):
            load_state.clear()
            load_manifest_clip_count.clear()
            st.rerun()

    table_df = summary["table_df"].copy()
    if search_term:
        table_df = table_df[table_df["Video Name"].str.contains(search_term, case=False, na=False)]
    if status_filter != "All Statuses":
        table_df = table_df[table_df["Status"] == status_filter]
    if step_filter != "All Steps":
        table_df = table_df[table_df["Current Step"] == step_filter]

    if table_df.empty:
        st.info("No videos match the current filters.")
        return

    page_size_col, info_col, pager_col = st.columns([0.9, 2.5, 2], gap="small")
    default_index = 0 if compact else 1
    with page_size_col:
        page_size = st.selectbox(
            "Rows",
            [7, 10, 20, 50],
            index=default_index,
            key=f"page_size_{'compact' if compact else 'full'}",
            label_visibility="collapsed",
        )

    total_rows = len(table_df)
    total_pages = max((total_rows - 1) // page_size + 1, 1)
    page_key = f"table_page_{'compact' if compact else 'full'}"
    current_page = min(max(st.session_state.get(page_key, 1), 1), total_pages)
    st.session_state[page_key] = current_page

    start_idx = (current_page - 1) * page_size
    end_idx = min(start_idx + page_size, total_rows)
    page_df = table_df.iloc[start_idx:end_idx]

    render_html_table(page_df)

    with info_col:
        st.caption(f"Showing {start_idx + 1} to {end_idx} of {total_rows} videos")

    with pager_col:
        pager = st.columns([0.7, 2.5, 0.7, 1.2], gap="small")
        with pager[0]:
            if st.button("\u2039", key=f"page_prev_{'compact' if compact else 'full'}", use_container_width=True, disabled=current_page <= 1):
                st.session_state[page_key] = max(1, current_page - 1)
                st.rerun(scope="fragment")
        with pager[1]:
            if total_pages <= 5:
                page_numbers = list(range(1, total_pages + 1))
            else:
                start_page = max(1, current_page - 1)
                end_page = min(total_pages, start_page + 2)
                if end_page - start_page < 2:
                    start_page = max(1, end_page - 2)
                page_numbers = list(range(start_page, end_page + 1))
            num_cols = st.columns(len(page_numbers), gap="small")
            for col, page_num in zip(num_cols, page_numbers):
                with col:
                    if st.button(
                        str(page_num),
                        key=f"page_{page_num}_{'compact' if compact else 'full'}",
                        use_container_width=True,
                        type="primary" if page_num == current_page else "secondary",
                    ):
                        st.session_state[page_key] = page_num
                        st.rerun(scope="fragment")
        with pager[2]:
            if st.button("\u203a", key=f"page_next_{'compact' if compact else 'full'}", use_container_width=True, disabled=current_page >= total_pages):
                st.session_state[page_key] = min(total_pages, current_page + 1)
                st.rerun(scope="fragment")
        with pager[3]:
            st.caption(f"{page_size} / page")


def render_overview_tab(summary: dict[str, Any]) -> None:
    render_page_intro("Overview", "Live operational view across the whole VOD pipeline.")
    kpi_cols = st.columns(5, gap="medium")
    kpis = [
        ("Total Videos", len(summary["videos"]), "All time", "grid", "#8b5cf6"),
        ("Videos Processing", summary["status_counts"].get("Processing", 0), "In progress", "refresh", "#3b82f6"),
        ("Videos Completed", summary["status_counts"].get("Completed", 0), "Completed", "check-circle", "#22c55e"),
        ("Videos Waiting", summary["status_counts"].get("Waiting", 0), "In queue", "clock", "#fbbf24"),
        ("Videos Failed", summary["status_counts"].get("Failed", 0), "Failed", "alert-circle", "#ef4444"),
    ]
    for col, (title, value, subtitle, icon, accent) in zip(kpi_cols, kpis):
        with col:
            render_kpi_card(title, value, subtitle, icon, accent)

    st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)

    center_cols = st.columns([1.05, 1.18], gap="medium")
    with center_cols[0]:
        with st.container(border=True):
            st.markdown("<div class='panel-title'>Pipeline Status</div>", unsafe_allow_html=True)
            stage_cols = st.columns([1, 0.2, 1, 0.2, 1, 0.2, 1], gap="small")
            for index, (stage_key, label, icon, accent) in enumerate(STAGES):
                target_col = index * 2
                with stage_cols[target_col]:
                    render_stage_card(label, summary["stage_running"].get(stage_key, 0), icon, accent)
                if index < len(STAGES) - 1:
                    with stage_cols[target_col + 1]:
                        st.markdown("<div class='stage-arrow'>&rarr;</div>", unsafe_allow_html=True)

            st.markdown("<div style='height:0.8rem'></div>", unsafe_allow_html=True)
            st.divider()
            st.markdown("<div class='panel-title' style='font-size:1rem;'>Queues</div>", unsafe_allow_html=True)
            for stage_key, label, _, _ in STAGES:
                queued_count = summary["stage_queued"].get(stage_key, 0)
                running_count = summary["stage_running"].get(stage_key, 0)
                active_count = queued_count + running_count
                q_cols = st.columns([1.15, 2.4, 0.55], gap="small")
                with q_cols[0]:
                    st.markdown(f"<div class='queue-label'>{label.replace(' Processing', '')} Queue</div>", unsafe_allow_html=True)
                with q_cols[1]:
                    st.progress(queue_fill_ratio(queued_count, running_count))
                with q_cols[2]:
                    st.markdown(f"<div class='queue-count'>{queued_count} / {active_count}</div>", unsafe_allow_html=True)

    with center_cols[1]:
        with st.container(border=True):
            chart_controls = st.columns([2.6, 1], gap="small")
            with chart_controls[0]:
                st.markdown("<div class='panel-title'>Clips Generated</div>", unsafe_allow_html=True)
            with chart_controls[1]:
                window_label = st.selectbox(
                    "Window",
                    list(chart_window_options().keys()),
                    index=1,
                    label_visibility="collapsed",
                    key="overview_window_label",
                )

            stat_grid_cols = st.columns(4, gap="medium")
            stat_items = [
                ("Total Clips", f"{summary['total_clips']:,}", "All time"),
                ("Clips / Day (avg)", f"{summary['clips_per_day']:.1f}", "Average"),
                ("Clips / Hour (avg)", f"{summary['clips_per_hour']:.2f}", "Average"),
                ("Clips / Minute (avg)", f"{summary['clips_per_minute']:.2f}", "Average"),
            ]
            for col, (label, value, sub) in zip(stat_grid_cols, stat_items):
                with col:
                    st.markdown(
                        f"""
                        <div class="mini-stat">
                            <div class="mini-stat-label">{label}</div>
                            <div class="mini-stat-value">{value}</div>
                            <div class="mini-stat-sub">{sub}</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

            timeline_df = summary["timeline_df"].copy()
            window_delta = chart_window_options()[window_label]
            if window_delta is not None and not timeline_df.empty:
                cutoff = datetime.now().astimezone() - window_delta
                timeline_df = timeline_df[timeline_df["timestamp"] >= cutoff]

            if timeline_df.empty:
                st.info("No completed clip history yet.")
            else:
                line_chart = (
                    alt.Chart(timeline_df)
                    .mark_line(strokeWidth=3, color="#3b82f6", point=alt.OverlayMarkDef(color="#60a5fa", filled=True, size=64))
                    .encode(
                        x=alt.X("timestamp:T", title=None, axis=alt.Axis(labelColor="#94a3b8", grid=False)),
                        y=alt.Y("clips:Q", title=None, axis=alt.Axis(labelColor="#94a3b8", gridColor="rgba(148,163,184,0.12)")),
                        tooltip=[alt.Tooltip("timestamp:T", title="Time"), alt.Tooltip("clips:Q", title="Clips")],
                    )
                    .properties(height=300)
                    .configure_view(strokeOpacity=0)
                    .configure(background="transparent")
                )
                st.altair_chart(line_chart, use_container_width=True)

    with st.container(border=True):
        render_video_table_panel(summary, compact=True)


def render_videos_tab(summary: dict[str, Any]) -> None:
    render_page_intro("Videos", "Search, filter, and inspect the current video processing catalog.")
    with st.container(border=True):
        render_video_table_panel(summary, compact=False)


def render_analytics_tab(summary: dict[str, Any]) -> None:
    render_page_intro("Analytics", "Performance trends, clip output, and stage distribution over time.")
    top_cols = st.columns(5, gap="medium")
    analytics_items = [
        ("Total Clips", f"{summary['total_clips']:,}", "All time"),
        ("Clips / Day (avg)", f"{summary['clips_per_day']:.1f}", "Average"),
        ("Clips / Hour (avg)", f"{summary['clips_per_hour']:.2f}", "Average"),
        ("Clips / Minute (avg)", f"{summary['clips_per_minute']:.2f}", "Average"),
        ("Total Videos", f"{len(summary['videos']):,}", "Tracked"),
    ]
    for col, (label, value, sub) in zip(top_cols, analytics_items):
        with col:
            with st.container(border=True):
                st.markdown(
                    f"""
                    <div class="mini-stat">
                        <div class="mini-stat-label">{label}</div>
                        <div class="mini-stat-value">{value}</div>
                        <div class="mini-stat-sub">{sub}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    chart_cols = st.columns([1.45, 0.9], gap="medium")
    with chart_cols[0]:
        with st.container(border=True):
            st.markdown("<div class='panel-title'>Clips Generated Over Time</div>", unsafe_allow_html=True)
            line_chart = (
                alt.Chart(summary["timeline_df"])
                .mark_line(strokeWidth=3, color="#3b82f6", point=alt.OverlayMarkDef(color="#60a5fa", filled=True, size=64))
                .encode(
                    x=alt.X("timestamp:T", title=None, axis=alt.Axis(labelColor="#94a3b8", grid=False)),
                    y=alt.Y("clips:Q", title=None, axis=alt.Axis(labelColor="#94a3b8", gridColor="rgba(148,163,184,0.12)")),
                    tooltip=[alt.Tooltip("timestamp:T", title="Time"), alt.Tooltip("clips:Q", title="Clips")],
                )
                .properties(height=300)
                .configure_view(strokeOpacity=0)
                .configure(background="transparent")
            )
            st.altair_chart(line_chart, use_container_width=True)

    with chart_cols[1]:
        with st.container(border=True):
            st.markdown("<div class='panel-title'>Clips by Processing Stage</div>", unsafe_allow_html=True)
            donut = (
                alt.Chart(summary["stage_distribution_df"])
                .mark_arc(innerRadius=55, outerRadius=95)
                .encode(
                    theta=alt.Theta("count:Q"),
                    color=alt.Color(
                        "stage:N",
                        scale=alt.Scale(
                            domain=[label for _, label, _, _ in STAGES],
                            range=[accent for _, _, _, accent in STAGES],
                        ),
                        legend=alt.Legend(labelColor="#cbd5e1", titleColor="#94a3b8"),
                    ),
                    tooltip=[alt.Tooltip("stage:N", title="Stage"), alt.Tooltip("count:Q", title="Videos")],
                )
                .properties(height=300)
                .configure_view(strokeOpacity=0)
                .configure(background="transparent")
            )
            st.altair_chart(donut, use_container_width=True)

    bottom_cols = st.columns([1.3, 0.9], gap="medium")
    with bottom_cols[0]:
        with st.container(border=True):
            st.markdown("<div class='panel-title'>Clips Generated by Hour of Day (avg)</div>", unsafe_allow_html=True)
            bar = (
                alt.Chart(summary["hourly_clips_df"])
                .mark_bar(color="#3b82f6", cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
                .encode(
                    x=alt.X("hour:O", title=None, axis=alt.Axis(labelColor="#94a3b8")),
                    y=alt.Y("clips:Q", title=None, axis=alt.Axis(labelColor="#94a3b8", gridColor="rgba(148,163,184,0.12)")),
                    tooltip=[alt.Tooltip("hour:O", title="Hour"), alt.Tooltip("clips:Q", title="Clips")],
                )
                .properties(height=280)
                .configure_view(strokeOpacity=0)
                .configure(background="transparent")
            )
            st.altair_chart(bar, use_container_width=True)

    with bottom_cols[1]:
        with st.container(border=True):
            st.markdown("<div class='panel-title'>Top Videos by Clips Generated</div>", unsafe_allow_html=True)
            st.dataframe(summary["top_videos_df"], use_container_width=True, hide_index=True)


def render_scores_tab(summary: dict[str, Any]) -> None:
    render_page_intro("Scores", "Ranked post-render quality scores for finished clips.")
    score_rows = load_score_rows(collect_score_output_dirs(summary))
    if not score_rows:
        with st.container(border=True):
            st.info("No scores found yet. New render batches will write scores_summary.json automatically.")
        return

    score_df = pd.DataFrame(score_rows)
    base_score_df = score_df[score_df["_row_type"] == "base"].copy()
    if base_score_df.empty:
        base_score_df = score_df.copy()
    products = ["All Products"] + sorted(
        product for product in base_score_df["Product"].dropna().astype(str).unique() if product
    )

    controls = st.columns([1.0, 1.25, 0.75, 0.95, 0.65, 0.65], gap="small")
    with controls[0]:
        product_filter = st.selectbox("Product", products, key="scores_product_filter")
    with controls[1]:
        search_term = st.text_input("Search", placeholder="Clip, source, flag...", key="scores_search")
    with controls[2]:
        min_score = st.slider("Min Score", 0.0, 10.0, 0.0, 0.1, key="scores_min_score")
    with controls[3]:
        sort_by = st.selectbox(
            "Sort",
            [
                "Scored At",
                "Total Score",
                "Host Focus",
                "Content",
                "Visual",
                "Quality",
                "Engagement",
                "Hook",
                "Similarity",
                "Variants",
                "Clip ID",
                "Product",
            ],
            key="scores_sort_by",
        )
    with controls[4]:
        descending = st.toggle("Desc", value=True, key="scores_sort_desc")
    with controls[5]:
        if st.button("Refresh", key="scores_refresh", use_container_width=True):
            load_score_rows.clear()
            st.rerun(scope="fragment")

    filtered = base_score_df.copy()
    if product_filter != "All Products":
        filtered = filtered[filtered["Product"] == product_filter]
    filtered = filtered[filtered["Total Score"].fillna(-1) >= min_score]
    if search_term:
        haystack_cols = ["Source Video", "Run Tag", "Clip ID", "Flags", "Summary", "Output File", "Variant Clips"]
        mask = pd.Series(False, index=filtered.index)
        for col in haystack_cols:
            mask = mask | filtered[col].astype(str).str.contains(search_term, case=False, na=False)
        filtered = filtered[mask]

    sort_column = "_scored_at_sort" if sort_by == "Scored At" else sort_by
    filtered = filtered.sort_values(sort_column, ascending=not descending, na_position="last").reset_index(drop=True)
    if filtered.empty:
        with st.container(border=True):
            st.info("No scored clips match the current filters.")
        return

    expanded_base_key = str(st.session_state.get("expanded_score_base_key", ""))
    visible_base_keys = set(filtered["_base_score_key"].astype(str))
    if expanded_base_key and expanded_base_key not in visible_base_keys:
        expanded_base_key = ""
        st.session_state.expanded_score_base_key = ""

    selected_key = str(st.session_state.get("selected_score_key", ""))
    if selected_key and selected_key in set(score_df["_score_key"].astype(str)):
        selected = score_df[score_df["_score_key"].astype(str) == selected_key].iloc[0]
    else:
        selected = filtered.iloc[0]
        selected_key = str(selected.get("_score_key", ""))
        st.session_state.selected_score_key = selected_key

    metric_cols = st.columns(4, gap="medium")
    average_score = filtered["Total Score"].dropna().mean()
    if pd.isna(average_score):
        average_score = 0.0
    high_count = int((filtered["Total Score"] >= 7).sum())
    low_count = int((filtered["Total Score"] < 4).sum())
    metric_items = [
        ("Base Clips", f"{len(filtered):,}", "Filtered"),
        ("Average", f"{average_score:.2f}", "Total score"),
        ("7+", f"{high_count:,}", "Strong"),
        ("<4", f"{low_count:,}", "Needs review"),
    ]
    for col, (label, value, sub) in zip(metric_cols, metric_items):
        with col:
            st.markdown(
                f"""
                <div class="mini-stat">
                    <div class="mini-stat-label">{label}</div>
                    <div class="mini-stat-value">{value}</div>
                    <div class="mini-stat-sub">{sub}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    selected_key = render_scores_click_table(filtered, score_df, selected_key)
    if selected_key and selected_key in set(score_df["_score_key"].astype(str)):
        selected = score_df[score_df["_score_key"].astype(str) == selected_key].iloc[0]
    render_score_detail(selected)
    render_score_methodology()


def render_trends_tab(summary: dict[str, Any]) -> None:
    render_page_intro("Trends", "Score trends by product, dimension, tier, and recurring flags.")
    score_rows = load_score_rows(collect_score_output_dirs(summary))
    if not score_rows:
        with st.container(border=True):
            st.info("No score trend data found yet.")
        return

    score_df = pd.DataFrame(score_rows)
    clip_df = score_df[score_df["_row_type"].isin(["base", "variant"])].copy()
    if clip_df.empty:
        clip_df = score_df.copy()

    clip_df["Product Bucket"] = clip_df["Product"].apply(trend_product_bucket)
    product_rows = []
    for product in TREND_PRODUCTS:
        values = clip_df.loc[clip_df["Product Bucket"] == product, "Total Score"].dropna()
        product_rows.append(
            {
                "Product": product,
                "Average Total": round(float(values.mean()), 2) if not values.empty else None,
                "Clips": int(values.count()),
            }
        )
    product_df = pd.DataFrame(product_rows)

    dimension_rows = []
    for label, column in [
        ("Content", "Content"),
        ("Visual", "Visual"),
        ("Quality", "Quality"),
        ("Engagement", "Engagement"),
    ]:
        values = clip_df[column].dropna()
        dimension_rows.append(
            {
                "Dimension": label,
                "Average Score": round(float(values.mean()), 2) if not values.empty else None,
            }
        )
    dimension_df = pd.DataFrame(dimension_rows)

    tier_order = ["Export Ready", "Review Needed", "Rejected"]
    tier_counts = Counter(score_tier_label(value) for value in clip_df["Total Score"])
    tier_df = pd.DataFrame(
        [{"Tier": tier, "Clips": int(tier_counts.get(tier, 0))} for tier in tier_order]
    )

    flag_counter: Counter[str] = Counter()
    for value in clip_df["Flags"]:
        flag_counter.update(split_flags_for_trends(value))
    flags_df = pd.DataFrame(
        [{"Flag": flag, "Count": count} for flag, count in flag_counter.most_common(10)]
    )

    top_df = (
        clip_df.sort_values("Total Score", ascending=False, na_position="last")
        .head(3)[["Clip ID", "Product", "Total Score", "Summary"]]
        .reset_index(drop=True)
    )

    chart_cols = st.columns([1, 1], gap="medium")
    with chart_cols[0]:
        with st.container(border=True):
            st.markdown("<div class='panel-title'>Average Total by Product</div>", unsafe_allow_html=True)
            chart = (
                alt.Chart(product_df)
                .mark_bar(color="#22c55e", cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
                .encode(
                    x=alt.X("Product:N", sort=TREND_PRODUCTS, title=None, axis=alt.Axis(labelColor="#94a3b8")),
                    y=alt.Y("Average Total:Q", title=None, scale=alt.Scale(domain=[0, 10]), axis=alt.Axis(labelColor="#94a3b8", gridColor="rgba(148,163,184,0.12)")),
                    tooltip=["Product", "Average Total", "Clips"],
                )
            )
            st.altair_chart(chart, use_container_width=True)

    with chart_cols[1]:
        with st.container(border=True):
            st.markdown("<div class='panel-title'>Average by Dimension</div>", unsafe_allow_html=True)
            chart = (
                alt.Chart(dimension_df)
                .mark_bar(color="#3b82f6", cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
                .encode(
                    x=alt.X("Dimension:N", title=None, axis=alt.Axis(labelColor="#94a3b8")),
                    y=alt.Y("Average Score:Q", title=None, scale=alt.Scale(domain=[0, 10]), axis=alt.Axis(labelColor="#94a3b8", gridColor="rgba(148,163,184,0.12)")),
                    tooltip=["Dimension", "Average Score"],
                )
            )
            st.altair_chart(chart, use_container_width=True)

    lower_cols = st.columns([0.8, 1.2], gap="medium")
    with lower_cols[0]:
        with st.container(border=True):
            st.markdown("<div class='panel-title'>Score Tiers</div>", unsafe_allow_html=True)
            chart = (
                alt.Chart(tier_df)
                .mark_bar(color="#fbbf24", cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
                .encode(
                    x=alt.X("Tier:N", sort=tier_order, title=None, axis=alt.Axis(labelColor="#94a3b8")),
                    y=alt.Y("Clips:Q", title=None, axis=alt.Axis(labelColor="#94a3b8", gridColor="rgba(148,163,184,0.12)")),
                    tooltip=["Tier", "Clips"],
                )
            )
            st.altair_chart(chart, use_container_width=True)

    with lower_cols[1]:
        with st.container(border=True):
            st.markdown("<div class='panel-title'>Most Common Flags</div>", unsafe_allow_html=True)
            if flags_df.empty:
                st.info("No flags found.")
            else:
                chart = (
                    alt.Chart(flags_df)
                    .mark_bar(color="#8b5cf6", cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
                    .encode(
                        x=alt.X("Count:Q", title=None, axis=alt.Axis(labelColor="#94a3b8", gridColor="rgba(148,163,184,0.12)")),
                        y=alt.Y("Flag:N", sort="-x", title=None, axis=alt.Axis(labelColor="#94a3b8")),
                        tooltip=["Flag", "Count"],
                    )
                )
                st.altair_chart(chart, use_container_width=True)

    with st.container(border=True):
        st.markdown("<div class='panel-title'>Top 3 Clips Overall</div>", unsafe_allow_html=True)
        st.dataframe(top_df, use_container_width=True, hide_index=True)


def render_focus_debug_tab(summary: dict[str, Any]) -> None:
    render_page_intro("Focus Debug", "Sampled host-focus frames and A/B/C classifications.")
    if not scorer_vision_debug_enabled():
        with st.container(border=True):
            st.info("Focus debug is disabled. Set SCORER_VISION_DEBUG = True to collect contact sheets.")
        return

    rows = load_focus_debug_rows(collect_score_output_dirs(summary))
    if not rows:
        with st.container(border=True):
            st.info("No focus debug artifacts found yet.")
        return

    labels = [
        f"{row['Clip ID']} | {row['Source Video']} {row['Run Tag']}".strip()
        for row in rows
    ]
    selected_label = st.selectbox("Clip", labels, key="focus_debug_clip")
    selected_index = labels.index(selected_label)
    selected = rows[selected_index]

    with st.container(border=True):
        st.markdown("<div class='panel-title'>Contact Sheet</div>", unsafe_allow_html=True)
        image_path = Path(selected["Image Path"])
        if image_path.exists():
            st.image(str(image_path), use_container_width=True)
        else:
            st.warning(f"Missing contact sheet: {image_path}")

    breakdown = selected.get("Breakdown", [])
    if isinstance(breakdown, list) and breakdown:
        frame = pd.DataFrame(breakdown)
        visible_cols = [
            col
            for col in [
                "frame_index",
                "timestamp_seconds",
                "label",
                "confidence",
                "outlier_dropped",
                "raw_response",
            ]
            if col in frame.columns
        ]
        with st.container(border=True):
            st.markdown("<div class='panel-title'>Per-Frame Breakdown</div>", unsafe_allow_html=True)
            st.dataframe(frame[visible_cols], use_container_width=True, hide_index=True)


def expand_score_group_rows(base_rows: pd.DataFrame, all_rows: pd.DataFrame) -> pd.DataFrame:
    expanded = []
    for _, base_row in base_rows.iterrows():
        base_key = str(base_row.get("_base_score_key", base_row.get("_score_key", "")))
        expanded.append(base_row)
        variants = all_rows[
            (all_rows["_base_score_key"].astype(str) == base_key)
            & (all_rows["_row_type"].astype(str) == "variant")
        ].sort_values(["_variant_index", "Clip ID"], na_position="last")
        for _, variant_row in variants.iterrows():
            expanded.append(variant_row)
    return pd.DataFrame(expanded).reset_index(drop=True)


def render_scores_click_table(filtered: pd.DataFrame, all_rows: pd.DataFrame, selected_key: str) -> str:
    page_size = st.selectbox("Base Rows", [10, 20, 50], index=1, key="scores_rows")
    total_rows = len(filtered)
    total_pages = max((total_rows - 1) // page_size + 1, 1)
    page = min(max(int(st.session_state.get("scores_page", 1)), 1), total_pages)
    st.session_state.scores_page = page

    start_idx = (page - 1) * page_size
    end_idx = min(start_idx + page_size, total_rows)
    page_df = filtered.iloc[start_idx:end_idx]
    selected_key = render_scores_compact_table(page_df, all_rows, selected_key)

    pager_cols = st.columns([0.55, 0.85, 0.55, 3.0], gap="small")
    with pager_cols[0]:
        if st.button("\u2039", key="scores_prev", use_container_width=True, disabled=page <= 1):
            st.session_state.scores_page = max(1, page - 1)
            st.rerun(scope="fragment")
    with pager_cols[1]:
        st.caption(f"Page {page} / {total_pages}")
    with pager_cols[2]:
        if st.button("\u203a", key="scores_next", use_container_width=True, disabled=page >= total_pages):
            st.session_state.scores_page = min(total_pages, page + 1)
            st.rerun(scope="fragment")
    with pager_cols[3]:
        st.caption(f"Showing base clips {start_idx + 1} to {end_idx} of {total_rows}")
    return selected_key


def render_scores_compact_table(page_df: pd.DataFrame, all_rows: pd.DataFrame, selected_key: str) -> str:
    st.markdown(
        """
        <style>
        .score-grid-header {
            display: grid;
            grid-template-columns: 0.24fr 1.12fr 0.55fr 1.28fr 0.8fr 0.42fr 0.42fr 0.42fr 0.58fr 0.42fr 0.42fr 0.52fr 0.68fr 0.4fr 1.55fr;
            gap: 0.42rem;
            align-items: center;
            color: #94a3b8;
            font-size: 0.76rem;
            font-weight: 700;
            padding: 0.22rem 0.4rem;
            border-bottom: 1px solid rgba(148, 163, 184, 0.14);
        }
        .score-accordion-shell {
            border: 1px solid rgba(148, 163, 184, 0.16);
            border-radius: 8px;
            overflow: hidden;
            background: rgba(2, 8, 23, 0.22);
        }
        .score-cell {
            font-size: 0.78rem;
            line-height: 1.1;
            max-height: 2.2rem;
            overflow: hidden;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            word-break: break-word;
            padding-top: 0.2rem;
        }
        .score-variant-panel {
            margin: 0.12rem 1.1rem 0.6rem 2.65rem;
            border-left: 2px solid rgba(96, 165, 250, 0.35);
            padding-left: 1rem;
        }
        .score-variant-card {
            border: 1px solid rgba(148, 163, 184, 0.12);
            border-radius: 8px;
            overflow: hidden;
            background: rgba(15, 23, 42, 0.45);
        }
        .score-variant-title {
            color: #e2e8f0;
            font-size: 0.82rem;
            font-weight: 700;
            padding: 0.62rem 0.86rem;
            border-bottom: 1px solid rgba(148, 163, 184, 0.10);
        }
        .score-sim-wrap {
            display: flex;
            align-items: center;
            gap: 0.48rem;
            min-width: 0;
        }
        .score-sim-value {
            min-width: 2.2rem;
            color: #e2e8f0;
            font-variant-numeric: tabular-nums;
        }
        .score-sim-track {
            height: 0.28rem;
            width: 5.4rem;
            border-radius: 999px;
            background: rgba(148, 163, 184, 0.18);
            overflow: hidden;
        }
        .score-sim-fill {
            height: 100%;
            border-radius: 999px;
            background: #60a5fa;
        }
        .score-selected-strip {
            height: 2px;
            background: rgba(96, 165, 250, 0.85);
            border-radius: 999px;
            margin: 0.08rem 0;
        }
        div[data-testid="stButton"] > button {
            min-height: 2.05rem;
            max-height: 2.35rem;
            padding: 0.1rem 0.35rem;
            font-size: 0.77rem;
            line-height: 1.05;
            overflow: hidden;
        }
        </style>
        <div class="score-accordion-shell">
        <div class="score-grid-header">
            <div></div>
            <div>Source Video</div>
            <div>Run Tag</div>
            <div>Clip ID</div>
            <div>Type</div>
            <div>Total</div>
            <div>Content</div>
            <div>Visual</div>
            <div>Host Focus</div>
            <div>Hook</div>
            <div>Quality</div>
            <div>Engage</div>
            <div>Similarity</div>
            <div>Vars</div>
            <div>Flags</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    for row_index, row in page_df.iterrows():
        key = str(row.get("_score_key", ""))
        base_key = str(row.get("_base_score_key", key))
        selected = key == selected_key
        expanded_key = str(st.session_state.get("expanded_score_base_key", ""))
        expanded = expanded_key == base_key
        if selected:
            st.markdown("<div class='score-selected-strip'></div>", unsafe_allow_html=True)

        cols = st.columns([0.24, 1.12, 0.55, 1.28, 0.8, 0.42, 0.42, 0.42, 0.58, 0.42, 0.42, 0.52, 0.68, 0.4, 1.55], gap="small")
        variant_count = row.get("Variants")
        cells = [
            str(row.get("Source Video", "")),
            str(row.get("Run Tag", "")) or "-",
            str(row.get("Product", "")),
            score_format(row.get("Total Score")),
            score_format(row.get("Content")),
            score_format(row.get("Visual")),
            score_format(row.get("Host Focus")),
            score_format(row.get("Hook")),
            score_format(row.get("Quality")),
            score_format(row.get("Engagement")),
            score_format(row.get("Similarity")),
            str(int(score_float(variant_count) or 0)),
            str(row.get("Flags", "")) or "-",
        ]

        with cols[0]:
            if st.button(
                "v" if expanded else ">",
                key=f"score_expand_{base_key}_{row_index}",
                help="Show variants",
                use_container_width=True,
            ):
                st.session_state.expanded_score_base_key = "" if expanded else base_key
                expanded = not expanded
        with cols[1]:
            st.markdown(f"<div class='score-cell'>{html.escape(cells[0])}</div>", unsafe_allow_html=True)
        with cols[2]:
            st.markdown(f"<div class='score-cell'>{html.escape(cells[1])}</div>", unsafe_allow_html=True)
        with cols[3]:
            button_label = str(row.get("Clip ID", ""))
            if st.button(
                button_label,
                key=f"score_clip_button_{key}_{row_index}",
                help="Show this clip's score breakdown below",
                use_container_width=True,
                type="primary" if selected else "secondary",
            ):
                selected_key = key
                st.session_state.selected_score_key = key
        for col, value in zip(cols[4:12], cells[2:10]):
            with col:
                st.markdown(f"<div class='score-cell'>{html.escape(value)}</div>", unsafe_allow_html=True)
        with cols[12]:
            st.markdown(render_similarity_bar(row.get("Similarity")), unsafe_allow_html=True)
        with cols[13]:
            st.markdown(f"<div class='score-cell'>{html.escape(cells[11])}</div>", unsafe_allow_html=True)
        with cols[14]:
            st.markdown(f"<div class='score-cell'>{html.escape(cells[12])}</div>", unsafe_allow_html=True)

        if expanded:
            variants = variants_for_score_base(all_rows, base_key)
            selected_key = render_score_variants_panel(variants, base_key, selected_key)

    st.markdown("</div>", unsafe_allow_html=True)
    return selected_key


def variants_for_score_base(all_rows: pd.DataFrame, base_key: str) -> pd.DataFrame:
    if all_rows.empty:
        return all_rows
    return (
        all_rows[
            (all_rows["_base_score_key"].astype(str) == str(base_key))
            & (all_rows["_row_type"].astype(str) == "variant")
        ]
        .sort_values(["_variant_index", "Clip ID"], na_position="last")
        .reset_index(drop=True)
    )


def render_score_variants_panel(variants: pd.DataFrame, base_key: str, selected_key: str) -> str:
    indent_cols = st.columns([0.35, 11.65], gap="small")
    with indent_cols[1]:
        with st.container(border=True):
            st.markdown(f"**Variants ({len(variants)})**")
            header = st.columns([2.45, 0.7, 1.35, 2.1], gap="small")
            for col, label in zip(header, ["Variant Clip ID", "Type", "Similarity", "Flags"]):
                with col:
                    st.markdown(f"<div class='score-grid-header' style='display:block; padding-left:0;'>{label}</div>", unsafe_allow_html=True)

            if variants.empty:
                st.caption("No variants found for this clip.")
                return selected_key

            for row_index, row in variants.iterrows():
                key = str(row.get("_score_key", ""))
                selected = key == selected_key
                if selected:
                    st.markdown("<div class='score-selected-strip'></div>", unsafe_allow_html=True)
                cols = st.columns([2.45, 0.7, 1.35, 2.1], gap="small")
                with cols[0]:
                    if st.button(
                        str(row.get("Clip ID", "")),
                        key=f"score_variant_clip_button_{base_key}_{key}_{row_index}",
                        help="Show this variant's score breakdown below",
                        use_container_width=True,
                        type="primary" if selected else "secondary",
                    ):
                        selected_key = key
                        st.session_state.selected_score_key = key
                with cols[1]:
                    st.markdown("<div class='score-cell'>variant</div>", unsafe_allow_html=True)
                with cols[2]:
                    st.markdown(render_similarity_bar(row.get("Similarity")), unsafe_allow_html=True)
                with cols[3]:
                    flags = str(row.get("Flags", "") or "inherits base")
                    st.markdown(f"<div class='score-cell'>{html.escape(flags)}</div>", unsafe_allow_html=True)
    return selected_key


def render_similarity_bar(value: Any) -> str:
    numeric = score_float(value)
    if numeric is None:
        return "<div class='score-cell'>-</div>"
    pct = max(0.0, min(float(numeric) / 10.0 * 100.0, 100.0))
    return (
        "<div class='score-cell score-sim-wrap'>"
        f"<span class='score-sim-value'>{numeric:.2f}</span>"
        "<span class='score-sim-track'>"
        f"<span class='score-sim-fill' style='width:{pct:.1f}%;'></span>"
        "</span>"
        "</div>"
    )


def score_band_color(score: float | None) -> str:
    if score is None:
        return "rgba(148, 163, 184, 0.08)"
    if score >= 7:
        return "rgba(34, 197, 94, 0.16)"
    if score >= 4:
        return "rgba(251, 191, 36, 0.16)"
    return "rgba(239, 68, 68, 0.16)"


def score_format(value: Any) -> str:
    numeric = score_float(value)
    return "-" if numeric is None else f"{numeric:.2f}"


def render_score_detail(selected: pd.Series) -> None:
    raw = selected.get("_raw", {}) if isinstance(selected.get("_raw", {}), dict) else {}
    base_raw = selected.get("_base_raw", {}) if isinstance(selected.get("_base_raw", {}), dict) else {}
    is_variant = str(selected.get("_row_type", "base")) == "variant"
    with st.container(border=True):
        st.markdown("<div class='panel-title'>Score Breakdown</div>", unsafe_allow_html=True)
        st.markdown(f"**{html.escape(str(selected.get('Clip ID', '')))}**")
        st.caption(str(selected.get("Output File", "")))
        if is_variant:
            st.caption(
                f"Variant of {selected.get('_base_clip_id', '')}; base scores are inherited, similarity is variant-specific."
            )

        breakdown_cols = st.columns(8, gap="medium")
        items = [
            ("Total", selected.get("Total Score")),
            ("Content", selected.get("Content")),
            ("Visual", selected.get("Visual")),
            ("Host Focus", selected.get("Host Focus")),
            ("Hook", selected.get("Hook")),
            ("Quality", selected.get("Quality")),
            ("Engagement", selected.get("Engagement")),
            ("Similarity", selected.get("Similarity")),
        ]
        for col, (label, value) in zip(breakdown_cols, items):
            numeric = score_float(value)
            accent = score_text_color(numeric)
            with col:
                st.markdown(
                    f"""
                    <div class="mini-stat">
                        <div class="mini-stat-label">{label}</div>
                        <div class="mini-stat-value" style="color:{accent};">{score_format(value)}</div>
                        <div class="mini-stat-sub">{score_band_label(numeric)}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

        summary = str(selected.get("Summary", "") or "No summary available.")
        st.markdown(f"<div class='small-muted'>{html.escape(summary)}</div>", unsafe_allow_html=True)
        flags = str(selected.get("Flags", "") or "-")
        st.markdown(f"Flags: `{flags}`")

        metrics = base_raw.get("metrics", {}) if isinstance(base_raw, dict) else {}
        if is_variant and isinstance(raw, dict):
            variant_metrics = raw.get("similarity_metrics", {})
            if variant_metrics:
                metrics = {**metrics, "similarity": variant_metrics}
        if metrics:
            with st.expander("Raw score metrics"):
                st.json(metrics)


def render_score_methodology() -> None:
    with st.container(border=True):
        st.markdown("<div class='panel-title'>How Scores Are Calculated</div>", unsafe_allow_html=True)
        st.markdown(
            """
            **Total Score** is a weighted average of available dimensions: Content 46.7%, Quality 20%, Engagement 33.3%. Visual scoring is disabled. Host Focus receives 20% only when the vision scorer returns a score, with the other weights scaled down proportionally.

            Base clip scores are calculated once per original clip ID, then inherited by all rendered variants of that clip.

            **Content** uses Qwen text scoring on the clip transcript plus deterministic keyword checks to label actual focus: promo, demo, benefit, ingredient, or product-only. Clips that only discuss price or promotion without benefit, demo, ingredient, or product explanation are capped low on content.

            **Visual** is no longer scored and does not contribute to Total Score.

            **Host Focus** is optional Qwen2.5-VL scoring. Every configured sample frame is classified as A engaged with livestream, B looking down at a personal device, or C not attending to the stream. Score = A frames / scored frames * 10.

            **Hook** scores the first 8 seconds when word timestamps are available. It is collected for tuning and is not included in Total Score yet.

            **Quality** uses FFprobe/FFmpeg for clip duration, loudness, and silence detection.

            **Engagement** is returned by the same text-model pass as Content, with the keyword scanner kept as fallback for price/promo, product mentions, demo signals, and benefit claims.

            **Similarity** is variant-only. OpenCV samples frames from sibling variants of the same base clip and compares HSV histograms; higher scores mean the variant looks more visually distinct from its siblings.
            """
        )


def score_text_color(score: float | None) -> str:
    if score is None:
        return "#94a3b8"
    if score >= 7:
        return "#22c55e"
    if score >= 4:
        return "#fbbf24"
    return "#ef4444"


def score_band_label(score: float | None) -> str:
    if score is None:
        return "Unavailable"
    if score >= 7:
        return "Green"
    if score >= 4:
        return "Yellow"
    return "Red"


def render_queues_tab(summary: dict[str, Any]) -> None:
    render_page_intro("Queues", "Queue depth, pending items, and per-stage throughput at a glance.")
    top_cards = st.columns(4, gap="medium")
    for col, (stage_key, label, icon, accent) in zip(top_cards, STAGES):
        with col:
            with st.container(border=True):
                st.markdown(
                    f"""
                    <div class="kpi-card" style="min-height:88px; padding:0.8rem 0.9rem;">
                        <div class="kpi-icon" style="color:{accent}; background: color-mix(in srgb, {accent} 16%, transparent); width:52px; height:52px;">
                            {svg_icon(icon, accent, size=20)}
                        </div>
                        <div>
                            <div class="kpi-label">{label.replace(' Processing', '')} Queue</div>
                            <div class="kpi-value" style="font-size:1.8rem;">{summary['stage_queued'].get(stage_key, 0)}</div>
                            <div class="kpi-sub">Processing: {summary['stage_running'].get(stage_key, 0)}</div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    queue_cols = st.columns(4, gap="medium")
    for col, (stage_key, label, _, accent) in zip(queue_cols, STAGES):
        with col:
            with st.container(border=True):
                st.markdown(f"<div class='panel-title'>{label.replace(' Processing', '')} Queue</div>", unsafe_allow_html=True)
                items = summary["queue_items"].get(stage_key, [])[:6]
                if not items:
                    st.caption("No queued items")
                else:
                    for idx, item in enumerate(items, start=1):
                        st.markdown(
                            f"{idx}. `{item['name']}`  \n<span style='color:{accent}'>{item['status']}</span>",
                            unsafe_allow_html=True,
                        )

    with st.container(border=True):
        st.markdown("<div class='panel-title'>Queue Throughput (items per hour)</div>", unsafe_allow_html=True)
        throughput_cols = st.columns(4, gap="medium")
        for col, (stage_key, label, _, accent) in zip(throughput_cols, STAGES):
            with col:
                st.markdown(f"<div class='small-muted'>{label}</div>", unsafe_allow_html=True)
                frame = summary["throughput_frames"][stage_key]
                rate = frame["count"].mean() if not frame.empty else 0.0
                st.markdown(f"### {rate:.1f} /hr")
                chart = (
                    alt.Chart(frame)
                    .mark_line(strokeWidth=2, color=accent)
                    .encode(
                        x=alt.X("timestamp:T", title=None, axis=alt.Axis(labels=False, ticks=False, domain=False)),
                        y=alt.Y("count:Q", title=None, axis=alt.Axis(labels=False, ticks=False, domain=False, grid=False)),
                    )
                    .properties(height=90)
                    .configure_view(strokeOpacity=0)
                    .configure(background="transparent")
                )
                st.altair_chart(chart, use_container_width=True)


def render_settings_tab() -> None:
    render_page_intro("Settings", "General controls for refresh, pipeline behavior, and future worker settings.")
    try:
        import config as runtime_cfg
    except Exception:
        runtime_cfg = None
    settings_tabs = st.tabs(["General", "Pipeline", "Workers", "Paths", "Notifications", "Advanced"])
    with settings_tabs[0]:
        cols = st.columns(2, gap="medium")
        with cols[0]:
            with st.container(border=True):
                st.markdown("<div class='panel-title'>General Settings</div>", unsafe_allow_html=True)
                app_name = st.text_input("App Name", value=st.session_state.get("cfg_app_name", "VOD Processing Dashboard"))
                refresh_interval = st.number_input("Refresh Interval (seconds)", min_value=2, max_value=30, value=int(st.session_state.get("cfg_refresh", 3)))
                timezone = st.text_input("Timezone", value=st.session_state.get("cfg_timezone", "Asia/Jakarta"))
                auto_start = st.toggle("Auto Start Processing", value=st.session_state.get("cfg_auto_start", True))
                scan_new = st.toggle("Scan for New Videos", value=st.session_state.get("cfg_scan_new", True))
                scan_interval = st.number_input("Scan Interval (minutes)", min_value=1, max_value=120, value=int(st.session_state.get("cfg_scan_interval", 5)))
                if st.button("Save Changes", key="save_general"):
                    st.session_state.cfg_app_name = app_name
                    st.session_state.cfg_refresh = refresh_interval
                    st.session_state.refresh_seconds_value = int(refresh_interval)
                    st.session_state.cfg_timezone = timezone
                    st.session_state.cfg_auto_start = auto_start
                    st.session_state.cfg_scan_new = scan_new
                    st.session_state.cfg_scan_interval = scan_interval
                    st.success("General settings saved. Queue automation toggles require restarting the queue runner.")
        with cols[1]:
            with st.container(border=True):
                st.markdown("<div class='panel-title'>Pipeline Settings</div>", unsafe_allow_html=True)
                max_retries = st.number_input("Max Retries", min_value=0, max_value=10, value=int(st.session_state.get("cfg_max_retries", 3)))
                retry_delay = st.number_input("Retry Delay (seconds)", min_value=0, max_value=3600, value=int(st.session_state.get("cfg_retry_delay", 30)))
                delete_source = st.toggle("Delete Source After Processing", value=st.session_state.get("cfg_delete_source", False))
                auto_generate = st.toggle("Auto Generate Clips", value=st.session_state.get("cfg_auto_generate", True))
                min_default = int(getattr(runtime_cfg, "MIN_CLIP_DURATION", st.session_state.get("cfg_min_clip", 30)))
                max_default = int(getattr(runtime_cfg, "MAX_CLIP_DURATION", st.session_state.get("cfg_max_clip", 120)))
                min_clip = st.number_input("Min Clip Duration (seconds)", min_value=1, max_value=600, value=min_default)
                max_clip = st.number_input("Max Clip Duration (seconds)", min_value=1, max_value=600, value=max_default)
                st.caption("Clip duration values are hot-applied to this Streamlit process. Queue runner policy changes require restart.")
                if st.button("Save Changes", key="save_pipeline"):
                    st.session_state.cfg_max_retries = max_retries
                    st.session_state.cfg_retry_delay = retry_delay
                    st.session_state.cfg_delete_source = delete_source
                    st.session_state.cfg_auto_generate = auto_generate
                    st.session_state.cfg_min_clip = min_clip
                    st.session_state.cfg_max_clip = max_clip
                    if runtime_cfg is not None:
                        runtime_cfg.MIN_CLIP_DURATION = int(min_clip)
                        runtime_cfg.MAX_CLIP_DURATION = int(max_clip)
                        st.success("Pipeline settings saved. Runtime clip duration config updated; queue runner-only controls require restart.")
                    else:
                        st.warning("Settings saved for this dashboard session, but config.py could not be imported.")

    for tab in settings_tabs[1:]:
        with tab:
            st.info("This settings section is ready for wiring when those controls are finalized.")


def render_stage_card(label: str, count: int, icon: str, accent: str) -> None:
    st.markdown(
        f"""
        <div class="pipeline-stage">
            <div class="stage-ring" style="color:{accent}; border: 3px solid color-mix(in srgb, {accent} 92%, white 8%);">
                {svg_icon(icon, accent, size=22, stroke=2.0)}
            </div>
            <div class="stage-name">{label}</div>
            <div class="stage-count">{count}</div>
            <div class="stage-caption">Processing</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def build_status_badge(status: str) -> str:
    css_map = {
        "Processing": "status-processing",
        "Completed": "status-completed",
        "Waiting": "status-waiting",
        "Failed": "status-failed",
    }
    return f"<span class='status-badge {css_map.get(status, 'status-waiting')}'>{html.escape(status)}</span>"


def build_progress_cell(progress: int, status: str) -> str:
    fill_class = "progress-fill completed" if status == "Completed" else "progress-fill"
    bounded = max(0, min(progress, 100))
    return (
        "<div class='progress-wrap'>"
        f"<div class='progress-track'><div class='{fill_class}' style='width:{bounded}%;'></div></div>"
        f"<div class='progress-value'>{bounded}%</div>"
        "</div>"
    )


def render_html_table(table_df: pd.DataFrame) -> None:
    headers = [
        "Video Name",
        "Status",
        "Current Step",
        "Progress",
        "Clips Generated",
        "Runs",
        "Redos",
        "Duration",
        "Started At",
        "Completed At",
        "",
    ]
    rows = []
    for _, row in table_df.iterrows():
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(row['Video Name']))}</td>"
            f"<td>{build_status_badge(str(row['Status']))}</td>"
            f"<td>{html.escape(str(row['Current Step']))}</td>"
            f"<td>{build_progress_cell(int(row['Progress']), str(row['Status']))}</td>"
            f"<td>{int(row['Clips Generated'])}</td>"
            f"<td>{int(row['Runs'])}</td>"
            f"<td>{int(row['Redos'])}</td>"
            f"<td>{html.escape(str(row['Duration']))}</td>"
            f"<td>{html.escape(str(row['Started At']))}</td>"
            f"<td>{html.escape(str(row['Completed At']))}</td>"
            "<td class='row-action'>&#8942;</td>"
            "</tr>"
        )

    header_html = "".join(f"<th>{html.escape(col)}</th>" for col in headers)
    body_html = "".join(rows)
    st.markdown(
        f"""
        <div class="table-shell">
            <table class="video-table">
                <thead><tr>{header_html}</tr></thead>
                <tbody>{body_html}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def chart_window_options() -> dict[str, timedelta | None]:
    return {
        "Last 24 hours": timedelta(days=1),
        "Last 7 days": timedelta(days=7),
        "Last 30 days": timedelta(days=30),
        "All time": None,
    }


default_state_path = str(resolve_default_state_path())
if "auto_refresh_enabled" not in st.session_state:
    st.session_state.auto_refresh_enabled = True
if "refresh_seconds_value" not in st.session_state:
    st.session_state.refresh_seconds_value = 3
if "state_path_value" not in st.session_state:
    st.session_state.state_path_value = default_state_path
if "table_page" not in st.session_state:
    st.session_state.table_page = 1
if "active_tab" not in st.session_state:
    st.session_state.active_tab = "overview"

fragment_interval = (
    f"{st.session_state.refresh_seconds_value}s"
    if st.session_state.auto_refresh_enabled
    else None
)


@st.fragment(run_every=fragment_interval)
def render_dashboard() -> None:
    state_path = st.session_state.state_path_value
    state = load_state(state_path)
    summary = summarize_state(state)
    system = get_system_stats()
    updated_at = parse_timestamp(state.get("updated_at"))
    active_tab = st.session_state.active_tab

    shell_cols = st.columns([1.05, 5.3], gap="large")
    left_rail, main_area = shell_cols

    with left_rail:
        with st.container(border=True):
            st.markdown("<div class='nav-shell'>", unsafe_allow_html=True)
            render_nav_controls(active_tab)
            st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)
            render_system_panel(system)
            st.markdown("</div>", unsafe_allow_html=True)

    with main_area:
        render_header(updated_at)

        if state.get("_error"):
            st.error(state["_error"])

        if active_tab == "overview":
            render_overview_tab(summary)
        elif active_tab == "videos":
            render_videos_tab(summary)
        elif active_tab == "analytics":
            render_analytics_tab(summary)
        elif active_tab == "scores":
            render_scores_tab(summary)
        elif active_tab == "trends":
            render_trends_tab(summary)
        elif active_tab == "focus_debug":
            render_focus_debug_tab(summary)
        elif active_tab == "queues":
            render_queues_tab(summary)
        elif active_tab == "settings":
            render_settings_tab()


render_dashboard()
