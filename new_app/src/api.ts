import { invalidateApiDataForMutation } from "./queryClient";

export type SourceSignature = {
  path: string;
  exists: boolean;
  mtime_ns: number;
  size: number;
};

export type ApiEnvelope<T> = {
  data: T;
  generated_at: string;
  source_signatures: SourceSignature[];
  warnings: string[];
};

export type ArtifactRef = {
  path: string;
  url: string;
  kind: "video" | "image" | "json" | "text" | "unknown";
  exists: boolean;
};

export type QueueRunRow = {
  run_id?: string;
  attempt_number: number;
  video_name: string;
  video_path?: string | null;
  status: string;
  current_step: string;
  progress: number;
  attention: string;
  clips_generated: number;
  runs: number;
  redos: number;
  duration: string;
  started_at: string;
  completed_at: string;
  output_dir?: string | null;
  working_dir?: string | null;
  current_stage?: string | null;
};

export type QueueRunMode = "single_video" | "folder_once" | "folder_repeat";
export type QueuePipelineMode = "full" | "clips_only" | "modules_only" | "raw_cuts_only";
export type QueueVariantMode = "all" | "original" | "custom";

export type QueueLaunchConfig = {
  run_mode: QueueRunMode;
  pipeline_mode: QueuePipelineMode;
  variant_mode: QueueVariantMode;
  variant_count: number;
  max_clips?: number | null;
  video_path?: string | null;
};

export type DashboardSummary = {
  state_path: string;
  updated_at?: string | null;
  queue_status: string;
  queue_health: Record<string, unknown>;
  status_counts: Record<string, number>;
  stage_running: Record<string, number>;
  stage_queued: Record<string, number>;
  stage_waiting: Record<string, number>;
  waiting_videos: number;
  stage_admission_limit: number;
  total_videos: number;
  total_clips: number;
  clips_today?: number;
  clips_last_24h: number;
  clips_per_hour: number;
  production_days?: Array<{ date: string; clips: number }>;
  rows: QueueRunRow[];
};

export type WhatsAppDeliveryStatus = {
  cutover: {
    direct_pc_delivery_enabled: boolean;
    legacy_drive_workflow_disabled: boolean;
    claims_enabled: boolean;
    blocking_reason?: string | null;
  };
  counts: Record<string, number>;
  assignments: Array<{
    affiliate_assignment_id: string;
    batch_number: number;
    affiliate_name: string;
    affiliate_identifier: string;
    delivery_status: string;
    canonical_folder_path: string;
    version: number;
    assigned_at: string;
    sent_at?: string | null;
    delivery_error?: string | null;
  }>;
};

export type QueueDetail = {
  state_path: string;
  updated_at?: string | null;
  queue_status: string;
  queue_health: Record<string, unknown>;
  control_status: string;
  launch_config: Partial<QueueLaunchConfig>;
  active_launch_config: Partial<QueueLaunchConfig>;
  stored_launch_config: Partial<QueueLaunchConfig>;
  launch_summary: string;
  stage_waiting: Record<string, number>;
  waiting_videos: number;
  stage_admission_limit: number;
  total: number;
  limit: number;
  offset: number;
  rows: QueueRunRow[];
};

export type QueueVodFile = {
  name: string;
  path: string;
  size: number;
  modified_at: string;
};

export type QueueVodList = {
  input_dir: string;
  exists: boolean;
  files: QueueVodFile[];
};

export type ScoreStats = {
  summary_count: number;
  previous_text_qwen_calls: number;
  actual_text_qwen_calls: number;
  saved_text_qwen_calls: number;
  actual_vision_qwen_calls: number;
  vision_base_group_count: number;
  vision_contact_sheet_groups: number;
  vision_contact_sheet_fallbacks: number;
};

export type ScoreRow = {
  score_key: string;
  base_score_key: string;
  row_type: "base" | "variant";
  source_video: string;
  run_tag: string;
  source_date: string;
  clip_id: string;
  product: string;
  total_score?: number | null;
  content_score?: number | null;
  host_focus_score?: number | null;
  hook_score?: number | null;
  quality_score?: number | null;
  engagement_score?: number | null;
  similarity_score?: number | null;
  variants?: number | null;
  flags: string[];
  flag_count: number;
  flag_severity: string;
  status: string;
  compliance_blocked: boolean;
  summary: string;
  output_file: string;
  clip_path: string;
  artifact?: ArtifactRef | null;
  scored_at: string;
  sort_timestamp?: string;
};

export type ScoreIndexPage = {
  rows: ScoreRow[];
  total: number;
  limit: number;
  offset: number;
  stats: ScoreStats;
  filter_options: Record<string, string[]>;
};

export type ScoreDetail = {
  selected?: ScoreRow | null;
  variants: ScoreRow[];
  raw: Record<string, unknown>;
  base_raw: Record<string, unknown>;
};

export type ComplianceRow = {
  source_video: string;
  run_tag: string;
  clip_id: string;
  product: string;
  status: string;
  passed: boolean;
  blocked: boolean;
  auto_fixed: boolean;
  violation_count: number;
  summary: string;
  compliance_file: string;
  output_dir: string;
  checked_at: string;
};

export type ComplianceViolationRow = {
  source_video: string;
  run_tag: string;
  clip_id: string;
  product: string;
  field: string;
  severity: string;
  violation_type: string;
  original_text: string;
  suggested_replacement: string;
  start?: number | null;
  end?: number | null;
  compliance_file: string;
  output_dir: string;
  checked_at: string;
};

export type ComplianceIndexPage = {
  rows: ComplianceRow[];
  violations: ComplianceViolationRow[];
  total: number;
  limit: number;
  offset: number;
  summary: Record<string, number>;
  filter_options: Record<string, string[]>;
};

export type LogTail = {
  path: string;
  exists: boolean;
  total_lines: number | null;
  returned_lines: number;
  lines: { line_number: number | null; text: string }[];
};

export type SettingsReadEntry = {
  name: string;
  value: boolean | number | string | null;
  source: string;
  value_type: string;
  category: string;
  minimum?: number | null;
  maximum?: number | null;
  editable?: boolean;
  read_only_reason?: string | null;
};

export type SettingsReadSnapshot = {
  revision: string;
  groups: Record<string, SettingsReadEntry[]>;
};

export type SystemStats = {
  cpu_percent?: number | null;
  ram_percent?: number | null;
  ram_label: string;
  disk_percent?: number | null;
  disk_label: string;
  gpu_percent?: number | null;
  gpu_mem_percent?: number | null;
  gpu_label: string;
};

export type VariationVariant = {
  name: string;
  hook_type: string;
  visual_mode: "host" | "broll_audio";
  random_broll_enabled: boolean;
  before_after_mode: "fullscreen";
  text_style_id: "current" | "creator_bold_pop" | "native_clean" | "premium_skincare" | "sales_karaoke" | "urgency_stack";
  font_id: string;
  headline_font_id: string;
  caption_font_id: string;
  font_color: string;
  highlight_color: string;
  subtitle_position: "top" | "center" | "bottom";
  subtitle_size: "compact" | "small" | "medium" | "large";
  subtitle_stroke_color: string;
  subtitle_stroke_width: number;
  subtitle_highlight_enabled: boolean;
  subtitle_animation: "current" | "phrase_cut";
  headline_animation: "current" | "pop_overshoot" | "soft_pop" | "fade_up" | "punch" | "slide_up";
  caption_animation: "current" | "staggered_reveal" | "fade_up" | "wipe" | "slide_up";
  headline_stroke_width: number;
  headline_shadow_color: string;
  headline_shadow_x: number;
  headline_shadow_y: number;
  headline_rotation_degrees: number;
  caption_stroke_width: number;
  color_grade: string;
  bgm_mode: "auto" | "none" | "selected";
  bgm_path: string;
  sfx_enabled: boolean;
  zoom_intensity: "none" | "subtle" | "normal" | "strong";
  product_zoom_enabled: boolean;
  subtitle_enabled: boolean;
  dynamic_text_mode: "off" | "minimal" | "balanced" | "high_energy";
  dynamic_text_roles: Array<"ingredients" | "benefits" | "usage" | "cta">;
  dynamic_text_settings: Record<
    "ingredients" | "benefits" | "usage" | "cta",
    {
      headline_font_id: string;
      body_font_id: string;
      font_size: number;
      animation: "current" | "staggered_reveal" | "fade_up" | "wipe" | "slide_up";
      duration_seconds: number;
    }
  >;
  letterbox_enabled: boolean;
  mirror_enabled: boolean;
  subtitle_y_frac: number;
  letterbox_top_frac: number;
  letterbox_bottom_frac: number;
  letterbox_hook_enabled: boolean;
  letterbox_hook_font_id: string;
  letterbox_hook_font_color: string;
  letterbox_hook_font_size: number;
  letterbox_hook_x_frac: number;
  letterbox_hook_y_frac: number;
};

export type VariationProfile = {
  schema_version: number;
  revision: string;
  variant_count: number;
  updated_at: string;
  variants: VariationVariant[];
  name?: string;
};

export type TrendConfiguration = {
  app_configured: boolean;
  access_configured: boolean;
  media_dir: string;
  media_dir_exists: boolean;
  qwen_enabled: boolean;
  ytdlp_available: boolean;
  ytdlp_version: string;
  download_concurrency: number;
  download_timeout_seconds: number;
  download_min_free_bytes: number;
  media_free_bytes: number;
  disk_reserve_satisfied: boolean;
  oauth?: TikTokOAuthStatus;
};

export type TikTokOAuthStatus = {
  app_configured: boolean;
  redirect_configured: boolean;
  redirect_uri: string;
  callback_supported: boolean;
  storage_path: string;
  storage_encrypted: boolean;
  connected: boolean;
  authorization_required: boolean;
  configuration_error: string;
  storage_error: string;
  flow?: string | null;
  refresh_supported?: boolean;
  access_token_expires_at?: string | null;
  refresh_token_expires_at?: string | null;
  advertiser_ids?: string[];
  selected_advertiser_id?: string | null;
  updated_at?: string | null;
};

export type TikTokOAuthStart = {
  authorization_url: string;
  redirect_uri: string;
  expires_in: number;
};

export type TrendSnapshot = {
  snapshot_id: string;
  retrieved_at: string;
  country_code: string;
  date_range: string;
  category_name: string;
  provider_request_id?: string;
};

export type TrendHashtag = {
  hashtag_id: string;
  hashtag_name: string;
  normalized_name?: string;
  source?: string;
  source_category?: string;
  original_rank?: number;
  display_rank?: number;
  relevance_type?: "topic" | "skin_concern" | "product" | "ingredient" | "routine" | "treatment" | "beauty_brand" | "personal_care_brand";
  matched_brand?: string | null;
  classification_reason?: string;
  rank_position: number;
  rank_change?: string;
  views?: number | null;
  posts?: number | null;
};

export type TrendHashtagClassification = {
  hashtag: string;
  normalized_name: string;
  source: string;
  source_category: string;
  original_rank: number;
  display_rank?: number | null;
  relevant: boolean;
  relevance_type: string;
  classification_reason: string;
  matched_brand?: string | null;
};

export type TrendHashtagDiagnostics = {
  source: string;
  source_category: string;
  total_candidates_returned: number;
  accepted_topical: number;
  accepted_brands: number;
  excluded: number;
  deduplicated: number;
  stored: number;
  backend_returned: number;
  selection_limit: number;
  classifications: TrendHashtagClassification[];
  exclusions: TrendHashtagClassification[];
};

export type TrendVideo = {
  snapshot_id: string;
  hashtag_id: string;
  hashtag_name: string;
  rank_position: number;
  video_id: string;
  provider_ordinal: number;
  original_provider_rank?: number;
  final_rank: number;
  share_url: string;
  embed_url: string;
  media_type: "video" | "image" | "carousel" | "unknown";
  is_available: boolean | number;
  classification_evidence: string;
  availability_evidence: string;
  video_duration_seconds?: number | null;
  image_count?: number | null;
  playable_url_count: number;
  provider_aweme_type?: number | null;
  exclusion_reason?: "image_or_carousel" | "unknown" | "unavailable" | null;
  relative_path?: string | null;
  file_sha256?: string | null;
  media_status?: "media_ready" | "analyzing" | "analyzed" | "failed" | null;
  media_error?: string | null;
  download_status?: "queued" | "downloading" | "downloaded" | "failed" | "interrupted" | null;
  download_error?: string | null;
  downloaded_relative_path?: string | null;
};

export type TrendVideoCandidateDiagnostic = {
  video_id: string;
  original_tiktok_rank: number;
  final_rank?: number | null;
  media_type: "video" | "image" | "carousel" | "unknown";
  is_available: boolean;
  classification_evidence: string;
  availability_evidence: string;
  exclusion_reason?: "image_or_carousel" | "unknown" | "unavailable" | null;
};

export type TrendVideoDiagnostics = {
  hashtag_id: string;
  hashtag_name: string;
  total_candidates_returned: number;
  video_posts_detected: number;
  image_carousel_posts_excluded: number;
  unknown_posts_excluded: number;
  unavailable_posts_excluded: number;
  valid_videos_stored: number;
  sent_to_frontend: number;
  pagination_available: boolean;
  candidate_limit: number;
  endpoint: string;
  candidates: TrendVideoCandidateDiagnostic[];
};

export type TrendDownloadSummary = {
  targets: number;
  queued: number;
  downloading: number;
  downloaded: number;
  reused: number;
  approved: number;
  failed: number;
  interrupted: number;
};

export type TrendRecommendation = {
  value: unknown;
  support_count: number;
  sample_count: number;
  confidence: number;
  applied_to_suggestion: boolean;
};

export type TrendPattern = {
  pattern_id: string;
  snapshot_id: string;
  created_at: string;
  analyzer_version: string;
  base_profile_revision: string;
  sample_count: number;
  recommendations: Record<string, TrendRecommendation>;
  suggested_profile: VariationProfile;
  failures?: Array<{ video_id: string; error: string }>;
};

export type TrendPageData = {
  configuration: TrendConfiguration;
  snapshot: TrendSnapshot | null;
  hashtags: TrendHashtag[];
  hashtag_diagnostics: TrendHashtagDiagnostics;
  videos: TrendVideo[];
  video_diagnostics: TrendVideoDiagnostics[];
  download_summary: TrendDownloadSummary | null;
  latest_pattern: TrendPattern | null;
  warnings: string[];
};

export type TrendMediaFile = {
  relative_path: string;
  name: string;
  size: number;
  mtime_ns: number;
};

export type TrendMediaFiles = {
  root: string;
  exists: boolean;
  files: TrendMediaFile[];
};

export type VariationOption = {
  id?: string;
  label: string;
  path?: string;
  exists?: boolean;
};

export type VariationTextStyle = {
  id: VariationVariant["text_style_id"];
  label: string;
  description: string;
  defaults: Partial<VariationVariant>;
};

export type VariationPresetRef = {
  preset_id: string;
  name: string;
  revision: string;
};

export type VariationPreviewSource = {
  path: string;
  url: string;
  kind: "video";
  exists: boolean;
};

export type ProductBrollPreviewRef = {
  path: string;
  url: string;
  kind: "video";
  exists: boolean;
};

export type ProductBrollPreviewProduct = {
  product_key: string;
  label: string;
  folder: string;
  exists: boolean;
  video_count: number;
  preview?: ProductBrollPreviewRef | null;
};

export type ProductBrollPreviewData = {
  root: string;
  exists: boolean;
  products: ProductBrollPreviewProduct[];
};

export type VariationPageData = {
  profile: VariationProfile;
  fonts: VariationOption[];
  text_styles: VariationTextStyle[];
  bgm_tracks: VariationOption[];
  hook_types: string[];
  visual_modes: Array<VariationVariant["visual_mode"]>;
  before_after_modes: Array<VariationVariant["before_after_mode"]>;
  subtitle_positions: string[];
  subtitle_sizes: Array<VariationVariant["subtitle_size"]>;
  dynamic_text_modes: Array<VariationVariant["dynamic_text_mode"]>;
  dynamic_text_roles: VariationVariant["dynamic_text_roles"];
  dynamic_text_animations: Array<VariationVariant["dynamic_text_settings"]["ingredients"]["animation"]>;
  color_grades: string[];
  bgm_modes: string[];
  zoom_intensities: string[];
  presets: VariationPresetRef[];
  limits: { min_variants: number; max_variants: number };
  preview_source: VariationPreviewSource;
  product_broll: ProductBrollPreviewData;
  global_feature_flags?: {
    sfx: boolean;
    bgm: boolean;
    before_after: boolean;
    broll_intro: boolean;
    transitional_hook: boolean;
    host_face_zoom: boolean;
  };
};

export type ProductInformationSource = {
  path: string;
  extension: string;
  size: number;
  sha256: string;
  status: string;
  cached: boolean;
  extraction_method: "llm" | "rules" | "rules_fallback";
  page_count: number;
  warnings: string[];
  products: string[];
  eligible_fact_count: number;
  fact_counts: Record<string, number>;
  unassigned_count: number;
};

export type ProductInformationProduct = {
  product_key: string;
  label: string;
  eligible_fact_count: number;
  fact_counts: Record<string, number>;
};

export type ProductInformationStatus = {
  schema_version: number;
  revision: string;
  scanned_at: string;
  root: string;
  sources: ProductInformationSource[];
  products: ProductInformationProduct[];
  unassigned_count: number;
  conflict_count: number;
  unassigned: Array<{
    role: string;
    text: string;
    source_file: string;
    locator: Record<string, unknown>;
    reason: string;
  }>;
  conflicts: Array<{
    product: string;
    role: string;
    key: string;
    fact_ids: string[];
    reason: string;
  }>;
  warnings: string[];
};

export type VariationPreviewMedia = {
  variant_index: number;
  variant_name: string;
  path: string;
  url: string;
  kind: "image" | "video";
  exists: boolean;
};

export type VariationPreviewResult = {
  profile_revision: string;
  source_clip: string;
  preview_source: VariationPreviewSource;
  previews: VariationPreviewMedia[];
  message: string;
  product_information_revision?: string;
  preview_product_key?: string;
};

export type ControlJobStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "interrupted"
  | "rejected";

export type ControlOperation =
  | "queue_control"
  | "settings_update"
  | "settings_delete"
  | "settings_reset"
  | "rescore"
  | "compliance_scan"
  | "module_assembly"
  | "export_batches"
  | "module_review"
  | "trend_refresh"
  | "trend_download"
  | "trend_analysis";

export type DesktopRuntimeStatus = {
  backend_running: boolean;
  backend_port?: number | null;
  project_root: string;
  python_exe: string;
  backend_command: string;
  last_error: string;
  recent_log: string[];
};

export type ControlJob = {
  job_id: string;
  operation: ControlOperation;
  status: ControlJobStatus;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  request: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  error?: string | null;
  conflict_key?: string | null;
  actor: string;
  result_metadata?: ControlJobResultMetadata | null;
};

export type ControlJobResultMetadata = {
  available: boolean;
  truncated: boolean;
  original_bytes?: number | null;
  stored_bytes?: number | null;
  expires_at?: string | null;
};

export type ControlJobResultPreview = {
  job_id?: string;
  preview: string;
  truncated: boolean;
  original_bytes?: number | null;
  stored_bytes?: number | null;
  expires_at?: string | null;
};

export type ControlJobResultSummary = {
  eligible_count?: number | null;
  actionable_count?: number | null;
  packaged_count?: number | null;
  pending_count?: number | null;
  packaged_total?: number | null;
  batch_size?: number | null;
  dry_run?: boolean | null;
};

export type ControlJobSummary = {
  job_id: string;
  operation: ControlOperation;
  status: ControlJobStatus;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  conflict_key?: string | null;
  actor: string;
  result_summary?: ControlJobResultSummary | null;
};

export type ControlJobPage = {
  jobs: ControlJobSummary[];
  total: number;
  limit: number;
  offset: number;
  active_count?: number;
};

export type OverviewTopClip = {
  score_key: string;
  clip_id: string;
  source_video?: string;
  product: string;
  total_score?: number | null;
  status?: string;
  scored_at: string;
  source_date: string;
  artifact?: ArtifactRef | null;
};

export type OverviewScoreTrendPoint = {
  date: string;
  average_score: number;
  scored_count: number;
};

export type OverviewData = {
  revision: string;
  queue_active: boolean;
  scored_count: number;
  review_needed_count: number;
  average_score?: number | null;
  export_ready_count: number;
  score_trend: OverviewScoreTrendPoint[];
  top_clips: OverviewTopClip[];
  compliance: {
    scanned: number;
    passed: number;
    blocked: number;
    rate: number;
  };
  export: {
    available: boolean;
    actionable: number;
    ready: number;
    packaged_last_run: number;
    packaged: number;
    pending: number;
    packaged_total: number;
    error_count: number;
    batch_size?: number | null;
    progress: number;
    status: string;
    updated_at: string;
    trigger: string;
    dry_run: boolean;
  };
};

export type RequestOptions = {
  signal?: AbortSignal;
  timeoutMs?: number;
};

export async function getJson<T>(path: string, options: RequestOptions = {}): Promise<ApiEnvelope<T>> {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? 30_000;
  const abortFromCaller = () => controller.abort(options.signal?.reason);
  if (options.signal?.aborted) {
    abortFromCaller();
  } else {
    options.signal?.addEventListener("abort", abortFromCaller, { once: true });
  }
  const timeout = window.setTimeout(() => controller.abort(new DOMException("Request timed out", "TimeoutError")), timeoutMs);
  try {
    const response = await fetch(path, { method: "GET", signal: controller.signal });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(`${response.status} ${response.statusText}: ${detail}`);
    }
    return (await response.json()) as ApiEnvelope<T>;
  } finally {
    window.clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abortFromCaller);
  }
}

export async function sendJson<T>(
  method: "POST" | "PUT" | "DELETE",
  path: string,
  body?: unknown
): Promise<ApiEnvelope<T>> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${detail}`);
  }
  const envelope = (await response.json()) as ApiEnvelope<T>;
  void invalidateApiDataForMutation(path);
  return envelope;
}

export function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  });
  const text = search.toString();
  return text ? `?${text}` : "";
}
