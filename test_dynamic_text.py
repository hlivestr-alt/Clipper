from __future__ import annotations

from types import SimpleNamespace

import dynamic_text


def _index():
    facts = [
        {
            "id": "ingredient-1",
            "product": "toner",
            "role": "ingredient",
            "text": "Niacinamide",
            "source_file": "toner.pdf",
            "locator": {"kind": "pdf", "page": 2, "line": 4},
            "source_excerpt": "Ingredients: Niacinamide",
            "confidence": 0.98,
            "eligible": True,
            "conflicted": False,
        },
        {
            "id": "benefit-1",
            "product": "toner",
            "role": "benefit",
            "text": "Membantu menjaga kelembapan kulit",
            "source_file": "toner.pdf",
            "locator": {"kind": "pdf", "page": 2, "line": 8},
            "source_excerpt": "Benefits: Membantu menjaga kelembapan kulit",
            "confidence": 0.98,
            "eligible": True,
            "conflicted": False,
        },
        {
            "id": "usage-1",
            "product": "toner",
            "role": "usage",
            "text": "Gunakan setelah membersihkan wajah",
            "source_file": "toner.pdf",
            "locator": {"kind": "pdf", "page": 3, "line": 2},
            "source_excerpt": "Cara pakai: Gunakan setelah membersihkan wajah",
            "confidence": 0.98,
            "eligible": True,
            "conflicted": False,
        },
    ]
    return {
        "revision": "info-revision",
        "products": {
            "toner": {"product_key": "toner", "label": "Toner", "facts": facts},
        },
    }


def _variant(mode="balanced"):
    return SimpleNamespace(
        dynamic_text_mode=mode,
        dynamic_text_roles=("ingredients", "benefits", "usage", "cta"),
        variant_index=1,
        hook_type="text",
        hook_duration=1.5,
    )


def _words(text: str, start: float, step: float = 0.28):
    return [
        {
            "word": word,
            "start": round(start + index * step, 3),
            "end": round(start + index * step + 0.22, 3),
        }
        for index, word in enumerate(text.split())
    ]


def test_balanced_plan_uses_speech_triggered_document_fact(monkeypatch):
    monkeypatch.setattr(dynamic_text, "load_product_information_index", lambda cfg: _index())
    moment = {
        "clip_id": "clip_0001",
        "start": 0,
        "product": "toner",
        "clip_type": "tips",
        "content_focus": "benefit",
    }

    plan = dynamic_text.build_dynamic_text_plan(
        moment,
        _words("fungsi produk ini untuk kulit kusam", 2.1),
        [],
        15.0,
        SimpleNamespace(HOOK_DURATION=1.5),
        variant=_variant(),
    )

    assert 1 <= len(plan["items"]) <= 4
    assert plan["product_key"] == "toner"
    assert plan["product_information_revision"] == "info-revision"
    rendered = dynamic_text.dynamic_plan_text(plan)
    assert "Membantu menjaga kelembapan kulit" in rendered
    assert "Niacinamide" not in rendered
    assert all(item["start"] >= 1.85 for item in plan["items"])
    assert all(item["end"] <= 15.0 for item in plan["items"])
    assert any(
        evidence.get("source_file") == "toner.pdf"
        for item in plan["items"]
        for evidence in item.get("evidence", [])
    )
    benefit = next(item for item in plan["items"] if item.get("content_role") == "benefits")
    assert benefit["role"] == "fact_badge"
    assert benefit["lines"] == []
    assert len(benefit["text"].split()) <= 6
    assert benefit["speech_evidence"]["matched_terms"] == ["fungsi", "kusam"]
    assert benefit["speech_evidence"]["start"] == 2.1
    assert plan["schema_version"] == 7
    assert all(item["layout"]["side"] in {"left", "right"} for item in plan["items"])
    assert all(0.38 <= item["layout"]["max_width_ratio"] <= 0.42 for item in plan["items"])


def test_dynamic_layout_alternates_deterministically_and_avoids_subtitle_band():
    items = [
        {"role": "checklist", "start": 2.0, "end": 4.0},
        {"role": "product_card", "start": 4.0, "end": 6.0},
        {"role": "closing_cta", "start": 6.0, "end": 7.0},
    ]

    first = dynamic_text.resolve_dynamic_text_layout(
        items,
        clip_id="clip-1",
        variant_index=2,
        subtitle_position="center",
    )
    second = dynamic_text.resolve_dynamic_text_layout(
        items,
        clip_id="clip-1",
        variant_index=2,
        subtitle_position="center",
    )

    assert first == second
    assert [item["layout"]["side"] for item in first] in (
        ["left", "right", "left"],
        ["right", "left", "right"],
    )
    assert all(item["layout"]["band"] in {"upper", "lower"} for item in first)


def test_dynamic_layout_preserves_valid_manifest_layout_and_avoids_letterbox_bands():
    items = [{
        "role": "fact_badge",
        "start": 1.0,
        "end": 3.0,
        "layout": {
            "side": "right",
            "band": "middle",
            "max_width_ratio": 0.39,
            "font_scale": 0.9,
        },
    }]

    resolved = dynamic_text.resolve_dynamic_text_layout(
        items,
        subtitle_position="top",
        letterbox_top_frac=0.25,
        letterbox_bottom_frac=0.25,
    )

    assert resolved[0]["layout"] == {
        "side": "right",
        "band": "middle",
        "max_width_ratio": 0.39,
        "font_scale": 0.9,
    }


def test_short_and_disabled_clips_skip_dynamic_text(monkeypatch):
    monkeypatch.setattr(dynamic_text, "load_product_information_index", lambda cfg: _index())
    moment = {"clip_id": "clip", "product": "toner", "clip_type": "tips"}

    short = dynamic_text.build_dynamic_text_plan(
        moment, [], [], 4.9, SimpleNamespace(HOOK_DURATION=1.0), variant=_variant()
    )
    disabled = dynamic_text.build_dynamic_text_plan(
        moment, [], [], 15.0, SimpleNamespace(HOOK_DURATION=1.0), variant=_variant("off")
    )

    assert short["items"] == []
    assert short["skip_reason"] == "clip_too_short"
    assert disabled["items"] == []
    assert disabled["skip_reason"] == "disabled"


def test_overlay_compliance_drops_only_unsafe_item():
    plan = {
        "items": [
            {"role": "checklist", "headline": "FUNGSI:", "lines": ["pasti menyembuhkan jerawat"], "source": "document"},
            {"role": "product_card", "text": "TONER", "source": "product_identity"},
        ],
        "dropped": [],
    }
    compliance = {
        "violations": [
            {
                "source_field": "overlay",
                "original_text": "pasti menyembuhkan",
                "severity": "high",
                "suggested_replacement": "",
            }
        ]
    }

    filtered = dynamic_text.apply_compliance_to_dynamic_plan(plan, compliance)
    blocking = dynamic_text.compliance_blocking_result(compliance)

    assert [item["role"] for item in filtered["items"]] == ["product_card"]
    assert filtered["dropped"][0]["reason"] == "compliance_high"
    assert blocking["violations"] == []


def test_document_facts_do_not_trigger_information_without_speech(monkeypatch):
    monkeypatch.setattr(dynamic_text, "load_product_information_index", lambda cfg: _index())
    plan = dynamic_text.build_dynamic_text_plan(
        {"clip_id": "clip", "product": "toner", "clip_type": "tips"},
        [],
        [],
        10.0,
        SimpleNamespace(HOOK_DURATION=1.0),
        variant=_variant("balanced"),
    )

    assert all(
        item.get("content_role") not in {"ingredients", "benefits", "usage"}
        for item in plan["items"]
    )
    assert "Niacinamide" not in dynamic_text.dynamic_plan_text(plan)
    assert not any(item.get("role") == "product_card" for item in plan["items"])


def test_no_generic_overlay_is_added_to_fill_density(monkeypatch):
    monkeypatch.setattr(
        dynamic_text,
        "load_product_information_index",
        lambda cfg: {"revision": "empty", "products": {}},
    )
    plan = dynamic_text.build_dynamic_text_plan(
        {"clip_id": "clip", "product": "general", "clip_type": "qna"},
        [],
        [],
        10.0,
        SimpleNamespace(HOOK_DURATION=1.0),
        variant=_variant("minimal"),
    )

    text = dynamic_text.dynamic_plan_text(plan)
    assert text == ""
    assert plan["items"] == []


def test_information_sections_are_once_only_and_speech_synced(monkeypatch):
    monkeypatch.setattr(dynamic_text, "load_product_information_index", lambda cfg: _index())
    words = [
        *_words("kandungan produk niacinamide", 2.0),
        *_words("kandungan lainnya vitamin c", 4.5),
        *_words("fungsi produk menjaga kulit lembap", 7.0),
        *_words("cara pakai gunakan setelah cleanser", 10.0),
    ]

    plan = dynamic_text.build_dynamic_text_plan(
        {"clip_id": "clip-once", "product": "toner", "clip_type": "demo"},
        words,
        [],
        15.0,
        SimpleNamespace(HOOK_DURATION=1.5),
        variant=_variant("high_energy"),
    )

    information = [
        item
        for item in plan["items"]
        if item.get("content_role") in {"ingredients", "benefits", "usage"}
    ]
    assert [item["content_role"] for item in information] == [
        "ingredients",
        "benefits",
        "usage",
    ]
    assert len({item["content_role"] for item in information}) == len(information)
    assert information[0]["start"] == 2.0
    assert information[1]["start"] == 7.0
    assert information[2]["start"] == 10.0
    assert information[2]["role"] == "usage_step"
    assert information[2]["lines"] == []
    assert len(information[2]["text"].split()) <= 6
    assert any(item["reason"] == "duplicate_content_role" for item in plan["dropped"])


def test_information_uses_configured_fixed_duration():
    candidate = {
        "role": "checklist",
        "content_role": "ingredients",
        "headline": "KANDUNGAN UTAMA:",
        "lines": ["Niacinamide"],
        "priority": 100,
        "speech_evidence": {
            "excerpt": "kandungan niacinamide",
            "start": 2.0,
            "end": 2.4,
            "matched_terms": ["kandungan", "niacinamide"],
            "score": 200,
        },
    }

    items, dropped = dynamic_text._schedule_items(
        [candidate],
        12.0,
        hook_duration=1.0,
        settings={"ingredients": {"duration_seconds": 4.2}},
    )

    assert dropped == []
    assert items[0]["start"] == 2.0
    assert items[0]["end"] == 6.2
    assert items[0]["end"] - items[0]["start"] == 4.2


def test_concise_fact_text_uses_exact_short_clause():
    source = "Manfaat Utama: Mengunci kelembapan, memperbaiki barrier, menghaluskan kulit."

    result = dynamic_text.concise_dynamic_fact_text(source, max_words=6)

    assert result == "menghaluskan kulit"
    assert result in source


def test_pre_hook_topics_are_rejected(monkeypatch):
    monkeypatch.setattr(dynamic_text, "load_product_information_index", lambda cfg: _index())
    plan = dynamic_text.build_dynamic_text_plan(
        {"clip_id": "clip-hook", "product": "toner", "clip_type": "tips"},
        _words("kandungan niacinamide", 1.0),
        [],
        10.0,
        SimpleNamespace(HOOK_DURATION=1.5),
        variant=_variant(),
    )

    assert not any(item.get("content_role") == "ingredients" for item in plan["items"])
    assert any(item["reason"] == "before_hook" for item in plan["dropped"])


def test_mixed_phrase_keeps_strongest_then_allows_later_separate_topic(monkeypatch):
    monkeypatch.setattr(dynamic_text, "load_product_information_index", lambda cfg: _index())
    words = [
        *_words("niacinamide untuk mencerahkan", 2.0),
        *_words("fungsi produk membantu kulit lembap dan glowing", 6.0),
    ]
    plan = dynamic_text.build_dynamic_text_plan(
        {"clip_id": "clip-mixed", "product": "toner", "clip_type": "tips"},
        words,
        [],
        12.0,
        SimpleNamespace(HOOK_DURATION=1.0),
        variant=_variant("high_energy"),
    )

    information_roles = [
        item.get("content_role")
        for item in plan["items"]
        if item.get("content_role") in {"ingredients", "benefits", "usage"}
    ]
    assert information_roles == ["ingredients", "benefits"]
    assert any(
        item["reason"] == "weaker_overlapping_topic"
        and item["content_role"] == "benefits"
        for item in plan["dropped"]
    )


def test_repeated_winning_role_does_not_block_new_role_in_later_phrase(monkeypatch):
    monkeypatch.setattr(dynamic_text, "load_product_information_index", lambda cfg: _index())
    words = [
        *_words("kandungan niacinamide", 2.0),
        *_words("kandungan niacinamide untuk mencerahkan", 6.0),
    ]
    plan = dynamic_text.build_dynamic_text_plan(
        {"clip_id": "clip-repeat-winner", "product": "toner", "clip_type": "tips"},
        words,
        [],
        12.0,
        SimpleNamespace(HOOK_DURATION=1.0),
        variant=_variant("high_energy"),
    )

    information_roles = [
        item.get("content_role")
        for item in plan["items"]
        if item.get("content_role") in {"ingredients", "benefits", "usage"}
    ]
    assert information_roles == ["ingredients", "benefits"]
    assert any(
        item["reason"] == "duplicate_content_role"
        and item["content_role"] == "ingredients"
        for item in plan["dropped"]
    )


def test_transcript_clause_is_used_when_document_role_is_missing(monkeypatch):
    index = _index()
    index["products"]["toner"]["facts"] = [
        fact for fact in index["products"]["toner"]["facts"] if fact["role"] != "ingredient"
    ]
    monkeypatch.setattr(dynamic_text, "load_product_information_index", lambda cfg: index)
    plan = dynamic_text.build_dynamic_text_plan(
        {"clip_id": "clip-transcript", "product": "toner", "clip_type": "tips"},
        _words("kandungan khususnya niacinamide", 2.0),
        [],
        10.0,
        SimpleNamespace(HOOK_DURATION=1.0),
        variant=_variant(),
    )

    ingredient = next(item for item in plan["items"] if item.get("content_role") == "ingredients")
    assert ingredient["source"] == "transcript"
    assert ingredient["lines"] == ["kandungan khususnya niacinamide"]
    assert ingredient["speech_evidence"]["start"] == 2.0


def test_speech_evidence_timing_is_remapped_for_speed():
    plan = {
        "items": [{
            "role": "checklist",
            "content_role": "ingredients",
            "start": 2.0,
            "end": 4.0,
            "speech_evidence": {
                "excerpt": "kandungan niacinamide",
                "start": 2.0,
                "end": 2.8,
                "matched_terms": ["kandungan", "niacinamide"],
                "score": 200,
            },
        }],
    }

    remapped = dynamic_text.remap_dynamic_plan_for_speed(plan, 1.25)

    assert remapped["items"][0]["start"] == 1.6
    assert remapped["items"][0]["end"] == 3.2
    assert remapped["items"][0]["speech_evidence"]["start"] == 1.6
    assert remapped["items"][0]["speech_evidence"]["end"] == 2.24


def test_speech_evidence_timing_is_remapped_after_silence_removal():
    plan = {
        "items": [{
            "role": "checklist",
            "content_role": "benefits",
            "start": 4.0,
            "end": 5.5,
            "speech_evidence": {
                "excerpt": "fungsi produk menjaga kulit lembap",
                "start": 4.0,
                "end": 4.8,
                "matched_terms": ["fungsi", "lembap"],
                "score": 200,
            },
        }],
    }
    silence_plan = {
        "trimmed": True,
        "kept_ranges": [
            {
                "source_start": 0.0,
                "source_end": 2.0,
                "output_start": 0.0,
                "output_end": 2.0,
            },
            {
                "source_start": 4.0,
                "source_end": 6.0,
                "output_start": 2.0,
                "output_end": 4.0,
            },
        ],
    }

    remapped = dynamic_text.remap_dynamic_plan_for_silence(plan, silence_plan)

    assert remapped["items"][0]["start"] == 2.0
    assert remapped["items"][0]["end"] == 3.5
    assert remapped["items"][0]["speech_evidence"]["start"] == 2.0
    assert remapped["items"][0]["speech_evidence"]["end"] == 2.8


def test_old_dynamic_plan_schema_is_rebuilt(monkeypatch):
    from main import _ensure_job_dynamic_text_plan

    expected = {"schema_version": dynamic_text.PLAN_SCHEMA_VERSION, "items": []}
    calls = []

    def fake_builder(*args, **kwargs):
        calls.append((args, kwargs))
        return expected

    monkeypatch.setattr(dynamic_text, "build_dynamic_text_plan", fake_builder)
    job = {
        "clip_id": "clip-old",
        "start": 0.0,
        "end": 10.0,
        "moment": {},
        "dynamic_text_plan": {"schema_version": 2, "items": [{"text": "stale"}]},
    }

    result = _ensure_job_dynamic_text_plan(job, [], [], SimpleNamespace())

    assert result is expected
    assert job["dynamic_text_plan"] is expected
    assert len(calls) == 1
