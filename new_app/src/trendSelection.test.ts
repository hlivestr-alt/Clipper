import { describe, expect, it } from "vitest";
import type { TrendHashtag, TrendPattern, TrendVideo } from "./api";
import {
  defaultTrendHashtagId,
  completedTrendDownloadCount,
  displayedTrendHashtags,
  trendDownloadDisabledReason,
  toggleTrendVideoSelection,
  trendRecommendationRows,
  trendVideoCountsByHashtag,
  trendVideoShortageMessage,
  trendVideosForHashtag,
  uniqueTrendVideos
} from "./trendSelection";

function video(id: string, status: TrendVideo["media_status"] = null): TrendVideo {
  return {
    snapshot_id: "snapshot", hashtag_id: "hashtag", hashtag_name: "trend", rank_position: 1,
    video_id: id, provider_ordinal: 1, final_rank: 1,
    share_url: "https://example.test/share", embed_url: "https://example.test/embed",
    media_type: "video", is_available: true, classification_evidence: "video_info",
    availability_evidence: "playable", playable_url_count: 1,
    media_status: status
  };
}

describe("trend selection", () => {
  it("renders all thirty unique backend-ranked hashtags without reordering them", () => {
    const hashtags: TrendHashtag[] = Array.from({ length: 32 }, (_, index) => ({
      hashtag_id: `h${index + 1}`,
      hashtag_name: `skincare${index + 1}`,
      rank_position: index + 1,
      original_rank: index + 1,
      display_rank: index + 1
    }));
    hashtags.splice(5, 0, {
      hashtag_id: "duplicate",
      hashtag_name: "Skin-care1",
      rank_position: 99
    });

    const rendered = displayedTrendHashtags(hashtags);

    expect(rendered).toHaveLength(30);
    expect(rendered.map((item) => item.hashtag_id)).toEqual([
      ...Array.from({ length: 30 }, (_, index) => `h${index + 1}`)
    ]);
  });

  it("deduplicates video references while preserving the first ranked occurrence", () => {
    const first = video("same", "media_ready");
    const duplicate = { ...video("same", "analyzed"), hashtag_name: "later" };
    expect(uniqueTrendVideos([first, duplicate])).toEqual([first]);
  });

  it("filters video references by the clicked hashtag and counts unique videos", () => {
    const first = video("first");
    const duplicate = { ...first, provider_ordinal: 2 };
    const second = { ...video("second"), hashtag_id: "other", hashtag_name: "other" };
    expect(trendVideosForHashtag([first, duplicate, second], "hashtag")).toEqual([first]);
    expect(trendVideoCountsByHashtag([first, duplicate, second])).toEqual({ hashtag: 1, other: 1 });
  });

  it("defensively excludes image, unknown, and unavailable posts and caps the rendered list at twenty", () => {
    const valid = Array.from({ length: 22 }, (_, index) => ({
      ...video(`v${index + 1}`), provider_ordinal: index + 1, final_rank: index + 1
    }));
    const image = { ...video("image"), media_type: "carousel" as const, is_available: false };
    const unavailable = { ...video("unavailable"), is_available: false };
    expect(trendVideosForHashtag([image, unavailable, ...valid], "hashtag")).toEqual(valid.slice(0, 20));
    expect(trendVideoCountsByHashtag([image, unavailable, ...valid])).toEqual({ hashtag: 22 });
  });

  it("reports an explicit shortage instead of padding the ranked list", () => {
    const diagnostics = {
      hashtag_id: "hashtag", hashtag_name: "moisturizer", total_candidates_returned: 20,
      video_posts_detected: 10, image_carousel_posts_excluded: 9, unknown_posts_excluded: 0,
      unavailable_posts_excluded: 8, valid_videos_stored: 2, sent_to_frontend: 2,
      pagination_available: false, candidate_limit: 20, endpoint: "/discovery/video_list/",
      candidates: []
    };
    expect(trendVideoShortageMessage(diagnostics, 2)).toBe(
      "Only 2 video posts were available from TikTok for this hashtag. Image and carousel posts were excluded."
    );
    expect(trendVideoShortageMessage(diagnostics, 20)).toBeNull();
  });

  it("defaults to the highest-ranked hashtag that has fetched video references", () => {
    const hashtags: TrendHashtag[] = [
      { hashtag_id: "empty", hashtag_name: "empty", rank_position: 1 },
      { hashtag_id: "hashtag", hashtag_name: "trend", rank_position: 2 }
    ];
    expect(defaultTrendHashtagId(hashtags, [video("first")])).toBe("hashtag");
    expect(defaultTrendHashtagId(hashtags, [])).toBe("empty");
  });

  it("selects only linked media and enforces the selection limit", () => {
    expect(toggleTrendVideoSelection([], video("discovered"))).toEqual([]);
    expect(toggleTrendVideoSelection([], video("ready", "media_ready"), 1)).toEqual(["ready"]);
    expect(toggleTrendVideoSelection(["ready"], video("extra", "media_ready"), 1)).toEqual(["ready"]);
    expect(toggleTrendVideoSelection(["ready"], video("ready", "media_ready"), 1)).toEqual([]);
  });

  it("keeps an unlinked download out of analysis and reports bulk-download eligibility", () => {
    const downloaded = { ...video("downloaded"), download_status: "downloaded" as const };
    expect(toggleTrendVideoSelection([], downloaded)).toEqual([]);
    expect(completedTrendDownloadCount([downloaded, { ...downloaded }])).toBe(1);
    const configuration = {
      app_configured: true, access_configured: true, media_dir: "media", media_dir_exists: true,
      qwen_enabled: false, ytdlp_available: true, ytdlp_version: "2026.07.04",
      download_concurrency: 2, download_timeout_seconds: 600, download_min_free_bytes: 5,
      media_free_bytes: 10, disk_reserve_satisfied: true
    };
    expect(trendDownloadDisabledReason(true, configuration, false)).toBeNull();
    expect(trendDownloadDisabledReason(true, { ...configuration, disk_reserve_satisfied: false }, false)).toMatch(/reserve/);
    expect(trendDownloadDisabledReason(true, configuration, true)).toMatch(/already active/);
  });

  it("sorts the read-only profile diff deterministically", () => {
    const pattern: TrendPattern = {
      pattern_id: "pattern", snapshot_id: "snapshot", created_at: "2026-07-15T00:00:00Z",
      analyzer_version: "editing_fingerprint_v1", base_profile_revision: "base", sample_count: 5,
      suggested_profile: { schema_version: 1, revision: "suggested", variant_count: 1, updated_at: "2026-07-15T00:00:00Z", variants: [] },
      recommendations: {
        zoom_intensity: { value: "strong", support_count: 4, sample_count: 5, confidence: 0.8, applied_to_suggestion: true },
        hook_type: { value: "text", support_count: 4, sample_count: 5, confidence: 0.8, applied_to_suggestion: true }
      }
    };
    expect(trendRecommendationRows(pattern).map(([field]) => field)).toEqual(["hook_type", "zoom_intensity"]);
  });
});
