import tempfile
import unittest
from pathlib import Path

from scripts.add_proya_cleanser_text import (
    CHECK_MARK,
    PRESET_VITAMIN_C_SHEET_MASK,
    PRESET_VITAMIN_C_SERUM,
    FontAsset,
    FontSelection,
    build_ffmpeg_command,
    build_overlay_events,
    discover_fonts,
    escape_ass_text,
    generate_ass,
    select_fonts,
    timestamp_to_ass,
)


class TimestampConversionTests(unittest.TestCase):
    def test_converts_and_rounds_to_ass_centiseconds(self):
        self.assertEqual(timestamp_to_ass(0), "0:00:00.00")
        self.assertEqual(timestamp_to_ass(2.4), "0:00:02.40")
        self.assertEqual(timestamp_to_ass(61.239), "0:01:01.24")
        self.assertEqual(timestamp_to_ass(59.999), "0:01:00.00")

    def test_rejects_invalid_timestamps(self):
        with self.assertRaises(ValueError):
            timestamp_to_ass(-0.01)
        with self.assertRaises(ValueError):
            timestamp_to_ass(float("nan"))


class AssEscapingTests(unittest.TestCase):
    def test_escapes_tags_backslashes_and_newlines(self):
        self.assertEqual(
            escape_ass_text("A{B}\\C\nD"),
            r"A\{B\}\\C\ND",
        )

    def test_preserves_ass_safe_punctuation_in_the_final_text_field(self):
        self.assertEqual(
            escape_ass_text('50%: "A", B'),
            '50%: "A", B',
        )


class FontDiscoveryTests(unittest.TestCase):
    def test_discovers_recursively_and_ignores_unsupported_files(self):
        with tempfile.TemporaryDirectory() as temp_name:
            assets = Path(temp_name) / "assets"
            nested = assets / "fonts" / "nested"
            nested.mkdir(parents=True)
            (assets / "Headline-ExtraBold.ttf").write_bytes(b"not-a-real-font")
            (nested / "Caption-SemiBold.OTF").write_bytes(b"not-a-real-font")
            (nested / "ignored.woff2").write_bytes(b"ignored")

            fonts = discover_fonts(assets)

        self.assertEqual(
            {path.name for path in fonts},
            {"Headline-ExtraBold.ttf", "Caption-SemiBold.OTF"},
        )

    def test_font_selection_prefers_expected_sans_weights(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            extra_bold = root / "BrandSans-ExtraBold.ttf"
            semibold = root / "BrandSans-SemiBold.ttf"
            serif = root / "BrandSerif-Regular.otf"
            for path in (extra_bold, semibold, serif):
                path.write_bytes(b"not-a-real-font")

            selection = select_fonts([serif, semibold, extra_bold])

        self.assertEqual(selection.headline.path.name, extra_bold.name)
        self.assertEqual(selection.caption.path.name, semibold.name)
        self.assertEqual(selection.benefit_marker, "-")

    def test_missing_font_directory_has_clear_error(self):
        with tempfile.TemporaryDirectory() as temp_name:
            missing = Path(temp_name) / "missing-assets"
            with self.assertRaisesRegex(RuntimeError, str(missing.name)):
                discover_fonts(missing)


class OverlayGenerationTests(unittest.TestCase):
    def test_generates_complete_timeline_with_sequential_benefits(self):
        events = build_overlay_events(CHECK_MARK)

        self.assertEqual(len(events), 17)
        self.assertTrue(all(event.start < event.end for event in events))
        self.assertTrue(all(len(event.text.splitlines()) <= 2 for event in events))

        benefits = [event for event in events if event.style == "Benefit"]
        self.assertEqual([event.start for event in benefits], [5.35, 5.75, 6.20])
        self.assertTrue(all(event.end == 8.20 for event in benefits))
        self.assertTrue(all(event.marker == CHECK_MARK for event in benefits))
        self.assertTrue(all(event.x == 330 for event in benefits))

        final_secondary = next(
            event
            for event in events
            if event.style == "Secondary" and event.start == 13.50
        )
        self.assertEqual((final_secondary.x, final_secondary.alignment), (54, 4))

    def test_ass_document_contains_required_canvas_styles_and_fades(self):
        placeholder = Path("font.ttf")
        fonts = FontSelection(
            headline=FontAsset(placeholder, "Example ExtraBold"),
            caption=FontAsset(placeholder, "Example SemiBold"),
            benefit=FontAsset(placeholder, "Example ExtraBold"),
            benefit_marker=CHECK_MARK,
        )

        document = generate_ass(fonts)

        self.assertIn("PlayResX: 720", document)
        self.assertIn("PlayResY: 1280", document)
        self.assertIn("WrapStyle: 2", document)
        self.assertIn("ScaledBorderAndShadow: yes", document)
        self.assertIn("Style: Headline,", document)
        self.assertIn("Style: Caption,", document)
        self.assertIn("Style: Secondary,", document)
        self.assertIn("Style: Benefit,", document)
        self.assertIn(",58,&H005AD8FF", document)
        self.assertIn(",41,&H00FFFFFF", document)
        self.assertIn(",35,&H00B2E0FF", document)
        self.assertIn(",36,&H00EFFFF0", document)
        self.assertEqual(document.count(r"\fad(100,100)"), 17)
        self.assertIn(r"\pos(360,1090)", document)
        self.assertIn("Membersihkan dengan lembut", document)

    def test_serum_timeline_uses_exact_ranges_and_safe_positions(self):
        events = build_overlay_events(
            CHECK_MARK,
            preset=PRESET_VITAMIN_C_SERUM,
            cross_marker="X",
        )

        self.assertEqual(len(events), 17)
        headline = next(event for event in events if event.text == "KULIT KUSAM?")
        self.assertEqual(
            (headline.start, headline.end, headline.x, headline.y),
            (0.00, 2.20, 360, 115),
        )
        self.assertEqual(headline.pop_in_ms, 120)

        comparison = [
            event
            for event in events
            if event.text in {"SATU JALUR", "MULTI-PATHWAY"}
        ]
        self.assertEqual([event.start for event in comparison], [2.40, 2.55])
        self.assertTrue(all(event.end == 5.10 for event in comparison))
        self.assertEqual(
            [event.suffix_marker for event in comparison],
            ["X", CHECK_MARK],
        )

        ingredients = [event for event in events if event.style == "Ingredient"]
        self.assertEqual(
            [round(event.start, 2) for event in ingredients],
            [5.25, 5.35, 5.45, 5.55],
        )
        self.assertTrue(all(event.end == 8.55 for event in ingredients))

        subtitles = [event for event in events if event.style == "Subtitle"]
        self.assertTrue(all(event.y == 1010 for event in subtitles))
        final_subtitle = next(event for event in subtitles if event.end == 15.00)
        self.assertEqual(final_subtitle.start, 12.20)
        cta = next(event for event in events if event.style == "CTA")
        self.assertEqual((cta.start, cta.end), (13.60, 15.00))

    def test_serum_ass_has_white_styles_colored_markers_and_pop_tags(self):
        placeholder = Path("font.ttf")
        fonts = FontSelection(
            headline=FontAsset(placeholder, "Example ExtraBold"),
            caption=FontAsset(placeholder, "Example SemiBold"),
            benefit=FontAsset(placeholder, "Example ExtraBold"),
            benefit_marker=CHECK_MARK,
            cross_marker="X",
        )

        document = generate_ass(fonts, preset=PRESET_VITAMIN_C_SERUM)

        self.assertIn("Style: Headline,Example ExtraBold,62,&H00FFFFFF", document)
        self.assertIn("Style: Subtitle,Example SemiBold,40,&H00FFFFFF", document)
        self.assertIn("Style: Ingredient,Example ExtraBold,32,&H00FFFFFF", document)
        self.assertIn(r"\t(0,120,\fscx100\fscy100)", document)
        self.assertIn(r"\fad(80,0)", document)
        self.assertIn(r"SATU JALUR {\1c&H004747FF&}X", document)
        self.assertIn(
            "MULTI-PATHWAY {\\1c&H004BCB72&}" + CHECK_MARK,
            document,
        )
        self.assertIn(
            r"PROYA punya 5X Vitamin C, Tranexamic Acid,\N"
            "Alpha-Arbutin, dan Ergothioneine.",
            document,
        )

        final_frame_document = generate_ass(
            fonts,
            preset=PRESET_VITAMIN_C_SERUM,
            final_frame_end=15.07,
        )
        self.assertIn(
            "Dialogue: 0,0:00:13.60,0:00:15.07,CTA,",
            final_frame_document,
        )
        self.assertIn(
            "Dialogue: 0,0:00:12.20,0:00:15.07,Subtitle,",
            final_frame_document,
        )

    def test_sheet_mask_timeline_has_exact_dialogue_and_hard_timestamps(self):
        events = build_overlay_events(preset=PRESET_VITAMIN_C_SHEET_MASK)
        subtitles = [event for event in events if event.style == "Subtitle"]

        self.assertEqual(
            [(event.start, event.end, event.text) for event in subtitles],
            [
                (0.00, 1.55, "Kulit lagi panas, kusam,\ndan kering?"),
                (1.55, 2.40, "Coba ini."),
                (2.40, 3.90, "PROYA Vitamin C Sheet Mask,"),
                (3.90, 5.20, "satu lembar dua puluh lima\nmililiter."),
                (5.20, 7.75, "Tempel sepuluh sampai\nlima belas menit,"),
                (7.75, 8.60, "lalu bilas."),
                (8.60, 10.55, "Hyaluronic acid dan\nekstrak tumbuhan"),
                (
                    10.55,
                    12.20,
                    "membantu melembapkan\ndan menenangkan.",
                ),
                (12.20, 15.00, "Simpan buat emergency\nskincare kamu."),
            ],
        )
        self.assertTrue(all(len(event.text.splitlines()) <= 2 for event in subtitles))
        self.assertTrue(all(event.y <= 1025 for event in subtitles))

        headline = next(
            event
            for event in events
            if event.text == "KULIT PANAS, KUSAM, KERING?"
        )
        self.assertEqual((headline.pop_in_ms, headline.pop_start_scale), (120, 92))
        self.assertEqual(headline.move_out_up_px, 18)

        cta = next(event for event in events if event.style == "CTA")
        self.assertEqual((cta.start, cta.end, cta.wiggle_at_ms), (12.20, 15.00, 800))

    def test_sheet_mask_ass_uses_rounded_panels_and_selected_font_roles(self):
        placeholder = Path("font.ttf")
        fonts = FontSelection(
            headline=FontAsset(placeholder, "Lilita One"),
            caption=FontAsset(placeholder, "Montserrat SemiBold"),
            benefit=FontAsset(placeholder, "TikTok Sans Bold"),
            benefit_marker="-",
            label=FontAsset(placeholder, "TikTok Sans Bold"),
        )

        document = generate_ass(fonts, preset=PRESET_VITAMIN_C_SHEET_MASK)

        self.assertIn("Style: Headline,Lilita One,62,&H00A65FEA", document)
        self.assertIn(
            "Style: Subtitle,Montserrat SemiBold,36,&H00FFFFFF",
            document,
        )
        self.assertIn("Style: Label,TikTok Sans Bold,31,", document)
        self.assertIn(r"\p1", document)
        self.assertIn(r"\move(360,80,360,62,2200,2400)", document)
        self.assertIn(r"\t(760,800,\frz2.5)", document)
        self.assertIn(r"\fade(255,0,255,220,400,3400,3600)", document)
        self.assertIn(
            r"Tempel sepuluh sampai\Nlima belas menit,",
            document,
        )

    def test_ffmpeg_filter_uses_safe_relative_ass_paths(self):
        source = Path(r"C:\Source Folder\(final), clip.mp4")
        output = Path(r"D:\Rendered Video\captioned (final).mp4")

        command = build_ffmpeg_command(
            r"C:\ffmpeg\bin\ffmpeg.exe",
            source,
            output,
        )
        video_filter = command[command.index("-vf") + 1]

        self.assertEqual(command[command.index("-i") + 1], str(source))
        self.assertEqual(command[-1], str(output))
        self.assertIn("subtitles=filename=overlay.ass:fontsdir=fonts", video_filter)
        self.assertNotIn("C:", video_filter)
        self.assertNotIn("D:", video_filter)
        self.assertNotIn("shell", command)
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertEqual(command[command.index("-t") + 1], "15.000")


if __name__ == "__main__":
    unittest.main()
