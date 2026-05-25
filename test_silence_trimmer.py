import unittest
from types import SimpleNamespace
from unittest import mock

from silence_trimmer import (
    SKIP_INSUFFICIENT_WORDS,
    SKIP_NO_GAPS_FOUND,
    SKIP_REMOVAL_FRACTION_EXCEEDED,
    SKIP_WORD_TIMING_INVALID,
    build_silence_compacted_ffmpeg_command,
    build_silence_trim_plan,
    remap_events_to_compacted_timeline,
    remap_words_to_compacted_timeline,
)


def cfg(**overrides):
    defaults = {
        "SILENCE_TRIM_ENABLED": True,
        "SILENCE_TRIM_MIN_GAP": 1.2,
        "SILENCE_TRIM_KEEP_GAP": 0.35,
        "SILENCE_TRIM_EDGE_KEEP": 0.25,
        "SILENCE_TRIM_MAX_REMOVAL_FRACTION": 0.45,
        "SILENCE_TRIM_MIN_WORDS": 6,
        "RAW_CUT_CODEC": "libx264",
        "RAW_CUT_PRESET": "ultrafast",
        "OUTPUT_CQ": 35,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def words(*spans):
    return [
        {"word": f"w{index}", "start": start, "end": end}
        for index, (start, end) in enumerate(spans, start=1)
    ]


class SilenceTrimPlanTests(unittest.TestCase):
    def test_no_trim_for_short_natural_pauses(self):
        plan = build_silence_trim_plan(
            words((0.0, 0.2), (0.7, 0.9), (1.4, 1.6), (2.1, 2.3), (2.8, 3.0), (3.5, 3.7)),
            4.2,
            cfg(),
        )

        self.assertFalse(plan["trimmed"])
        self.assertEqual(plan["skip_reason"], SKIP_NO_GAPS_FOUND)

    def test_internal_gap_is_reduced_to_keep_gap(self):
        plan = build_silence_trim_plan(
            words((0.0, 0.2), (0.5, 0.7), (1.0, 1.2), (4.0, 4.2), (4.5, 4.7), (5.0, 5.2)),
            5.8,
            cfg(),
        )

        self.assertTrue(plan["trimmed"])
        self.assertAlmostEqual(plan["removed_seconds"], 2.45, places=2)
        self.assertEqual(len(plan["kept_ranges"]), 2)
        self.assertAlmostEqual(plan["silence_ranges"][0]["source_start"], 1.375, places=3)
        self.assertAlmostEqual(plan["silence_ranges"][0]["source_end"], 3.825, places=3)

        remapped = remap_words_to_compacted_timeline(
            words((0.0, 0.2), (0.5, 0.7), (1.0, 1.2), (4.0, 4.2), (4.5, 4.7), (5.0, 5.2)),
            plan,
        )
        self.assertAlmostEqual(remapped[3]["start"], 1.55, places=2)

    def test_leading_and_trailing_silence_are_trimmed(self):
        plan = build_silence_trim_plan(
            words((2.0, 2.2), (2.5, 2.7), (3.0, 3.2), (3.5, 3.7), (4.0, 4.2), (4.5, 4.7)),
            7.0,
            cfg(SILENCE_TRIM_MAX_REMOVAL_FRACTION=0.8),
        )

        self.assertTrue(plan["trimmed"])
        self.assertAlmostEqual(plan["silence_ranges"][0]["source_end"], 1.75, places=2)
        self.assertAlmostEqual(plan["silence_ranges"][-1]["source_start"], 4.95, places=2)

    def test_skip_reason_insufficient_words(self):
        plan = build_silence_trim_plan(words((0.0, 0.2), (3.0, 3.2)), 5.0, cfg())

        self.assertFalse(plan["trimmed"])
        self.assertEqual(plan["skip_reason"], SKIP_INSUFFICIENT_WORDS)

    def test_skip_reason_word_timing_invalid(self):
        bad_words = words((0.0, 0.2), (0.5, 0.7), (1.0, 1.2), (0.9, 1.0), (2.0, 2.2), (2.5, 2.7))
        plan = build_silence_trim_plan(bad_words, 5.0, cfg())

        self.assertFalse(plan["trimmed"])
        self.assertEqual(plan["skip_reason"], SKIP_WORD_TIMING_INVALID)

    def test_skip_reason_removal_fraction_exceeded(self):
        plan = build_silence_trim_plan(
            words((4.0, 4.2), (4.5, 4.7), (5.0, 5.2), (5.5, 5.7), (6.0, 6.2), (6.5, 6.7)),
            12.0,
            cfg(SILENCE_TRIM_MAX_REMOVAL_FRACTION=0.2),
        )

        self.assertFalse(plan["trimmed"])
        self.assertEqual(plan["skip_reason"], SKIP_REMOVAL_FRACTION_EXCEEDED)

    def test_product_events_and_tracks_are_remapped(self):
        plan = build_silence_trim_plan(
            words((0.0, 0.2), (0.5, 0.7), (1.0, 1.2), (4.0, 4.2), (4.5, 4.7), (5.0, 5.2)),
            5.8,
            cfg(),
        )
        events = [
            {
                "relative_start": 0.8,
                "relative_end": 4.5,
                "relative_track": [
                    {"relative_time": 1.0, "bbox": [0, 0, 10, 10]},
                    {"relative_time": 2.0, "bbox": [1, 1, 11, 11]},
                    {"relative_time": 4.0, "bbox": [2, 2, 12, 12]},
                ],
            }
        ]

        remapped = remap_events_to_compacted_timeline(events, plan)

        self.assertEqual(len(remapped), 1)
        self.assertAlmostEqual(remapped[0]["relative_start"], 0.8, places=2)
        self.assertAlmostEqual(remapped[0]["relative_end"], 2.05, places=2)
        self.assertEqual(len(remapped[0]["relative_track"]), 2)
        self.assertAlmostEqual(remapped[0]["relative_track"][1]["relative_time"], 1.55, places=2)


class SilenceTrimCommandTests(unittest.TestCase):
    def test_multi_range_command_uses_concat(self):
        command = build_silence_compacted_ffmpeg_command(
            "input.mp4",
            10.0,
            "out.mp4",
            [
                {"source_start": 0.0, "source_end": 1.0},
                {"source_start": 3.0, "source_end": 5.0},
            ],
            None,
            cfg(),
        )

        joined = " ".join(command)
        self.assertIn("concat=n=2:v=1:a=1", joined)
        self.assertIn("-map [vcat] -map [acat]", joined)

    def test_variant_baked_command_applies_video_and_audio_speed_filters(self):
        variant = SimpleNamespace(speed_ramp=1.1)
        with mock.patch("silence_trimmer._variant_vf_chain", return_value="hflip,setpts=0.9091*PTS"):
            command = build_silence_compacted_ffmpeg_command(
                "input.mp4",
                10.0,
                "out.mp4",
                [{"source_start": 0.0, "source_end": 5.0}],
                variant,
                cfg(),
            )

        joined = " ".join(command)
        self.assertIn("hflip,setpts=0.9091*PTS", joined)
        self.assertIn("atempo=1.1000", joined)
        self.assertIn("-map [vout] -map [aout]", joined)


if __name__ == "__main__":
    unittest.main()
