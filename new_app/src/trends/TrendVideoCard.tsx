import { CheckCircle2, Clock3, Download, Play, TriangleAlert } from "lucide-react";
import type { TrendVideo } from "../api";

export function trendVideoState(video: TrendVideo): "Saved" | "Downloading" | "Failed" | "Interrupted" | "Available" {
  if (video.relative_path && ["media_ready", "analyzing", "analyzed"].includes(video.media_status ?? "")) return "Saved";
  if (video.download_status === "failed") return "Failed";
  if (video.download_status === "interrupted") return "Interrupted";
  if (video.download_status === "queued" || video.download_status === "downloading") return "Downloading";
  if (video.download_status === "downloaded" || video.download_status === "reused" || video.downloaded_relative_path) return "Saved";
  return "Available";
}

const stateIcons = {
  Saved: CheckCircle2,
  Downloading: Download,
  Failed: TriangleAlert,
  Interrupted: TriangleAlert,
  Available: Play
};

export function TrendVideoCard({ video, onOpen }: { video: TrendVideo; onOpen: () => void }) {
  const state = trendVideoState(video);
  const StateIcon = stateIcons[state];
  return (
    <button type="button" className="trend-video-card" onClick={onOpen} aria-label={`Open video rank ${video.final_rank}`}>
      <div className="trend-video-preview" aria-hidden="true">
        <Play size={28} strokeWidth={1.5} />
        <span>View TikTok</span>
      </div>
      <div className="trend-video-card-body">
        <div>
          <strong>#{video.final_rank}</strong>
          <span>#{video.hashtag_name.replace(/^#+/, "")}</span>
        </div>
        <div className="trend-video-meta">
          {video.video_duration_seconds != null && <span><Clock3 size={13} /> {Math.round(video.video_duration_seconds)}s</span>}
          <span className={`trend-video-state ${state.toLowerCase()}`}><StateIcon size={13} /> {state}</span>
        </div>
      </div>
    </button>
  );
}
