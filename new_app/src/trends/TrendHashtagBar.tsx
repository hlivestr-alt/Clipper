import type { TrendHashtag } from "../api";

type TrendHashtagBarProps = {
  hashtags: TrendHashtag[];
  selectedId: string;
  videoCounts: Record<string, number>;
  onSelect: (id: string) => void;
};

export function TrendHashtagBar({ hashtags, selectedId, videoCounts, onSelect }: TrendHashtagBarProps) {
  return (
    <section className="trend-hashtag-section" aria-labelledby="trend-hashtag-title">
      <h2 id="trend-hashtag-title">Trending hashtags</h2>
      {hashtags.length > 0 ? (
        <div className="trend-hashtag-bar">
          {hashtags.map((hashtag) => (
            <button
              type="button"
              className={selectedId === hashtag.hashtag_id ? "active" : ""}
              aria-pressed={selectedId === hashtag.hashtag_id}
              key={hashtag.hashtag_id}
              onClick={() => onSelect(hashtag.hashtag_id)}
            >
              <span>#{hashtag.hashtag_name.replace(/^#+/, "")}</span>
              <small>{videoCounts[hashtag.hashtag_id] ?? 0}</small>
            </button>
          ))}
        </div>
      ) : <p className="trend-inline-empty">No relevant trending hashtags in this snapshot.</p>}
    </section>
  );
}
