from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from clipper_app.modular_scanner.transcripts import transcript_fingerprint
from clipper_app.modular_variant_pilot.transcript_bridge import (
    BRIDGE_VERSION,
    SourceTranscriptResolver,
    bridge_composition_words,
    crop_and_remap_words,
)


def _source_item(tmp_path: Path, *, source_id: str = "source-a", scan_id: str = "scan-a") -> dict:
    return {
        "position": 0, "role": "hook", "source_id": source_id, "scan_id": scan_id,
        "canonical_path": str(tmp_path / "vod.mp4"), "source_file_size": 123,
        "source_mtime_ns": 456, "source_content_fingerprint": "content-a",
        "start_seconds": 10.0, "end_seconds": 12.0, "transcript_text": "alpha beta",
    }


def _library(tmp_path: Path, item: dict, raw: dict, *, attached_source_id: str | None = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text(json.dumps(raw), encoding="utf-8")
    db_path = tmp_path / "library.sqlite3"
    db = sqlite3.connect(db_path)
    db.executescript("""
        CREATE TABLE media_sources(source_id TEXT PRIMARY KEY,canonical_path TEXT,file_size INTEGER,mtime_ns INTEGER,content_fingerprint TEXT);
        CREATE TABLE transcripts(transcript_id TEXT PRIMARY KEY,source_id TEXT,origin TEXT,cache_path TEXT,transcript_fingerprint TEXT,schema_version INTEGER,status TEXT);
        CREATE TABLE scans(scan_id TEXT PRIMARY KEY,source_id TEXT,transcript_id TEXT,status TEXT);
    """)
    source_id = attached_source_id or item["source_id"]
    db.execute("INSERT INTO media_sources VALUES(?,?,?,?,?)", (
        source_id, item["canonical_path"], item["source_file_size"], item["source_mtime_ns"],
        item["source_content_fingerprint"],
    ))
    db.execute("INSERT INTO transcripts VALUES(?,?,?,?,?,?,?)", (
        "transcript-a", source_id, "production", str(transcript_path), transcript_fingerprint(raw), 3, "completed",
    ))
    db.execute("INSERT INTO scans VALUES(?,?,?,?)", (item["scan_id"], source_id, "transcript-a", "completed"))
    db.commit(); db.close()
    return db_path


def _raw(tmp_path: Path, *, words=True) -> dict:
    segment = {"start": 9.8, "end": 12.2, "text": "before alpha beta after"}
    payload = {
        "segments": [segment],
        "metadata": {"source_video_path": str(tmp_path / "vod.mp4"), "timestamp_precision": "float_seconds"},
    }
    if words:
        payload["words"] = [
            {"word": "before", "start": 9.7, "end": 10.01, "probability": .7},
            {"word": "alpha", "start": 10.1, "end": 10.4, "probability": .8},
            {"word": "beta", "start": 11.7, "end": 12.1, "probability": .9},
            {"word": "after", "start": 12.1, "end": 12.3, "probability": .6},
        ]
    return payload


def test_resolver_selects_exact_scan_source_and_rejects_wrong_identity(tmp_path):
    item = _source_item(tmp_path)
    resolver = SourceTranscriptResolver(_library(tmp_path, item, _raw(tmp_path)))
    resolved = resolver.resolve(item)
    assert resolved and resolved.transcript_id == "transcript-a"
    assert resolved.words[1]["word"] == "alpha"
    assert resolver.resolve({**item, "source_id": "wrong-source"}) is None
    assert SourceTranscriptResolver(resolver.library_path).resolve({**item, "source_content_fingerprint": "stale"}) is None


def test_crop_clamps_meaningful_boundary_words_and_drops_tiny_overlap():
    words, counts = crop_and_remap_words([
        {"word": "tiny", "start": 9.8, "end": 10.01},
        {"word": "start", "start": 9.98, "end": 10.2},
        {"word": "inside", "start": 10.5, "end": 10.8},
        {"word": "end", "start": 11.9, "end": 12.2},
        {"word": "outside", "start": 12.2, "end": 12.4},
    ], 10.0, 12.0, 20.0)
    assert [word["word"] for word in words] == ["start", "inside", "end"]
    assert words[0]["start"] == 20.0 and words[-1]["end"] == 22.0
    assert counts == {"included": 3, "boundary_clamped": 2, "dropped_at_cuts": 1}


def test_three_item_timeline_uses_exact_source_range_durations(tmp_path):
    class Resolver:
        def resolve(self, item):
            from clipper_app.modular_variant_pilot.transcript_bridge import SourceTranscript
            start = float(item["start_seconds"])
            return SourceTranscript(
                item["scan_id"], item["scan_id"], 3, "production", "cache",
                ({"word": item["role"], "start": start + .25, "end": start + .75},), (), {},
            )

    items = []
    for position, (role, start, end) in enumerate((("hook", 100, 120), ("benefits", 500, 522), ("cta", 900, 918))):
        items.append({"position": position, "role": role, "scan_id": f"scan-{position}", "source_id": f"source-{position}",
                      "start_seconds": start, "end_seconds": end, "transcript_text": role})
    words, _, diagnostics = bridge_composition_words({"items": items}, Resolver(), rendered_duration=60.0)
    assert [(word["start"], word["end"]) for word in words] == [(.25, .75), (20.25, 20.75), (42.25, 42.75)]
    assert diagnostics["base_duration"] == 60.0
    assert diagnostics["timing_mode"] == "source_word_timestamps"
    assert diagnostics["validation_result"] == "valid"


def test_fallback_hierarchy_prefers_words_then_segments_then_synthetic(tmp_path):
    item = _source_item(tmp_path)
    word_resolver = SourceTranscriptResolver(_library(tmp_path / "word", item, _raw(tmp_path, words=True)))
    words, _, diag = bridge_composition_words({"items": [item]}, word_resolver, rendered_duration=2.0)
    assert diag["timing_mode"] == "source_word_timestamps" and [w["word"] for w in words] == ["alpha", "beta"]

    segment_root = tmp_path / "segment"; segment_root.mkdir()
    segment_item = {**item, "canonical_path": str(segment_root / "vod.mp4")}
    segment_resolver = SourceTranscriptResolver(_library(segment_root, segment_item, _raw(segment_root, words=False)))
    _, _, diag = bridge_composition_words({"items": [segment_item]}, segment_resolver, rendered_duration=2.0)
    assert diag["timing_mode"] == "source_segment_timestamps"

    _, _, diag = bridge_composition_words({"items": [item]}, SourceTranscriptResolver(tmp_path / "missing.sqlite3"), rendered_duration=2.0)
    assert diag["timing_mode"] == "synthetic_distribution"
    assert diag["fallback_items"] == [0]
    assert diag["bridge_version"] == BRIDGE_VERSION


def test_comparison_diagnostic_retains_real_source_timestamp(tmp_path):
    item = _source_item(tmp_path)
    resolver = SourceTranscriptResolver(_library(tmp_path, item, _raw(tmp_path)))
    _, _, diagnostics = bridge_composition_words({"items": [item]}, resolver, rendered_duration=2.0)
    assert diagnostics["comparison_examples"][0]["source_timestamp"] == 10.1
    assert diagnostics["comparison_examples"][0]["new_base_timestamp"] == .1
    assert diagnostics["comparison_examples"][0]["old_synthetic_base_timestamp"] == 0.0


def test_transitional_hook_concat_offsets_already_burned_base_subtitles():
    from ffmpeg_editor import _add_transitional_hook_concat_filters

    filters = []
    video, audio = _add_transitional_hook_concat_filters(
        filters, "[vsub]", "[abase]", 1, {"duration": 3.25, "has_audio": False}, 720, 1280, 30,
    )
    joined = ";".join(filters)
    assert "[vsub]setpts=PTS-STARTPTS" in joined
    assert "[vtranshook][atranshook][vclipmain][aclipmain]concat=n=2" in joined
    assert video == "[vtransout]" and audio == "[atransout]"
