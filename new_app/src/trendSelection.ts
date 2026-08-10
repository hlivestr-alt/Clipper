import type { TrendConfiguration, TrendHashtag, TrendPattern, TrendVideo, TrendVideoDiagnostics } from "./api";

export const TREND_HASHTAG_DISPLAY_LIMIT = 30;

export function displayedTrendHashtags(
  hashtags: TrendHashtag[],
  limit = TREND_HASHTAG_DISPLAY_LIMIT
): TrendHashtag[] {
  const normalized = new Set<string>();
  return hashtags.filter((hashtag) => {
    const name = hashtag.hashtag_name
      .normalize("NFKC")
      .toLocaleLowerCase()
      .replace(/^#+/, "")
      .replace(/[^a-z0-9]+/g, "");
    if (!name || normalized.has(name)) return false;
    normalized.add(name);
    return true;
  }).slice(0, limit);
}

export function uniqueTrendVideos(videos: TrendVideo[]): TrendVideo[] {
  const result = new Map<string, TrendVideo>();
  videos.forEach((video) => {
    if (!result.has(video.video_id)) result.set(video.video_id, video);
  });
  return Array.from(result.values());
}

export function trendVideosForHashtag(videos: TrendVideo[], hashtagId: string): TrendVideo[] {
  return uniqueTrendVideos(
    videos.filter((video) =>
      video.hashtag_id === hashtagId
      && video.media_type === "video"
      && Boolean(video.is_available)
    )
  ).sort((left, right) => left.final_rank - right.final_rank).slice(0, 20);
}

export function trendVideoCountsByHashtag(videos: TrendVideo[]): Record<string, number> {
  const videoIds = new Map<string, Set<string>>();
  videos.forEach((video) => {
    if (video.media_type !== "video" || !Boolean(video.is_available)) return;
    const ids = videoIds.get(video.hashtag_id) ?? new Set<string>();
    ids.add(video.video_id);
    videoIds.set(video.hashtag_id, ids);
  });
  return Object.fromEntries(Array.from(videoIds, ([hashtagId, ids]) => [hashtagId, ids.size]));
}

export function defaultTrendHashtagId(hashtags: TrendHashtag[], videos: TrendVideo[]): string {
  const counts = trendVideoCountsByHashtag(videos);
  return hashtags.find((hashtag) => (counts[hashtag.hashtag_id] ?? 0) > 0)?.hashtag_id ?? hashtags[0]?.hashtag_id ?? "";
}

export function trendVideoIsSelectable(video: TrendVideo): boolean {
  return video.media_type === "video"
    && Boolean(video.is_available)
    && Boolean(video.media_status && ["media_ready", "analyzed", "failed"].includes(video.media_status));
}

export function trendDownloadDisabledReason(
  hasSnapshot: boolean,
  configuration: TrendConfiguration | undefined,
  active: boolean
): string | null {
  if (!hasSnapshot) return "Refresh Discovery before saving videos";
  if (!configuration?.ytdlp_available) return "yt-dlp is unavailable in the backend environment";
  if (!configuration.disk_reserve_satisfied) return "The configured free-disk reserve is not available";
  if (active) return "A bulk download is already active";
  return null;
}

export function completedTrendDownloadCount(videos: TrendVideo[]): number {
  const references = new Map<string, TrendVideo>();
  videos.forEach((video) => references.set(`${video.hashtag_id}:${video.video_id}`, video));
  return Array.from(references.values()).filter((video) =>
    Boolean(video.media_status)
    || ["downloaded", "failed", "interrupted"].includes(video.download_status ?? "")
  ).length;
}

export function trendVideoShortageMessage(
  diagnostics: TrendVideoDiagnostics | undefined,
  renderedCount: number
): string | null {
  if (!diagnostics || renderedCount >= 20) return null;
  const noun = renderedCount === 1 ? "post was" : "posts were";
  return `Only ${renderedCount} video ${noun} available from TikTok for this hashtag. Image and carousel posts were excluded.`;
}

export function toggleTrendVideoSelection(current: string[], video: TrendVideo, limit = 20): string[] {
  if (!trendVideoIsSelectable(video)) return current;
  if (current.includes(video.video_id)) return current.filter((item) => item !== video.video_id);
  return current.length < limit ? [...current, video.video_id] : current;
}

export function trendRecommendationRows(pattern: TrendPattern | null): Array<[string, TrendPattern["recommendations"][string]]> {
  return Object.entries(pattern?.recommendations ?? {}).sort(([left], [right]) => left.localeCompare(right));
}
