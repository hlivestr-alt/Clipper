import { Download, RefreshCw, ShieldCheck, TrendingUp, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { ControlJob, ControlJobPage, TikTokOAuthStart, TrendPageData, TrendVideo } from "../api";
import { sendJson } from "../api";
import { invalidateApiPrefix } from "../queryClient";
import {
  completedTrendDownloadCount,
  defaultTrendHashtagId,
  displayedTrendHashtags,
  TREND_HASHTAG_DISPLAY_LIMIT,
  trendDownloadDisabledReason,
  trendVideoCountsByHashtag,
  trendVideosForHashtag
} from "../trendSelection";
import { useApiQuery } from "../useApiQuery";
import { TrendFilters } from "./TrendFilters";
import { TrendHashtagBar } from "./TrendHashtagBar";
import { TrendVideoDrawer } from "./TrendVideoDrawer";
import { TrendVideoGrid } from "./TrendVideoGrid";
import "./trends.css";

type Notice = { kind: "good" | "bad" | "info"; text: string };

function relativeTime(timestamp?: string | null): string {
  if (!timestamp) return "Not refreshed yet";
  const elapsed = Math.max(0, Date.now() - new Date(timestamp).getTime());
  const minutes = Math.floor(elapsed / 60_000);
  if (minutes < 1) return "Updated just now";
  if (minutes < 60) return `Updated ${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `Updated ${hours}h ago`;
  return `Updated ${Math.floor(hours / 24)}d ago`;
}

function uniqueAvailableVideoCount(videos: TrendVideo[]): number {
  return new Set(videos.filter((video) => video.media_type === "video" && Boolean(video.is_available)).map((video) => video.video_id)).size;
}

function downloadedVideoCount(videos: TrendVideo[]): number {
  return new Set(videos.filter((video) =>
    video.download_status === "downloaded"
    || video.download_status === "reused"
    || Boolean(video.downloaded_relative_path)
    || Boolean(video.relative_path)
    || video.media_status === "media_ready"
    || video.media_status === "analyzed"
  ).map((video) => video.video_id)).size;
}

function DownloadConfirmation({ open, targetCount, savedCount, newCount, rightsConfirmed, onRightsChange, onClose, onConfirm }: {
  open: boolean;
  targetCount: number;
  savedCount: number;
  newCount: number;
  rightsConfirmed: boolean;
  onRightsChange: (value: boolean) => void;
  onClose: () => void;
  onConfirm: () => void;
}) {
  if (!open) return null;
  return (
    <div className="trend-modal-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="trend-confirm" role="dialog" aria-modal="true" aria-labelledby="trend-confirm-title">
        <header><div><h2 id="trend-confirm-title">Save all videos?</h2><p>{targetCount} unique videos · {savedCount} already saved · {newCount} new videos to download.</p></div><button type="button" className="trend-icon-button" aria-label="Close download confirmation" onClick={onClose}><X size={19} /></button></header>
        <label className="trend-rights-confirm"><input type="checkbox" checked={rightsConfirmed} onChange={(event) => onRightsChange(event.target.checked)} />I confirm I have permission to download and store this content.</label>
        <footer><button type="button" className="secondary-button" onClick={onClose}>Cancel</button><button type="button" className="primary-button" disabled={!rightsConfirmed} onClick={onConfirm}><Download size={16} /> Save All Videos</button></footer>
      </div>
    </div>
  );
}

export function TrendsPage({ active }: { active: boolean }) {
  const [country, setCountry] = useState("ID");
  const [timeRange, setTimeRange] = useState("1DAY");
  const [category, setCategory] = useState("BEAUTY_AND_PERSONAL_CARE");
  const path = `/api/trends?country_code=${encodeURIComponent(country)}&date_range=${encodeURIComponent(timeRange)}&category_name=${encodeURIComponent(category)}`;
  const downloadJobs = useApiQuery<ControlJobPage>("/api/control/jobs?limit=10&operation=trend_download", 2_000, active);
  const recentDownloadJobs = downloadJobs.envelope?.data.jobs ?? [];
  const activeDownloadJob = recentDownloadJobs.find((job) => ["queued", "running"].includes(job.status));
  const latestDownloadJob = recentDownloadJobs[0];
  const stoppedDownloadJob = !activeDownloadJob && latestDownloadJob
    && ["failed", "interrupted"].includes(latestDownloadJob.status)
    ? latestDownloadJob
    : undefined;
  const trends = useApiQuery<TrendPageData>(path, activeDownloadJob ? 2_000 : 15_000, active);
  const data = trends.envelope?.data;
  const [selectedHashtag, setSelectedHashtag] = useState("");
  const [selectedVideo, setSelectedVideo] = useState<TrendVideo>();
  const [notice, setNotice] = useState<Notice>();
  const [downloadConfirmOpen, setDownloadConfirmOpen] = useState(false);
  const [rightsConfirmed, setRightsConfirmed] = useState(false);

  const hashtags = useMemo(() => displayedTrendHashtags(data?.hashtags ?? []), [data?.hashtags]);
  const videoCounts = useMemo(() => trendVideoCountsByHashtag(data?.videos ?? []), [data?.videos]);
  const videos = useMemo(() => trendVideosForHashtag(data?.videos ?? [], selectedHashtag), [data?.videos, selectedHashtag]);
  const selectedHashtagName = hashtags.find((hashtag) => hashtag.hashtag_id === selectedHashtag)?.hashtag_name ?? "";
  const availableCount = useMemo(() => uniqueAvailableVideoCount(data?.videos ?? []), [data?.videos]);
  const downloadedCount = useMemo(() => downloadedVideoCount(data?.videos ?? []), [data?.videos]);
  const completedDownloadProgress = useMemo(() => completedTrendDownloadCount(data?.videos ?? []), [data?.videos]);
  const downloadDisabledReason = trendDownloadDisabledReason(Boolean(data?.snapshot), data?.configuration, Boolean(activeDownloadJob));

  useEffect(() => {
    if (!data) return;
    if (!selectedHashtag || !hashtags.some((hashtag) => hashtag.hashtag_id === selectedHashtag)) {
      setSelectedHashtag(defaultTrendHashtagId(hashtags, data.videos));
    }
  }, [data, hashtags, selectedHashtag]);

  async function startJob(run: () => Promise<{ data: ControlJob }>, successText: string) {
    try {
      const result = await run();
      setNotice({ kind: "good", text: `${successText} (${result.data.status}).` });
      await invalidateApiPrefix("/api/control/jobs");
      trends.refresh();
    } catch (caught: unknown) {
      setNotice({ kind: "bad", text: caught instanceof Error ? caught.message : String(caught) });
    }
  }

  function refreshTrends() {
    void startJob(() => sendJson<ControlJob>("POST", "/api/operations/trend-refresh", {
      country_code: country,
      date_range: timeRange,
      category_name: category,
      top_hashtag_limit: TREND_HASHTAG_DISPLAY_LIMIT
    }), "Trend refresh started");
  }

  async function connectTikTok() {
    try {
      const result = await sendJson<TikTokOAuthStart>("POST", "/api/integrations/tiktok/oauth/start", {});
      if (window.clipperDesktop?.openOAuth) await window.clipperDesktop.openOAuth(result.data.authorization_url);
      else window.open(result.data.authorization_url, "_blank", "noopener,noreferrer");
      setNotice({ kind: "info", text: "TikTok authorization opened. Complete it, then return here." });
    } catch (caught: unknown) {
      setNotice({ kind: "bad", text: caught instanceof Error ? caught.message : String(caught) });
    }
  }

  async function selectAdvertiser(advertiserId: string) {
    try {
      await sendJson<Record<string, unknown>>("PUT", "/api/integrations/tiktok/oauth/advertiser", { advertiser_id: advertiserId });
      setNotice({ kind: "good", text: "TikTok advertiser updated." });
      trends.refresh();
    } catch (caught: unknown) {
      setNotice({ kind: "bad", text: caught instanceof Error ? caught.message : String(caught) });
    }
  }

  function confirmDownload() {
    if (!data?.snapshot || !rightsConfirmed) return;
    setDownloadConfirmOpen(false);
    setRightsConfirmed(false);
    void startJob(() => sendJson<ControlJob>("POST", "/api/operations/trend-download", {
      snapshot_id: data.snapshot?.snapshot_id,
      rights_confirmed: true,
      retry_failed: true
    }), "Video download started");
  }

  const targetCount = data?.download_summary?.unique_videos ?? data?.videos.length ?? 0;
  const savedCount = data?.download_summary?.saved ?? downloadedCount;
  const newCount = data?.download_summary?.new ?? Math.max(0, targetCount - savedCount);
  const summaryDownloaded = data?.download_summary?.downloaded ?? 0;
  const summaryFailed = data?.download_summary?.failed ?? 0;
  const summaryRemaining = Math.max(0, targetCount - summaryDownloaded - summaryFailed);
  const progressCount = data?.download_summary
    ? Math.min(data.download_summary.targets, completedDownloadProgress)
    : 0;

  return (
    <section className="page-stack trend-page">
      <header className="trend-page-header">
        <div><h1>TikTok Trends</h1><p>Browse current TikTok trends and save reference videos locally.</p></div>
        <div className="trend-header-actions">
          {data?.configuration.oauth?.authorization_required && <button type="button" className="secondary-button" onClick={() => void connectTikTok()} disabled={!data.configuration.oauth.app_configured || !data.configuration.oauth.redirect_configured || !data.configuration.oauth.callback_supported}><ShieldCheck size={16} /> Connect TikTok</button>}
          <button type="button" className="primary-button" onClick={refreshTrends} disabled={Boolean(data && !data.configuration.access_configured)}><RefreshCw size={16} /> Refresh Trends</button>
        </div>
      </header>

      <TrendFilters
        country={country}
        timeRange={timeRange}
        category={category}
        updatedLabel={relativeTime(data?.snapshot?.retrieved_at)}
        advertiserIds={data?.configuration.oauth?.advertiser_ids}
        selectedAdvertiserId={data?.configuration.oauth?.selected_advertiser_id}
        onCountryChange={setCountry}
        onTimeRangeChange={setTimeRange}
        onCategoryChange={setCategory}
        onAdvertiserChange={(value) => void selectAdvertiser(value)}
      />

      {notice && <div className={`trend-notice ${notice.kind}`} role="status">{notice.text}</div>}
      {trends.error && <div className="trend-notice bad" role="alert"><strong>Couldn’t load trends.</strong> {trends.error}</div>}
      {(trends.envelope?.warnings ?? []).concat(data?.warnings ?? []).length > 0 && <div className="trend-notice info">{(trends.envelope?.warnings ?? []).concat(data?.warnings ?? [])[0]}</div>}

      {trends.loading && !data ? (
        <div className="trend-loading" aria-label="Loading trends"><span /><span /><span /><span /><span /><span /></div>
      ) : !data?.snapshot ? (
        <div className="trend-empty trend-snapshot-empty">
          <TrendingUp size={28} />
          <strong>No trends loaded yet</strong>
          <span>Refresh Trends to fetch the latest TikTok videos.</span>
          <div>{data?.configuration.oauth?.authorization_required && <button type="button" className="secondary-button" onClick={() => void connectTikTok()}>Connect TikTok</button>}<button type="button" className="primary-button" onClick={refreshTrends}>Refresh Trends</button></div>
        </div>
      ) : (
        <>
          <div className="trend-summary" aria-label="Trend summary">
            <span><strong>{hashtags.length}</strong> trending hashtags</span>
            <span><strong>{data.download_summary?.target_references ?? data.videos.length}</strong> ranked references</span>
            <span><strong>{availableCount}</strong> unique videos</span>
            <span><strong>{savedCount}</strong> saved locally</span>
            <span><strong>{newCount}</strong> new</span>
          </div>

          {activeDownloadJob && data.download_summary && (
            <div className="trend-download-strip" role="status">
              <div><strong>Downloading</strong><span>{summaryDownloaded} downloaded · {summaryFailed} failed · {summaryRemaining} remaining</span></div>
              <progress max={Math.max(1, data.download_summary.targets)} value={progressCount} />
            </div>
          )}

          {stoppedDownloadJob && data.download_summary && (
            <div className="trend-download-stopped" role="alert">
              <strong>Download stopped</strong>
              <span>TikTok downloader failed repeatedly. {data.download_summary.interrupted} videos were not attempted.</span>
              {stoppedDownloadJob.error && <details><summary>Details</summary><p>{stoppedDownloadJob.error}</p></details>}
            </div>
          )}

          <TrendHashtagBar hashtags={hashtags} selectedId={selectedHashtag} videoCounts={videoCounts} onSelect={setSelectedHashtag} />

          <section className="trend-video-section" aria-labelledby="trend-video-title">
            <header><div><h2 id="trend-video-title">Top Videos {selectedHashtagName && `— #${selectedHashtagName.replace(/^#+/, "")}`}</h2><p>Ranked TikTok video references, up to 20 per hashtag.</p></div><button type="button" className="secondary-button" disabled={Boolean(downloadDisabledReason)} title={downloadDisabledReason ?? "Save all videos in this snapshot"} onClick={() => setDownloadConfirmOpen(true)}><Download size={16} /> {activeDownloadJob ? "Downloading" : "Save All Videos"}</button></header>
            <TrendVideoGrid videos={videos} hashtag={selectedHashtagName} onOpen={setSelectedVideo} />
          </section>

          {!activeDownloadJob && savedCount > 0 && <p className="trend-saved-status">{savedCount} videos saved locally · {newCount} new</p>}
        </>
      )}

      <TrendVideoDrawer video={selectedVideo} onClose={() => setSelectedVideo(undefined)} onSaveAll={() => { setSelectedVideo(undefined); setDownloadConfirmOpen(true); }} />
      <DownloadConfirmation open={downloadConfirmOpen} targetCount={targetCount} savedCount={savedCount} newCount={newCount} rightsConfirmed={rightsConfirmed} onRightsChange={setRightsConfirmed} onClose={() => { setDownloadConfirmOpen(false); setRightsConfirmed(false); }} onConfirm={confirmDownload} />
    </section>
  );
}
