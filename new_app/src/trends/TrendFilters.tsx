type TrendFiltersProps = {
  country: string;
  timeRange: string;
  category: string;
  updatedLabel: string;
  advertiserIds?: string[];
  selectedAdvertiserId?: string | null;
  onCountryChange: (value: string) => void;
  onTimeRangeChange: (value: string) => void;
  onCategoryChange: (value: string) => void;
  onAdvertiserChange: (value: string) => void;
};

const timeRanges: Array<[string, string]> = [
  ["1DAY", "Last 1 Day"],
  ["7DAY", "Last 7 Days"],
  ["30DAY", "Last 30 Days"],
  ["120DAY", "Last 120 Days"]
];

export function TrendFilters(props: TrendFiltersProps) {
  return (
    <div className="trend-filters" aria-label="Trend discovery controls">
      <label>
        <span>Country</span>
        <input
          aria-label="Country"
          value={props.country}
          maxLength={2}
          onChange={(event) => props.onCountryChange(event.target.value.toUpperCase())}
        />
      </label>
      <label>
        <span>Time Range</span>
        <select aria-label="Time Range" value={props.timeRange} onChange={(event) => props.onTimeRangeChange(event.target.value)}>
          {timeRanges.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label>
        <span>Category</span>
        <input aria-label="Category" value={props.category} onChange={(event) => props.onCategoryChange(event.target.value.toUpperCase())} />
      </label>
      {(props.advertiserIds?.length ?? 0) > 1 && (
        <label>
          <span>TikTok advertiser</span>
          <select aria-label="TikTok advertiser" value={props.selectedAdvertiserId ?? ""} onChange={(event) => props.onAdvertiserChange(event.target.value)}>
            <option value="">Select advertiser</option>
            {props.advertiserIds?.map((advertiserId) => <option key={advertiserId} value={advertiserId}>{advertiserId}</option>)}
          </select>
        </label>
      )}
      <span className="trend-updated">{props.updatedLabel}</span>
    </div>
  );
}
