import type { TrendVideo } from "../api";
import { TrendVideoCard } from "./TrendVideoCard";

export function TrendVideoGrid({ videos, hashtag, onOpen }: { videos: TrendVideo[]; hashtag: string; onOpen: (video: TrendVideo) => void }) {
  if (videos.length === 0) {
    return (
      <div className="trend-empty trend-no-videos">
        <strong>No videos available for #{hashtag || "this hashtag"}</strong>
        <span>Only video posts are shown.</span>
      </div>
    );
  }
  return <div className="trend-video-grid">{videos.map((video) => <TrendVideoCard key={video.video_id} video={video} onOpen={() => onOpen(video)} />)}</div>;
}
