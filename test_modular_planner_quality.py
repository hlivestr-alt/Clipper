from __future__ import annotations

import pytest

from clipper_app.modular_planner.quality import (
    JoinabilityEvaluator,
    normalize_transcript,
    topical_continuity_score,
)


@pytest.mark.parametrize("transcript", [
    "Ya mendingan kalian ganti aja ke facial cleanser dari",
    "kalau mau ambil dari",
    "untuk etalase nomor",
])
def test_obviously_cut_off_endings_are_hard_unusable(transcript):
    result = JoinabilityEvaluator().evaluate(transcript)
    assert result.hard_unusable
    assert result.end_quality == "truncated"


def test_complete_etalase_reference_is_clean_at_the_end():
    result = JoinabilityEvaluator().evaluate("cek produknya di etalase nomor satu")
    assert not result.hard_unusable
    assert result.end_quality == "clean"


@pytest.mark.parametrize("transcript", [
    "dan juga sudah aman banget untuk dipakai",
    "seperti yang tadi aku bilang produk ini lembap",
    "Jadi cuma di 89 ribuan saja",
])
def test_context_dependent_starts_receive_soft_penalty(transcript):
    result = JoinabilityEvaluator().evaluate(transcript)
    assert not result.hard_unusable
    assert result.joinability_score < 1.0
    assert result.boundary_label == "Contextual"


@pytest.mark.parametrize("transcript", [
    "Nah ini serumnya untuk kulit kusam",
    "Ya kalau kulit kalian kusam coba yang ini",
    "Nih buat kalian yang kulitnya kering",
    "Jadi kulit terasa lebih lembap",
])
def test_normal_conversational_openings_are_not_penalized(transcript):
    result = JoinabilityEvaluator().evaluate(transcript)
    assert not result.hard_unusable
    assert result.joinability_score == 1.0


def test_continuity_normalizes_unicode_case_and_related_concepts():
    assert normalize_transcript("  KULIT\u3000KERING  ") == "kulit kering"
    related = topical_continuity_score(
        "Debu dan POLUSI bikin kulit kotor",
        "membersihkan wajah sekaligus menjaga kulit lembap",
    )
    unrelated = topical_continuity_score(
        "flek dan noda hitam",
        "kulit kusam jadi lebih cerah",
    )
    assert related > unrelated
    assert related > 0


def test_product_name_alone_does_not_create_continuity_bonus():
    assert topical_continuity_score("serum", "serum") == 0.0
    assert topical_continuity_score("cleanser produk ini", "cleanser produk ini") == 0.0
