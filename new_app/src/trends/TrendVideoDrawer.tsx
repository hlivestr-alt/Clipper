import { Download, ExternalLink, X } from "lucide-react";
import { useEffect } from "react";
import type { TrendVideo } from "../api";
import { trendVideoState } from "./TrendVideoCard";

export function TrendVideoDrawer({ video, onClose, onSaveAll }: { video?: TrendVideo; onClose: () => void; onSaveAll: () => void }) {
  useEffect(() => {
    if (!video) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [video, onClose]);

  if (!video) return null;
  const state = trendVideoState(video);
  return (
    <div className="trend-drawer-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <aside className="trend-drawer" role="dialog" aria-modal="true" aria-labelledby="trend-drawer-title">
        <header>
          <div><span>Video detail</span><h2 id="trend-drawer-title">#{video.final_rank} · #{video.hashtag_name.replace(/^#+/, "")}</h2></div>
          <button type="button" className="trend-icon-button" aria-label="Close video detail" onClick={onClose}><X size={19} /></button>
        </header>
        {video.embed_url ? (
          <iframe className="trend-embed" src={video.embed_url.replace("autoplay=1", "autoplay=0")} title={`TikTok ${video.video_id}`} allow="encrypted-media; picture-in-picture" />
        ) : <div className="trend-empty"><strong>Preview unavailable</strong></div>}
        <dl className="trend-detail-list">
          <div><dt>Rank</dt><dd>#{video.final_rank}</dd></div>
          <div><dt>Hashtag</dt><dd>#{video.hashtag_name.replace(/^#+/, "")}</dd></div>
          {video.video_duration_seconds != null && <div><dt>Duration</dt><dd>{Math.round(video.video_duration_seconds)} seconds</dd></div>}
          <div><dt>Download status</dt><dd className={`trend-video-state ${state.toLowerCase()}`}>{state}</dd></div>
        </dl>
        <a className="secondary-button trend-reference-link" href={video.share_url} target="_blank" rel="noreferrer"><ExternalLink size={15} /> Open TikTok reference</a>
        {state === "Saved" ? (
          <button type="button" className="primary-button full-width" disabled>Saved locally</button>
        ) : (
          <button type="button" className="primary-button full-width" onClick={onSaveAll}><Download size={16} /> {state === "Failed" || state === "Interrupted" ? "Retry with Save All" : "Save All Videos"}</button>
        )}
        {(video.download_error || video.media_error) && <p className="trend-error-copy">{video.download_error || video.media_error}</p>}
      </aside>
    </div>
  );
}
