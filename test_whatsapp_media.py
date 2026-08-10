from __future__ import annotations

from fractions import Fraction
from copy import deepcopy

from whatsapp_media import (
    APPROVED_STALE_NCLX_RUN_NUMBERS,
    APPROVED_COLOR_OVERRIDES,
    Classification,
    MediaProbe,
    MediaPolicy,
    STALE_NCLX_POLICY_ID,
    build_transcode_command,
    calculate_bitrate_plan,
    classify_media,
    choose_final_fps,
    retry_bitrate,
    resolve_approved_color_override,
    source_color_normalization_filters,
)


def test_bitrate_plan_reserves_mux_audio_and_retry_budget() -> None:
    policy = MediaPolicy()
    plan = calculate_bitrate_plan(60, has_audio=True, policy=policy)
    total = (
        plan.container_reserve_bytes
        + plan.retry_reserve_bytes
        + plan.audio_budget_bytes
        + (plan.target_video_bps * 60 // 8)
    )
    assert total <= policy.target_bytes
    assert plan.container_reserve_bytes >= 256_000
    assert plan.retry_reserve_bytes >= 128_000


def test_frame_rate_policy_preserves_normal_rates_and_caps_high_rates() -> None:
    assert choose_final_fps("24/1", "24/1") == Fraction(24, 1)
    assert choose_final_fps("25/1", "25/1") == Fraction(25, 1)
    assert choose_final_fps("30000/1001", "30000/1001") == Fraction(30_000, 1_001)
    assert choose_final_fps("60/1", "60/1") == Fraction(30, 1)
    assert choose_final_fps("15/1", "15/1") == Fraction(15, 1)


def test_retry_bitrate_reduces_oversized_attempt() -> None:
    policy = MediaPolicy()
    assert retry_bitrate(4_000_000, 16_000_000, policy) < 4_000_000


def _compliant_probe(**overrides) -> MediaProbe:
    values = {
        "path": "clip.mp4",
        "size_bytes": 1_000_000,
        "format_name": "mov,mp4",
        "major_brand": "isom",
        "duration_seconds": 30.0,
        "faststart": True,
        "video_stream_count": 1,
        "audio_stream_count": 1,
        "primary_video_stream_index": 0,
        "primary_audio_stream_index": 1,
        "video_codec": "h264",
        "video_profile": "Main",
        "h264_level": 41,
        "has_b_frames": 0,
        "pixel_format": "yuv420p",
        "width": 1080,
        "height": 1920,
        "sample_aspect_ratio": "1:1",
        "field_order": "progressive",
        "r_frame_rate": "30/1",
        "avg_frame_rate": "30/1",
        "source_frame_rate_mode": "cfr",
        "color_range": "tv",
        "color_space": "bt709",
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "audio_codec": "aac",
        "audio_profile": "LC",
        "audio_channels": 2,
    }
    values.update(overrides)
    return MediaProbe(**values)


def test_copy_remux_transcode_and_unsupported_classification() -> None:
    policy = MediaPolicy()
    assert classify_media(_compliant_probe(), policy).classification is Classification.COPY
    assert (
        classify_media(_compliant_probe(faststart=False), policy).classification
        is Classification.REMUX
    )
    assert (
        classify_media(_compliant_probe(subtitle_stream_count=1), policy).classification
        is Classification.REMUX
    )
    assert (
        classify_media(_compliant_probe(has_b_frames=2), policy).classification
        is Classification.TRANSCODE
    )
    assert (
        classify_media(
            _compliant_probe(
                pixel_format="yuvj420p",
                color_range="pc",
            ),
            policy,
        ).classification
        is Classification.TRANSCODE
    )
    assert (
        classify_media(
            _compliant_probe(primary_stream_ambiguous=True), policy
        ).classification
        is Classification.UNSUPPORTED
    )


def test_full_range_color_path_performs_sample_conversion_not_retag_only() -> None:
    filters = source_color_normalization_filters(
        _compliant_probe(pixel_format="yuvj420p", color_range="pc")
    )
    joined = ",".join(filters)
    assert "rangein=full" in joined
    assert "range=limited" in joined
    assert "matrix=bt709" in joined


def test_nvenc_command_enforces_main_b_zero_gop_level_and_aac_lc() -> None:
    probe = _compliant_probe(width=720, height=1280)
    command, _plan, fps, dimensions = build_transcode_command(
        "source.mp4",
        "destination.mp4",
        probe=probe,
        policy=MediaPolicy(),
    )
    joined = " ".join(command)
    assert "-profile:v main" in joined
    assert "-bf 0" in joined
    assert "-profile:a aac_low" in joined
    assert "-level:v 3.2" in joined
    assert "-g 60" in joined
    assert "-pix_fmt yuv420p" in joined
    assert fps == Fraction(30, 1)
    assert dimensions == (720, 1280)


def _audited_conflict_probe(source_sha256: str) -> MediaProbe:
    approved = APPROVED_COLOR_OVERRIDES[source_sha256]
    signature = approved["signature"]
    probe = _compliant_probe(
        source_sha256=source_sha256,
        color_conflict=True,
        size_bytes=signature["size_bytes"],
        video_codec=signature["video_codec"],
        video_encoder=signature["video_encoder"],
        pixel_format=signature["pixel_format"],
        bits_per_raw_sample=signature["bits_per_raw_sample"],
        width=signature["width"],
        height=signature["height"],
        color_range=signature["color_range"],
        color_space=signature["color_space"],
        color_primaries=signature["color_primaries"],
        color_transfer=signature["color_transfer"],
        has_b_frames=2,
    )
    probe.color_policy_override = resolve_approved_color_override(probe)
    return probe


def test_two_audited_color_conflicts_select_limited_sdr_override() -> None:
    policy = MediaPolicy()
    assert len(APPROVED_COLOR_OVERRIDES) == 2
    for source_sha256, approved in APPROVED_COLOR_OVERRIDES.items():
        probe = _audited_conflict_probe(source_sha256)
        override = probe.color_policy_override
        assert override is not None
        assert override["override_id"] == approved["override_id"]
        assert override["override_policy_id"] == STALE_NCLX_POLICY_ID
        assert override["decision_source"] == "exact_sha256_allowlist"
        assert override["metadata_overridden"] is True
        assert override["original_container_stream_tags"]["color_space"] == "gbr"
        assert override["selected_interpretation"] == (
            "limited_range_bt470bg_matrix_sdr_bt709"
        )
        assert override["conversion_path"] == (
            "limited_bt470bg_sdr_to_limited_bt709"
        )
        classified = classify_media(probe, policy)
        assert classified.classification is Classification.TRANSCODE
        assert "color_metadata_override" in classified.reasons


def test_audited_override_performs_vui_faithful_conversion_without_hdr_tags() -> None:
    for source_sha256 in APPROVED_COLOR_OVERRIDES:
        probe = _audited_conflict_probe(source_sha256)
        joined = ",".join(source_color_normalization_filters(probe))
        assert "rangein=limited" in joined
        assert "matrixin=bt470bg" in joined
        assert "transferin=bt709" in joined
        assert "primariesin=bt709" in joined
        assert "range=limited" in joined
        assert "matrix=bt709" in joined
        assert "transfer=bt709" in joined
        assert "primaries=bt709" in joined
        assert "tonemap" not in joined
        assert "arib-std-b67" not in joined
        assert "bt2020" not in joined

        command, plan, _fps, _dimensions = build_transcode_command(
            "source.mp4",
            "destination.mp4",
            probe=probe,
            policy=MediaPolicy(),
        )
        joined_command = " ".join(command)
        assert "-profile:v main" in joined_command
        assert "-bf 0" in joined_command
        assert "-pix_fmt yuv420p" in joined_command
        assert "-color_range tv" in joined_command
        assert "-colorspace bt709" in joined_command
        assert "-color_primaries bt709" in joined_command
        assert "-color_trc bt709" in joined_command
        assert plan.target_video_bps > 0


def test_similar_unapproved_color_conflicts_remain_fail_closed() -> None:
    approved_hash = next(iter(APPROVED_COLOR_OVERRIDES))
    probe = _audited_conflict_probe(approved_hash)
    probe.source_sha256 = "0" * 64
    probe.color_policy_override = resolve_approved_color_override(probe)
    assert probe.color_policy_override is None
    assert (
        classify_media(probe, MediaPolicy()).classification
        is Classification.UNSUPPORTED
    )

    probe = _audited_conflict_probe(approved_hash)
    probe.width = 1078
    probe.color_policy_override = resolve_approved_color_override(probe)
    assert probe.color_policy_override is None
    assert (
        classify_media(probe, MediaPolicy()).classification
        is Classification.UNSUPPORTED
    )


OBSERVED_6902_6906_CONFLICTS = (
    ("6011eeaa0757c8bb601d32af7dc4ca88ab72ec045f56fe95fc4785082ebf8da0", "sdr"),
    ("fbbf00213dd22eddf8ceea4fb7af18d9677ce56fa75d9fdd64775438b04b9315", "sdr"),
    ("23701b2081e371d2e907e0bda6093cab1ce71bd9c7adb88e7ea123ee4f764219", "sdr"),
    ("cc473bfc995f3cdf13bc5b6d77f03ac617c3048158a4186231cce5cc4d8a9395", "hlg"),
    ("68e60b6247f79a31192f8007e2b0e51c9af0cf44e89f04dddee83a8f012ae680", "sdr"),
    ("59a23cd6a40ebe7ea2faf77000078a54b58ff6bd3eda613b9569a8a435dcf2a3", "hlg"),
    ("3101d9d78eea26df9543c862c17ead9935a84d175f7e58f947832fe0fb490435", "hlg"),
    ("b80d0a9a41b3a54c8289b43bf2fe4e9ae57615ebfbe4b130879d39c5d43601e4", "hlg"),
    ("30f3abf0df2f1a63a079b4a21fb3c9f4ff55c1b5d2886b76b8df688525dfb4f7", "sdr"),
    ("842f00f930493272def7c0f6a6ed67c6539d9a7e40684370bf8d1c0fa8f61e36", "sdr"),
    ("80195476366620ee66201d1dd08e99beedddf400f895f4b136162df97c033560", "hlg"),
    ("e1c04e25f981975ba00570ab2aecb534920ba943d3729074f3db202949c67085", "sdr"),
    ("edefc1a0f7fac44de0e32b571bce84ec1180e98d7bdcd67ffc5d3e0dab2514b6", "sdr"),
    ("21123a3868a79c1e5834ab91db99128387defe5b9ebe3e3722c0a5c45b1acaca", "sdr"),
)


def _production_signature_probe(source_sha256: str, kind: str = "sdr") -> MediaProbe:
    hlg = kind == "hlg"
    primaries = "bt2020" if hlg else "bt709"
    transfer = "arib-std-b67" if hlg else "bt709"
    side_data = (
        [{
            "side_data_type": "Ambient viewing environment",
            "ambient_illuminance": "3140000/10000",
            "ambient_light_x": "15635/50000",
            "ambient_light_y": "16450/50000",
        }]
        if hlg else []
    )
    return _compliant_probe(
        path=(
            "D:\\output_clips\\export_batches\\6904\\"
            "2026_05_30_12_13_26_run_191__2026_05_30_12_13_26_run_191_"
            "clip_0008_v4_b_roll_only_score9_TEST.mp4"
        ),
        source_sha256=source_sha256,
        color_conflict=True,
        color_range="pc",
        color_space="gbr",
        color_primaries=primaries,
        color_transfer=transfer,
        video_encoder="Lavc62.29.101 h264_nvenc",
        muxer_encoder="Lavf62.13.102",
        video_profile="Main",
        h264_level=40,
        has_b_frames=2,
        pixel_format="yuv420p",
        bits_per_raw_sample=8,
        width=1080,
        height=1920,
        chroma_location="left",
        codec_vui={
            "color_range": "limited",
            "color_space": "bt470bg",
            "color_primaries": primaries,
            "color_transfer": transfer,
        },
        decoded_frame_color={
            "pix_fmt": "yuv420p",
            "color_range": "tv",
            "color_space": "bt470bg",
            "color_primaries": primaries,
            "color_transfer": transfer,
        },
        hdr_side_data=side_data,
        production_provenance={
            "recognized": True,
            "batch_number": 6904,
            "run_identity": "2026_05_30_12_13_26_run_191",
            "run_number": 191,
            "base_clip_identity": "2026_05_30_12_13_26_run_191_clip_0008",
            "variation_index": 4,
            "variation_kind": "b_roll_only",
        },
    )


def _resolve_and_classify(probe: MediaProbe) -> Classification:
    probe.color_policy_override = resolve_approved_color_override(probe)
    return classify_media(probe, MediaPolicy()).classification


def test_all_14_observed_conflicts_match_reusable_production_signature() -> None:
    assert len(OBSERVED_6902_6906_CONFLICTS) == 14
    for source_sha256, kind in OBSERVED_6902_6906_CONFLICTS:
        probe = _production_signature_probe(source_sha256, kind)
        assert _resolve_and_classify(probe) is Classification.TRANSCODE
        override = probe.color_policy_override
        assert override is not None
        assert override["override_policy_id"] == STALE_NCLX_POLICY_ID
        assert override["decision_source"] == "reusable_source_signature"
        assert override["decoded_frame_properties"]["color_space"] == "bt470bg"
        assert override["conversion_path"] == "limited_bt470bg_sdr_to_limited_bt709"


def test_reusable_signature_rejects_near_matches_and_genuine_hdr() -> None:
    baseline = _production_signature_probe("1" * 64)
    mutations = []

    probe = deepcopy(baseline)
    probe.codec_vui["color_space"] = "bt709"
    mutations.append(probe)

    probe = deepcopy(baseline)
    probe.pixel_format = "yuv420p10le"
    probe.bits_per_raw_sample = 10
    mutations.append(probe)

    probe = deepcopy(baseline)
    probe.video_encoder = "Lavc62.29.101 libx264"
    mutations.append(probe)

    probe = deepcopy(baseline)
    probe.production_provenance["recognized"] = False
    mutations.append(probe)

    probe = deepcopy(baseline)
    probe.production_provenance["run_number"] = 201
    mutations.append(probe)

    probe = _production_signature_probe("2" * 64, "hlg")
    mastering = {"side_data_type": "Mastering display metadata", "max_luminance": "1000/1"}
    probe.hdr_side_data.append(mastering)
    probe.mastering_display_metadata.append(mastering)
    mutations.append(probe)

    probe = _production_signature_probe("3" * 64, "hlg")
    probe.codec_vui["color_space"] = "bt2020nc"
    probe.decoded_frame_color["color_space"] = "bt2020nc"
    mutations.append(probe)

    for probe in mutations:
        assert _resolve_and_classify(probe) is Classification.UNSUPPORTED
        assert probe.color_policy_override is None


def test_remaining_stale_nclx_family_accepts_audited_runs_and_hlg_side_variants() -> None:
    assert APPROVED_STALE_NCLX_RUN_NUMBERS == frozenset(range(191, 201))
    for run_number in (193, 200):
        for kind in ("sdr", "hlg"):
            probe = _production_signature_probe((str(run_number) + kind) * 32, kind)
            probe.production_provenance["run_number"] = run_number
            assert _resolve_and_classify(probe) is Classification.TRANSCODE
            assert probe.color_policy_override is not None
            if kind == "hlg":
                probe.hdr_side_data = []
                assert _resolve_and_classify(probe) is Classification.TRANSCODE
            else:
                assert probe.color_policy_override["matched_production_signature"][
                    "hdr_side_data_pattern"
                ] == "none"

    unapproved = _production_signature_probe("9" * 64, "hlg")
    unapproved.production_provenance["run_number"] = 201
    assert _resolve_and_classify(unapproved) is Classification.UNSUPPORTED


def test_reusable_override_output_contract_keeps_limited_bt709_yuv420p() -> None:
    for kind in ("sdr", "hlg"):
        probe = _production_signature_probe(("4" if kind == "sdr" else "5") * 64, kind)
        assert _resolve_and_classify(probe) is Classification.TRANSCODE
        command, _plan, fps, dimensions = build_transcode_command(
            "source.mp4", "destination.mp4", probe=probe, policy=MediaPolicy()
        )
        joined = " ".join(command)
        assert "-pix_fmt yuv420p" in joined
        assert "-color_range tv" in joined
        assert "-colorspace bt709" in joined
        assert "-color_primaries bt709" in joined
        assert "-color_trc bt709" in joined
        assert "-profile:v main" in joined
        assert "-bf 0" in joined
        assert fps == Fraction(30, 1)
        assert dimensions in {(1080, 1920), (720, 1280)}
