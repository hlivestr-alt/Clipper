from __future__ import annotations

import html
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import psutil
import streamlit as st

import config as cfg
import queue_control


STAGES = [
    ("transcribe", "Transcription", "audio", "#22c55e"),
    ("llm", "LLM Processing", "chip", "#3b82f6"),
    ("yolo", "YOLO Detection", "focus", "#fbbf24"),
    ("ffmpeg", "Video Editing", "scissors", "#8b5cf6"),
]
STAGE_LABELS = {key: label for key, label, _, _ in STAGES}
DEFAULT_STATE_CANDIDATES = [
    Path("state.json"),
    Path(
        getattr(
            cfg,
            "QUEUE_STATE_FILE",
            Path(getattr(cfg, "WORKING_DIR", "working")) / "video_queue_state.json",
        )
    ),
]
DEFAULT_CONTROL_FILE = Path(
    getattr(
        cfg,
        "QUEUE_CONTROL_FILE",
        Path(getattr(cfg, "WORKING_DIR", "working")) / "queue_control.json",
    )
)
DEFAULT_FOREVER_STATE_FILE = Path(
    getattr(
        cfg,
        "QUEUE_FOREVER_STATE_FILE",
        Path(getattr(cfg, "WORKING_DIR", "working")) / "queue_forever_state.json",
    )
)
TREND_PRODUCTS = ["Cleanser", "Serum", "Toner", "Eye Cream", "Sheet Mask", "Moisturizer"]
MODULE_PRODUCTS = [
    ("cleanser", "Cleanser"),
    ("toner", "Toner"),
    ("serum", "Serum"),
    ("eye_cream", "Eye Cream"),
    ("mask", "Mask"),
    ("skin_cream", "Skin Cream"),
]
MODULE_ROLES = ("hook", "main", "cta")
MODULE_PRODUCT_LABELS = dict(MODULE_PRODUCTS)
VOD_SOURCE_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-\d{2}-\d{2}-\d{2}\.mp4$",
    re.IGNORECASE,
)
VOD_SOURCE_DATE_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})-\d{2}-\d{2}-\d{2}")
SCORES_INDEX_CACHE_KEY = "scores_index_cache"
SCORES_DETAIL_CACHE_KEY = "scores_detail_cache"
SCORES_EXPORT_CACHE_KEY = "scores_export_cache"
SCORES_CACHE_VERSION = 4
SCORES_DEFAULT_SORT_VERSION = 2
AUTO_REFRESH_TABS = {"overview", "videos", "analytics", "queues"}
LOGGER = logging.getLogger(__name__)
LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo
MIN_SORT_TIMESTAMP = datetime.min.replace(tzinfo=LOCAL_TIMEZONE)

try:
    psutil.cpu_percent(interval=None)
except Exception:
    pass


def source_date_from_source_video_value(source_video: Any) -> str:
    if not source_video:
        return ""
    filename = re.split(r"[\\/]", str(source_video).strip())[-1]
    match = VOD_SOURCE_FILENAME_RE.match(filename)
    if not match:
        return ""
    try:
        datetime.strptime(filename[:-4], "%Y-%m-%d-%H-%M-%S")
    except ValueError:
        return ""
    return match.group("date")


def module_source_date_value(module: dict[str, Any]) -> str:
    for key in ("source_date", "source_video_date"):
        explicit = str(module.get(key) or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", explicit):
            return explicit
        if re.fullmatch(r"\d{8}", explicit):
            return f"{explicit[:4]}-{explicit[4:6]}-{explicit[6:8]}"
    return source_date_from_source_video_value(module.get("source_video"))


def source_video_filename(source_video: Any) -> str:
    if not source_video:
        return ""
    return re.split(r"[\\/]", str(source_video).strip())[-1]


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

    .module-ready-grid {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 0.85rem;
        margin-bottom: 0.8rem;
    }

    .module-ready-card {
        border: 1px solid var(--line);
        border-radius: 8px;
        background: rgba(9, 15, 25, 0.28);
        padding: 0.9rem;
        min-height: 138px;
    }

    .module-ready-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.7rem;
        margin-bottom: 0.85rem;
    }

    .module-ready-name {
        color: var(--text);
        font-size: 1rem;
        font-weight: 700;
        line-height: 1.2;
    }

    .module-ready-badge {
        border-radius: 999px;
        padding: 0.25rem 0.55rem;
        font-size: 0.75rem;
        font-weight: 700;
        text-transform: uppercase;
        white-space: nowrap;
        border: 1px solid transparent;
    }

    .module-ready-badge.ready {
        color: #86efac;
        background: rgba(22, 163, 74, 0.14);
        border-color: rgba(34, 197, 94, 0.18);
    }

    .module-ready-badge.partial {
        color: #fde68a;
        background: rgba(202, 138, 4, 0.16);
        border-color: rgba(251, 191, 36, 0.18);
    }

    .module-ready-badge.empty {
        color: #cbd5e1;
        background: rgba(100, 116, 139, 0.14);
        border-color: rgba(148, 163, 184, 0.16);
    }

    .module-ready-counts {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 0.55rem;
    }

    .module-ready-role {
        border-radius: 8px;
        background: rgba(15, 23, 42, 0.58);
        padding: 0.55rem 0.45rem;
    }

    .module-ready-role-label {
        color: var(--muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        margin-bottom: 0.3rem;
    }

    .module-ready-role-value {
        color: var(--text);
        font-size: 1.35rem;
        font-weight: 700;
        line-height: 1;
    }

    .module-ready-foot {
        color: var(--muted);
        font-size: 0.78rem;
        margin-top: 0.75rem;
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
        overflow-x: auto;
        overflow-y: hidden;
        background: rgba(9, 15, 25, 0.28);
    }

    .mobile-card-list,
    .mobile-overview-strip,
    .mobile-queue-status-card,
    .mobile-score-card-shell {
        display: none;
    }

    .mobile-card-list {
        gap: 0.75rem;
    }

    .mobile-card {
        border: 1px solid var(--line);
        border-radius: 10px;
        background: rgba(9, 15, 25, 0.42);
        padding: 0.85rem;
        margin-bottom: 0.75rem;
    }

    .mobile-card-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 0.65rem;
        margin-bottom: 0.65rem;
    }

    .mobile-card-title {
        color: var(--text);
        font-weight: 800;
        font-size: 0.96rem;
        line-height: 1.2;
        word-break: break-word;
    }

    .mobile-card-meta {
        color: var(--muted);
        font-size: 0.8rem;
        line-height: 1.35;
    }

    .mobile-card-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.55rem;
        margin-top: 0.72rem;
    }

    .mobile-card-stat {
        border-radius: 8px;
        background: rgba(15, 23, 42, 0.58);
        padding: 0.58rem;
        min-width: 0;
    }

    .mobile-card-stat-label {
        color: var(--muted);
        font-size: 0.68rem;
        font-weight: 700;
        text-transform: uppercase;
        margin-bottom: 0.22rem;
    }

    .mobile-card-stat-value {
        color: var(--text);
        font-size: 0.93rem;
        font-weight: 800;
        line-height: 1.18;
        word-break: break-word;
    }

    .mobile-progress {
        margin-top: 0.7rem;
    }

    .mobile-progress-track {
        height: 9px;
        border-radius: 999px;
        background: rgba(30, 41, 59, 0.9);
        overflow: hidden;
    }

    .mobile-progress-fill {
        height: 100%;
        border-radius: 999px;
        background: linear-gradient(90deg, #3b82f6, #60a5fa);
    }

    .mobile-progress-label {
        color: #dbeafe;
        font-size: 0.78rem;
        margin-top: 0.3rem;
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

    @media (max-width: 1100px) {
        .module-ready-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
    }

    @media (max-width: 760px) {
        .block-container {
            padding: 0.7rem 0.55rem 1rem 0.55rem;
        }

        div[data-testid="stHorizontalBlock"] {
            flex-wrap: wrap;
        }

        div[data-testid="stColumn"] {
            flex: 1 1 100% !important;
            width: 100% !important;
            min-width: 0 !important;
        }

        div[data-testid="stColumn"]:has(.desktop-left-rail-anchor) {
            display: none !important;
        }

        div[data-testid="stElementContainer"]:has(.mobile-nav-anchor),
        div[data-testid="stElementContainer"]:has(.mobile-nav-anchor) + div[data-testid="stElementContainer"] {
            display: block !important;
        }

        .mobile-nav-title {
            color: var(--muted);
            font-size: 0.74rem;
            font-weight: 800;
            letter-spacing: 0.08em;
            margin: 0 0 0.35rem 0;
            text-transform: uppercase;
        }

        .topbar {
            align-items: flex-start;
            gap: 0.65rem;
        }

        .app-icon {
            width: 38px;
            height: 38px;
            border-radius: 10px;
        }

        .app-title {
            font-size: 1.24rem;
            line-height: 1.18;
        }

        .app-subtitle {
            display: none;
        }

        .status-pill {
            width: 100%;
            justify-content: center;
            padding: 0.48rem 0.6rem;
            font-size: 0.8rem;
        }

        div[data-testid="stButton"] > button {
            min-height: 44px;
            font-size: 0.92rem;
        }

        .kpi-card {
            height: auto;
            min-height: 92px;
            border-radius: 10px;
            padding: 0.78rem;
        }

        .kpi-icon {
            width: 42px;
            height: 42px;
            border-radius: 10px;
            flex: 0 0 auto;
        }

        .kpi-label {
            min-height: auto;
            font-size: 0.82rem;
        }

        .kpi-value {
            font-size: 1.62rem;
        }

        .mini-grid,
        .module-ready-grid {
            grid-template-columns: 1fr;
        }

        .stage-arrow {
            display: none;
        }

        .stage-ring {
            width: 58px;
            height: 58px;
            margin-bottom: 0.55rem;
        }

        .queue-row {
            grid-template-columns: 1fr;
            gap: 0.4rem;
        }

        .video-table-desktop {
            display: none;
        }

        .mobile-card-list,
        .mobile-overview-strip,
        .mobile-queue-status-card,
        .mobile-score-card-shell {
            display: block;
        }

        .mobile-overview-strip {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 0.65rem;
            margin-bottom: 0.8rem;
        }

        .mobile-queue-status-card {
            border: 1px solid var(--line);
            border-radius: 10px;
            background: rgba(9, 15, 25, 0.42);
            padding: 0.85rem;
            margin-bottom: 0.8rem;
        }

        .mobile-score-button-anchor + div[data-testid="stElementContainer"] {
            display: block !important;
        }

        .stTabs [data-baseweb="tab-list"] {
            overflow-x: auto;
            flex-wrap: nowrap;
            scrollbar-width: thin;
        }

        .stTabs [data-baseweb="tab"] {
            min-width: max-content;
            padding: 0.7rem 0.82rem;
        }

        div[data-testid="stDataFrame"] {
            max-width: 100%;
        }

        div[data-testid="stElementContainer"]:has(.desktop-dataframe-anchor),
        div[data-testid="stElementContainer"]:has(.desktop-dataframe-anchor) + div[data-testid="stElementContainer"] {
            display: none !important;
        }

        div[data-testid="stElementContainer"]:has(.queue-control-desktop-anchor),
        div[data-testid="stElementContainer"]:has(.queue-control-desktop-anchor) + div[data-testid="stLayoutWrapper"] {
            display: none !important;
        }

        div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .desktop-score-detail-anchor) {
            display: none !important;
        }

        div[data-testid="stElementContainer"]:has(.desktop-compliance-filters-anchor),
        div[data-testid="stElementContainer"]:has(.desktop-compliance-filters-anchor) + div[data-testid="stLayoutWrapper"] {
            display: none !important;
        }

        div[data-testid="stElementContainer"]:has(.mobile-compliance-filters-anchor),
        div[data-testid="stElementContainer"]:has(.mobile-compliance-filters-anchor) + details,
        div[data-testid="stElementContainer"]:has(.mobile-compliance-filters-anchor) + div[data-testid="stExpander"],
        div[data-testid="stElementContainer"]:has(.mobile-compliance-filters-anchor) + div[data-testid="stElementContainer"] {
            display: block !important;
        }

        div[data-testid="stColumn"]:has(.score-detail-anchor) {
            position: static !important;
            border-left: 0 !important;
            padding-left: 0 !important;
        }

        video[data-testid="stVideo"] {
            max-height: 62vh;
            object-fit: contain;
            background: #020617;
        }

        .module-ready-grid {
            grid-template-columns: 1fr;
        }
    }

    @media (min-width: 761px) {
        div[data-testid="stElementContainer"]:has(.mobile-nav-anchor),
        div[data-testid="stElementContainer"]:has(.mobile-nav-anchor) + div[data-testid="stElementContainer"],
        div[data-testid="stElementContainer"]:has(.mobile-score-card-shell),
        div[data-testid="stElementContainer"]:has(.mobile-score-button-anchor) + div[data-testid="stElementContainer"],
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.mobile-inline-score-detail-anchor),
        div[data-testid="stElementContainer"]:has(.mobile-compliance-filters-anchor),
        div[data-testid="stElementContainer"]:has(.mobile-compliance-filters-anchor) + details,
        div[data-testid="stElementContainer"]:has(.mobile-compliance-filters-anchor) + div[data-testid="stExpander"],
        div[data-testid="stElementContainer"]:has(.mobile-compliance-filters-anchor) + div[data-testid="stElementContainer"] {
            display: none !important;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed


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


def sum_clip_events_since(events: list[tuple[datetime, int]], cutoff: datetime) -> int:
    return sum(count for timestamp, count in events if timestamp >= cutoff)


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


@st.cache_data(ttl=2, show_spinner=False)
def load_queue_control_snapshot(state_path: str) -> dict[str, Any]:
    return queue_control.read_status_snapshot(
        control_path=DEFAULT_CONTROL_FILE,
        forever_state_path=DEFAULT_FOREVER_STATE_FILE,
        queue_state_path=state_path,
    )


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


def file_signature(path: str | Path) -> tuple[str, int, int]:
    target = Path(path)
    normalized = os.path.normcase(os.path.abspath(os.fspath(target)))
    try:
        stat = target.stat()
    except OSError:
        return normalized, 0, 0
    return normalized, int(stat.st_mtime_ns), int(stat.st_size)


@st.cache_data(show_spinner=False, max_entries=1024)
def load_json_payload_by_signature(path_key: str, mtime_ns: int, size: int) -> Any:
    if not mtime_ns or not size:
        return None
    try:
        return json.loads(Path(path_key).read_text(encoding="utf-8"))
    except Exception:
        return None


def load_json_dict_by_signature(signature: tuple[str, int, int]) -> dict[str, Any]:
    payload = load_json_payload_by_signature(*signature)
    return payload if isinstance(payload, dict) else {}


def load_json_list_by_signature(signature: tuple[str, int, int]) -> list[Any]:
    payload = load_json_payload_by_signature(*signature)
    return payload if isinstance(payload, list) else []


def empty_scorer_stats() -> dict[str, Any]:
    return {
        "summary_count": 0,
        "previous_text_qwen_calls": 0,
        "actual_text_qwen_calls": 0,
        "saved_text_qwen_calls": 0,
        "actual_vision_qwen_calls": 0,
        "vision_base_group_count": 0,
        "vision_contact_sheet_groups": 0,
        "vision_contact_sheet_fallbacks": 0,
    }


def accumulate_scorer_stats(totals: dict[str, Any], payload: dict[str, Any]) -> None:
    stats = payload.get("scoring_optimization", {}) if isinstance(payload, dict) else {}
    if not isinstance(stats, dict):
        return
    vision_stats = stats.get("vision_scoring", {})
    if not isinstance(vision_stats, dict):
        vision_stats = {}
    totals["summary_count"] += 1
    totals["previous_text_qwen_calls"] += int(score_float(stats.get("previous_text_qwen_calls")) or 0)
    totals["actual_text_qwen_calls"] += int(score_float(stats.get("actual_text_qwen_calls")) or 0)
    totals["saved_text_qwen_calls"] += int(score_float(stats.get("saved_text_qwen_calls")) or 0)
    totals["actual_vision_qwen_calls"] += int(
        score_float(stats.get("actual_vision_qwen_calls"))
        or score_float(vision_stats.get("actual_vision_qwen_calls"))
        or 0
    )
    totals["vision_base_group_count"] += int(score_float(vision_stats.get("vision_base_group_count")) or 0)
    totals["vision_contact_sheet_groups"] += int(score_float(vision_stats.get("vision_contact_sheet_groups")) or 0)
    totals["vision_contact_sheet_fallbacks"] += int(score_float(vision_stats.get("vision_contact_sheet_fallbacks")) or 0)


def invalidate_scores_session_cache() -> None:
    st.session_state.pop(SCORES_INDEX_CACHE_KEY, None)
    st.session_state.pop(SCORES_DETAIL_CACHE_KEY, None)
    st.session_state.pop(SCORES_EXPORT_CACHE_KEY, None)


def build_scores_summary_signature(output_dirs: tuple[str, ...]) -> tuple[tuple[str, int, int], ...]:
    return tuple(file_signature(Path(output_dir) / "scores_summary.json") for output_dir in output_dirs)


def iter_score_summary_payloads(
    output_dirs: tuple[str, ...],
) -> list[tuple[Path, dict[str, Any]]]:
    payloads: list[tuple[Path, dict[str, Any]]] = []
    for output_dir in output_dirs:
        folder = Path(output_dir)
        payload = load_json_dict_by_signature(file_signature(folder / "scores_summary.json"))
        if payload:
            payloads.append((folder, payload))
    return payloads


def score_groups_from_summary(payload: dict[str, Any]) -> list[dict[str, Any]]:
    groups = payload.get("groups", []) if isinstance(payload, dict) else []
    if isinstance(groups, list) and groups:
        return [group for group in groups if isinstance(group, dict)]
    clips = payload.get("clips", []) if isinstance(payload, dict) else []
    if isinstance(clips, list):
        return synthesize_score_groups_from_clips(clips)
    return []


def load_scores_index_payload(output_dirs: tuple[str, ...]) -> dict[str, Any]:
    signature = build_scores_summary_signature(output_dirs)
    cached = st.session_state.get(SCORES_INDEX_CACHE_KEY)
    if (
        isinstance(cached, dict)
        and cached.get("version") == SCORES_CACHE_VERSION
        and cached.get("signature") == signature
    ):
        return cached

    rows, stats = build_score_index_rows(output_dirs)
    payload = {
        "version": SCORES_CACHE_VERSION,
        "output_dirs": tuple(output_dirs),
        "signature": signature,
        "rows": rows,
        "stats": stats,
        "loaded_at": datetime.now().isoformat(timespec="seconds"),
    }
    st.session_state[SCORES_INDEX_CACHE_KEY] = payload
    st.session_state[SCORES_DETAIL_CACHE_KEY] = {}
    return payload


def build_score_index_rows(output_dirs: tuple[str, ...]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    stats = empty_scorer_stats()
    for folder, payload in iter_score_summary_payloads(output_dirs):
        accumulate_scorer_stats(stats, payload)
        source_video, run_tag = split_output_folder_name(folder.name)
        for group in score_groups_from_summary(payload):
            rows.append(build_group_score_index_row(group, folder, source_video, run_tag))

    rows.sort(key=lambda row: row["_scored_at_sort"], reverse=True)
    return rows, stats


def build_group_score_index_row(
    group: dict[str, Any],
    output_dir: Path,
    source_video: str,
    run_tag: str,
) -> dict[str, Any]:
    base_key = build_score_key(
        {
            "clip_id": group.get("base_clip_id") or group.get("clip_id"),
            "clip_path": group.get("representative_clip_path") or group.get("representative_output_file"),
        }
    )
    scored_at = str(group.get("scored_at") or "")
    flags = score_flags_list(group.get("flags", []))
    flag_severity = score_flag_severity(flags)
    host_focus = score_float(group.get("host_focus_score"))
    hook = score_float(group.get("hook_score"))
    clip_id = str(group.get("base_clip_id") or group.get("clip_id") or "")
    product = str(group.get("product", "general") or "general")
    flag_text = score_flags_text(flags)
    total_score = score_float(group.get("total_score"))
    quality_score = score_float(group.get("quality_score"))
    compliance_blocked = bool(group.get("compliance_blocked", False))
    search_text = " ".join(
        [
            source_video,
            run_tag,
            clip_id,
            product,
            flag_text,
        ]
    )
    return {
        "Source Video": source_video,
        "Run Tag": run_tag,
        "Source Date": score_source_date_value(source_video),
        "Clip ID": clip_id,
        "Product": product,
        "Product Bucket": trend_product_bucket(product),
        "Total Score": total_score,
        "Content": score_float(group.get("content_score")),
        "Host Focus": host_focus,
        "Hook": hook,
        "H/H": score_pair_format(host_focus, hook),
        "Quality": quality_score,
        "Engagement": score_float(group.get("engagement_score")),
        "Variants": int(score_float(group.get("variant_count")) or 0),
        "Flags": flag_text,
        "Flag Count": len(flags),
        "Flag Severity": flag_severity,
        "Flags Label": score_flags_label(len(flags)),
        "Quality Label": score_quality_label(quality_score),
        "Status": score_status_label(total_score, flag_severity, compliance_blocked),
        "Compliance Blocked": compliance_blocked,
        "Exported": bool(group.get("exported", True)),
        "Scored At": scored_at,
        "_scored_at_sort": parse_timestamp(scored_at) or MIN_SORT_TIMESTAMP,
        "_score_key": base_key,
        "_base_score_key": base_key,
        "_base_clip_id": clip_id,
        "_row_type": "base",
        "_variant_index": -1,
        "_output_dir": str(output_dir),
        "_summary_path": str(output_dir / "scores_summary.json"),
        "_search_text": search_text,
    }


def score_source_date_value(source_video: Any) -> str:
    filename = source_video_filename(source_video)
    match = VOD_SOURCE_DATE_RE.search(filename)
    if match:
        return match.group("date")
    return source_date_from_source_video_value(source_video)


def score_flags_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    flags = []
    for item in raw_items:
        clean = str(item or "").strip()
        if clean and clean != "inherits base":
            flags.append(clean)
    return flags


def score_flags_text(value: Any) -> str:
    return ", ".join(score_flags_list(value))


def score_flag_severity(flags: list[str]) -> str:
    severities = {score_single_flag_severity(flag) for flag in flags}
    if "high" in severities:
        return "high"
    if "medium" in severities:
        return "medium"
    return "none"


def score_single_flag_severity(flag: Any) -> str:
    normalized = str(flag or "").casefold()
    high_flags = {
        "audio_too_loud",
        "audio_too_quiet",
        "content_qwen_unavailable",
        "host_doing_other",
        "host_not_focused",
        "long_silence",
        "off_topic",
        "quality_probe_unavailable",
        "silence_probe_unavailable",
    }
    medium_flags = {
        "contact_sheet_fallback",
        "filler_heavy",
        "host_focus_uncertain",
        "host_looking_at_phone",
        "long_duration",
        "loudness_unavailable",
        "product_ambiguous",
        "promo_price_only",
        "short_duration",
        "visually_similar_variant",
    }
    if normalized in high_flags:
        return "high"
    if normalized in medium_flags:
        return "medium"
    return "none"


def score_flags_label(count: int) -> str:
    if count <= 0:
        return "No flags"
    suffix = "" if count == 1 else "s"
    return f"{count} flag{suffix}"


def score_pair_format(left: Any, right: Any) -> str:
    left_score = score_float(left)
    right_score = score_float(right)
    left_text = "-" if left_score is None else f"{left_score:.1f}"
    right_text = "-" if right_score is None else f"{right_score:.1f}"
    return f"{left_text}/{right_text}"


def score_quality_label(value: Any) -> str:
    numeric = score_float(value)
    if numeric is None:
        return "Unknown"
    if numeric >= 7:
        return "High"
    if numeric >= 5:
        return "Medium"
    return "Low"


def score_status_label(total_score: Any, flag_severity: str = "none", compliance_blocked: bool = False) -> str:
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


def load_score_detail_dataframe(index_row: pd.Series) -> pd.DataFrame:
    base_key = str(index_row.get("_base_score_key") or index_row.get("_score_key") or "")
    output_dir = str(index_row.get("_output_dir") or "")
    cache_key = f"{output_dir.casefold()}::{base_key}"
    detail_cache = st.session_state.setdefault(SCORES_DETAIL_CACHE_KEY, {})
    if cache_key not in detail_cache:
        detail_cache[cache_key] = load_score_detail_rows(
            output_dir,
            base_key,
            str(index_row.get("_base_clip_id") or index_row.get("Clip ID") or ""),
        )
    rows = detail_cache.get(cache_key) or [index_row.to_dict()]
    return pd.DataFrame(rows)


def load_score_detail_rows(output_dir: str, base_key: str, base_clip_id: str) -> list[dict[str, Any]]:
    folder = Path(output_dir)
    payload = load_json_dict_by_signature(file_signature(folder / "scores_summary.json"))
    if not payload:
        return []

    source_video, run_tag = split_output_folder_name(folder.name)
    for group in score_groups_from_summary(payload):
        rows = build_group_score_rows(group, source_video, run_tag)
        if not rows:
            continue
        row = rows[0]
        if str(row.get("_base_score_key") or "") == str(base_key):
            return rows
        if base_clip_id and str(row.get("_base_clip_id") or "") == str(base_clip_id):
            return rows
    return []


@st.cache_data(show_spinner=False, max_entries=32)
def _load_score_rows_cached(summary_signature: tuple[tuple[str, int, int], ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path_key, _mtime_ns, _size in summary_signature:
        folder = Path(path_key).parent
        payload = load_json_dict_by_signature((path_key, _mtime_ns, _size))
        if not payload:
            continue
        source_video, run_tag = split_output_folder_name(folder.name)
        for group in score_groups_from_summary(payload):
            rows.extend(build_group_score_rows(group, source_video, run_tag))
    rows.sort(key=lambda row: row["_scored_at_sort"], reverse=True)
    return rows


def load_score_rows(output_dirs: tuple[str, ...]) -> list[dict[str, Any]]:
    return _load_score_rows_cached(build_scores_summary_signature(output_dirs))


load_score_rows.clear = _load_score_rows_cached.clear  # type: ignore[attr-defined]


@st.cache_data(show_spinner=False, max_entries=32)
def _load_scorer_stats_cached(summary_signature: tuple[tuple[str, int, int], ...]) -> dict[str, Any]:
    totals = empty_scorer_stats()
    for signature in summary_signature:
        payload = load_json_dict_by_signature(signature)
        if payload:
            accumulate_scorer_stats(totals, payload)
    return totals


def load_scorer_stats(output_dirs: tuple[str, ...]) -> dict[str, Any]:
    return _load_scorer_stats_cached(build_scores_summary_signature(output_dirs))


load_scorer_stats.clear = _load_scorer_stats_cached.clear  # type: ignore[attr-defined]


@st.cache_data(show_spinner=False, max_entries=32)
def _load_score_trend_payload_cached(summary_signature: tuple[tuple[str, int, int], ...]) -> dict[str, Any]:
    product_scores: dict[str, list[float]] = {product: [] for product in TREND_PRODUCTS}
    dimension_scores: dict[str, list[float]] = {"Content": [], "Quality": [], "Engagement": []}
    tier_counts: Counter[str] = Counter()
    flag_counter: Counter[str] = Counter()
    top_candidates: list[dict[str, Any]] = []
    clip_count = 0

    def add_clip(row: dict[str, Any]) -> None:
        nonlocal clip_count
        clip_count += 1
        total_score = score_float(row.get("Total Score"))
        product = trend_product_bucket(row.get("Product"))
        if total_score is not None and product in product_scores:
            product_scores[product].append(total_score)
        for label in dimension_scores:
            value = score_float(row.get(label))
            if value is not None:
                dimension_scores[label].append(value)
        tier_counts.update([score_tier_label(total_score)])
        flag_counter.update(split_flags_for_trends(row.get("Flags")))
        top_candidates.append(
            {
                "Clip ID": row.get("Clip ID", ""),
                "Product": row.get("Product", ""),
                "Total Score": total_score,
                "Summary": row.get("Summary", ""),
            }
        )

    for path_key, _mtime_ns, _size in summary_signature:
        folder = Path(path_key).parent
        payload = load_json_dict_by_signature((path_key, _mtime_ns, _size))
        if not payload:
            continue
        source_video, run_tag = split_output_folder_name(folder.name)
        for group in score_groups_from_summary(payload):
            for row in build_group_score_rows(group, source_video, run_tag):
                if row.get("_row_type") in {"base", "variant"}:
                    add_clip(row)

    product_df = pd.DataFrame(
        [
            {
                "Product": product,
                "Average Total": round(float(sum(values) / len(values)), 2) if values else None,
                "Clips": len(values),
            }
            for product, values in product_scores.items()
        ]
    )
    dimension_df = pd.DataFrame(
        [
            {
                "Dimension": label,
                "Average Score": round(float(sum(values) / len(values)), 2) if values else None,
            }
            for label, values in dimension_scores.items()
        ]
    )
    tier_order = ["Export Ready", "Review Needed", "Rejected"]
    tier_df = pd.DataFrame(
        [{"Tier": tier, "Clips": int(tier_counts.get(tier, 0))} for tier in tier_order]
    )
    flags_df = pd.DataFrame(
        [{"Flag": flag, "Count": count} for flag, count in flag_counter.most_common(10)]
    )
    top_df = pd.DataFrame(
        sorted(
            top_candidates,
            key=lambda row: (score_float(row.get("Total Score")) is not None, score_float(row.get("Total Score")) or -1),
            reverse=True,
        )[:3]
    )
    if top_df.empty:
        top_df = pd.DataFrame(columns=["Clip ID", "Product", "Total Score", "Summary"])
    return {
        "clip_count": clip_count,
        "product_df": product_df,
        "dimension_df": dimension_df,
        "tier_df": tier_df,
        "flags_df": flags_df,
        "top_df": top_df[["Clip ID", "Product", "Total Score", "Summary"]],
        "tier_order": tier_order,
    }


def load_score_trend_payload(output_dirs: tuple[str, ...]) -> dict[str, Any]:
    return _load_score_trend_payload_cached(build_scores_summary_signature(output_dirs))


load_score_trend_payload.clear = _load_score_trend_payload_cached.clear  # type: ignore[attr-defined]


def module_index_signature(library_dir: str) -> tuple[str, int, int]:
    return file_signature(Path(library_dir) / "index.json")


@st.cache_data(show_spinner=False, max_entries=16)
def _load_module_index_payload_cached(index_signature: tuple[str, int, int]) -> dict[str, Any]:
    index_path = Path(index_signature[0])
    payload = load_json_dict_by_signature(index_signature)
    modules = payload.get("modules", []) if isinstance(payload, dict) else []
    if not isinstance(modules, list):
        modules = []
    return {
        "index_path": str(index_path),
        "index_exists": bool(index_signature[1] and index_signature[2]),
        "index_updated_at": payload.get("updated_at", "") if isinstance(payload, dict) else "",
        "index_module_count": module_index_count(payload, modules),
        "error": "",
        "payload": payload,
        "modules": [module for module in modules if isinstance(module, dict)],
    }


def load_module_index_payload(library_dir: str) -> dict[str, Any]:
    return _load_module_index_payload_cached(module_index_signature(library_dir))


load_module_index_payload.clear = _load_module_index_payload_cached.clear  # type: ignore[attr-defined]


def module_index_count(payload: dict[str, Any], modules: list[Any]) -> int:
    try:
        return int(payload.get("module_count") or len(modules) or 0) if isinstance(payload, dict) else len(modules)
    except (TypeError, ValueError):
        return len(modules)


def load_module_product_readiness(
    library_dir: str,
    min_hook: int,
    min_main: int,
    min_cta: int,
) -> dict[str, Any]:
    return _load_module_product_readiness_cached(module_index_signature(library_dir), min_hook, min_main, min_cta)


@st.cache_data(show_spinner=False, max_entries=16)
def _load_module_product_readiness_cached(
    index_signature: tuple[str, int, int],
    min_hook: int,
    min_main: int,
    min_cta: int,
) -> dict[str, Any]:
    index_payload = _load_module_index_payload_cached(index_signature)
    modules = index_payload.get("modules", [])
    counts = {product: {role: 0 for role in MODULE_ROLES} for product, _label in MODULE_PRODUCTS}
    if isinstance(modules, list):
        for module in modules:
            product = str(module.get("product") or "")
            role = str(module.get("role") or "")
            if product in counts and role in counts[product]:
                counts[product][role] += 1

    rows = []
    for product, label in MODULE_PRODUCTS:
        role_counts = counts[product]
        total = sum(role_counts.values())
        if role_counts["hook"] >= min_hook and role_counts["main"] >= min_main and role_counts["cta"] >= min_cta:
            readiness = "ready"
        elif total > 0:
            readiness = "partial"
        else:
            readiness = "empty"
        rows.append(
            {
                "Product": label,
                "product_key": product,
                "Hook": role_counts["hook"],
                "Main": role_counts["main"],
                "CTA": role_counts["cta"],
                "Total": total,
                "Readiness": readiness,
            }
        )
    return {
        **{key: index_payload.get(key) for key in ("index_path", "index_exists", "index_updated_at", "index_module_count", "error")},
        "thresholds": {"hook": min_hook, "main": min_main, "cta": min_cta},
        "rows": rows,
    }


load_module_product_readiness.clear = _load_module_product_readiness_cached.clear  # type: ignore[attr-defined]


def load_module_visual_readiness(library_dir: str, min_events: int) -> dict[str, Any]:
    return _load_module_visual_readiness_cached(module_index_signature(library_dir), min_events)


@st.cache_data(show_spinner=False, max_entries=16)
def _load_module_visual_readiness_cached(index_signature: tuple[str, int, int], min_events: int) -> dict[str, Any]:
    index_payload = _load_module_index_payload_cached(index_signature)
    modules = index_payload.get("modules", [])
    counts = {
        product: {
            "total": 0,
            "approved": 0,
            "passed": 0,
            "failed": 0,
            "not_run": 0,
            "zoom_ready_candidate_count": 0,
        }
        for product, _label in MODULE_PRODUCTS
    }
    min_events = max(1, int(min_events or 1))
    if isinstance(modules, list):
        for module in modules:
            product = str(module.get("product") or "")
            if product not in counts:
                continue
            row = counts[product]
            row["total"] += 1
            approved = str(module.get("quality_status") or "") in {"approved", "no_visual_events"}
            if approved:
                row["approved"] += 1
            status = module_visual_status(module.get("visual_validation_status"))
            row[status] += 1
            hits = int(score_float(module.get("visual_product_hits")) or 0)
            if approved and status == "passed" and hits >= min_events:
                row["zoom_ready_candidate_count"] += 1

    rows = []
    for product, label in MODULE_PRODUCTS:
        row = counts[product]
        total = row["total"]
        validated = row["passed"] + row["failed"]
        rows.append(
            {
                "Product": label,
                "product_key": product,
                "Total": total,
                "Approved": row["approved"],
                "Passed": row["passed"],
                "Failed": row["failed"],
                "Not Run": row["not_run"],
                "Visual Coverage %": round((validated / total) * 100.0, 1) if total else 0.0,
                "Zoom-ready Candidates": row["zoom_ready_candidate_count"],
                "Zoom Ready": row["zoom_ready_candidate_count"] >= min_events,
            }
        )
    return {
        **{key: index_payload.get(key) for key in ("index_path", "index_exists", "index_updated_at", "index_module_count", "error")},
        "min_events": min_events,
        "rows": rows,
    }


load_module_visual_readiness.clear = _load_module_visual_readiness_cached.clear  # type: ignore[attr-defined]


def module_visual_status(value: Any) -> str:
    status = str(value or "not_run").strip().lower()
    if status in {"passed", "failed", "not_run"}:
        return status
    return "not_run"


def load_module_library_rows(library_dir: str) -> dict[str, Any]:
    return _load_module_library_rows_cached(module_index_signature(library_dir))


@st.cache_data(show_spinner=False, max_entries=16)
def _load_module_library_rows_cached(index_signature: tuple[str, int, int]) -> dict[str, Any]:
    index_payload = _load_module_index_payload_cached(index_signature)
    modules = index_payload.get("modules", [])
    rows: list[dict[str, Any]] = []
    if isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            product_key = str(module.get("product") or "")
            source_date = module_source_date_value(module)
            row = {
                "module_id": str(module.get("module_id") or Path(str(module.get("file_path") or "")).stem),
                "product": MODULE_PRODUCT_LABELS.get(product_key, product_key),
                "product_key": product_key,
                "role": str(module.get("role") or ""),
                "source_date": source_date,
                "source_video": source_video_filename(module.get("source_video")),
                "duration": round(score_float(module.get("duration")) or 0.0, 2),
                "confidence": round(score_float(module.get("confidence")) or 0.0, 3),
                "quality_status": str(module.get("quality_status") or ""),
                "review_status": str(module.get("review_status") or ""),
                "boundary_mode": str(module.get("boundary_mode") or ""),
                "visual_validation_status": str(module.get("visual_validation_status") or "not_run"),
                "visual_product_hits": int(score_float(module.get("visual_product_hits")) or 0),
                "visual_product_confidence_max": round(score_float(module.get("visual_product_confidence_max")) or 0.0, 3),
                "visual_validation_reason": str(module.get("visual_validation_reason") or ""),
                "file_path": str(module.get("file_path") or ""),
                "sidecar_path": str(module.get("sidecar_path") or ""),
                "transcript_text": str(module.get("transcript_text") or ""),
            }
            row["_search_text"] = " ".join(
                [
                    row["module_id"],
                    row["source_video"],
                    row["transcript_text"],
                ]
            ).casefold()
            row["_label"] = f"{row['module_id']} | {row['source_date'] or '-'} | {row['role']}"
            rows.append(row)
    rows.sort(key=lambda row: (row["product"], row["source_date"], row["role"], row["module_id"]))
    filter_options = {
        "product": sorted(value for value in {row["product"] for row in rows} if value),
        "source_date": sorted(value for value in {row["source_date"] for row in rows} if value),
        "quality_status": sorted(value for value in {row["quality_status"] for row in rows} if value),
        "visual_validation_status": sorted(value for value in {row["visual_validation_status"] for row in rows} if value),
        "review_status": sorted(value for value in {row["review_status"] for row in rows} if value),
    }
    return {
        **{key: index_payload.get(key) for key in ("index_path", "index_exists", "index_updated_at", "index_module_count", "error")},
        "rows": rows,
        "filter_options": filter_options,
    }


load_module_library_rows.clear = _load_module_library_rows_cached.clear  # type: ignore[attr-defined]


def build_manifest_signature(output_dirs: tuple[str, ...]) -> tuple[tuple[str, int, int], ...]:
    return tuple(file_signature(Path(output_dir) / "manifest.json") for output_dir in output_dirs)


@st.cache_data(show_spinner=False, max_entries=32)
def _load_compliance_rows_cached(manifest_signature: tuple[tuple[str, int, int], ...]) -> dict[str, Any]:
    clip_rows: list[dict[str, Any]] = []

    for manifest_sig in manifest_signature:
        folder = Path(manifest_sig[0]).parent
        source_video, run_tag = split_output_folder_name(folder.name)
        for row in _manifest_rows_from_signature(manifest_sig):
            if not isinstance(row, dict):
                continue
            if not _manifest_row_has_compliance_fields(row):
                continue
            clip_rows.append(_build_compliance_clip_row(folder, source_video, run_tag, row, None))

    clip_rows.sort(key=lambda row: row["_checked_at_sort"], reverse=True)
    return {
        "clips": clip_rows,
        "violations": [],
        "summary": {
            "scanned": len(clip_rows),
            "passed": sum(1 for row in clip_rows if row.get("Passed")),
            "blocked": sum(1 for row in clip_rows if row.get("Blocked")),
            "auto_fixed": sum(1 for row in clip_rows if row.get("Auto Fixed")),
            "violation_count": sum(int(row.get("Violation Count") or 0) for row in clip_rows),
        },
    }


def load_compliance_rows(output_dirs: tuple[str, ...]) -> dict[str, Any]:
    return _load_compliance_rows_cached(build_manifest_signature(output_dirs))


load_compliance_rows.clear = _load_compliance_rows_cached.clear  # type: ignore[attr-defined]


def build_compliance_detail_signature(output_dir: str) -> tuple[tuple[str, int, int], ...]:
    folder = Path(output_dir)
    signatures = [file_signature(folder / "manifest.json")]
    signatures.extend(file_signature(path) for path in _iter_compliance_files(folder))
    return tuple(signatures)


@st.cache_data(show_spinner=False, max_entries=64)
def _load_compliance_detail_rows_cached(detail_signature: tuple[tuple[str, int, int], ...]) -> dict[str, Any]:
    manifest_sig = detail_signature[0] if detail_signature else ("", 0, 0)
    folder = Path(manifest_sig[0]).parent
    source_video, run_tag = split_output_folder_name(folder.name)
    clip_rows: list[dict[str, Any]] = []
    violation_rows: list[dict[str, Any]] = []
    seen_compliance_files: set[str] = set()

    for row in _manifest_rows_from_signature(manifest_sig):
        if not isinstance(row, dict):
            continue
        compliance_path = _resolve_compliance_path(folder, row)
        result = _read_compliance_result(compliance_path) if _manifest_row_needs_compliance_file(row) else None
        if result is None and not _manifest_row_has_compliance_fields(row):
            continue
        if compliance_path:
            seen_compliance_files.add(_path_key(compliance_path))
        clip_record = _build_compliance_clip_row(folder, source_video, run_tag, row, result)
        clip_rows.append(clip_record)
        for violation in (result or {}).get("violations", []):
            if isinstance(violation, dict):
                violation_rows.append(_build_violation_row(clip_record, violation))

    for compliance_path in _iter_compliance_files(folder):
        key = _path_key(compliance_path)
        if key in seen_compliance_files:
            continue
        result = _read_compliance_result(compliance_path)
        if result is None:
            continue
        clip_id = compliance_path.stem.removesuffix("_compliance")
        try:
            relative_path = str(compliance_path.relative_to(folder)).replace("\\", "/")
        except ValueError:
            relative_path = str(compliance_path)
        row = {
            "clip_id": clip_id,
            "product": "general",
            "status": "unknown",
            "compliance_file": relative_path,
        }
        clip_record = _build_compliance_clip_row(folder, source_video, run_tag, row, result)
        clip_rows.append(clip_record)
        for violation in result.get("violations", []):
            if isinstance(violation, dict):
                violation_rows.append(_build_violation_row(clip_record, violation))

    clip_rows.sort(key=lambda row: row["_checked_at_sort"], reverse=True)
    violation_rows.sort(key=lambda row: row["_checked_at_sort"], reverse=True)
    return {"clips": clip_rows, "violations": violation_rows}


def load_compliance_detail_rows(output_dir: str) -> dict[str, Any]:
    return _load_compliance_detail_rows_cached(build_compliance_detail_signature(output_dir))


load_compliance_detail_rows.clear = _load_compliance_detail_rows_cached.clear  # type: ignore[attr-defined]


def _manifest_row_has_compliance_fields(row: dict[str, Any]) -> bool:
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


def _manifest_row_needs_compliance_file(row: dict[str, Any]) -> bool:
    if not _manifest_row_has_compliance_fields(row):
        return True
    if row.get("compliance_passed") is False or row.get("compliance_blocked"):
        return True
    return int(score_float(row.get("violation_count")) or 0) > 0


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _iter_compliance_files(folder: Path):
    patterns = ("*_compliance.json", "v*/*_compliance.json", "compliance/*_compliance.json")
    for pattern in patterns:
        try:
            yield from folder.glob(pattern)
        except OSError:
            continue


def _manifest_rows_from_signature(signature: tuple[str, int, int]) -> list[dict[str, Any]]:
    payload = load_json_payload_by_signature(*signature)
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("clips", "items"):
            if isinstance(payload.get(key), list):
                return [row for row in payload[key] if isinstance(row, dict)]
    return []


def _load_manifest_rows(folder: Path) -> list[dict[str, Any]]:
    return _manifest_rows_from_signature(file_signature(folder / "manifest.json"))


def _resolve_compliance_path(folder: Path, row: dict[str, Any]) -> Path | None:
    compliance_file = str(row.get("compliance_file") or row.get("compliance_json") or "").strip()
    candidates = []
    if compliance_file:
        path = Path(compliance_file)
        if path.is_absolute():
            candidates.append(path)
        else:
            candidates.append(folder / path)
            candidates.append(path)
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


def _read_compliance_result(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    payload = load_json_dict_by_signature(file_signature(path))
    return payload if isinstance(payload, dict) else None


def _build_compliance_clip_row(
    folder: Path,
    source_video: str,
    run_tag: str,
    row: dict[str, Any],
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    checked_at = str(
        (result or {}).get("checked_at")
        or row.get("compliance_checked_at")
        or row.get("checked_at")
        or row.get("completed_at")
        or ""
    )
    violation_count = int((result or {}).get("violation_count", row.get("violation_count") or 0) or 0)
    compliance_file = str(row.get("compliance_file") or row.get("compliance_json") or "")
    return {
        "Source Video": source_video,
        "Run Tag": run_tag,
        "Clip ID": row.get("clip_id", ""),
        "Product": row.get("product", "general") or "general",
        "Status": row.get("status", ""),
        "Passed": bool((result or {}).get("passed", row.get("compliance_passed", False))),
        "Blocked": bool((result or {}).get("blocked", row.get("compliance_blocked", False))),
        "Auto Fixed": bool((result or {}).get("auto_fixed", row.get("auto_fixed", False))),
        "Violation Count": violation_count,
        "Summary": (result or {}).get("compliance_summary", row.get("compliance_summary", "")),
        "Compliance File": compliance_file,
        "Output Dir": str(folder),
        "Checked At": checked_at,
        "_checked_at_sort": parse_timestamp(checked_at) or MIN_SORT_TIMESTAMP,
        "_raw": result or {},
    }


def _build_violation_row(clip_record: dict[str, Any], violation: dict[str, Any]) -> dict[str, Any]:
    position = violation.get("position") if isinstance(violation.get("position"), dict) else {}
    return {
        "Source Video": clip_record.get("Source Video", ""),
        "Run Tag": clip_record.get("Run Tag", ""),
        "Clip ID": clip_record.get("Clip ID", ""),
        "Product": clip_record.get("Product", "general"),
        "Field": str(violation.get("source_field") or "transcript"),
        "Severity": str(violation.get("severity") or ""),
        "Violation Type": str(violation.get("violation_type") or ""),
        "Original Text": str(violation.get("original_text") or ""),
        "Suggested Replacement": str(violation.get("suggested_replacement") or ""),
        "Start": position.get("start"),
        "End": position.get("end"),
        "Compliance File": clip_record.get("Compliance File", ""),
        "Output Dir": clip_record.get("Output Dir", ""),
        "Checked At": clip_record.get("Checked At", ""),
        "_checked_at_sort": clip_record.get("_checked_at_sort", MIN_SORT_TIMESTAMP),
    }


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
    source_date = score_source_date_value(source_video)
    output_dir_value = str(group.get("output_dir") or "")
    base_flags = score_flags_list(group.get("flags", []))
    base_flag_severity = score_flag_severity(base_flags)
    base_total_score = score_float(group.get("total_score"))
    base_quality_score = score_float(group.get("quality_score"))
    base_compliance_blocked = bool(group.get("compliance_blocked", False))
    variant_search_text = " ".join(
        str(item.get("clip_id", "")) + " " + str(item.get("output_file", ""))
        for item in group.get("variants", [])
        if isinstance(item, dict)
    )
    base_row = {
        "Source Video": source_video,
        "Run Tag": run_tag,
        "Source Date": source_date,
        "Clip ID": group.get("base_clip_id") or group.get("clip_id", ""),
        "Product": group.get("product", "general") or "general",
        "Product Bucket": trend_product_bucket(group.get("product", "general")),
        "Total Score": base_total_score,
        "Content": score_float(group.get("content_score")),
        "Host Focus": score_float(group.get("host_focus_score")),
        "Hook": score_float(group.get("hook_score")),
        "H/H": score_pair_format(group.get("host_focus_score"), group.get("hook_score")),
        "Quality": base_quality_score,
        "Engagement": score_float(group.get("engagement_score")),
        "Similarity": score_float(group.get("average_similarity_score")),
        "Variants": int(score_float(group.get("variant_count")) or 0),
        "Flags": score_flags_text(base_flags),
        "Flag Count": len(base_flags),
        "Flag Severity": base_flag_severity,
        "Flags Label": score_flags_label(len(base_flags)),
        "Quality Label": score_quality_label(base_quality_score),
        "Status": score_status_label(base_total_score, base_flag_severity, base_compliance_blocked),
        "Compliance Blocked": base_compliance_blocked,
        "Summary": group.get("summary", ""),
        "Output File": group.get("representative_output_file", ""),
        "Clip Path": group.get("representative_clip_path", ""),
        "Variant Clips": variant_search_text,
        "Exported": bool(group.get("exported", True)),
        "Scored At": scored_at,
        "_scored_at_sort": parse_timestamp(scored_at) or MIN_SORT_TIMESTAMP,
        "_score_key": base_key,
        "_base_score_key": base_key,
        "_base_clip_id": group.get("base_clip_id") or group.get("clip_id", ""),
        "_row_type": "base",
        "_variant_index": -1,
        "_output_dir": output_dir_value,
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
        variant_flag_list = score_flags_list(variant_flags)
        variant_flag_severity = score_flag_severity(variant_flag_list)
        variant_blocked = bool(variant.get("compliance_blocked", group.get("compliance_blocked", False)))
        rows.append(
            {
                "Source Video": source_video,
                "Run Tag": run_tag,
                "Source Date": source_date,
                "Clip ID": variant.get("clip_id", ""),
                "Product": group.get("product", "general") or "general",
                "Product Bucket": trend_product_bucket(group.get("product", "general")),
                "Total Score": base_total_score,
                "Content": score_float(group.get("content_score")),
                "Host Focus": score_float(group.get("host_focus_score")),
                "Hook": score_float(group.get("hook_score")),
                "H/H": score_pair_format(group.get("host_focus_score"), group.get("hook_score")),
                "Quality": base_quality_score,
                "Engagement": score_float(group.get("engagement_score")),
                "Similarity": score_float(variant.get("similarity_score")),
                "Variants": None,
                "Flags": score_flags_text(variant_flags) or "inherits base",
                "Flag Count": len(variant_flag_list),
                "Flag Severity": variant_flag_severity,
                "Flags Label": score_flags_label(len(variant_flag_list)),
                "Quality Label": score_quality_label(base_quality_score),
                "Status": score_status_label(base_total_score, variant_flag_severity, variant_blocked),
                "Compliance Blocked": variant_blocked,
                "Summary": group.get("summary", ""),
                "Output File": variant.get("output_file", ""),
                "Clip Path": variant.get("clip_path", ""),
                "Variant Clips": "",
                "Exported": bool(variant.get("exported", group.get("exported", True))),
                "Scored At": variant_scored_at,
                "_scored_at_sort": parse_timestamp(variant_scored_at) or MIN_SORT_TIMESTAMP,
                "_score_key": build_score_key(variant),
                "_base_score_key": base_key,
                "_base_clip_id": group.get("base_clip_id") or group.get("clip_id", ""),
                "_row_type": "variant",
                "_variant_index": int(score_float(variant.get("variant_index")) or 0),
                "_output_dir": str(variant.get("output_dir") or output_dir_value),
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
        if not folder.is_dir():
            continue
        if not (
            (folder / "scores_summary.json").exists()
            or (folder / "manifest.json").exists()
            or any(folder.glob("*_compliance.json"))
            or any(folder.glob("v*/*_compliance.json"))
            or any(folder.glob("compliance/*_compliance.json"))
        ):
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
            image_path = json_path.with_suffix(".jpg")
            clip_id = json_path.name.removesuffix("_focus_debug.json")
            signature = file_signature(json_path)
            rows.append(
                {
                    "Source Video": source_video,
                    "Run Tag": run_tag,
                    "Clip ID": clip_id,
                    "Image Path": str(image_path),
                    "JSON Path": str(json_path),
                    "Frames": "",
                    "_sort_key": signature[1],
                }
            )
    rows.sort(key=lambda item: (item["_sort_key"], item["Clip ID"]), reverse=True)
    return rows


@st.cache_data(show_spinner=False, max_entries=128)
def _load_focus_debug_detail_cached(signature: tuple[str, int, int]) -> list[dict[str, Any]]:
    payload = load_json_list_by_signature(signature)
    return [row for row in payload if isinstance(row, dict)]


def load_focus_debug_detail(json_path: str) -> list[dict[str, Any]]:
    return _load_focus_debug_detail_cached(file_signature(json_path))


load_focus_debug_detail.clear = _load_focus_debug_detail_cached.clear  # type: ignore[attr-defined]


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
    live_count = get_live_clip_count(run)
    if live_count > 0:
        return live_count
    return load_manifest_clip_count(run.get("output_dir"))


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


@st.cache_data(ttl=3, show_spinner=False)
def get_system_stats() -> dict[str, Any]:
    cpu_percent = psutil.cpu_percent(interval=None)
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
    if status == "paused":
        return "Paused"
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
    clip_events: list[tuple[datetime, int]] = []
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
                clip_events.append((run_clip_time, run_clips))
                bucket = floor_to_hour(run_clip_time)
                timeline_points.append({"timestamp": bucket, "clips": run_clips})
                hourly_clips[run_clip_time.hour] += run_clips
                minute_clip_buckets[floor_to_minute(run_clip_time)] += run_clips
                hour_clip_buckets[bucket] += run_clips

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
    clips_last_24h = sum_clip_events_since(clip_events, now - timedelta(days=1))
    clips_per_day = float(clips_last_24h)

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
        "clips_last_24h": clips_last_24h,
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
        "star": (
            "<path d='m12 3.8 2.35 4.76 5.25.76-3.8 3.7.9 5.23L12 15.78l-4.7 2.47.9-5.23-3.8-3.7 5.25-.76L12 3.8z'></path>"
        ),
        "shield": (
            "<path d='M12 3.8 19 6.5v5.2c0 4.2-2.8 7.1-7 8.5-4.2-1.4-7-4.3-7-8.5V6.5L12 3.8z'></path>"
            "<path d='M9.1 12.1h5.8'></path>"
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


def render_mobile_overview_strip(summary: dict[str, Any]) -> None:
    running_total = sum(int(value or 0) for value in summary["stage_running"].values())
    queued_total = sum(int(value or 0) for value in summary["stage_queued"].values())
    queue_label = "Running" if running_total else ("Queued" if queued_total else "Idle")
    items = [
        ("Clips / 24h", f"{summary['clips_last_24h']:,}", "Produced today"),
        ("Queue", queue_label, f"{running_total} running, {queued_total} queued"),
        ("Waiting", f"{summary['status_counts'].get('Waiting', 0):,}", "Videos"),
        ("Failed", f"{summary['status_counts'].get('Failed', 0):,}", "Needs attention"),
    ]
    cards = []
    for label, value, sub in items:
        cards.append(
            "<div class='mobile-card-stat'>"
            f"<div class='mobile-card-stat-label'>{html.escape(label)}</div>"
            f"<div class='mobile-card-stat-value'>{html.escape(str(value))}</div>"
            f"<div class='mobile-card-meta'>{html.escape(str(sub))}</div>"
            "</div>"
        )
    st.markdown(f"<div class='mobile-overview-strip'>{''.join(cards)}</div>", unsafe_allow_html=True)


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
    if label not in {"Disk", "GPU"} and trailing:
        st.markdown(f"<div class='small-muted'>{trailing}</div>", unsafe_allow_html=True)


def queue_fill_ratio(queued_count: int, running_count: int) -> float:
    active_count = queued_count + running_count
    if active_count <= 0:
        return 0.0
    return max(0.0, min(queued_count / active_count, 1.0))


def dashboard_nav_items() -> list[tuple[str, str, str]]:
    items = [
        ("overview", "Overview", "home"),
        ("videos", "Videos", "video"),
        ("analytics", "Analytics", "chart"),
        ("scores", "Scores", "check-circle"),
        ("compliance", "Compliance", "alert-circle"),
        ("modules", "Modules", "grid"),
        ("trends", "Trends", "chart"),
        ("queues", "Queues", "list"),
        ("settings", "Settings", "gear"),
    ]
    if scorer_vision_debug_enabled():
        items.insert(5, ("focus_debug", "Focus Debug", "focus"))
    return items


def render_nav_controls(active_tab: str) -> None:
    items = dashboard_nav_items()
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
                st.session_state.mobile_nav_select = tab_key
                st.rerun()


def render_mobile_nav_controls(active_tab: str) -> None:
    items = dashboard_nav_items()
    tab_keys = [tab_key for tab_key, _label, _icon_name in items]
    labels = {tab_key: label for tab_key, label, _icon_name in items}
    if active_tab not in tab_keys:
        active_tab = tab_keys[0]
    if st.session_state.get("mobile_nav_select") not in tab_keys:
        st.session_state.mobile_nav_select = active_tab
    st.markdown(
        "<div class='mobile-nav-anchor'></div><div class='mobile-nav-title'>Dashboard Section</div>",
        unsafe_allow_html=True,
    )
    selected_tab = st.selectbox(
        "Dashboard section",
        tab_keys,
        format_func=lambda key: labels.get(key, key.title()),
        key="mobile_nav_select",
        label_visibility="collapsed",
    )
    if selected_tab != active_tab:
        st.session_state.active_tab = selected_tab
        st.rerun()


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
                load_json_payload_by_signature.clear()
                load_manifest_clip_count.clear()
                load_score_rows.clear()
                load_scorer_stats.clear()
                load_score_trend_payload.clear()
                load_compliance_rows.clear()
                load_compliance_detail_rows.clear()
                invalidate_scores_session_cache()
                load_focus_debug_rows.clear()
                load_focus_debug_detail.clear()
                get_system_stats.clear()
                load_module_index_payload.clear()
                load_module_product_readiness.clear()
                load_module_visual_readiness.clear()
                load_module_library_rows.clear()
                _discover_score_output_dirs_cached.clear()
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
                    load_json_payload_by_signature.clear()
                    load_manifest_clip_count.clear()
                    load_score_rows.clear()
                    load_scorer_stats.clear()
                    load_score_trend_payload.clear()
                    load_compliance_rows.clear()
                    load_compliance_detail_rows.clear()
                    invalidate_scores_session_cache()
                    load_focus_debug_rows.clear()
                    load_focus_debug_detail.clear()
                    get_system_stats.clear()
                    load_module_index_payload.clear()
                    load_module_product_readiness.clear()
                    load_module_visual_readiness.clear()
                    load_module_library_rows.clear()
                    _discover_score_output_dirs_cached.clear()
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
    render_video_mobile_cards(page_df)

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


def render_modules_tab(summary: dict[str, Any]) -> None:
    render_page_intro("Modules", "Raw module library readiness from the global index.")
    render_product_readiness_panel()
    st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)
    render_visual_readiness_panel()
    st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)
    render_module_library_panel()


def render_product_readiness_panel() -> None:
    min_hook = int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_HOOK", 5) or 5)
    min_main = int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_MAIN", 3) or 3)
    min_cta = int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_CTA", 3) or 3)
    readiness = load_module_product_readiness(
        str(getattr(cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules")),
        min_hook,
        min_main,
        min_cta,
    )
    rows = readiness.get("rows", [])
    thresholds = readiness.get("thresholds", {"hook": min_hook, "main": min_main, "cta": min_cta})

    with st.container(border=True):
        header_cols = st.columns([2.2, 1.0], gap="medium")
        with header_cols[0]:
            st.markdown("<div class='panel-title'>Product Readiness</div>", unsafe_allow_html=True)
            st.markdown(
                (
                    "<div class='small-muted'>"
                    f"Ready threshold: hook >= {thresholds['hook']}, "
                    f"main >= {thresholds['main']}, cta >= {thresholds['cta']}."
                    "</div>"
                ),
                unsafe_allow_html=True,
            )
        with header_cols[1]:
            if st.button("Refresh", key="modules_readiness_refresh", use_container_width=True):
                load_json_payload_by_signature.clear()
                load_module_index_payload.clear()
                load_module_product_readiness.clear()
                st.rerun(scope="fragment")

        if readiness.get("error"):
            st.warning(f"Could not read module index: {readiness['error']}")
        elif not readiness.get("index_exists"):
            st.info(f"No module index found at {readiness.get('index_path')}")
        else:
            st.caption(
                f"Index: {readiness.get('index_path')} | "
                f"Indexed modules: {int(readiness.get('index_module_count') or 0):,} | "
                f"Updated: {readiness.get('index_updated_at') or '-'}"
            )

        cards = []
        for row in rows:
            status = str(row.get("Readiness") or "empty")
            product = html.escape(str(row.get("Product") or ""))
            cards.append(
                "<div class='module-ready-card'>"
                "<div class='module-ready-head'>"
                f"<div class='module-ready-name'>{product}</div>"
                f"<div class='module-ready-badge {html.escape(status)}'>{html.escape(status)}</div>"
                "</div>"
                "<div class='module-ready-counts'>"
                "<div class='module-ready-role'>"
                "<div class='module-ready-role-label'>Hook</div>"
                f"<div class='module-ready-role-value'>{int(row.get('Hook') or 0):,}</div>"
                "</div>"
                "<div class='module-ready-role'>"
                "<div class='module-ready-role-label'>Main</div>"
                f"<div class='module-ready-role-value'>{int(row.get('Main') or 0):,}</div>"
                "</div>"
                "<div class='module-ready-role'>"
                "<div class='module-ready-role-label'>CTA</div>"
                f"<div class='module-ready-role-value'>{int(row.get('CTA') or 0):,}</div>"
                "</div>"
                "</div>"
                f"<div class='module-ready-foot'>All indexed modules: {int(row.get('Total') or 0):,}</div>"
                "</div>"
            )
        st.markdown(
            f"<div class='module-ready-grid'>{''.join(cards)}</div>",
            unsafe_allow_html=True,
        )

        table_rows = [
            {
                "Product": row["Product"],
                "Hook": row["Hook"],
                "Main": row["Main"],
                "CTA": row["CTA"],
                "Total": row["Total"],
                "Readiness": str(row["Readiness"]).title(),
            }
            for row in rows
        ]
        st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)


def render_visual_readiness_panel() -> None:
    library_dir = str(getattr(cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules"))
    min_events = int(getattr(cfg, "MODULE_ASSEMBLY_ZOOM_READY_MIN_EVENTS", 1) or 1)
    readiness = load_module_visual_readiness(library_dir, min_events)
    rows = readiness.get("rows", [])

    with st.container(border=True):
        header_cols = st.columns([2.2, 1.0], gap="medium")
        with header_cols[0]:
            st.markdown("<div class='panel-title'>Visual Readiness</div>", unsafe_allow_html=True)
            st.markdown(
                (
                    "<div class='small-muted'>"
                    f"Zoom-ready threshold: at least {int(readiness.get('min_events') or min_events)} validated event."
                    "</div>"
                ),
                unsafe_allow_html=True,
            )
        with header_cols[1]:
            if st.button("Refresh", key="modules_visual_readiness_refresh", use_container_width=True):
                load_json_payload_by_signature.clear()
                load_module_index_payload.clear()
                load_module_visual_readiness.clear()
                load_module_library_rows.clear()
                st.rerun(scope="fragment")

        if readiness.get("error"):
            st.warning(f"Could not read module index: {readiness['error']}")
        elif not readiness.get("index_exists"):
            st.info(f"No module index found at {readiness.get('index_path')}")
        else:
            st.caption(
                f"Index: {readiness.get('index_path')} | "
                f"Indexed modules: {int(readiness.get('index_module_count') or 0):,} | "
                f"Updated: {readiness.get('index_updated_at') or '-'}"
            )

        cards = []
        for row in rows:
            product = html.escape(str(row.get("Product") or ""))
            status = "ready" if row.get("Zoom Ready") else ("partial" if int(row.get("Total") or 0) else "empty")
            coverage = float(row.get("Visual Coverage %") or 0.0)
            cards.append(
                "<div class='module-ready-card'>"
                "<div class='module-ready-head'>"
                f"<div class='module-ready-name'>{product}</div>"
                f"<div class='module-ready-badge {html.escape(status)}'>{html.escape(status)}</div>"
                "</div>"
                "<div class='module-ready-counts'>"
                "<div class='module-ready-role'>"
                "<div class='module-ready-role-label'>Passed</div>"
                f"<div class='module-ready-role-value'>{int(row.get('Passed') or 0):,}</div>"
                "</div>"
                "<div class='module-ready-role'>"
                "<div class='module-ready-role-label'>Failed</div>"
                f"<div class='module-ready-role-value'>{int(row.get('Failed') or 0):,}</div>"
                "</div>"
                "<div class='module-ready-role'>"
                "<div class='module-ready-role-label'>Not Run</div>"
                f"<div class='module-ready-role-value'>{int(row.get('Not Run') or 0):,}</div>"
                "</div>"
                "</div>"
                f"<div class='module-ready-foot'>Coverage {coverage:.1f}% | Zoom-ready {int(row.get('Zoom-ready Candidates') or 0):,}</div>"
                "</div>"
            )
        st.markdown(
            f"<div class='module-ready-grid'>{''.join(cards)}</div>",
            unsafe_allow_html=True,
        )

        table_rows = [
            {
                "Product": row["Product"],
                "Approved": row["Approved"],
                "Passed": row["Passed"],
                "Failed": row["Failed"],
                "Not Run": row["Not Run"],
                "Coverage %": row["Visual Coverage %"],
                "Zoom-ready": row["Zoom-ready Candidates"],
            }
            for row in rows
        ]
        st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)


def render_module_library_panel() -> None:
    library_dir = str(getattr(cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules"))
    payload = load_module_library_rows(library_dir)
    rows = payload.get("rows", [])

    with st.container(border=True):
        header_cols = st.columns([2.2, 1.0], gap="medium")
        with header_cols[0]:
            st.markdown("<div class='panel-title'>Module Library</div>", unsafe_allow_html=True)
            st.caption(
                f"Index: {payload.get('index_path')} | "
                f"Indexed modules: {int(payload.get('index_module_count') or 0):,} | "
                f"Updated: {payload.get('index_updated_at') or '-'}"
            )
        with header_cols[1]:
            if st.button("Refresh", key="modules_library_refresh", use_container_width=True):
                load_json_payload_by_signature.clear()
                load_module_index_payload.clear()
                load_module_product_readiness.clear()
                load_module_visual_readiness.clear()
                load_module_library_rows.clear()
                st.rerun(scope="fragment")

        if payload.get("error"):
            st.warning(f"Could not read module index: {payload['error']}")
        elif not payload.get("index_exists"):
            st.info(f"No module index found at {payload.get('index_path')}")
        if not rows:
            st.info("No modules are indexed yet.")
            return

        df = pd.DataFrame(rows)
        visual_counts = df["visual_validation_status"].value_counts().to_dict()
        st.caption(
            "Visual status: "
            f"passed={int(visual_counts.get('passed', 0)):,} | "
            f"failed={int(visual_counts.get('failed', 0)):,} | "
            f"not_run={int(visual_counts.get('not_run', 0)):,}"
        )
        filter_col, table_col = st.columns([0.9, 2.4], gap="medium")
        with filter_col:
            with st.container(border=True):
                st.markdown("<div class='panel-title' style='font-size:1rem;'>Filters</div>", unsafe_allow_html=True)
                search_term = st.text_input("Search", placeholder="Module, video, transcript...", key="modules_search")
                filter_options = payload.get("filter_options", {}) if isinstance(payload.get("filter_options"), dict) else {}
                product_options = list(filter_options.get("product") or sorted(value for value in df["product"].dropna().unique() if value))
                role_options = list(MODULE_ROLES)
                date_options = list(filter_options.get("source_date") or sorted(value for value in df["source_date"].dropna().unique() if value))
                quality_options = list(filter_options.get("quality_status") or sorted(value for value in df["quality_status"].dropna().unique() if value))
                visual_options = list(filter_options.get("visual_validation_status") or sorted(value for value in df["visual_validation_status"].dropna().unique() if value))
                selected_products = st.multiselect("Product", product_options, default=product_options, key="modules_product_filter")
                selected_roles = st.multiselect("Role", role_options, default=role_options, key="modules_role_filter")
                selected_dates = st.multiselect("source_date", date_options, default=date_options, key="modules_source_date_filter")
                include_undated = st.checkbox("Include undated", value=True, key="modules_include_undated")
                selected_quality = st.multiselect("Quality", quality_options, default=quality_options, key="modules_quality_filter")
                selected_visual = st.multiselect("Visual", visual_options, default=visual_options, key="modules_visual_filter")
                review_options = list(filter_options.get("review_status") or sorted(value for value in df["review_status"].dropna().unique() if value))
                selected_review = st.multiselect("Review", review_options, default=review_options, key="modules_review_filter")

        filtered = df.copy()
        if selected_products:
            filtered = filtered[filtered["product"].isin(selected_products)]
        else:
            filtered = filtered.iloc[0:0]
        if selected_roles:
            filtered = filtered[filtered["role"].isin(selected_roles)]
        else:
            filtered = filtered.iloc[0:0]
        if selected_dates or include_undated:
            date_mask = filtered["source_date"].isin(selected_dates)
            if include_undated:
                date_mask = date_mask | filtered["source_date"].eq("")
            filtered = filtered[date_mask]
        else:
            filtered = filtered.iloc[0:0]
        if selected_quality:
            filtered = filtered[filtered["quality_status"].isin(selected_quality)]
        elif quality_options:
            filtered = filtered.iloc[0:0]
        if selected_visual:
            filtered = filtered[filtered["visual_validation_status"].isin(selected_visual)]
        elif visual_options:
            filtered = filtered.iloc[0:0]
        if selected_review:
            filtered = filtered[filtered["review_status"].isin(selected_review)]
        elif review_options:
            filtered = filtered.iloc[0:0]
        if search_term:
            filtered = filtered[filtered["_search_text"].astype(str).str.contains(search_term.casefold(), na=False, regex=False)]
        filtered = filtered.reset_index(drop=True)

        with table_col:
            st.caption(f"Showing {len(filtered):,} of {len(df):,} modules")
            if filtered.empty:
                st.info("No modules match the current filters.")
                return
            if st.session_state.get("modules_rows") not in (50, 100, 200):
                st.session_state.modules_rows = 50
            page_size = int(st.session_state.get("modules_rows") or 50)
            total_rows = len(filtered)
            total_pages = max((total_rows - 1) // page_size + 1, 1)
            page = min(max(int(st.session_state.get("modules_page", 1)), 1), total_pages)
            st.session_state.modules_page = page
            start_idx = (page - 1) * page_size
            end_idx = min(start_idx + page_size, total_rows)
            page_df = filtered.iloc[start_idx:end_idx].reset_index(drop=True)
            pager_cols = st.columns([0.65, 0.8, 0.65, 1.9, 1.05], gap="small")
            with pager_cols[0]:
                if st.button("<", key="modules_prev", use_container_width=True, disabled=page <= 1):
                    st.session_state.modules_page = max(1, page - 1)
                    st.rerun(scope="fragment")
            with pager_cols[1]:
                st.caption(f"Page {page} / {total_pages}")
            with pager_cols[2]:
                if st.button(">", key="modules_next", use_container_width=True, disabled=page >= total_pages):
                    st.session_state.modules_page = min(total_pages, page + 1)
                    st.rerun(scope="fragment")
            with pager_cols[3]:
                st.caption(f"Showing {start_idx + 1} to {end_idx} of {total_rows} filtered")
            with pager_cols[4]:
                st.selectbox("Rows", [50, 100, 200], key="modules_rows", label_visibility="collapsed")
            display_cols = [
                "module_id",
                "product",
                "role",
                "source_date",
                "duration",
                "confidence",
                "quality_status",
                "review_status",
                "visual_validation_status",
                "visual_product_hits",
                "boundary_mode",
                "source_video",
            ]
            st.dataframe(page_df[display_cols], use_container_width=True, hide_index=True)

            labels = list(page_df["_label"].astype(str))
            if st.session_state.get("modules_selected_module") not in labels:
                st.session_state.modules_selected_module = labels[0]
            selected_label = st.selectbox("Selected module", labels, key="modules_selected_module")
            selected_index = labels.index(selected_label)
            render_module_detail_panel(page_df.iloc[selected_index])


def render_module_detail_panel(selected: pd.Series) -> None:
    with st.container(border=True):
        st.markdown("<div class='panel-title'>Selected Module</div>", unsafe_allow_html=True)
        metric_cols = st.columns(6, gap="medium")
        details = [
            ("Product", selected.get("product", "")),
            ("Role", selected.get("role", "")),
            ("source_date", selected.get("source_date", "") or "-"),
            ("Quality", selected.get("quality_status", "") or "-"),
            ("Visual", selected.get("visual_validation_status", "") or "-"),
            ("Duration", f"{score_float(selected.get('duration')) or 0.0:.2f}s"),
        ]
        for col, (label, value) in zip(metric_cols, details):
            with col:
                st.markdown(f"<div class='small-muted'>{html.escape(label)}</div>", unsafe_allow_html=True)
                st.markdown(f"**{html.escape(str(value))}**")
        st.caption(f"Source video: {selected.get('source_video', '') or '-'}")
        st.caption(
            "Visual: "
            f"hits={int(score_float(selected.get('visual_product_hits')) or 0)} | "
            f"max_conf={score_float(selected.get('visual_product_confidence_max')) or 0.0:.3f} | "
            f"reason={selected.get('visual_validation_reason', '') or '-'}"
        )
        st.caption(f"Media: {selected.get('file_path', '') or '-'}")
        if selected.get("sidecar_path"):
            st.caption(f"Sidecar: {selected.get('sidecar_path')}")
        transcript = str(selected.get("transcript_text") or "").strip()
        if transcript:
            st.markdown(f"<div class='small-muted'>{html.escape(transcript[:600])}</div>", unsafe_allow_html=True)
        media_path = Path(str(selected.get("file_path") or ""))
        if media_path.exists():
            module_key = hashlib.sha1(str(media_path).encode("utf-8", errors="ignore")).hexdigest()[:12]
            if st.session_state.get("module_preview_loaded_key") == module_key:
                st.video(str(media_path))
            elif st.button("Load module preview", key=f"module_load_preview_{module_key}", use_container_width=True):
                st.session_state.module_preview_loaded_key = module_key
                st.rerun(scope="fragment")
        render_module_review_controls(selected)


def render_module_review_controls(selected: pd.Series) -> None:
    module_id = str(selected.get("module_id") or "").strip()
    if not module_id:
        return
    st.divider()
    st.markdown("<div class='panel-title' style='font-size:1rem;'>Review</div>", unsafe_allow_html=True)
    current_quality = str(selected.get("quality_status") or "-")
    current_review = str(selected.get("review_status") or "-")
    st.caption(f"Current: quality={current_quality} | review={current_review}")
    reviewer_key = f"modules_reviewer_{module_id}"
    note_key = f"modules_review_note_{module_id}"
    reviewer = st.text_input("Reviewer", value="operator", key=reviewer_key)
    note = st.text_area("Note", value="", height=72, key=note_key)
    action_cols = st.columns(3, gap="small")
    actions = [
        ("Approve", "approved"),
        ("Needs Review", "needs_review"),
        ("Block", "blocked"),
    ]
    for col, (label, status) in zip(action_cols, actions):
        with col:
            if st.button(label, key=f"modules_review_{status}_{module_id}", use_container_width=True):
                apply_module_review_from_dashboard(module_id, status, reviewer=reviewer, note=note)


def apply_module_review_from_dashboard(module_id: str, status: str, reviewer: str, note: str) -> None:
    try:
        from module_review import update_module_review

        result = update_module_review(
            module_id,
            status,
            cfg,
            note=note,
            reviewer=reviewer or "operator",
        )
    except Exception as exc:
        st.error(f"Review update failed: {exc}")
        return
    load_module_product_readiness.clear()
    load_module_visual_readiness.clear()
    load_module_library_rows.clear()
    load_module_index_payload.clear()
    load_json_payload_by_signature.clear()
    st.success(
        "Updated "
        f"{html.escape(str(result.get('module_id') or module_id))}: "
        f"{html.escape(str(result.get('quality_status') or status))}"
    )
    time.sleep(0.4)
    st.rerun()


def render_overview_tab(summary: dict[str, Any]) -> None:
    render_page_intro("Overview", "Live operational view across the whole VOD pipeline.")
    render_mobile_overview_strip(summary)
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
                ("Clips / 24h", f"{summary['clips_last_24h']:,}", "Last 24 hours"),
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
        ("Clips / 24h", f"{summary['clips_last_24h']:,}", "Last 24 hours"),
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
    render_scores_review_styles()
    output_dirs = collect_score_output_dirs(summary)
    scores_payload = load_scores_index_payload(output_dirs)
    score_rows = scores_payload.get("rows", [])
    score_df = pd.DataFrame(score_rows)
    if not score_df.empty:
        score_df = score_df.copy()
        warn_if_uniform_hook_scores(score_df)

    product_options = ["All Products"] + TREND_PRODUCTS
    flag_options = ["All Flags"] + all_score_flags(score_df)
    ensure_scores_filter_state(product_options, flag_options)

    filtered_all_dates = filter_score_dataframe(score_df, today_only=False)
    filtered = filter_score_dataframe(score_df, today_only=bool(st.session_state.get("scores_today_only", False)))
    filtered = sort_score_dataframe(filtered)
    export_signature = build_scores_export_signature(filtered, scores_payload.get("signature", ()))

    header_cols = st.columns([2.55, 1.45], gap="medium")
    with header_cols[0]:
        render_page_intro("Scores", "Review clip quality and variants faster.")
    with header_cols[1]:
        action_cols = st.columns([0.95, 1.35], gap="small")
        with action_cols[0]:
            if st.button("Refresh", key="scores_refresh", use_container_width=True):
                load_json_payload_by_signature.clear()
                invalidate_scores_session_cache()
                load_score_rows.clear()
                load_scorer_stats.clear()
                load_score_trend_payload.clear()
                _discover_score_output_dirs_cached.clear()
                st.rerun(scope="fragment")
        with action_cols[1]:
            export_cache = get_scores_export_cache(export_signature)
            if export_cache:
                st.download_button(
                    "Export Report",
                    data=export_cache["data"],
                    file_name=export_cache["file_name"],
                    mime="text/csv",
                    key="scores_export_report",
                    use_container_width=True,
                )
            elif st.button("Export Report", key="scores_export_prepare", use_container_width=True):
                st.session_state[SCORES_EXPORT_CACHE_KEY] = {
                    "signature": export_signature,
                    "data": build_scores_export_csv(filtered),
                    "file_name": f"scores_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                }
                st.rerun(scope="fragment")

    if not score_rows:
        with st.container(border=True):
            st.info("No scores found yet. New render batches will write scores_summary.json automatically.")
        return

    expanded_base_key = str(st.session_state.get("expanded_score_base_key", ""))
    visible_base_keys = set(filtered["_base_score_key"].astype(str))
    if expanded_base_key and expanded_base_key not in visible_base_keys:
        expanded_base_key = ""
        st.session_state.expanded_score_base_key = ""

    selected_key = str(st.session_state.get("selected_score_key", ""))
    selected = resolve_selected_score_row(score_df, selected_key, None) if selected_key else None

    left_panel, right_panel = st.columns([0.65, 0.35], gap="medium")
    with left_panel:
        render_scores_kpi_cards(filtered, filtered_all_dates)
        render_scores_filter_bar(product_options, flag_options)
        st.markdown("<div class='score-filter-table-gap'></div>", unsafe_allow_html=True)
        render_scores_table_toolbar(filtered)
        if filtered.empty:
            with st.container(border=True):
                st.info("No scored clips match the current filters.")
        else:
            selected_key = render_scores_click_table(filtered, selected_key, score_df)
            selected = resolve_selected_score_row(score_df, selected_key, None) if selected_key else selected

    with right_panel:
        st.markdown("<div class='desktop-score-detail-anchor'></div>", unsafe_allow_html=True)
        render_score_detail_panel(selected, score_df)


def ensure_scores_filter_state(product_options: list[str], flag_options: list[str]) -> None:
    if "scores_today_only" not in st.session_state:
        st.session_state.scores_today_only = False
    if st.session_state.get("scores_product_filter") not in product_options:
        st.session_state.scores_product_filter = "All Products"
    if "scores_search" not in st.session_state:
        st.session_state.scores_search = ""
    score_range = st.session_state.get("scores_score_range")
    if not isinstance(score_range, (list, tuple)) or len(score_range) != 2:
        st.session_state.scores_score_range = (0.0, 10.0)
    if st.session_state.get("scores_flag_filter") not in flag_options:
        st.session_state.scores_flag_filter = "All Flags"
    status_options = score_status_filter_options()
    if st.session_state.get("scores_status_filter") not in status_options:
        st.session_state.scores_status_filter = "All Statuses"
    sort_options = score_sort_options()
    current_sort = st.session_state.get("scores_sort_preset")
    if current_sort not in sort_options:
        st.session_state.scores_sort_preset = "Scored At"
    elif (
        st.session_state.get("scores_default_sort_version") != SCORES_DEFAULT_SORT_VERSION
        and current_sort == "Score High to Low"
    ):
        st.session_state.scores_sort_preset = "Scored At"
    st.session_state.scores_default_sort_version = SCORES_DEFAULT_SORT_VERSION
    if st.session_state.get("scores_rows") not in (50, 100, 200):
        st.session_state.scores_rows = 50


def warn_if_uniform_hook_scores(score_df: pd.DataFrame) -> None:
    if score_df.empty or "Hook" not in score_df:
        return
    base_rows = score_df
    if "_row_type" in score_df:
        base_rows = score_df[score_df["_row_type"].astype(str) == "base"]
    hook_scores = [score_float(value) for value in base_rows["Hook"].tolist()]
    hook_scores = [score for score in hook_scores if score is not None]
    unique_scores = {round(float(score), 4) for score in hook_scores}
    if len(hook_scores) <= 1 or len(unique_scores) != 1:
        return
    score_value = next(iter(unique_scores))
    warning_key = f"{len(hook_scores)}::{score_value:.4f}"
    if st.session_state.get("scores_hook_uniform_warning") == warning_key:
        return
    LOGGER.warning(
        "All %s scored base clips have the same hook score (%s); this may indicate a scoring data issue.",
        len(hook_scores),
        f"{score_value:.2f}",
    )
    st.session_state.scores_hook_uniform_warning = warning_key


def render_scores_review_styles() -> None:
    st.markdown(
        """
        <style>
        .score-kpi-grid {
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 0.55rem;
            margin-bottom: 0.75rem;
        }
        .score-kpi-card {
            min-height: 96px;
            border: 1px solid rgba(148, 163, 184, 0.14);
            border-left: 3px solid var(--score-kpi-accent, #3b82f6);
            border-radius: 8px;
            background: linear-gradient(180deg, rgba(15, 23, 42, 0.78), rgba(8, 15, 27, 0.72));
            padding: 0.72rem 0.68rem 0.72rem 0.78rem;
            min-width: 0;
        }
        .score-kpi-icon {
            width: 26px;
            height: 26px;
            border-radius: 8px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 0.42rem;
        }
        .score-kpi-label {
            color: #cbd5e1;
            font-size: 0.68rem;
            font-weight: 700;
            line-height: 1.1;
            word-break: normal;
        }
        .score-kpi-value {
            color: #f8fafc;
            font-size: 28px;
            font-weight: 800;
            line-height: 1.05;
            margin-top: 0.25rem;
        }
        .score-kpi-sub {
            font-size: 0.72rem;
            margin-top: 0.35rem;
            line-height: 1.15;
        }
        .score-review-shell {
            border: 1px solid rgba(148, 163, 184, 0.16);
            border-radius: 8px;
            overflow: hidden;
            background: rgba(2, 8, 23, 0.24);
        }
        .score-review-header {
            display: grid;
            grid-template-columns: 0.22fr 1.12fr 1.02fr 0.74fr 0.62fr 0.72fr 0.68fr 0.46fr;
            gap: 0.46rem;
            align-items: center;
            color: #94a3b8;
            font-size: 0.74rem;
            font-weight: 800;
            padding: 0.5rem 0.58rem;
            border-bottom: 1px solid rgba(148, 163, 184, 0.14);
        }
        .score-review-cell {
            font-size: 0.78rem;
            line-height: 1.12;
            overflow: hidden;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            word-break: break-word;
            padding-top: 0.18rem;
        }
        .score-table-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 3.4rem;
            border-radius: 999px;
            padding: 0.18rem 0.44rem;
            border: 1px solid rgba(148, 163, 184, 0.12);
            font-weight: 800;
            font-size: 0.72rem;
            font-variant-numeric: tabular-nums;
        }
        .score-table-row-marker + div[data-testid="stHorizontalBlock"] {
            border-left: 2px solid transparent;
            padding: 0.08rem 0 0.08rem 0.18rem;
            border-radius: 6px;
        }
        div[data-testid="stElementContainer"]:has(.score-table-row-marker) + div[data-testid="stLayoutWrapper"] {
            border-left: 2px solid transparent;
            padding: 0.08rem 0 0.08rem 0.18rem;
            border-radius: 6px;
        }
        .score-table-row-marker.is-selected + div[data-testid="stHorizontalBlock"] {
            border-left-color: #7c3aed;
            background: rgba(99, 102, 241, 0.055);
        }
        div[data-testid="stElementContainer"]:has(.score-table-row-marker.is-selected) + div[data-testid="stLayoutWrapper"] {
            border-left-color: #7c3aed;
            background: rgba(99, 102, 241, 0.055);
        }
        .score-variant-row-marker + div[data-testid="stHorizontalBlock"] {
            padding: 0.12rem 0.2rem;
            border-radius: 6px;
            border-left: 2px solid transparent;
        }
        div[data-testid="stElementContainer"]:has(.score-variant-row-marker) + div[data-testid="stLayoutWrapper"] {
            padding: 0.12rem 0.2rem;
            border-radius: 6px;
            border-left: 2px solid transparent;
        }
        .score-variant-row-marker.is-alt + div[data-testid="stHorizontalBlock"] {
            background: rgba(148, 163, 184, 0.055);
        }
        div[data-testid="stElementContainer"]:has(.score-variant-row-marker.is-alt) + div[data-testid="stLayoutWrapper"] {
            background: rgba(148, 163, 184, 0.055);
        }
        .score-variant-row-marker.is-selected + div[data-testid="stHorizontalBlock"] {
            border-left-color: #7c3aed;
            background: rgba(99, 102, 241, 0.075);
        }
        div[data-testid="stElementContainer"]:has(.score-variant-row-marker.is-selected) + div[data-testid="stLayoutWrapper"] {
            border-left-color: #7c3aed;
            background: rgba(99, 102, 241, 0.075);
        }
        .score-filter-table-gap {
            height: 16px;
        }
        div[data-testid="stElementContainer"]:has(.score-today-button-marker) + div[data-testid="stElementContainer"] button {
            border-color: rgba(148, 163, 184, 0.28);
            background: rgba(15, 23, 42, 0.24);
            color: #edf3ff;
        }
        div[data-testid="stElementContainer"]:has(.score-today-button-marker.is-active) + div[data-testid="stElementContainer"] button {
            border-color: rgba(124, 58, 237, 0.82);
            background: linear-gradient(135deg, rgba(37, 99, 235, 0.88), rgba(124, 58, 237, 0.88));
            color: #ffffff;
            box-shadow: 0 8px 22px rgba(59, 130, 246, 0.18);
        }
        .score-detail-title-row {
            display:flex;
            align-items:flex-start;
            justify-content:space-between;
            gap:0.8rem;
            margin-bottom:0.45rem;
        }
        .score-detail-title {
            color:#f8fafc;
            font-size:1.1rem;
            font-weight:800;
            line-height:1.15;
        }
        .score-detail-product {
            color:#cbd5e1;
            font-size:0.88rem;
            margin-top:0.18rem;
        }
        .score-meta-line {
            color:#94a3b8;
            font-size:0.78rem;
            margin-bottom:0.75rem;
        }
        .score-dimension-row {
            display:grid;
            grid-template-columns: 5.8rem minmax(0, 1fr) 3rem;
            gap:12px;
            align-items:center;
            margin:0 0 12px 0;
        }
        .score-dimension-label {
            color:#cbd5e1;
            font-size:0.78rem;
        }
        .score-dimension-track {
            height:8px;
            border-radius:4px;
            background:rgba(148, 163, 184, 0.16);
            overflow:hidden;
        }
        .score-dimension-fill {
            height:100%;
            border-radius:4px;
        }
        .score-dimension-value {
            color:#e2e8f0;
            font-size:0.78rem;
            text-align:right;
            font-variant-numeric:tabular-nums;
        }
        .score-transcript-box {
            max-height:340px;
            overflow-y:auto;
            border:1px solid rgba(148, 163, 184, 0.14);
            border-radius:8px;
            padding:0.82rem;
            background:rgba(2, 8, 23, 0.22);
            color:#dbeafe;
            font-size:0.86rem;
            line-height:1.55;
        }
        .score-price-hit {
            background:rgba(250, 204, 21, 0.28);
            color:#fef9c3;
            border-radius:4px;
            padding:0.02rem 0.16rem;
        }
        .score-violation-hit {
            background:rgba(239, 68, 68, 0.30);
            color:#fee2e2;
            border-radius:4px;
            padding:0.02rem 0.16rem;
        }
        div[data-testid="stColumn"]:has(.score-detail-anchor) {
            position: sticky;
            top: 0.75rem;
            align-self: flex-start;
            border-left: 1px solid rgba(148, 163, 184, 0.16);
            padding-left: 1rem;
        }
        div[data-testid="stColumn"]:has(.score-detail-anchor) video[data-testid="stVideo"] {
            max-height: 220px;
            object-fit: contain;
            background: #020617;
            width: 100%;
        }
        div[data-testid="stColumn"]:has(.score-detail-anchor) video[data-testid="stVideo"] {
            margin-bottom: 0.25rem;
        }
        div[data-testid="stColumn"]:has(.score-detail-anchor) button[data-baseweb="tab"][aria-selected="true"] {
            border-bottom: 2px solid #7c3aed;
            color: #dbeafe;
        }
        div[data-testid="stColumn"]:has(.score-detail-anchor) button[data-baseweb="tab"] {
            padding-bottom: 0.42rem;
        }
        @media (max-width: 760px) {
            .score-kpi-grid {
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 0.5rem;
            }
            .score-kpi-card {
                min-height: 88px;
                padding: 0.64rem;
            }
            .score-kpi-value {
                font-size: 23px;
            }
            .score-review-shell,
            div[data-testid="stElementContainer"]:has(.score-table-row-marker),
            div[data-testid="stElementContainer"]:has(.score-table-row-marker) + div[data-testid="stLayoutWrapper"],
            div[data-testid="stElementContainer"]:has(.score-table-row-marker) + div[data-testid="stHorizontalBlock"],
            div[data-testid="stElementContainer"]:has(.score-variant-row-marker),
            div[data-testid="stElementContainer"]:has(.score-variant-row-marker) + div[data-testid="stLayoutWrapper"],
            div[data-testid="stElementContainer"]:has(.score-variant-row-marker) + div[data-testid="stHorizontalBlock"] {
                display: none !important;
            }
            .score-filter-table-gap {
                height: 8px;
            }
            .score-dimension-row {
                grid-template-columns: 4.6rem minmax(0, 1fr) 2.5rem;
                gap: 8px;
            }
            .score-transcript-box {
                max-height: 46vh;
                font-size: 0.82rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def score_sort_options() -> list[str]:
    return [
        "Scored At",
        "Score High to Low",
        "Score Low to High",
        "Hook Score",
        "Content Score",
    ]


def all_score_flags(score_df: pd.DataFrame) -> list[str]:
    if score_df.empty or "Flags" not in score_df:
        return []
    flags: set[str] = set()
    for value in score_df["Flags"]:
        flags.update(score_flags_list(value))
    return sorted(flags)


def score_status_filter_options() -> list[str]:
    return ["All Statuses", "Export Ready", "Review", "Okay", "Blocked"]


def filter_score_dataframe(score_df: pd.DataFrame, today_only: bool) -> pd.DataFrame:
    if score_df.empty:
        return score_df.copy()
    filtered = score_df.copy()
    product_filter = str(st.session_state.get("scores_product_filter") or "All Products")
    if product_filter != "All Products":
        product_bucket = filtered.get("Product Bucket", pd.Series("", index=filtered.index)).astype(str)
        product_name = filtered.get("Product", pd.Series("", index=filtered.index)).astype(str)
        filtered = filtered[(product_bucket == product_filter) | (product_name == product_filter)]

    low_score, high_score = score_range_values(st.session_state.get("scores_score_range"))
    totals = filtered["Total Score"].fillna(-1)
    filtered = filtered[(totals >= low_score) & (totals <= high_score)]

    flag_filter = str(st.session_state.get("scores_flag_filter") or "All Flags")
    if flag_filter != "All Flags":
        filtered = filtered[filtered["Flags"].apply(lambda value: flag_filter in score_flags_list(value))]

    status_filter = str(st.session_state.get("scores_status_filter") or "All Statuses")
    if status_filter != "All Statuses":
        status_value = "Strong" if status_filter == "Export Ready" else status_filter
        filtered = filtered[filtered["Status"].astype(str) == status_value]

    search_term = str(st.session_state.get("scores_search") or "").strip()
    if search_term:
        mask = filtered["_search_text"].astype(str).str.contains(search_term, case=False, na=False)
        filtered = filtered[mask]

    if today_only:
        today = datetime.now().strftime("%Y-%m-%d")
        filtered = filtered[filtered["Source Date"].astype(str) == today]
    return filtered.reset_index(drop=True)


def sort_score_dataframe(score_df: pd.DataFrame) -> pd.DataFrame:
    if score_df.empty:
        return score_df.copy()
    sort_by = str(st.session_state.get("scores_sort_preset") or "Scored At")
    sort_map = {
        "Score High to Low": ("Total Score", False),
        "Score Low to High": ("Total Score", True),
        "Scored At": ("_scored_at_sort", False),
        "Hook Score": ("Hook", False),
        "Content Score": ("Content", False),
    }
    column, ascending = sort_map.get(sort_by, ("_scored_at_sort", False))
    return score_df.sort_values(column, ascending=ascending, na_position="last").reset_index(drop=True)


def score_range_values(value: Any) -> tuple[float, float]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        low = score_float(value[0])
        high = score_float(value[1])
        if low is not None and high is not None:
            return min(low, high), max(low, high)
    return 0.0, 10.0


def build_scores_export_csv(filtered: pd.DataFrame) -> bytes:
    columns = [
        "Source Date",
        "Source Video",
        "Run Tag",
        "Clip ID",
        "Product",
        "Total Score",
        "Content",
        "Host Focus",
        "Hook",
        "Quality",
        "Engagement",
        "Flag Count",
        "Flag Severity",
        "Status",
        "Compliance Blocked",
        "Scored At",
    ]
    available = [column for column in columns if column in filtered.columns]
    if not available:
        return b""
    return filtered[available].to_csv(index=False).encode("utf-8-sig")


def build_scores_export_signature(filtered: pd.DataFrame, scores_signature: Any) -> str:
    digest = hashlib.sha1(repr(scores_signature).encode("utf-8", errors="ignore"))
    digest.update(str(len(filtered)).encode("ascii"))
    key_series = filtered.get("_score_key")
    if key_series is not None:
        for key in key_series.astype(str):
            digest.update(b"\0")
            digest.update(key.encode("utf-8", errors="ignore"))
    return digest.hexdigest()


def get_scores_export_cache(export_signature: str) -> dict[str, Any] | None:
    cached = st.session_state.get(SCORES_EXPORT_CACHE_KEY)
    if not isinstance(cached, dict):
        return None
    if cached.get("signature") != export_signature:
        st.session_state.pop(SCORES_EXPORT_CACHE_KEY, None)
        return None
    if not isinstance(cached.get("data"), (bytes, bytearray)):
        st.session_state.pop(SCORES_EXPORT_CACHE_KEY, None)
        return None
    return cached


def render_scores_kpi_cards(filtered: pd.DataFrame, filtered_all_dates: pd.DataFrame) -> None:
    total = len(filtered)
    average_score = filtered["Total Score"].dropna().mean() if total else 0.0
    if pd.isna(average_score):
        average_score = 0.0
    strong_count = int((filtered["Total Score"] >= 7).sum()) if total else 0
    needs_review_mask = (
        (filtered["Total Score"].fillna(-1) < 5)
        | (filtered["Flag Severity"].astype(str).str.casefold() == "high")
    ) if total else pd.Series(dtype=bool)
    needs_review_count = int(needs_review_mask.sum()) if total else 0
    blocked_count = int(filtered["Compliance Blocked"].fillna(False).astype(bool).sum()) if total else 0
    delta = average_delta_vs_yesterday(average_score, filtered_all_dates)
    cards = [
        ("Base Clips", f"{total:,}", "Filtered", "clapboard", "#3b82f6"),
        ("Average Score", f"{average_score:.2f}", delta, "star", "#8b5cf6"),
        ("Strong Clips", f"{strong_count:,}", f"{percentage(strong_count, total)} of total", "check-circle", "#22c55e"),
        ("Needs Review", f"{needs_review_count:,}", f"{percentage(needs_review_count, total)} of total", "alert-circle", "#f59e0b"),
        ("Compliance Blocked", f"{blocked_count:,}", f"{percentage(blocked_count, total)} of total", "shield", "#ef4444"),
    ]
    card_html = []
    for label, value, sub, icon, accent in cards:
        sub_html = f"<div class='score-kpi-sub' style='color:{accent};'>{html.escape(sub)}</div>" if sub else ""
        card_html.append(
            f"<div class='score-kpi-card' style='--score-kpi-accent:{accent};'>"
            f"<div class='score-kpi-icon' style='color:{accent}; background: color-mix(in srgb, {accent} 18%, transparent);'>"
            f"{svg_icon(icon, accent, size=18)}</div>"
            f"<div class='score-kpi-label'>{html.escape(label)}</div>"
            f"<div class='score-kpi-value'>{html.escape(value)}</div>"
            f"{sub_html}"
            "</div>"
        )
    st.markdown(f"<div class='score-kpi-grid'>{''.join(card_html)}</div>", unsafe_allow_html=True)


def average_delta_vs_yesterday(current_average: float, filtered_all_dates: pd.DataFrame) -> str:
    if filtered_all_dates.empty:
        return ""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_scores = filtered_all_dates.loc[
        filtered_all_dates["Source Date"].astype(str) == yesterday,
        "Total Score",
    ].dropna()
    if yesterday_scores.empty:
        return ""
    delta = current_average - float(yesterday_scores.mean())
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.2f} vs yesterday"


def percentage(count: int, total: int) -> str:
    if total <= 0:
        return "0%"
    return f"{(count / total * 100):.0f}%"


def render_scores_filter_bar(product_options: list[str], flag_options: list[str]) -> None:
    with st.container(border=True):
        controls = st.columns([1.0, 1.35, 1.15, 1.0, 1.0, 1.05], gap="small")
        with controls[0]:
            st.selectbox("Product", product_options, key="scores_product_filter")
        with controls[1]:
            st.text_input("Search", placeholder="Clip ID, source, flag...", key="scores_search")
        with controls[2]:
            st.slider("Score Range", 0.0, 10.0, key="scores_score_range", step=0.1)
        with controls[3]:
            st.selectbox("Status", score_status_filter_options(), key="scores_status_filter")
        with controls[4]:
            st.selectbox("Flags", flag_options, key="scores_flag_filter")
        with controls[5]:
            st.selectbox("Sort By", score_sort_options(), key="scores_sort_preset")


def render_scores_table_toolbar(filtered: pd.DataFrame) -> None:
    cols = st.columns([0.8, 2.0, 1.2], gap="small")
    with cols[0]:
        today_active = bool(st.session_state.get("scores_today_only", False))
        marker_class = "score-today-button-marker is-active" if today_active else "score-today-button-marker"
        st.markdown(f"<div class='{marker_class}'></div>", unsafe_allow_html=True)
        if st.button(
            "Today only",
            key="scores_today_only_button",
            use_container_width=True,
            type="secondary",
        ):
            st.session_state.scores_today_only = not bool(st.session_state.scores_today_only)
            st.session_state.scores_page = 1
            st.rerun(scope="fragment")
    with cols[1]:
        suffix = "from today's source videos" if bool(st.session_state.get("scores_today_only", False)) else "after filters"
        st.caption(f"{len(filtered):,} clips {suffix}")
    with cols[2]:
        st.caption("Preview loads in the right panel")


def render_paginated_dataframe(
    frame: pd.DataFrame,
    display_cols: list[str],
    key_prefix: str,
    height: int,
    style_fn: Any | None = None,
    mobile_card_renderer: Any | None = None,
) -> None:
    if frame.empty:
        return
    page_size_key = f"{key_prefix}_rows"
    page_key = f"{key_prefix}_page"
    if st.session_state.get(page_size_key) not in (50, 100, 200):
        st.session_state[page_size_key] = 50
    page_size = int(st.session_state.get(page_size_key) or 50)
    total_rows = len(frame)
    total_pages = max((total_rows - 1) // page_size + 1, 1)
    page = min(max(int(st.session_state.get(page_key, 1)), 1), total_pages)
    st.session_state[page_key] = page
    start_idx = (page - 1) * page_size
    end_idx = min(start_idx + page_size, total_rows)
    page_df = frame.iloc[start_idx:end_idx]

    control_cols = st.columns([0.7, 1.2, 0.7, 2.2, 1.0], gap="small")
    with control_cols[0]:
        if st.button("<", key=f"{key_prefix}_prev", use_container_width=True, disabled=page <= 1):
            st.session_state[page_key] = max(1, page - 1)
            st.rerun(scope="fragment")
    with control_cols[1]:
        st.caption(f"Page {page} / {total_pages}")
    with control_cols[2]:
        if st.button(">", key=f"{key_prefix}_next", use_container_width=True, disabled=page >= total_pages):
            st.session_state[page_key] = min(total_pages, page + 1)
            st.rerun(scope="fragment")
    with control_cols[3]:
        st.caption(f"Showing {start_idx + 1} to {end_idx} of {total_rows} rows")
    with control_cols[4]:
        st.selectbox("Rows", [50, 100, 200], key=page_size_key, label_visibility="collapsed")

    if mobile_card_renderer is not None:
        mobile_card_renderer(page_df)
        st.markdown("<div class='desktop-dataframe-anchor'></div>", unsafe_allow_html=True)

    table = page_df[display_cols]
    if style_fn is not None:
        table = table.style.apply(style_fn, axis=1)
    st.dataframe(table, hide_index=True, use_container_width=True, height=height)


def render_compliance_violation_mobile_cards(page_df: pd.DataFrame) -> None:
    cards = []
    for _, row in page_df.iterrows():
        severity = str(row.get("Severity") or "medium").lower()
        original = str(row.get("Original Text") or "-")
        replacement = str(row.get("Suggested Replacement") or "-")
        cards.append(
            "<div class='mobile-card'>"
            "<div class='mobile-card-head'>"
            "<div>"
            f"<div class='mobile-card-title'>{html.escape(str(row.get('Clip ID', '-')))}</div>"
            f"<div class='mobile-card-meta'>{html.escape(str(row.get('Product', 'general')))} | {html.escape(str(row.get('Violation Type', '-')))}</div>"
            "</div>"
            f"{severity_badge_html(severity)}"
            "</div>"
            "<div class='mobile-card-stat'>"
            "<div class='mobile-card-stat-label'>Original</div>"
            f"<div class='mobile-card-stat-value'>{html.escape(original[:220])}</div>"
            "</div>"
            "<div class='mobile-card-stat' style='margin-top:0.55rem;'>"
            "<div class='mobile-card-stat-label'>Suggested</div>"
            f"<div class='mobile-card-stat-value'>{html.escape(replacement[:220])}</div>"
            "</div>"
            f"<div class='mobile-card-meta' style='margin-top:0.55rem;'>Field: {html.escape(str(row.get('Field', '-')))}</div>"
            "</div>"
        )
    st.markdown(f"<div class='mobile-card-list'>{''.join(cards)}</div>", unsafe_allow_html=True)


def render_compliance_clip_mobile_cards(page_df: pd.DataFrame) -> None:
    cards = []
    for _, row in page_df.iterrows():
        status_class = "status-failed" if bool(row.get("Blocked")) else "status-completed" if bool(row.get("Passed")) else "status-waiting"
        status_label = "Blocked" if bool(row.get("Blocked")) else "Passed" if bool(row.get("Passed")) else str(row.get("Status") or "Unknown")
        cards.append(
            "<div class='mobile-card'>"
            "<div class='mobile-card-head'>"
            "<div>"
            f"<div class='mobile-card-title'>{html.escape(str(row.get('Clip ID', '-')))}</div>"
            f"<div class='mobile-card-meta'>{html.escape(str(row.get('Product', 'general')))}</div>"
            "</div>"
            f"<span class='status-badge {status_class}'>{html.escape(status_label)}</span>"
            "</div>"
            "<div class='mobile-card-grid'>"
            "<div class='mobile-card-stat'>"
            "<div class='mobile-card-stat-label'>Violations</div>"
            f"<div class='mobile-card-stat-value'>{int(row.get('Violation Count', 0) or 0):,}</div>"
            "</div>"
            "<div class='mobile-card-stat'>"
            "<div class='mobile-card-stat-label'>Auto Fixed</div>"
            f"<div class='mobile-card-stat-value'>{'Yes' if bool(row.get('Auto Fixed')) else 'No'}</div>"
            "</div>"
            "</div>"
            f"<div class='mobile-card-meta' style='margin-top:0.55rem;'>{html.escape(str(row.get('Summary', '') or 'No summary'))}</div>"
            "</div>"
        )
    st.markdown(f"<div class='mobile-card-list'>{''.join(cards)}</div>", unsafe_allow_html=True)


def compliance_status_options() -> list[str]:
    return ["All", "Passed", "Auto-fixed", "Blocked", "Needs review"]


def compliance_status_for_row(row: pd.Series | dict[str, Any]) -> str:
    getter = row.get
    if bool(getter("Blocked", False)):
        return "Blocked"
    if bool(getter("Auto Fixed", False)):
        return "Auto-fixed"
    if bool(getter("Passed", False)):
        return "Passed"
    return "Needs review"


def add_compliance_status_column(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    output = frame.copy()
    output["Compliance Status"] = output.apply(compliance_status_for_row, axis=1)
    return output


def filter_compliance_status(frame: pd.DataFrame, status_filter: str) -> pd.DataFrame:
    if frame.empty or status_filter == "All" or "Compliance Status" not in frame:
        return frame
    return frame[frame["Compliance Status"].astype(str) == status_filter].reset_index(drop=True)


def compliance_summary_from_frame(frame: pd.DataFrame) -> dict[str, int]:
    if frame.empty:
        return {"scanned": 0, "passed": 0, "blocked": 0, "auto_fixed": 0}
    return {
        "scanned": int(len(frame)),
        "passed": int(frame["Passed"].fillna(False).astype(bool).sum()) if "Passed" in frame else 0,
        "blocked": int(frame["Blocked"].fillna(False).astype(bool).sum()) if "Blocked" in frame else 0,
        "auto_fixed": int(frame["Auto Fixed"].fillna(False).astype(bool).sum()) if "Auto Fixed" in frame else 0,
    }


def sync_compliance_filter_source(source: str) -> None:
    st.session_state.compliance_filter_source = source


def ensure_compliance_filter_state() -> None:
    options = compliance_status_options()
    if st.session_state.get("compliance_status_filter") not in options:
        st.session_state.compliance_status_filter = "All"
    if st.session_state.get("compliance_status_filter_mobile") not in options:
        st.session_state.compliance_status_filter_mobile = st.session_state.compliance_status_filter
    if st.session_state.get("compliance_filter_source") not in {"desktop", "mobile"}:
        st.session_state.compliance_filter_source = "desktop"


def active_compliance_status_filter() -> str:
    ensure_compliance_filter_state()
    source = str(st.session_state.get("compliance_filter_source") or "desktop")
    key = "compliance_status_filter_mobile" if source == "mobile" else "compliance_status_filter"
    value = str(st.session_state.get(key) or "All")
    return value if value in compliance_status_options() else "All"


def render_compliance_tab(summary: dict[str, Any]) -> None:
    render_page_intro("Compliance", "Advertising claim checks before subtitle rendering.")
    output_dirs = collect_score_output_dirs(summary)
    payload = load_compliance_rows(output_dirs)
    clip_rows = payload.get("clips", [])
    clip_df = add_compliance_status_column(pd.DataFrame(clip_rows))
    status_filter = active_compliance_status_filter()
    status_filtered_clip_df = filter_compliance_status(clip_df, status_filter)
    totals = compliance_summary_from_frame(status_filtered_clip_df)

    kpi_cols = st.columns(4, gap="medium")
    kpis = [
        ("Scanned", int(totals.get("scanned") or 0), "clips", "focus", "#3b82f6"),
        ("Passed", int(totals.get("passed") or 0), "clips", "check-circle", "#22c55e"),
        ("Blocked", int(totals.get("blocked") or 0), "high severity", "alert-circle", "#ef4444"),
        ("Auto-fixed", int(totals.get("auto_fixed") or 0), "low severity", "refresh", "#fbbf24"),
    ]
    for col, (title, value, subtitle, icon_name, accent) in zip(kpi_cols, kpis):
        with col:
            render_kpi_card(title, value, subtitle, icon_name, accent)

    if not clip_rows:
        st.info("No compliance results found yet.")
        return

    st.markdown("<div style='height:0.8rem'></div>", unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown("<div class='panel-title'>Re-scan</div>", unsafe_allow_html=True)
        run_options = ["Select run"] + list(output_dirs)
        selected_run = st.selectbox(
            "Output Run",
            run_options,
            format_func=lambda value: "Select run" if value == "Select run" else Path(value).name,
            key="compliance_rescan_run",
        )
        if st.button(
            "Re-run Compliance Scan",
            key="compliance_rescan_button",
            use_container_width=True,
            disabled=selected_run == "Select run",
        ):
            try:
                import config as cfg
                from compliance_checker import scan_output_dir

                result = scan_output_dir(selected_run, cfg=cfg, force=True)
                load_json_payload_by_signature.clear()
                load_compliance_rows.clear()
                load_compliance_detail_rows.clear()
                load_score_rows.clear()
                load_scorer_stats.clear()
                load_score_trend_payload.clear()
                invalidate_scores_session_cache()
                load_manifest_clip_count.clear()
                st.success(
                    "Compliance scan complete: "
                    f"{result.get('scanned', 0)} scanned, "
                    f"{result.get('blocked', 0)} blocked, "
                    f"{result.get('auto_fixed', 0)} auto-fixed."
                )
                st.rerun(scope="fragment")
            except Exception as exc:
                st.error(f"Compliance re-scan failed: {exc}")

    st.markdown("<div style='height:0.8rem'></div>", unsafe_allow_html=True)
    detail_payload = (
        load_compliance_detail_rows(selected_run)
        if selected_run != "Select run"
        else {"clips": [], "violations": []}
    )
    detail_clip_df = add_compliance_status_column(pd.DataFrame(detail_payload.get("clips", [])))
    violation_df = pd.DataFrame(detail_payload.get("violations", []))
    if not violation_df.empty:
        status_map = (
            detail_clip_df.set_index("Clip ID")["Compliance Status"].to_dict()
            if not detail_clip_df.empty and "Clip ID" in detail_clip_df and "Compliance Status" in detail_clip_df
            else {}
        )
        violation_df = violation_df.copy()
        violation_df["Compliance Status"] = violation_df["Clip ID"].astype(str).map(status_map).fillna("Needs review")

    st.markdown("<div class='desktop-compliance-filters-anchor'></div>", unsafe_allow_html=True)
    filter_cols = st.columns([0.9, 1, 1, 1, 1.35], gap="medium")
    with filter_cols[0]:
        st.selectbox(
            "Status",
            compliance_status_options(),
            key="compliance_status_filter",
            on_change=sync_compliance_filter_source,
            args=("desktop",),
        )
    with filter_cols[1]:
        severity_options = ["All"] + sorted(
            {str(value) for value in violation_df.get("Severity", pd.Series(dtype=str)).dropna().unique()}
        )
        severity_filter = st.selectbox("Severity", severity_options, key="compliance_severity_filter")
    with filter_cols[2]:
        product_options = ["All"] + sorted(
            {str(value) for value in clip_df.get("Product", pd.Series(dtype=str)).dropna().unique() if str(value)}
        )
        product_filter = st.selectbox("Product", product_options, key="compliance_product_filter")
    with filter_cols[3]:
        type_options = ["All"] + sorted(
            {str(value) for value in violation_df.get("Violation Type", pd.Series(dtype=str)).dropna().unique()}
        )
        type_filter = st.selectbox("Type", type_options, key="compliance_type_filter")
    with filter_cols[4]:
        search_term = st.text_input("Search", placeholder="Clip or claim", key="compliance_search")

    for key, options in (
        ("compliance_severity_filter_mobile", severity_options),
        ("compliance_product_filter_mobile", product_options),
        ("compliance_type_filter_mobile", type_options),
    ):
        if st.session_state.get(key) not in options:
            st.session_state[key] = options[0]

    st.markdown("<div class='mobile-compliance-filters-anchor'></div>", unsafe_allow_html=True)
    with st.expander("Filters"):
        mobile_status_filter = st.selectbox(
            "Status",
            compliance_status_options(),
            key="compliance_status_filter_mobile",
            on_change=sync_compliance_filter_source,
            args=("mobile",),
        )
        mobile_severity_filter = st.selectbox("Severity", severity_options, key="compliance_severity_filter_mobile")
        mobile_product_filter = st.selectbox("Product", product_options, key="compliance_product_filter_mobile")
        mobile_type_filter = st.selectbox("Type", type_options, key="compliance_type_filter_mobile")
        mobile_search_term = st.text_input("Search", placeholder="Clip or claim", key="compliance_search_mobile")

    if str(st.session_state.get("compliance_filter_source")) == "mobile":
        status_filter = mobile_status_filter
        severity_filter = mobile_severity_filter
        product_filter = mobile_product_filter
        type_filter = mobile_type_filter
        search_term = mobile_search_term
    else:
        status_filter = active_compliance_status_filter()

    if selected_run == "Select run":
        st.info("Select an output run above to load detailed compliance violations.")
    elif violation_df.empty:
        st.info("No violations found for the selected run.")
    else:
        filtered = filter_compliance_status(violation_df.copy(), status_filter)
        if severity_filter != "All":
            filtered = filtered[filtered["Severity"].astype(str) == severity_filter]
        if product_filter != "All":
            filtered = filtered[filtered["Product"].astype(str) == product_filter]
        if type_filter != "All":
            filtered = filtered[filtered["Violation Type"].astype(str) == type_filter]
        if search_term:
            needle = search_term.casefold()
            haystack = (
                filtered["Clip ID"].astype(str)
                + " "
                + filtered["Original Text"].astype(str)
                + " "
                + filtered["Suggested Replacement"].astype(str)
            ).str.casefold()
            filtered = filtered[haystack.str.contains(needle, na=False)]

        display_cols = [
            "Clip ID",
            "Product",
            "Field",
            "Severity",
            "Violation Type",
            "Original Text",
            "Suggested Replacement",
            "Compliance File",
        ]
        render_paginated_dataframe(
            filtered,
            display_cols,
            "compliance_violations",
            420,
            style_fn=_compliance_severity_style,
            mobile_card_renderer=render_compliance_violation_mobile_cards,
        )

    with st.expander("Scanned Clips"):
        render_paginated_dataframe(
            filter_compliance_status(clip_df, status_filter),
            [
                "Clip ID",
                "Product",
                "Status",
                "Passed",
                "Blocked",
                "Auto Fixed",
                "Violation Count",
                "Summary",
                "Compliance File",
            ],
            "compliance_clips",
            360,
            mobile_card_renderer=render_compliance_clip_mobile_cards,
        )


def _compliance_severity_style(row: pd.Series) -> list[str]:
    severity = str(row.get("Severity") or "").lower()
    colors = {
        "high": "background-color: rgba(239, 68, 68, 0.24); color: #fee2e2;",
        "medium": "background-color: rgba(249, 115, 22, 0.22); color: #ffedd5;",
        "low": "background-color: rgba(234, 179, 8, 0.20); color: #fef9c3;",
    }
    style = colors.get(severity, "")
    return [style for _ in row]


def render_trends_tab(summary: dict[str, Any]) -> None:
    render_page_intro("Trends", "Score trends by product, dimension, tier, and recurring flags.")
    trend_payload = load_score_trend_payload(collect_score_output_dirs(summary))
    if not int(trend_payload.get("clip_count") or 0):
        with st.container(border=True):
            st.info("No score trend data found yet.")
        return

    product_df = trend_payload["product_df"]
    dimension_df = trend_payload["dimension_df"]
    tier_df = trend_payload["tier_df"]
    flags_df = trend_payload["flags_df"]
    top_df = trend_payload["top_df"]
    tier_order = trend_payload["tier_order"]

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
            image_key = hashlib.sha1(str(image_path).encode("utf-8", errors="ignore")).hexdigest()[:12]
            if st.session_state.get("focus_debug_image_loaded_key") == image_key:
                st.image(str(image_path), use_container_width=True)
            elif st.button("Load contact sheet", key=f"focus_debug_load_image_{image_key}", use_container_width=True):
                st.session_state.focus_debug_image_loaded_key = image_key
                st.rerun(scope="fragment")
        else:
            st.warning(f"Missing contact sheet: {image_path}")

    breakdown = load_focus_debug_detail(str(selected.get("JSON Path") or ""))
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


def resolve_selected_score_row(score_df: pd.DataFrame, selected_key: str, fallback: pd.Series | None) -> pd.Series | None:
    if selected_key:
        base_match = score_df[score_df["_score_key"].astype(str) == str(selected_key)]
        if not base_match.empty:
            detail_df = load_score_detail_dataframe(base_match.iloc[0])
            full_match = detail_df[detail_df["_score_key"].astype(str) == str(selected_key)]
            if not full_match.empty:
                return full_match.iloc[0]
            return base_match.iloc[0]

        selected_base_key = str(st.session_state.get("selected_score_base_key", ""))
        if selected_base_key:
            parent_match = score_df[score_df["_base_score_key"].astype(str) == selected_base_key]
            if not parent_match.empty:
                detail_df = load_score_detail_dataframe(parent_match.iloc[0])
                full_match = detail_df[detail_df["_score_key"].astype(str) == str(selected_key)]
                if not full_match.empty:
                    return full_match.iloc[0]

        detail_cache = st.session_state.get(SCORES_DETAIL_CACHE_KEY, {})
        if isinstance(detail_cache, dict):
            for rows in detail_cache.values():
                cached_df = pd.DataFrame(rows)
                if cached_df.empty or "_score_key" not in cached_df:
                    continue
                cached_match = cached_df[cached_df["_score_key"].astype(str) == str(selected_key)]
                if not cached_match.empty:
                    return cached_match.iloc[0]

    if fallback is None:
        return None
    detail_df = load_score_detail_dataframe(fallback)
    fallback_key = str(fallback.get("_score_key", ""))
    if fallback_key:
        fallback_match = detail_df[detail_df["_score_key"].astype(str) == fallback_key]
        if not fallback_match.empty:
            return fallback_match.iloc[0]
    return fallback


def render_scores_click_table(filtered: pd.DataFrame, selected_key: str, score_df: pd.DataFrame) -> str:
    if st.session_state.get("scores_rows") not in (50, 100, 200):
        st.session_state.scores_rows = 50
    page_size = int(st.session_state.get("scores_rows") or 50)
    total_rows = len(filtered)
    total_pages = max((total_rows - 1) // page_size + 1, 1)
    page = min(max(int(st.session_state.get("scores_page", 1)), 1), total_pages)
    st.session_state.scores_page = page

    start_idx = (page - 1) * page_size
    end_idx = min(start_idx + page_size, total_rows)
    page_df = filtered.iloc[start_idx:end_idx]
    selected_key = render_scores_compact_table(page_df, selected_key)
    selected_key = render_scores_mobile_cards(page_df, selected_key, score_df)

    pager_cols = st.columns([0.65, 0.7, 0.65, 1.75, 1.05], gap="small")
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
        st.caption(f"Showing {start_idx + 1} to {end_idx} of {total_rows} clips")
    with pager_cols[4]:
        st.selectbox("Rows", [50, 100, 200], key="scores_rows", label_visibility="collapsed")
    return selected_key


def render_scores_compact_table(page_df: pd.DataFrame, selected_key: str) -> str:
    st.markdown(
        """
        <div class="score-review-shell">
        <div class="score-review-header">
            <div></div>
            <div>Clip ID</div>
            <div>Product</div>
            <div>Total Score</div>
            <div>Quality</div>
            <div>Flags</div>
            <div>Status</div>
            <div>Preview</div>
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
        row_classes = ["score-table-row-marker"]
        if selected:
            row_classes.append("is-selected")
        st.markdown(f"<div class='{' '.join(row_classes)}'></div>", unsafe_allow_html=True)

        cols = st.columns([0.22, 1.12, 1.02, 0.74, 0.62, 0.72, 0.68, 0.46], gap="small")
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
            st.markdown(f"<div class='score-review-cell'>{html.escape(str(row.get('Clip ID', '')))}</div>", unsafe_allow_html=True)
        with cols[2]:
            st.markdown(f"<div class='score-review-cell'>{html.escape(str(row.get('Product', '')))}</div>", unsafe_allow_html=True)
        with cols[3]:
            st.markdown(render_total_score_cell(row.get("Total Score")), unsafe_allow_html=True)
        with cols[4]:
            st.markdown(render_quality_badge(row.get("Quality")), unsafe_allow_html=True)
        with cols[5]:
            st.markdown(
                render_flags_count_cell(row.get("Flag Count"), str(row.get("Flag Severity") or "none")),
                unsafe_allow_html=True,
            )
        with cols[6]:
            st.markdown(render_status_badge(row), unsafe_allow_html=True)
        with cols[7]:
            if st.button(
                "▶",
                key=f"score_preview_{key}_{row_index}",
                help="Preview in the detail panel",
                use_container_width=True,
                type="secondary",
            ):
                selected_key = key
                st.session_state.selected_score_key = key
                st.session_state.selected_score_base_key = base_key
                st.session_state.score_preview_loaded_key = key

        if expanded:
            detail_df = load_score_detail_dataframe(row)
            selected_key = render_score_expanded_panel(detail_df, base_key, selected_key)

    st.markdown("</div>", unsafe_allow_html=True)
    return selected_key


def render_scores_mobile_cards(page_df: pd.DataFrame, selected_key: str, score_df: pd.DataFrame) -> str:
    for page_position, (row_index, row) in enumerate(page_df.iterrows()):
        key = str(row.get("_score_key", ""))
        base_key = str(row.get("_base_score_key", key))
        selected = key == selected_key
        selected_style = "border-color: rgba(124, 58, 237, 0.62);" if selected else ""
        flags = render_flags_count_cell(row.get("Flag Count"), str(row.get("Flag Severity") or "none"))
        variants = int(score_float(row.get("Variants")) or 0)
        st.markdown(
            f"""
            <div class="mobile-score-card-shell">
                <div class="mobile-card" style="{selected_style}">
                    <div class="mobile-card-head">
                        <div>
                            <div class="mobile-card-title">{html.escape(str(row.get('Clip ID', '')))}</div>
                            <div class="mobile-card-meta">{html.escape(str(row.get('Product', '')))} | {html.escape(str(row.get('Source Date', 'Undated') or 'Undated'))}</div>
                        </div>
                        <div>{render_status_badge(row)}</div>
                    </div>
                    <div class="mobile-card-grid">
                        <div class="mobile-card-stat">
                            <div class="mobile-card-stat-label">Total</div>
                            <div class="mobile-card-stat-value">{render_total_score_cell(row.get('Total Score'))}</div>
                        </div>
                        <div class="mobile-card-stat">
                            <div class="mobile-card-stat-label">Quality</div>
                            <div class="mobile-card-stat-value">{render_quality_badge(row.get('Quality'))}</div>
                        </div>
                        <div class="mobile-card-stat">
                            <div class="mobile-card-stat-label">Flags</div>
                            <div class="mobile-card-stat-value">{flags}</div>
                        </div>
                        <div class="mobile-card-stat">
                            <div class="mobile-card-stat-label">Variants</div>
                            <div class="mobile-card-stat-value">{variants:,}</div>
                        </div>
                    </div>
                </div>
            </div>
            <div class="mobile-score-button-anchor"></div>
            """,
            unsafe_allow_html=True,
        )
        if st.button(
            "Preview clip",
            key=f"score_mobile_preview_{key}_{row_index}",
            use_container_width=True,
            type="primary" if selected else "secondary",
        ):
            selected_key = key
            selected = True
            st.session_state.selected_score_key = key
            st.session_state.selected_score_base_key = base_key
            st.session_state.score_mobile_selected_index = int(row_index) if isinstance(row_index, int) else page_position
        if selected:
            st.session_state.score_mobile_selected_index = int(row_index) if isinstance(row_index, int) else page_position
            inline_selected = resolve_selected_score_row(score_df, key, row)
            render_score_mobile_inline_detail(inline_selected if inline_selected is not None else row)
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


def render_score_expanded_panel(detail_df: pd.DataFrame, base_key: str, selected_key: str) -> str:
    variants = variants_for_score_base(detail_df, base_key)
    indent_cols = st.columns([0.35, 11.65], gap="small")
    with indent_cols[1]:
        with st.container(border=True):
            st.markdown(f"**Variants ({len(variants)})**")
            with st.container(height=200, border=False):
                selected_key = render_score_variants_panel(variants, base_key, selected_key, key_prefix="expanded")
    return selected_key


def render_score_variants_panel(
    variants: pd.DataFrame,
    base_key: str,
    selected_key: str,
    key_prefix: str = "variants",
) -> str:
    header = st.columns([1.55, 0.72, 0.7, 1.0, 0.42], gap="small")
    for col, label in zip(header, ["Variant ID", "Total Score", "Similarity", "Render Status", "Preview"]):
        with col:
            st.markdown(f"<div class='score-review-cell' style='color:#94a3b8;font-weight:800;'>{label}</div>", unsafe_allow_html=True)

    if variants.empty:
        st.caption("No variants found for this clip.")
        return selected_key

    for row_index, row in variants.iterrows():
        key = str(row.get("_score_key", ""))
        selected = key == selected_key
        row_classes = ["score-variant-row-marker"]
        if row_index % 2 == 1:
            row_classes.append("is-alt")
        if selected:
            row_classes.append("is-selected")
        st.markdown(f"<div class='{' '.join(row_classes)}'></div>", unsafe_allow_html=True)
        cols = st.columns([1.55, 0.72, 0.7, 1.0, 0.42], gap="small")
        with cols[0]:
            variant_id = str((row.get("_raw") or {}).get("variant_id") or row.get("Clip ID", ""))
            st.markdown(f"<div class='score-review-cell'>{html.escape(variant_id)}</div>", unsafe_allow_html=True)
        with cols[1]:
            st.markdown(render_total_score_cell(row.get("Total Score")), unsafe_allow_html=True)
        with cols[2]:
            st.markdown(f"<div class='score-review-cell'>{html.escape(score_format(row.get('Similarity')))}</div>", unsafe_allow_html=True)
        with cols[3]:
            st.markdown(render_variant_status_badge(row), unsafe_allow_html=True)
        with cols[4]:
            if st.button(
                "▶",
                key=f"score_variant_preview_{key_prefix}_{base_key}_{key}_{row_index}",
                help="Preview this variant",
                use_container_width=True,
                type="secondary",
            ):
                selected_key = key
                st.session_state.selected_score_key = key
                st.session_state.selected_score_base_key = base_key
                st.session_state.score_preview_loaded_key = key
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


def render_total_score_cell(value: Any) -> str:
    numeric = score_float(value)
    text = score_format(value)
    color = score_text_color(numeric)
    background = score_band_color(numeric)
    return (
        "<div class='score-review-cell'>"
        f"<span class='score-table-badge' style='color:{color}; background:{background};'>{html.escape(text)}</span>"
        "</div>"
    )


def render_quality_badge(value: Any) -> str:
    label = score_quality_label(value)
    colors = {
        "High": ("#86efac", "rgba(22, 163, 74, 0.22)", "rgba(34, 197, 94, 0.32)"),
        "Medium": ("#fde68a", "rgba(202, 138, 4, 0.22)", "rgba(251, 191, 36, 0.32)"),
        "Low": ("#fecaca", "rgba(220, 38, 38, 0.22)", "rgba(239, 68, 68, 0.32)"),
        "Unknown": ("#cbd5e1", "rgba(100, 116, 139, 0.16)", "rgba(148, 163, 184, 0.24)"),
    }
    color, background, border = colors.get(label, colors["Unknown"])
    return (
        "<div class='score-review-cell'>"
        f"<span class='score-table-badge' style='color:{color}; background:{background}; border-color:{border};'>"
        f"{html.escape(label)}</span></div>"
    )


def render_status_badge(row: pd.Series) -> str:
    status = str(row.get("Status") or score_status_label(row.get("Total Score"), row.get("Flag Severity"), bool(row.get("Compliance Blocked"))))
    colors = {
        "Strong": ("#86efac", "rgba(22, 163, 74, 0.22)", "rgba(34, 197, 94, 0.34)"),
        "Review": ("#fde68a", "rgba(202, 138, 4, 0.22)", "rgba(251, 191, 36, 0.34)"),
        "Okay": ("#bfdbfe", "rgba(59, 130, 246, 0.18)", "rgba(96, 165, 250, 0.30)"),
        "Blocked": ("#fecaca", "rgba(220, 38, 38, 0.24)", "rgba(239, 68, 68, 0.36)"),
    }
    color, background, border = colors.get(status, colors["Okay"])
    return (
        "<div class='score-review-cell'>"
        f"<span class='score-table-badge' style='color:{color}; background:{background}; border-color:{border};'>"
        f"{html.escape(status)}</span></div>"
    )


def render_variant_status_badge(row: pd.Series) -> str:
    raw = row.get("_raw", {}) if isinstance(row.get("_raw", {}), dict) else {}
    status = str(raw.get("status") or "").strip()
    if not status:
        if bool(row.get("Compliance Blocked")):
            status = "Blocked"
        elif raw.get("exported", row.get("Exported", False)):
            status = "Rendered"
        else:
            status = "Pending"
    status_key = status.casefold()
    if "block" in status_key or "fail" in status_key:
        color, background, border = "#fecaca", "rgba(220, 38, 38, 0.24)", "rgba(239, 68, 68, 0.36)"
    elif "render" in status_key or "export" in status_key:
        color, background, border = "#86efac", "rgba(22, 163, 74, 0.22)", "rgba(34, 197, 94, 0.34)"
    else:
        color, background, border = "#fde68a", "rgba(202, 138, 4, 0.22)", "rgba(251, 191, 36, 0.34)"
    return (
        "<div class='score-review-cell'>"
        f"<span class='score-table-badge' style='color:{color}; background:{background}; border-color:{border};'>"
        f"{html.escape(status)}</span></div>"
    )


def render_flags_count_cell(count_value: Any, severity: str) -> str:
    try:
        count = int(score_float(count_value) or 0)
    except (TypeError, ValueError):
        count = 0
    suffix = "" if count == 1 else "s"
    label = f"{count} flag{suffix}"
    severity_key = str(severity or "none").casefold()
    colors = {
        "high": ("#ffffff", "#dc2626", "rgba(248, 113, 113, 0.55)"),
        "medium": ("#ffffff", "#f59e0b", "rgba(251, 191, 36, 0.55)"),
        "none": ("#ffffff", "#64748b", "rgba(148, 163, 184, 0.48)"),
    }
    color, background, border = colors.get(severity_key, colors["none"])
    return (
        "<div class='score-review-cell'>"
        f"<span class='score-table-badge' style='color:{color}; background:{background}; border-color:{border};'>"
        f"{html.escape(label)}</span></div>"
    )


def score_band_color(score: float | None) -> str:
    if score is None:
        return "rgba(148, 163, 184, 0.08)"
    if score >= 7:
        return "rgba(34, 197, 94, 0.16)"
    if score >= 5:
        return "rgba(251, 191, 36, 0.16)"
    return "rgba(239, 68, 68, 0.16)"


def score_format(value: Any) -> str:
    numeric = score_float(value)
    return "-" if numeric is None else f"{numeric:.2f}"


def render_score_mobile_inline_detail(selected: pd.Series) -> None:
    selected_key = str(selected.get("_score_key", ""))
    raw = selected.get("_raw", {}) if isinstance(selected.get("_raw", {}), dict) else {}
    base_raw = selected.get("_base_raw", {}) if isinstance(selected.get("_base_raw", {}), dict) else {}
    with st.container(border=True):
        st.markdown("<div class='mobile-inline-score-detail-anchor'></div>", unsafe_allow_html=True)
        st.markdown("<div class='panel-title'>Selected Clip</div>", unsafe_allow_html=True)
        clip_path = score_clip_path(selected)
        if clip_path and clip_path.exists():
            if st.session_state.get("score_preview_loaded_key") == selected_key:
                st.video(str(clip_path))
            elif st.button("Load preview", key=f"score_mobile_load_preview_{selected_key}", use_container_width=True):
                st.session_state.score_preview_loaded_key = selected_key
                st.rerun(scope="fragment")
            else:
                st.caption("Preview is ready. Tap Load preview to stream the clip.")
        else:
            st.caption(str(selected.get("Output File") or raw.get("output_file") or "Preview file unavailable"))

        st.markdown("**Score breakdown**")
        render_score_dimension_bars(selected)

        flags = score_flags_list(selected.get("Flags"))
        if not flags or selected.get("Flags") == "inherits base":
            flags = score_flags_list(base_raw.get("flags", []))
        st.markdown("**Flags**")
        if flags:
            for flag in flags:
                severity = score_single_flag_severity(flag)
                st.markdown(f"{severity_badge_html(severity)} `{html.escape(flag)}`", unsafe_allow_html=True)
        else:
            st.caption("No flags for this clip.")

        hook_summary = (
            score_hook_text(base_raw)
            or score_hook_text(raw)
            or str(selected.get("Summary") or "").strip()
        )
        st.markdown("**Hook summary**")
        if hook_summary:
            st.markdown(f"<div class='small-muted'>{html.escape(hook_summary[:600])}</div>", unsafe_allow_html=True)
        else:
            st.caption("Hook summary is not available.")


def render_score_detail_panel(selected: pd.Series | None, score_df: pd.DataFrame) -> None:
    st.markdown("<div class='score-detail-anchor'></div>", unsafe_allow_html=True)
    with st.container(border=True):
        if selected is None:
            st.markdown("<div class='panel-title'>Selected Clip</div>", unsafe_allow_html=True)
            st.info("Select a clip to preview")
            return

        raw = selected.get("_raw", {}) if isinstance(selected.get("_raw", {}), dict) else {}
        is_variant = str(selected.get("_row_type", "base")) == "variant"
        title = str(selected.get("Clip ID", ""))
        product = str(selected.get("Product", ""))
        meta_parts = [
            str(selected.get("Source Date") or "Undated"),
            score_duration_label(selected),
        ]
        if is_variant:
            meta_parts.insert(0, f"Variant of {selected.get('_base_clip_id', '')}")

        st.markdown(
            f"""
            <div class="score-detail-title-row">
                <div>
                    <div class="score-detail-title">{html.escape(title)}</div>
                    <div class="score-detail-product">{html.escape(product)}</div>
                </div>
                <div>{render_status_badge(selected)}</div>
            </div>
            <div class="score-meta-line">{html.escape('  •  '.join(part for part in meta_parts if part))}</div>
            """,
            unsafe_allow_html=True,
        )

        clip_path = score_clip_path(selected)
        selected_key = str(selected.get("_score_key", ""))
        if clip_path and clip_path.exists():
            if st.session_state.get("score_preview_loaded_key") == selected_key:
                st.video(str(clip_path))
            elif st.button("Load preview", key=f"score_load_preview_{selected_key}", use_container_width=True):
                st.session_state.score_preview_loaded_key = selected_key
                st.rerun(scope="fragment")
            else:
                st.caption("Preview is ready. Tap Load preview to stream the clip.")
        else:
            st.caption(str(selected.get("Output File") or raw.get("output_file") or "Preview file unavailable"))

        compliance_result = load_score_compliance_result_for_row(selected)
        score_tab, transcript_tab, variants_tab, flags_tab = st.tabs(["Scores", "Transcript", "Variants", "Flags"])
        with score_tab:
            render_score_dimension_bars(selected)
        with transcript_tab:
            render_score_transcript_panel(selected, compliance_result)
        with variants_tab:
            render_detail_variants_panel(selected, score_df)
        with flags_tab:
            render_detail_flags_panel(selected, compliance_result)


def score_clip_path(selected: pd.Series) -> Path | None:
    raw = selected.get("_raw", {}) if isinstance(selected.get("_raw", {}), dict) else {}
    candidates = [
        selected.get("Clip Path"),
        raw.get("clip_path"),
    ]
    output_file = str(selected.get("Output File") or raw.get("output_file") or "").strip()
    output_dir = score_row_output_dir(selected)
    if output_dir and output_file:
        candidates.append(str(output_dir / output_file))
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate))
        if path.exists():
            return path
    if candidates and candidates[0]:
        return Path(str(candidates[0]))
    return None


def score_row_output_dir(selected: pd.Series) -> Path | None:
    raw = selected.get("_raw", {}) if isinstance(selected.get("_raw", {}), dict) else {}
    for value in (selected.get("_output_dir"), raw.get("output_dir")):
        if value:
            return Path(str(value))
    clip_path = str(selected.get("Clip Path") or raw.get("clip_path") or "").strip()
    if clip_path:
        try:
            path = Path(clip_path)
            for parent in path.parents:
                if (parent / "scores_summary.json").exists():
                    return parent
        except OSError:
            return None
    return None


def score_duration_label(selected: pd.Series) -> str:
    base_raw = selected.get("_base_raw", {}) if isinstance(selected.get("_base_raw", {}), dict) else {}
    raw = selected.get("_raw", {}) if isinstance(selected.get("_raw", {}), dict) else {}
    for source in (base_raw, raw):
        metrics = source.get("metrics", {}) if isinstance(source, dict) else {}
        quality = metrics.get("quality", {}) if isinstance(metrics, dict) else {}
        duration = score_float(quality.get("duration_seconds"))
        if duration is not None:
            total_seconds = max(0, int(round(duration)))
            minutes = total_seconds // 60
            seconds = total_seconds % 60
            return f"{minutes:02d}:{seconds:02d}"
    return "Duration unavailable"


def render_score_dimension_bars(selected: pd.Series) -> None:
    dimensions = [
        ("Content", selected.get("Content")),
        ("Host Focus", selected.get("Host Focus")),
        ("Hook", score_base_hook_value(selected)),
        ("Quality", selected.get("Quality")),
        ("Engage", selected.get("Engagement")),
    ]
    for label, value in dimensions:
        numeric = score_float(value)
        pct = 0.0 if numeric is None else max(0.0, min(numeric / 10.0 * 100.0, 100.0))
        color = score_text_color(numeric)
        text = score_format(value)
        st.markdown(
            f"""
            <div class="score-dimension-row">
                <div class="score-dimension-label">{html.escape(label)}</div>
                <div class="score-dimension-track">
                    <div class="score-dimension-fill" style="width:{pct:.1f}%; background:{color};"></div>
                </div>
                <div class="score-dimension-value">{html.escape(text)}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def score_base_hook_value(selected: pd.Series) -> Any:
    base_raw = selected.get("_base_raw", {}) if isinstance(selected.get("_base_raw", {}), dict) else {}
    base_hook = score_float(base_raw.get("hook_score"))
    return selected.get("Hook") if base_hook is None else base_hook


def load_score_compliance_result_for_row(selected: pd.Series) -> dict[str, Any]:
    path = score_compliance_path(selected)
    payload = _read_compliance_result(path)
    return payload if isinstance(payload, dict) else {}


def score_compliance_path(selected: pd.Series) -> Path | None:
    raw = selected.get("_raw", {}) if isinstance(selected.get("_raw", {}), dict) else {}
    compliance_file = str(raw.get("compliance_file") or "").strip()
    if not compliance_file:
        return None
    path = Path(compliance_file)
    if path.is_absolute():
        return path
    output_dir = score_row_output_dir(selected)
    if output_dir is None:
        return None
    return output_dir / compliance_file


def render_score_transcript_panel(selected: pd.Series, compliance_result: dict[str, Any]) -> None:
    raw = selected.get("_raw", {}) if isinstance(selected.get("_raw", {}), dict) else {}
    base_raw = selected.get("_base_raw", {}) if isinstance(selected.get("_base_raw", {}), dict) else {}
    transcript = (
        str(compliance_result.get("transcript_text") or "").strip()
        or str(compliance_result.get("cleaned_transcript") or "").strip()
        or score_hook_text(base_raw)
        or score_hook_text(raw)
        or str(selected.get("Summary") or "").strip()
    )
    if not transcript:
        st.info("Transcript text is not available for this clip.")
        return
    violations = compliance_result.get("violations", [])
    html_text = score_highlighted_transcript_html(transcript, violations if isinstance(violations, list) else [])
    st.markdown(f"<div class='score-transcript-box'>{html_text}</div>", unsafe_allow_html=True)


def score_hook_text(raw: dict[str, Any]) -> str:
    metrics = raw.get("metrics", {}) if isinstance(raw, dict) else {}
    hook = metrics.get("hook", {}) if isinstance(metrics, dict) else {}
    return str(hook.get("text") or "").strip()


def score_highlighted_transcript_html(text: str, violations: list[dict[str, Any]]) -> str:
    spans: list[tuple[int, int, str]] = []
    for violation in violations:
        if not isinstance(violation, dict):
            continue
        if str(violation.get("source_field") or "transcript") not in ("", "transcript"):
            continue
        field_pos = violation.get("field_position") if isinstance(violation.get("field_position"), dict) else {}
        start = score_int(field_pos.get("start"))
        end = score_int(field_pos.get("end"))
        original = str(violation.get("original_text") or "")
        if start is None or end is None or start >= end or end > len(text):
            index = text.casefold().find(original.casefold()) if original else -1
            if index >= 0:
                start, end = index, index + len(original)
        if start is not None and end is not None and 0 <= start < end <= len(text):
            spans.append((start, end, "score-violation-hit"))

    price_pattern = re.compile(r"(?i)\b(?:rp\.?\s?\d[\d.,]*|\d+\s*(?:ribu|ribuan|k)|harga|promo|diskon|gratis|cuma|murah|potongan)\b")
    for match in price_pattern.finditer(text):
        span = (match.start(), match.end())
        if any(not (span[1] <= start or span[0] >= end) for start, end, _ in spans):
            continue
        spans.append((span[0], span[1], "score-price-hit"))

    spans.sort(key=lambda item: (item[0], item[1]))
    output = []
    cursor = 0
    for start, end, class_name in spans:
        if start < cursor:
            continue
        output.append(html.escape(text[cursor:start]))
        output.append(f"<span class='{class_name}'>{html.escape(text[start:end])}</span>")
        cursor = end
    output.append(html.escape(text[cursor:]))
    return "".join(output)


def score_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def render_detail_variants_panel(selected: pd.Series, score_df: pd.DataFrame) -> None:
    detail_df = load_score_detail_dataframe(selected)
    base_key = str(selected.get("_base_score_key") or selected.get("_score_key") or "")
    variants = variants_for_score_base(detail_df, base_key)
    if variants.empty:
        st.caption("No variants found for this clip.")
        return
    header = st.columns([1.2, 0.95, 0.65, 0.65, 0.95, 0.42], gap="small")
    for col, label in zip(header, ["Variant ID", "Hook Type", "Total", "Similarity", "Render Status", "Preview"]):
        with col:
            st.markdown(f"<div class='score-review-cell' style='color:#94a3b8;font-weight:800;'>{label}</div>", unsafe_allow_html=True)
    for idx, row in variants.iterrows():
        key = str(row.get("_score_key", ""))
        raw = row.get("_raw", {}) if isinstance(row.get("_raw", {}), dict) else {}
        row_classes = ["score-variant-row-marker"]
        if idx % 2 == 1:
            row_classes.append("is-alt")
        if key == str(st.session_state.get("selected_score_key", "")):
            row_classes.append("is-selected")
        st.markdown(f"<div class='{' '.join(row_classes)}'></div>", unsafe_allow_html=True)
        cols = st.columns([1.2, 0.95, 0.65, 0.65, 0.95, 0.42], gap="small")
        with cols[0]:
            st.markdown(f"<div class='score-review-cell'>{html.escape(str(raw.get('variant_id') or row.get('Clip ID', '')))}</div>", unsafe_allow_html=True)
        with cols[1]:
            st.markdown(f"<div class='score-review-cell'>{html.escape(hook_type_from_variant(raw.get('variant_id') or row.get('Clip ID')))}</div>", unsafe_allow_html=True)
        with cols[2]:
            st.markdown(render_total_score_cell(row.get("Total Score")), unsafe_allow_html=True)
        with cols[3]:
            st.markdown(f"<div class='score-review-cell'>{html.escape(score_format(row.get('Similarity')))}</div>", unsafe_allow_html=True)
        with cols[4]:
            st.markdown(render_variant_status_badge(row), unsafe_allow_html=True)
        with cols[5]:
            if st.button("▶", key=f"score_detail_variant_preview_{key}_{idx}", use_container_width=True):
                st.session_state.selected_score_key = key
                st.session_state.selected_score_base_key = base_key
                st.session_state.score_preview_loaded_key = key
                st.rerun(scope="fragment")


def hook_type_from_variant(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    clean = re.sub(r"^v\d+_?", "", text)
    clean = clean.replace("_", " ").strip()
    return clean.title() if clean else text


def render_detail_flags_panel(selected: pd.Series, compliance_result: dict[str, Any]) -> None:
    raw = selected.get("_raw", {}) if isinstance(selected.get("_raw", {}), dict) else {}
    base_raw = selected.get("_base_raw", {}) if isinstance(selected.get("_base_raw", {}), dict) else {}
    flags = score_flags_list(selected.get("Flags"))
    if not flags or selected.get("Flags") == "inherits base":
        flags = score_flags_list(base_raw.get("flags", []))
    if flags:
        for flag in flags:
            severity = score_single_flag_severity(flag)
            st.markdown(
                f"{severity_badge_html(severity)} `{html.escape(flag)}`",
                unsafe_allow_html=True,
            )
    else:
        st.caption("No flags for this clip.")

    violations = compliance_result.get("violations", [])
    if isinstance(violations, list) and violations:
        st.markdown("**Compliance Violations**")
        for violation in violations:
            if not isinstance(violation, dict):
                continue
            severity = str(violation.get("severity") or "medium")
            original = str(violation.get("original_text") or "")
            replacement = str(violation.get("suggested_replacement") or "")
            st.markdown(
                f"{severity_badge_html(severity)} {html.escape(original)} → {html.escape(replacement)}",
                unsafe_allow_html=True,
            )

    caps = raw.get("score_caps_applied") or base_raw.get("score_caps_applied") or []
    if isinstance(caps, list) and caps:
        st.markdown("**Score Caps Applied**")
        for cap in caps:
            st.markdown(f"- `{html.escape(str(cap))}`")


def severity_badge_html(severity: str) -> str:
    severity_key = str(severity or "none").casefold()
    colors = {
        "high": ("#fecaca", "rgba(220, 38, 38, 0.24)", "rgba(239, 68, 68, 0.36)"),
        "medium": ("#fde68a", "rgba(202, 138, 4, 0.22)", "rgba(251, 191, 36, 0.34)"),
        "low": ("#bfdbfe", "rgba(59, 130, 246, 0.18)", "rgba(96, 165, 250, 0.30)"),
        "none": ("#bbf7d0", "rgba(34, 197, 94, 0.16)", "rgba(34, 197, 94, 0.30)"),
    }
    color, background, border = colors.get(severity_key, colors["none"])
    return (
        f"<span class='score-table-badge' style='min-width:3.2rem;color:{color};background:{background};border-color:{border};'>"
        f"{html.escape(severity_key.title())}</span>"
    )


def render_score_methodology() -> None:
    with st.container(border=True):
        st.markdown("<div class='panel-title'>How Scores Are Calculated</div>", unsafe_allow_html=True)
        st.markdown(
            """
            **Total Score** is a weighted average of available dimensions: Content 46.7%, Quality 20%, Engagement 33.3%. Host Focus receives 20% only when the vision scorer returns a score, with the other weights scaled down proportionally.

            Base clip scores are calculated once per original clip ID, then inherited by all rendered variants of that clip.

            **Content** uses Qwen text scoring on the clip transcript plus deterministic keyword checks to label actual focus: promo, demo, benefit, ingredient, or product-only. Clips that only discuss price or promotion without benefit, demo, ingredient, or product explanation are capped low on content.

            **Host Focus** is optional Qwen2.5-VL scoring. Every configured sample frame is classified as A engaged with livestream, B looking down at a personal device, or C not attending to the stream. When contact-sheet batching is enabled, the same frames are labeled in one numbered grid request per base clip, with per-frame fallback on invalid responses. Score = A frames / scored frames * 10.

            **Hook** is returned by the same text-model pass as Content and Engagement. It scores the opening window when word timestamps are available, falls back to the early transcript excerpt when they are not, and is not included in Total Score yet.

            **Quality** uses FFprobe/FFmpeg for clip duration, loudness, and silence detection.

            **Engagement** is returned by the same text-model pass as Content and Hook, with the keyword scanner kept as fallback for price/promo, product mentions, demo signals, and benefit claims.

            **Similarity** is variant-only. OpenCV samples frames from sibling variants of the same base clip and compares HSV histograms; higher scores mean the variant looks more visually distinct from its siblings.
            """
        )


def score_text_color(score: float | None) -> str:
    if score is None:
        return "#94a3b8"
    if score >= 7:
        return "#22c55e"
    if score >= 5:
        return "#fbbf24"
    return "#ef4444"


def score_band_label(score: float | None) -> str:
    if score is None:
        return "Unavailable"
    if score >= 7:
        return "Green"
    if score >= 5:
        return "Yellow"
    return "Red"


def render_queue_control_panel() -> None:
    snapshot = load_queue_control_snapshot(st.session_state.state_path_value)
    control = snapshot.get("control") if isinstance(snapshot.get("control"), dict) else {}
    supervisor = snapshot.get("supervisor") if isinstance(snapshot.get("supervisor"), dict) else {}
    queue_summary = supervisor.get("queue_summary") if isinstance(supervisor.get("queue_summary"), dict) else {}
    current_run = supervisor.get("current_run_tag") or control.get("current_run_tag") or "-"
    status = supervisor.get("status") or control.get("status") or "idle"
    requested = control.get("requested_action", "run")
    reason = queue_summary.get("reason", "")

    with st.container(border=True):
        st.markdown(
            f"""
            <div class="mobile-queue-status-card">
                <div class="mobile-card-head">
                    <div>
                        <div class="mobile-card-title">Queue {html.escape(str(status).title())}</div>
                        <div class="mobile-card-meta">Requested: {html.escape(str(requested).replace('_', ' ').title())}</div>
                    </div>
                    <span class="status-badge status-processing">{html.escape(str(current_run))}</span>
                </div>
                <div class="mobile-card-meta">{html.escape(str(reason or 'No terminal summary yet.'))}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown("<div class='queue-control-desktop-anchor'></div>", unsafe_allow_html=True)
        cols = st.columns([1.1, 1.1, 1.1, 2.6], gap="medium")
        with cols[0]:
            st.markdown("<div class='small-muted'>Current Run</div>", unsafe_allow_html=True)
            st.markdown(f"### {html.escape(str(current_run))}")
        with cols[1]:
            st.markdown("<div class='small-muted'>Supervisor</div>", unsafe_allow_html=True)
            st.markdown(f"### {html.escape(str(status).title())}")
        with cols[2]:
            st.markdown("<div class='small-muted'>Requested Action</div>", unsafe_allow_html=True)
            st.markdown(f"### {html.escape(str(requested).replace('_', ' ').title())}")
        with cols[3]:
            st.markdown("<div class='small-muted'>Terminal Check</div>", unsafe_allow_html=True)
            st.caption(reason or "No terminal summary yet.")

        action_cols = st.columns([1, 1, 1, 3], gap="small")
        with action_cols[0]:
            if st.button("Start", key="queue_start", use_container_width=True, type="secondary"):
                queue_control.request_start(DEFAULT_CONTROL_FILE)
                load_queue_control_snapshot.clear()
                st.success("Start requested. Launch the supervisor if it is not already running.")
        with action_cols[1]:
            if st.button("Continue", key="queue_continue", use_container_width=True, type="primary"):
                queue_control.request_continue(DEFAULT_CONTROL_FILE)
                load_queue_control_snapshot.clear()
                st.success("Continue requested for the current run.")
        with action_cols[2]:
            if st.button("Graceful Stop", key="queue_stop", use_container_width=True, type="secondary"):
                queue_control.request_stop(DEFAULT_CONTROL_FILE)
                load_queue_control_snapshot.clear()
                st.warning("Graceful stop requested. Active clip renders will finish first.")
        with action_cols[3]:
            st.caption(f"Control file: {DEFAULT_CONTROL_FILE}")


def render_queues_tab(summary: dict[str, Any]) -> None:
    render_page_intro("Queues", "Queue depth, pending items, and per-stage throughput at a glance.")
    render_queue_control_panel()
    st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)
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
        "Paused": "status-waiting",
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
        <div class="table-shell video-table-desktop">
            <table class="video-table">
                <thead><tr>{header_html}</tr></thead>
                <tbody>{body_html}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_video_mobile_cards(table_df: pd.DataFrame) -> None:
    cards = []
    for _, row in table_df.iterrows():
        progress = max(0, min(int(row.get("Progress", 0) or 0), 100))
        status = str(row.get("Status", ""))
        cards.append(
            "<div class='mobile-card'>"
            "<div class='mobile-card-head'>"
            f"<div class='mobile-card-title'>{html.escape(str(row.get('Video Name', '-')))}</div>"
            f"{build_status_badge(status)}"
            "</div>"
            "<div class='mobile-card-grid'>"
            "<div class='mobile-card-stat'>"
            "<div class='mobile-card-stat-label'>Step</div>"
            f"<div class='mobile-card-stat-value'>{html.escape(str(row.get('Current Step', '-')))}</div>"
            "</div>"
            "<div class='mobile-card-stat'>"
            "<div class='mobile-card-stat-label'>Clips</div>"
            f"<div class='mobile-card-stat-value'>{int(row.get('Clips Generated', 0) or 0):,}</div>"
            "</div>"
            "<div class='mobile-card-stat'>"
            "<div class='mobile-card-stat-label'>Runs</div>"
            f"<div class='mobile-card-stat-value'>{int(row.get('Runs', 0) or 0):,}</div>"
            "</div>"
            "<div class='mobile-card-stat'>"
            "<div class='mobile-card-stat-label'>Duration</div>"
            f"<div class='mobile-card-stat-value'>{html.escape(str(row.get('Duration', '-')))}</div>"
            "</div>"
            "</div>"
            "<div class='mobile-progress'>"
            "<div class='mobile-progress-track'>"
            f"<div class='mobile-progress-fill' style='width:{progress}%;'></div>"
            "</div>"
            f"<div class='mobile-progress-label'>Progress {progress}%"
            f" | Started {html.escape(str(row.get('Started At', '-')))}</div>"
            "</div>"
            "</div>"
        )
    st.markdown(f"<div class='mobile-card-list'>{''.join(cards)}</div>", unsafe_allow_html=True)


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
    if st.session_state.auto_refresh_enabled and st.session_state.active_tab in AUTO_REFRESH_TABS
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
        st.markdown("<div class='desktop-left-rail-anchor'></div>", unsafe_allow_html=True)
        with st.container(border=True):
            st.markdown("<div class='nav-shell'>", unsafe_allow_html=True)
            render_nav_controls(active_tab)
            st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)
            render_system_panel(system)
            st.markdown("</div>", unsafe_allow_html=True)

    with main_area:
        render_mobile_nav_controls(active_tab)
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
        elif active_tab == "compliance":
            render_compliance_tab(summary)
        elif active_tab == "modules":
            render_modules_tab(summary)
        elif active_tab == "trends":
            render_trends_tab(summary)
        elif active_tab == "focus_debug":
            render_focus_debug_tab(summary)
        elif active_tab == "queues":
            render_queues_tab(summary)
        elif active_tab == "settings":
            render_settings_tab()


render_dashboard()
