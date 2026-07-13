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

export type ModuleReadinessRow = {
  product: string;
  product_key: string;
  hook: number;
  main: number;
  cta: number;
  total: number;
  readiness: "ready" | "partial" | "empty";
  visual_total: number;
  visual_passed: number;
  visual_failed: number;
  visual_not_run: number;
  zoom_ready_candidates: number;
};

export type ModuleReadiness = {
  library_dir: string;
  index_path: string;
  index_exists: boolean;
  index_updated_at: string;
  index_module_count: number;
  thresholds: Record<string, number>;
  rows: ModuleReadinessRow[];
};

export type ModuleLibraryRow = {
  module_id: string;
  product: string;
  product_key: string;
  role: string;
  source_date: string;
  source_video: string;
  duration: number;
  confidence: number;
  quality_status: string;
  review_status: string;
  boundary_mode?: string;
  visual_validation_status: string;
  visual_product_hits: number;
  visual_product_confidence_max?: number;
  visual_validation_reason?: string;
  file_artifact?: ArtifactRef | null;
  transcript_text?: string;
};

export type ModuleDetail = {
  selected?: ModuleLibraryRow | null;
  transcript_text: string;
};

export type ModuleLibraryPage = {
  library_dir: string;
  rows: ModuleLibraryRow[];
  total: number;
  limit: number;
  offset: number;
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
  font_id: string;
  font_color: string;
  highlight_color: string;
  subtitle_position: "top" | "center" | "bottom";
  subtitle_size: "small" | "medium" | "large";
  color_grade: string;
  bgm_mode: "auto" | "none" | "selected";
  bgm_path: string;
  sfx_enabled: boolean;
  zoom_intensity: "none" | "subtle" | "normal" | "strong";
  product_zoom_enabled: boolean;
  subtitle_enabled: boolean;
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

export type VariationOption = {
  id?: string;
  label: string;
  path?: string;
  exists?: boolean;
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
  bgm_tracks: VariationOption[];
  hook_types: string[];
  visual_modes: Array<VariationVariant["visual_mode"]>;
  before_after_modes: Array<VariationVariant["before_after_mode"]>;
  subtitle_positions: string[];
  subtitle_sizes: Array<VariationVariant["subtitle_size"]>;
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

export type VariationPreviewImage = {
  variant_index: number;
  variant_name: string;
  path: string;
  url: string;
  kind: "image";
  exists: boolean;
};

export type VariationPreviewResult = {
  profile_revision: string;
  source_clip: string;
  preview_source: VariationPreviewSource;
  previews: VariationPreviewImage[];
  message: string;
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
  | "module_review";

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
