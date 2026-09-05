// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TrendPageData, TrendVideo } from "../api";
import { TrendsPage } from "./TrendsPage";

function envelope(data: unknown, warnings: string[] = []) {
  return { data, generated_at: "2026-09-02T08:00:00Z", source_signatures: [], warnings };
}

function response(data: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(status >= 400 ? { error: data } : envelope(data)), {
    status,
    headers: { "Content-Type": "application/json" }
  }));
}

function video(overrides: Partial<TrendVideo>): TrendVideo {
  return {
    snapshot_id: "snapshot-1",
    hashtag_id: "skin",
    hashtag_name: "skincare",
    rank_position: 1,
    video_id: "video-1",
    provider_ordinal: 1,
    final_rank: 1,
    share_url: "https://www.tiktok.com/video-1",
    embed_url: "https://www.tiktok.com/player/v1/video-1?autoplay=1",
    media_type: "video",
    is_available: true,
    classification_evidence: "video payload",
    availability_evidence: "available",
    playable_url_count: 1,
    ...overrides
  };
}

const baseData: TrendPageData = {
  configuration: {
    app_configured: true,
    access_configured: true,
    media_dir: "trend-media",
    media_dir_exists: true,
    qwen_enabled: true,
    ytdlp_available: true,
    ytdlp_version: "2026.08",
    download_concurrency: 2,
    download_timeout_seconds: 60,
    download_min_free_bytes: 100,
    media_free_bytes: 1000,
    disk_reserve_satisfied: true,
    oauth: {
      app_configured: true,
      redirect_configured: true,
      redirect_uri: "http://localhost/callback",
      callback_supported: true,
      storage_path: "oauth.json",
      storage_encrypted: true,
      connected: true,
      authorization_required: false,
      configuration_error: "",
      storage_error: ""
    }
  },
  snapshot: {
    snapshot_id: "snapshot-1",
    retrieved_at: new Date().toISOString(),
    country_code: "ID",
    date_range: "1DAY",
    category_name: "BEAUTY_AND_PERSONAL_CARE"
  },
  hashtags: [
    { hashtag_id: "skin", hashtag_name: "skincare", rank_position: 1 },
    { hashtag_id: "serum", hashtag_name: "serum", rank_position: 2 }
  ],
  hashtag_diagnostics: {
    source: "tiktok",
    source_category: "beauty",
    total_candidates_returned: 50,
    accepted_topical: 2,
    accepted_brands: 0,
    excluded: 48,
    deduplicated: 0,
    stored: 2,
    backend_returned: 2,
    selection_limit: 30,
    classifications: [],
    exclusions: []
  },
  videos: [
    video({ video_id: "rank-2", final_rank: 2, video_duration_seconds: 14 }),
    video({ video_id: "rank-1", final_rank: 1, download_status: "downloaded", downloaded_relative_path: "skin/rank-1.mp4" }),
    video({ video_id: "image", final_rank: 0, media_type: "image" }),
    video({ video_id: "unavailable", final_rank: 0, is_available: false }),
    ...Array.from({ length: 21 }, (_, index) => video({ video_id: `serum-${index}`, hashtag_id: "serum", hashtag_name: "serum", final_rank: 21 - index }))
  ],
  video_diagnostics: [],
  download_summary: { targets: 25, target_references: 25, unique_videos: 23, saved: 1, new: 22, queued: 0, downloading: 0, downloaded: 1, reused: 0, approved: 1, failed: 0, interrupted: 0 },
  latest_pattern: {
    pattern_id: "pattern-1",
    snapshot_id: "snapshot-1",
    created_at: "now",
    analyzer_version: "qwen",
    base_profile_revision: "one",
    sample_count: 5,
    recommendations: {},
    suggested_profile: { schema_version: 1, revision: "one", variant_count: 1, updated_at: "now", variants: [] }
  },
  warnings: []
};

function renderPage(data: TrendPageData = baseData, jobs: unknown[] = [], fetchOverride?: ReturnType<typeof vi.fn>) {
  const fetchMock = fetchOverride ?? vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.includes("/api/control/jobs")) return response({ jobs, active_count: jobs.length });
    if (path.includes("/api/trends?")) return response(data);
    if (path.includes("/api/operations/")) return response({ job_id: "job-12345678", operation: path.split("/").pop(), status: "queued" });
    if (path.includes("/oauth/start")) return response({ authorization_url: "https://tiktok.test/oauth", redirect_uri: "http://localhost", expires_in: 60 });
    if (init?.method === "PUT") return response({});
    return response({});
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const rendered = render(<QueryClientProvider client={client}><TrendsPage active /></QueryClientProvider>);
  return { ...rendered, fetchMock, client };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("TrendsPage", () => {
  it("renders the browser workflow without analysis or recommendation UI", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "TikTok Trends" })).toBeTruthy();
    expect(screen.getByLabelText("Trend discovery controls")).toBeTruthy();
    expect(await screen.findByText("Trending hashtags")).toBeTruthy();
    expect(screen.getByRole("heading", { name: /Top Videos — #skincare/ })).toBeTruthy();
    expect(screen.queryByText("Analyze selected videos")).toBeNull();
    expect(screen.queryByText("Suggested Variation profile")).toBeNull();
    expect(screen.queryByLabelText(/Select .* for analysis/)).toBeNull();
    expect(screen.queryByText("Hashtag diagnostics")).toBeNull();
  });

  it("preserves discovery query controls and refresh request fields", async () => {
    const { fetchMock } = renderPage();
    await screen.findByText("Trending hashtags");
    fireEvent.change(screen.getByLabelText("Country"), { target: { value: "us" } });
    fireEvent.change(screen.getByLabelText("Time Range"), { target: { value: "7DAY" } });
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes("country_code=US") && String(input).includes("date_range=7DAY"))).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Refresh Trends" }));
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([input, init]) => String(input).includes("trend-refresh") && init?.method === "POST");
      expect(call).toBeTruthy();
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ country_code: "US", date_range: "7DAY", category_name: "BEAUTY_AND_PERSONAL_CARE", top_hashtag_limit: 30 });
    });
  });

  it("changes hashtags, keeps only available videos, sorts ranks, and caps at 20", async () => {
    const { container } = renderPage();
    await screen.findByRole("heading", { name: /Top Videos — #skincare/ });
    const initialCards = within(container.querySelector(".trend-video-grid") as HTMLElement).getAllByRole("button");
    expect(initialCards).toHaveLength(2);
    expect(initialCards[0].getAttribute("aria-label")).toBe("Open video rank 1");
    fireEvent.click(screen.getByRole("button", { name: "#serum 21" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: /Top Videos — #serum/ })).toBeTruthy());
    const serumCards = within(container.querySelector(".trend-video-grid") as HTMLElement).getAllByRole("button");
    expect(serumCards).toHaveLength(20);
    expect(serumCards[0].getAttribute("aria-label")).toBe("Open video rank 1");
  });

  it("requires rights confirmation before launching bulk download", async () => {
    const { fetchMock } = renderPage();
    await screen.findByText("Trending hashtags");
    fireEvent.click(screen.getByRole("button", { name: "Save All Videos" }));
    const dialog = screen.getByRole("dialog", { name: "Save all videos?" });
    const confirm = within(dialog).getByRole("button", { name: "Save All Videos" });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(confirm);
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([input, init]) => String(input).includes("trend-download") && init?.method === "POST");
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ snapshot_id: "snapshot-1", rights_confirmed: true, retry_failed: true });
    });
  });

  it("shows download progress, downloaded state, and a closable TikTok detail drawer", async () => {
    const jobs = [{ job_id: "download-1", operation: "trend_download", status: "running" }];
    renderPage(baseData, jobs);
    expect(await screen.findByText("1 downloaded · 0 failed · 22 remaining")).toBeTruthy();
    expect(screen.getAllByText("Downloading").length).toBe(2);
    expect(screen.getAllByText("Saved").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Open video rank 1" }));
    const drawer = screen.getByRole("dialog", { name: /#1 · #skincare/ });
    expect(within(drawer).getByTitle("TikTok rank-1").getAttribute("src")).toContain("autoplay=0");
    expect(within(drawer).getByRole("link", { name: "Open TikTok reference" }).getAttribute("href")).toContain("tiktok.com");
    fireEvent.click(within(drawer).getByRole("button", { name: "Close video detail" }));
    expect(screen.queryByRole("dialog", { name: /#1 · #skincare/ })).toBeNull();
  });

  it("shows stopped progress details and the sanitized per-video error", async () => {
    const stoppedData: TrendPageData = {
      ...baseData,
      videos: baseData.videos.map((item) => item.video_id === "rank-2" ? {
        ...item,
        download_status: "failed",
        download_error: "TikTok extractor returned an unexpected webpage response."
      } : item),
      download_summary: { ...baseData.download_summary!, failed: 2, interrupted: 20 }
    };
    const jobs = [{
      job_id: "download-1",
      operation: "trend_download",
      status: "failed",
      error: "TikTok downloader failed repeatedly. 20 videos were not attempted."
    }];
    renderPage(stoppedData, jobs);
    expect(await screen.findByText("Download stopped")).toBeTruthy();
    expect(screen.getAllByText("TikTok downloader failed repeatedly. 20 videos were not attempted.").length).toBe(2);
    fireEvent.click(screen.getByText("Details"));
    fireEvent.click(screen.getByRole("button", { name: "Open video rank 2" }));
    expect(screen.getByText("TikTok extractor returned an unexpected webpage response.")).toBeTruthy();
  });

  it("handles connection, empty snapshot, no-video, loading, and error states", async () => {
    const disconnected = { ...baseData, snapshot: null, hashtags: [], videos: [], download_summary: null, configuration: { ...baseData.configuration, access_configured: false, oauth: { ...baseData.configuration.oauth!, connected: false, authorization_required: true } } };
    const { unmount } = renderPage(disconnected);
    expect(await screen.findByText("No trends loaded yet")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Connect TikTok" }).length).toBeGreaterThan(0);
    unmount();

    renderPage({ ...baseData, videos: baseData.videos.map((item) => ({ ...item, is_available: false })) });
    expect(await screen.findByText("No videos available for #skincare")).toBeTruthy();
    expect(screen.getByText("Only video posts are shown.")).toBeTruthy();
    cleanup();

    let resolveTrends!: (value: Response) => void;
    const pending = new Promise<Response>((resolve) => { resolveTrends = resolve; });
    const loadingFetch = vi.fn((input: RequestInfo | URL) => String(input).includes("/api/trends?") ? pending : response({ jobs: [], active_count: 0 }));
    renderPage(baseData, [], loadingFetch);
    expect(screen.getByLabelText("Loading trends")).toBeTruthy();
    resolveTrends(await response(baseData));
    cleanup();

    const errorFetch = vi.fn((input: RequestInfo | URL) => String(input).includes("/api/trends?") ? response("TikTok unavailable", 503) : response({ jobs: [], active_count: 0 }));
    renderPage(baseData, [], errorFetch);
    expect((await screen.findByRole("alert")).textContent).toContain("Couldn’t load trends");
  });
});
