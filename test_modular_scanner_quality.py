from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from clipper_app.modular_scanner.analyzer import SYSTEM_PROMPT
from clipper_app.modular_scanner.constants import (
    ACTIVE_PRODUCT_CONTEXT_SECONDS,
    ANALYZER_VERSION,
    COMPOSITION_MAXIMUM_CHAIN_LENGTH,
    COMPOSITION_MAXIMUM_GAP_SECONDS,
    CROSS_WINDOW_PRODUCT_CONFLICT_IOU_THRESHOLD,
    PROMPT_VERSION,
)
from clipper_app.modular_scanner.repository import ScannerRepository
from clipper_app.modular_scanner.service import ModularScannerService
from clipper_app.modular_scanner.transcripts import build_windows
from clipper_app.modular_scanner.validation import (
    build_product_context,
    compose_candidates,
    deduplicate,
    product_evidence,
    resolve_cross_window_product_conflicts,
    validate_candidate,
)


def transcript(*segments: tuple[float, float, str]) -> dict:
    return {"segments": [{"start": start, "end": end, "text": text} for start, end, text in segments]}


def one_window(value: dict) -> dict:
    return build_windows(value, character_budget=100_000, overlap_seconds=10.0)[0]


def candidate(product: str, start: float, end: float, *, role: str = "benefits") -> dict:
    return {
        "start_seconds": start,
        "end_seconds": end,
        "product": product,
        "role": role,
        "confidence": 0.78,
        "reason": "Focused regression candidate",
    }


class SequenceAnalyzer:
    def __init__(self, responses: list[list[dict]]):
        self.responses = responses
        self.calls: list[dict] = []

    def analyze(self, window):
        self.calls.append(window)
        return self.responses[len(self.calls) - 1]


class ModularScannerEvidenceQualityTests(unittest.TestCase):
    def test_indonesian_suffixes_and_asr_aliases_are_transcript_evidence_only(self) -> None:
        cases = {
            "hydrating tonernya": "toner",
            "serumnya membantu kulit": "serum",
            "facial cleansernya lembut": "cleanser",
            "pakai air krim untuk mata": "eye_cream",
            "gunakan sheet mesh ini": "mask",
            "creamnya terasa lembut": "skin_cream",
            "krimnya membantu kulit": "skin_cream",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertGreater(product_evidence(text).get(expected, 0), 0)

        value = one_window(transcript((0, 20, "air krim membantu area mata")))
        for invalid in ("air krim", "tonernya"):
            accepted, rejection = validate_candidate(candidate(invalid, 0, 20), value, 20)
            self.assertIsNone(accepted)
            self.assertEqual(rejection.code, "invalid_product")

    def test_direct_and_bounded_context_evidence_with_conflict_rejection(self) -> None:
        context_window = one_window(transcript(
            (0, 5, "sekarang kita bahas hydrating tonernya"),
            (5, 25, "ini membantu kulit terasa lebih lembap dan segar"),
        ))
        accepted, rejection = validate_candidate(candidate("toner", 5, 25), context_window, 25)
        self.assertIsNotNone(accepted)
        self.assertIsNone(rejection)

        conflict = one_window(transcript((0, 20, "toner dan serum tersedia untuk kulit")))
        accepted, rejection = validate_candidate(candidate("toner", 0, 20), conflict, 20)
        self.assertIsNone(accepted)
        self.assertEqual(rejection.code, "conflicting_product")

    def test_usage_tutorial_is_not_cta_but_purchase_action_is(self) -> None:
        tutorial = one_window(transcript((0, 20, "facial cleansernya taruh di telapak, tambah air, foam, pijat lalu bilas")))
        accepted, rejection = validate_candidate(candidate("cleanser", 0, 20, role="cta"), tutorial, 20)
        self.assertIsNone(accepted)
        self.assertEqual(rejection.code, "role_not_supported")

        purchase = one_window(transcript((0, 20, "facial cleansernya lagi promo, klik keranjang kuning dan checkout sekarang")))
        accepted, rejection = validate_candidate(candidate("cleanser", 0, 20, role="cta"), purchase, 20)
        self.assertIsNotNone(accepted)
        self.assertIsNone(rejection)


class ModularScannerDurationQualityTests(unittest.TestCase):
    def test_near_miss_expands_to_smallest_coherent_authoritative_boundary(self) -> None:
        for short_end in (14.66, 13.0):
            with self.subTest(short_end=short_end):
                value = one_window(transcript(
                    (0, short_end, "serum vitamin c membantu mencerahkan kulit"),
                    (short_end, 16.2, "dan membuat kulit terasa lebih lembap"),
                ))
                accepted, rejection = validate_candidate(candidate("serum", 0, short_end), value, 16.2)
                self.assertIsNone(rejection)
                self.assertEqual(accepted["end_seconds"], 16.2)
                self.assertGreaterEqual(accepted["duration_seconds"], 15.0)
                repair = accepted["validation_diagnostics"]["duration_repair"]
                self.assertEqual(repair["outcome"], "expanded")

    def test_below_ten_seconds_is_not_repaired(self) -> None:
        value = one_window(transcript(
            (0, 9.9, "serum membantu kulit"),
            (9.9, 18, "kulit menjadi lebih lembap"),
        ))
        accepted, rejection = validate_candidate(candidate("serum", 0, 9.9), value, 18)
        self.assertIsNone(accepted)
        self.assertEqual(rejection.code, "duration_too_short")
        self.assertIn("not attempted", rejection.detail)

    def test_conflicting_product_and_unrelated_filler_cannot_pad_duration(self) -> None:
        cases = (
            ((0, 13, "serum membantu kulit"), (13, 17, "sekarang toner untuk wajah")),
            ((0, 13, "serum membantu kulit"), (13, 17, "terima kasih semuanya sampai jumpa")),
        )
        for first, extension in cases:
            with self.subTest(extension=extension[2]):
                value = one_window(transcript(first, extension))
                accepted, rejection = validate_candidate(candidate("serum", 0, 13), value, 17)
                self.assertIsNone(accepted)
                self.assertEqual(rejection.code, "duration_too_short")
                self.assertIn("repair failed", rejection.detail.casefold())


class ModularScannerCompositionTests(unittest.TestCase):
    def normalized(self, value: dict, *raw: dict) -> tuple[dict, list[dict], list]:
        window = one_window(value)
        context = build_product_context(value["segments"])
        normalized = []
        for order, item in enumerate(raw):
            accepted, rejection = validate_candidate(
                item, window, window["end"], order=order, product_context=context,
                allow_short=True, attempt_duration_repair=False,
            )
            self.assertIsNone(rejection)
            normalized.append(accepted)
        return window, normalized, context

    def test_cta_price_and_checkout_compose_with_authoritative_text_and_confidence(self) -> None:
        value = transcript(
            (0, 7, "cleanser harga normal Rp222k sekarang Rp89k"),
            (7, 9, "promo ini masih berlaku"),
            (9, 17, "cleanser cek etalase nomor 1 dan checkout"),
        )
        first = {**candidate("cleanser", 0, 7, role="cta"), "confidence": 0.8}
        second = {**candidate("cleanser", 9, 17, role="cta"), "confidence": 0.6}
        window, normalized, context = self.normalized(value, first, second)
        result, diagnostics = compose_candidates(normalized, window, context)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["start_seconds"], result[0]["end_seconds"]), (0.0, 17.0))
        self.assertEqual(
            result[0]["transcript_text"],
            "cleanser harga normal Rp222k sekarang Rp89k promo ini masih berlaku cleanser cek etalase nomor 1 dan checkout",
        )
        self.assertAlmostEqual(result[0]["confidence"], 0.683333)
        self.assertEqual([item["status"] for item in diagnostics], ["composed_into_segment"] * 2)

    def test_ingredients_identity_and_explanation_compose(self) -> None:
        value = transcript(
            (0, 11, "serum Vitamin C Tranexamic Acid Alpha Arbutin Ergothioneine"),
            (11, 17, "serum membantu mencerahkan dan menyamarkan flek hitam"),
        )
        window, normalized, context = self.normalized(
            value, candidate("serum", 0, 11, role="ingredients"), candidate("serum", 11, 17, role="ingredients"),
        )
        result, _ = compose_candidates(normalized, window, context)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "ingredients")
        self.assertEqual(result[0]["duration_seconds"], 17.0)

    def test_incompatible_role_product_gap_and_transition_do_not_compose(self) -> None:
        cases = [
            (
                transcript((0, 7, "serum untuk kulit kusam"), (7, 15, "serum checkout sekarang")),
                candidate("serum", 0, 7, role="benefits"), candidate("serum", 7, 15, role="cta"),
            ),
            (
                transcript((0, 7, "serum untuk kulit kusam"), (7, 15, "toner untuk kulit berminyak")),
                candidate("serum", 0, 7), candidate("toner", 7, 15),
            ),
            (
                transcript((0, 7, "serum untuk kulit kusam"), (7, 16, "kulit tetap terasa nyaman"), (16, 23, "serum membantu flek")),
                candidate("serum", 0, 7), candidate("serum", 16, 23),
            ),
            (
                transcript((0, 7, "serum untuk kulit kusam"), (7, 9, "sekarang toner"), (9, 16, "serum membantu flek")),
                candidate("serum", 0, 7), candidate("serum", 9, 16),
            ),
        ]
        for value, first, second in cases:
            with self.subTest(value=value):
                window, normalized, context = self.normalized(value, first, second)
                result, diagnostics = compose_candidates(normalized, window, context)
                self.assertEqual(len(result), 2)
                self.assertFalse(diagnostics)

    def test_chain_is_bounded_and_stops_at_smallest_valid_range(self) -> None:
        value = transcript(*[
            (index * 5, (index + 1) * 5, f"serum vitamin c membantu kulit bagian {index}")
            for index in range(4)
        ])
        raw = [candidate("serum", index * 5, (index + 1) * 5) for index in range(4)]
        window, normalized, context = self.normalized(value, *raw)
        result, diagnostics = compose_candidates(normalized, window, context)
        composed = [item for item in result if item["duration_seconds"] >= 15]
        self.assertEqual(COMPOSITION_MAXIMUM_CHAIN_LENGTH, 3)
        self.assertEqual(len(composed), 1)
        self.assertEqual(composed[0]["end_seconds"], 15.0)
        self.assertEqual(len(diagnostics), 3)
        self.assertEqual(len(result), 2)

    def test_configured_gap_is_strict(self) -> None:
        self.assertEqual(COMPOSITION_MAXIMUM_GAP_SECONDS, 8.0)

    def test_tighter_boundary_repair_wins_and_composed_output_deduplicates_normally(self) -> None:
        value = transcript(
            (0, 7, "serum vitamin c untuk kulit kusam"),
            (7, 9, "membantu kulit tetap nyaman"),
            (9, 17, "serum vitamin c membantu flek hitam"),
        )
        window, normalized, context = self.normalized(
            value, candidate("serum", 0, 7), candidate("serum", 9, 17),
        )
        compact_repair = {
            **normalized[0], "end_seconds": 16.0, "duration_seconds": 16.0,
            "validation_diagnostics": {"duration_repair": {"outcome": "expanded"}},
        }
        result, diagnostics = compose_candidates(normalized, window, context, {0: compact_repair})
        self.assertEqual(len(result), 2)
        self.assertFalse(diagnostics)

        composed, _ = compose_candidates(normalized, window, context)
        stronger_overlap = {**composed[0], "end_seconds": 16.5, "duration_seconds": 16.5, "confidence": 0.95}
        deduped = deduplicate([composed[0], stronger_overlap])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["confidence"], 0.95)

    def test_real_july_1_cleanser_cta_composes_across_authoritative_silence(self) -> None:
        value = transcript(
            (220.0, 225.0, "sekarang facial cleanser"),
            (229.145, 232.119, "lagi ada diskon dan promo"),
            (233.023, 235.533, "harga turun jadi Rp89.000"),
            (238.402, 245.640, "cek etalase nomor 1 facial cleanser dan checkout"),
        )
        first = {**candidate("cleanser", 229.145, 235.533, role="cta"), "confidence": 0.90}
        second = {**candidate("cleanser", 238.402, 245.640, role="cta"), "confidence": 0.95}
        window, normalized, context = self.normalized(value, first, second)
        result, diagnostics = compose_candidates(normalized, window, context)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["start_seconds"], result[0]["end_seconds"]), (229.145, 245.64))
        self.assertEqual(result[0]["duration_seconds"], 16.495)
        self.assertEqual(result[0]["validation_diagnostics"]["composition"]["total_gap_seconds"], 2.869)
        self.assertEqual([item["status"] for item in diagnostics], ["composed_into_segment"] * 2)

    def test_real_toner_benefits_composition_remains_valid(self) -> None:
        value = transcript(
            (391.690, 392.838, "aku ada hydrating tonernya"),
            (397.715, 407.290, "hydrating toner melembapkan kulit wajah"),
            (412.081, 426.760, "menyeimbangkan pH meredakan kemerahan dan memperbaiki tekstur kulit"),
        )
        window, normalized, context = self.normalized(
            value,
            candidate("toner", 397.715, 407.290),
            candidate("toner", 412.081, 426.760),
        )
        result, diagnostics = compose_candidates(normalized, window, context)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["start_seconds"], result[0]["end_seconds"]), (397.715, 426.76))
        self.assertEqual(len(diagnostics), 2)

    def test_cleanser_cta_still_blocks_confirmed_toner_transition(self) -> None:
        value = transcript(
            (0, 7, "facial cleanser lagi promo harga Rp89.000"),
            (7, 9, "sekarang kita lanjut hydrating toner"),
            (9, 17, "facial cleanser cek etalase nomor 1 dan checkout"),
        )
        window, normalized, context = self.normalized(
            value,
            candidate("cleanser", 0, 7, role="cta"),
            candidate("cleanser", 9, 17, role="cta"),
        )
        result, diagnostics = compose_candidates(normalized, window, context)
        self.assertEqual(len(result), 2)
        self.assertFalse(diagnostics)


class ModularScannerActiveProductContextTests(unittest.TestCase):
    def validate_with_context(self, value: dict, raw: dict):
        window = one_window(value)
        return validate_candidate(
            raw, window, window["end"], product_context=build_product_context(value["segments"]),
        )

    def test_toner_and_serum_context_survive_without_repeated_names(self) -> None:
        for product, introduction, later in (
            ("toner", "sekarang lanjut etalase nomor 2, aku ada hydrating tonernya", "ini bantu balancing oil dan replenishing moisture"),
            ("serum", "etalase nomor 3, ini serum 5x vitamin C", "untuk kulit kusam bekas jerawat dan flek hitam"),
        ):
            value = transcript((0, 5, introduction), (45, 65, later))
            accepted, rejection = self.validate_with_context(value, candidate(product, 45, 65))
            self.assertIsNone(rejection)
            self.assertEqual(accepted["validation_diagnostics"]["product_evidence"]["source"], "active_context")

    def test_transition_conflict_and_expiration_end_inherited_context(self) -> None:
        transition = transcript(
            (0, 5, "sekarang hydrating toner"),
            (30, 35, "sekarang lanjut etalase nomor 3 serum"),
            (40, 60, "membantu kulit kusam dan flek hitam"),
        )
        accepted, rejection = self.validate_with_context(transition, candidate("toner", 40, 60))
        self.assertIsNone(accepted)
        self.assertEqual(rejection.code, "conflicting_product")

        conflict = transcript((0, 5, "sekarang serum"), (40, 60, "eye cream membantu area mata"))
        accepted, rejection = self.validate_with_context(conflict, candidate("serum", 40, 60))
        self.assertIsNone(accepted)
        self.assertEqual(rejection.code, "conflicting_product")

        expired_start = ACTIVE_PRODUCT_CONTEXT_SECONDS + 10
        expired = transcript((0, 5, "sekarang hydrating toner"), (expired_start, expired_start + 20, "membantu balancing oil"))
        accepted, rejection = self.validate_with_context(expired, candidate("toner", expired_start, expired_start + 20))
        self.assertIsNone(accepted)
        self.assertEqual(rejection.code, "product_not_supported")

    def test_etalase_mapping_requires_confirmation_and_explicit_product_wins(self) -> None:
        confirmed = transcript(
            (0, 5, "etalase nomor 2 hydrating toner"),
            (130, 135, "sekarang kembali ke etalase nomor 2"),
            (140, 160, "bantu balancing oil dan replenishing moisture"),
        )
        accepted, rejection = self.validate_with_context(confirmed, candidate("toner", 140, 160))
        self.assertIsNone(rejection)
        self.assertEqual(accepted["validation_diagnostics"]["product_evidence"]["source"], "etalase_context")

        unsupported = transcript((0, 5, "sekarang etalase nomor 6"), (10, 30, "bikin kulit terasa nyaman"))
        accepted, rejection = self.validate_with_context(unsupported, candidate("mask", 10, 30))
        self.assertIsNone(accepted)
        self.assertEqual(rejection.code, "product_not_supported")

        explicit = transcript((0, 5, "etalase nomor 2 tetapi ini serum"), (10, 30, "membantu kulit kusam"))
        accepted, rejection = self.validate_with_context(explicit, candidate("serum", 10, 30))
        self.assertIsNone(rejection)
        self.assertIsNotNone(accepted)

    def test_all_six_proya_etalase_pairs_can_be_safely_confirmed(self) -> None:
        names = {
            1: ("cleanser", "facial cleanser"),
            2: ("toner", "hydrating toner"),
            3: ("serum", "vitamin c serum"),
            4: ("skin_cream", "skin cream"),
            5: ("eye_cream", "eye cream"),
            6: ("mask", "sheet mask"),
        }
        for number, (product, name) in names.items():
            with self.subTest(number=number):
                value = transcript(
                    (0, 5, f"etalase nomor {number} ini {name}"),
                    (130, 135, f"kembali ke etalase nomor {number}"),
                    (140, 160, "ini membantu kulit terasa nyaman dan lembap"),
                )
                accepted, rejection = self.validate_with_context(value, candidate(product, 140, 160))
                self.assertIsNone(rejection)
                self.assertEqual(accepted["validation_diagnostics"]["product_evidence"]["source"], "etalase_context")


class ModularScannerCrossWindowConflictTests(unittest.TestCase):
    def validate_with_context(self, value: dict, raw: dict):
        window = one_window(value)
        return validate_candidate(
            raw, window, window["end"], product_context=build_product_context(value["segments"]),
        )

    @staticmethod
    def cross_candidate(product: str, start: float, end: float, chunk: int, order: int = 0) -> dict:
        return {
            "product": product, "role": "cta", "start_seconds": start, "end_seconds": end,
            "duration_seconds": round(end - start, 3), "confidence": 0.9,
            "transcript_text": "generic CTA", "reason": "Cross-window candidate",
            "validation_diagnostics": {}, "_product_evidence": 0, "_coverage": 1.0,
            "_order": order, "_chunk_index": chunk,
        }

    @staticmethod
    def generic_segments() -> list[dict]:
        return [
            {"start": 860.746, "end": 870.0, "text": "harga lagi turun"},
            {"start": 870.0, "end": 882.890, "text": "checkout sekarang sebelum harganya berubah"},
        ]

    def real_pair(self) -> list[dict]:
        return [
            self.cross_candidate("serum", 865.917, 882.890, 0, 0),
            self.cross_candidate("skin_cream", 860.746, 878.830, 1, 1),
        ]

    def test_real_generic_cta_conflict_rejects_ambiguous_region(self) -> None:
        kept, diagnostics = resolve_cross_window_product_conflicts(
            self.real_pair(), self.generic_segments(), [],
        )
        self.assertEqual(CROSS_WINDOW_PRODUCT_CONFLICT_IOU_THRESHOLD, 0.50)
        self.assertFalse(kept)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["status"], "cross_window_product_conflict")
        self.assertEqual(diagnostics[0]["resolution"], "rejected_ambiguous")
        self.assertGreaterEqual(diagnostics[0]["temporal_iou"], 0.50)

    def test_explicit_overlap_evidence_resolves_each_product(self) -> None:
        for product, text in (("serum", "serum 5x vitamin C"), ("skin_cream", "skin cream")):
            with self.subTest(product=product):
                segments = [{"start": 866.0, "end": 878.0, "text": text}]
                kept, diagnostics = resolve_cross_window_product_conflicts(self.real_pair(), segments, [])
                self.assertEqual([item["product"] for item in kept], [product])
                self.assertEqual(diagnostics[0]["evidence"], "explicit_product_in_overlap")
                self.assertEqual(diagnostics[0]["status"], "cross_window_product_conflict_resolved")

    def test_confirmed_transition_resolves_but_stale_active_context_does_not(self) -> None:
        transition_segments = [
            {"start": 850.0, "end": 855.0, "text": "sekarang lanjut etalase nomor 3 serum"},
            *self.generic_segments(),
        ]
        transition_context = build_product_context(transition_segments)
        kept, diagnostics = resolve_cross_window_product_conflicts(
            self.real_pair(), transition_segments, transition_context,
        )
        self.assertEqual([item["product"] for item in kept], ["serum"])
        self.assertEqual(diagnostics[0]["evidence"], "confirmed_explicit_transition")

        stale_segments = [
            {"start": 750.0, "end": 755.0, "text": "serum 5x vitamin C"},
            *self.generic_segments(),
        ]
        stale_context = build_product_context(stale_segments)
        kept, diagnostics = resolve_cross_window_product_conflicts(
            self.real_pair(), stale_segments, stale_context,
        )
        self.assertFalse(kept)
        self.assertEqual(diagnostics[0]["evidence"], "no_decisive_authoritative_product_evidence")

    def test_same_product_and_low_overlap_remain_normal(self) -> None:
        same_product = [
            self.cross_candidate("serum", 865.917, 882.890, 0),
            self.cross_candidate("serum", 860.746, 878.830, 1),
        ]
        kept, diagnostics = resolve_cross_window_product_conflicts(same_product, self.generic_segments(), [])
        self.assertEqual(len(kept), 2)
        self.assertFalse(diagnostics)

        low_overlap = [
            self.cross_candidate("serum", 0, 20, 0),
            self.cross_candidate("skin_cream", 18, 38, 1),
        ]
        kept, diagnostics = resolve_cross_window_product_conflicts(low_overlap, [], [])
        self.assertEqual(len(kept), 2)
        self.assertFalse(diagnostics)

    def test_composed_candidate_participates_in_conflict_detection(self) -> None:
        window = one_window(transcript(
            (865.917, 872.305, "harga lagi turun"),
            (875.174, 882.890, "checkout sekarang sebelum harganya berubah"),
        ))
        source_candidates = [
            self.cross_candidate("serum", 865.917, 872.305, 0, 0),
            self.cross_candidate("serum", 875.174, 882.890, 0, 1),
        ]
        composed, _ = compose_candidates(source_candidates, window, [])
        self.assertEqual(len(composed), 1)
        competing = self.cross_candidate("skin_cream", 860.746, 878.830, 1, 2)
        kept, diagnostics = resolve_cross_window_product_conflicts(
            [composed[0], competing], window["segments"], [],
        )
        self.assertFalse(kept)
        self.assertTrue(diagnostics[0]["candidates"][0]["composed"])

    def test_stale_context_never_overrides_explicit_local_product(self) -> None:
        value = transcript(
            (0, 5, "serum 5x vitamin c"),
            (40, 60, "facial cleansernya mengangkat debu dan polusi"),
        )
        accepted, rejection = self.validate_with_context(value, candidate("cleanser", 40, 60))
        self.assertIsNone(rejection)
        self.assertEqual(accepted["validation_diagnostics"]["product_evidence"]["source"], "local")

    def test_inherited_mismatch_alone_is_not_a_hard_conflict(self) -> None:
        value = transcript((0, 5, "serum 5x vitamin c"), (40, 60, "membantu kulit terasa nyaman"))
        accepted, rejection = self.validate_with_context(value, candidate("cleanser", 40, 60))
        self.assertIsNone(accepted)
        self.assertEqual(rejection.code, "product_not_supported")

    def test_true_local_conflict_and_confirmed_immediate_transition_still_reject(self) -> None:
        local = transcript((0, 20, "serum 5x vitamin c membantu kulit kusam"))
        accepted, rejection = self.validate_with_context(local, candidate("cleanser", 0, 20))
        self.assertIsNone(accepted)
        self.assertEqual(rejection.code, "conflicting_product")

        transition = transcript(
            (0, 5, "host menyapa penonton"),
            (5, 9, "sekarang kita lanjut serum"),
            (10, 30, "ini membantu kulit terasa lembap"),
        )
        accepted, rejection = self.validate_with_context(transition, candidate("toner", 10, 30))
        self.assertIsNone(accepted)
        self.assertEqual(rejection.code, "conflicting_product")

    def test_explicit_product_overrides_stale_confirmed_etalase_context(self) -> None:
        value = transcript(
            (0, 5, "etalase nomor 2 hydrating toner"),
            (40, 60, "serum 5x vitamin c membantu kulit kusam"),
        )
        accepted, rejection = self.validate_with_context(value, candidate("serum", 40, 60))
        self.assertIsNone(rejection)
        self.assertEqual(accepted["validation_diagnostics"]["product_evidence"]["source"], "local")

    def test_confirmed_etalase_asr_variant_supports_early_cleanser_section(self) -> None:
        value = transcript(
            (0, 5, "telasan nomor 1"),
            (10, 30, "kulit tidak terasa ketarik atau kering setelah cuci muka"),
            (100, 110, "etalase nomor 1 facial cleanser"),
        )
        accepted, rejection = self.validate_with_context(value, candidate("cleanser", 10, 30))
        self.assertIsNone(rejection)
        self.assertEqual(accepted["validation_diagnostics"]["product_evidence"]["source"], "etalase_context")


class ModularScannerWindowAndFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repository = ScannerRepository(root / "scanner.sqlite3")
        self.cfg = SimpleNamespace(
            WORKING_DIR=str(root), QUEUE_INPUT_DIR=str(root), LM_STUDIO_MOMENT_MODEL_ID="test/model",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def service(self, analyzer) -> ModularScannerService:
        return ModularScannerService(
            self.cfg, repository=self.repository, analyzer_factory=lambda: analyzer,
            production_active=lambda: False, start_worker=False,
        )

    @staticmethod
    def long_transcript(text: str = "serum membantu kulit") -> dict:
        return transcript(*[(i * 30.0, (i + 1) * 30.0, text) for i in range(34)])

    def test_time_cap_splits_even_under_character_budget_with_absolute_ownership(self) -> None:
        windows = build_windows(self.long_transcript(), character_budget=1_000_000, overlap_seconds=45)
        self.assertGreater(len(windows), 1)
        self.assertTrue(all(window["end"] - window["start"] <= 900.0 for window in windows))
        self.assertGreater(windows[1]["start"], 0)
        self.assertIn(f"[{windows[1]['start']:.3f}-", windows[1]["text"])
        for left, right in zip(windows, windows[1:]):
            self.assertAlmostEqual(left["ownership_end"], right["ownership_start"])

    def test_product_rich_empty_result_subdivides_and_keeps_absolute_timestamps(self) -> None:
        window = build_windows(self.long_transcript(), character_budget=1_000_000)[0]
        recovered_candidate = candidate("serum", 60, 80)
        analyzer = SequenceAnalyzer([[], [recovered_candidate], [recovered_candidate]])
        self.repository.upsert_source({
            "source_id": "source", "filename": "vod.mp4", "canonical_path": str(Path(self.temp.name) / "vod.mp4"),
            "file_size": 1, "mtime_ns": 1, "content_fingerprint": "fingerprint", "duration_seconds": 900,
        })
        scan = self.repository.create_scan("source", "scan", ANALYZER_VERSION, PROMPT_VERSION, "test/model")
        result = self.service(analyzer)._analyze_with_empty_recovery(analyzer, window, scan["scan_id"])
        self.assertEqual(result, [recovered_candidate])
        self.assertEqual(len(analyzer.calls), 3)
        self.assertEqual(analyzer.calls[1]["start"], window["start"])
        self.assertGreater(analyzer.calls[2]["start"], 0)
        self.assertEqual(len(self.repository.list_scans("source")), 1)

    def test_product_empty_result_is_valid_without_subdivision(self) -> None:
        window = build_windows(self.long_transcript("host sedang menyapa penonton"), character_budget=1_000_000)[0]
        analyzer = SequenceAnalyzer([[]])
        self.assertEqual(self.service(analyzer)._analyze_with_empty_recovery(analyzer, window, "scan-id"), [])
        self.assertEqual(len(analyzer.calls), 1)

    def test_recursive_subdivision_is_bounded(self) -> None:
        window = build_windows(self.long_transcript(), character_budget=1_000_000)[0]
        analyzer = SequenceAnalyzer([[] for _ in range(20)])
        self.assertEqual(self.service(analyzer)._analyze_with_empty_recovery(analyzer, window, "scan-id"), [])
        self.assertLessEqual(len(analyzer.calls), 7)


class ModularScannerPromptAndPersistenceTests(unittest.TestCase):
    def test_prompt_covers_roles_duration_coverage_deduplication_and_confidence_bands(self) -> None:
        prompt = SYSTEM_PROMPT.casefold()
        for phrase in (
            "generic product description alone is not a hook",
            "usage/tutorial steps",
            "passing ingredient mention",
            "hook 15-25 seconds",
            "benefits 15-35 seconds",
            "ingredients 15-40 seconds",
            "0.90-1.00",
            "0.75-0.89",
            "0.60-0.74",
            "for each product independently",
            "same local discussion",
        ):
            self.assertIn(phrase, prompt)

    def test_versions_are_bumped_and_duration_diagnostics_are_persisted(self) -> None:
        self.assertEqual(ANALYZER_VERSION, "modscan-v3.2")
        self.assertEqual(PROMPT_VERSION, "modscan-prompt-v3")
        with tempfile.TemporaryDirectory() as directory:
            repository = ScannerRepository(Path(directory) / "scanner.sqlite3")
            source = repository.upsert_source({
                "source_id": "source", "filename": "vod.mp4", "canonical_path": str(Path(directory) / "vod.mp4"),
                "file_size": 1, "mtime_ns": 1, "content_fingerprint": "fingerprint", "duration_seconds": 20,
            })
            scan = repository.create_scan("source", "scan", ANALYZER_VERSION, PROMPT_VERSION, "model")
            value = one_window(transcript(
                (0, 13, "serum membantu kulit"), (13, 16, "kulit menjadi lebih lembap"),
            ))
            accepted, _ = validate_candidate(candidate("serum", 0, 13), value, 20)
            repository.replace_segments(scan["scan_id"], source, [accepted])
            with closing(sqlite3.connect(repository.path)) as db:
                raw = db.execute("SELECT validation_diagnostics_json FROM segments").fetchone()[0]
            self.assertEqual(json.loads(raw)["duration_repair"]["outcome"], "expanded")

    def test_v1_scanner_database_migrates_without_losing_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scanner.sqlite3"
            repository = ScannerRepository(path)
            source = repository.upsert_source({
                "source_id": "source", "filename": "vod.mp4", "canonical_path": str(Path(directory) / "vod.mp4"),
                "file_size": 1, "mtime_ns": 1, "content_fingerprint": "fingerprint", "duration_seconds": 20,
            })
            scan = repository.create_scan("source", "scan", "modscan-v1", "modscan-prompt-v1", "model")
            segment = {
                **candidate("serum", 0, 20), "duration_seconds": 20, "transcript_text": "serum membantu kulit",
                "validation_diagnostics": {},
            }
            repository.replace_segments(scan["scan_id"], source, [segment])
            with closing(sqlite3.connect(path)) as db:
                db.execute("ALTER TABLE segments DROP COLUMN validation_diagnostics_json")
                db.execute("UPDATE schema_meta SET version=1")
                db.commit()

            migrated = ScannerRepository(path)
            self.assertEqual([item["scan_id"] for item in migrated.list_scans("source")], [scan["scan_id"]])
            self.assertEqual(len(migrated.list_segments(scan["scan_id"])), 1)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT version FROM schema_meta").fetchone()[0], 4)
                names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"scan_batches", "scan_batch_items", "scan_batch_failures"}.issubset(names))

    def test_v1_through_v31_history_remains_readable_and_is_not_a_v32_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ScannerRepository(Path(directory) / "scanner.sqlite3")
            source = repository.upsert_source({
                "source_id": "source", "filename": "vod.mp4", "canonical_path": str(Path(directory) / "vod.mp4"),
                "file_size": 1, "mtime_ns": 1, "content_fingerprint": "fingerprint", "duration_seconds": 20,
            })
            record = repository.add_transcript("source", "scanner", "cache.json", "transcript-fingerprint")
            for analyzer, prompt in (
                ("modscan-v1", "modscan-prompt-v1"),
                ("modscan-v2", "modscan-prompt-v2"),
                ("modscan-v3", "modscan-prompt-v3"),
                ("modscan-v3.1", "modscan-prompt-v3"),
            ):
                scan = repository.create_scan("source", "scan", analyzer, prompt, "model")
                repository.update_scan(scan["scan_id"], "completed", transcript_id=record["transcript_id"])
                repository.replace_segments(scan["scan_id"], source, [{
                    **candidate("serum", 0, 20), "duration_seconds": 20,
                    "transcript_text": "serum membantu kulit", "validation_diagnostics": {},
                }])
            history = repository.list_scans("source")
            self.assertEqual(
                [item["analyzer_version"] for item in history],
                ["modscan-v3.1", "modscan-v3", "modscan-v2", "modscan-v1"],
            )
            self.assertTrue(all(repository.list_segments(item["scan_id"]) for item in history))
            self.assertIsNone(repository.compatible_scan(
                "source", "transcript-fingerprint", ANALYZER_VERSION, PROMPT_VERSION, "model",
            ))


if __name__ == "__main__":
    unittest.main()
