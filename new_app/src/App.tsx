import type { ReactNode } from "react";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import {
  BrowserRouter,
  Link,
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation
} from "react-router-dom";
import type { LucideIcon } from "lucide-react";
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Archive,
  BadgeCheck,
  Boxes,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ClipboardCheck,
  Clock,
  Cpu,
  Download,
  Eye,
  FileText,
  FolderOpen,
  Gauge,
  HardDrive,
  Layers3,
  LayoutDashboard,
  Library,
  ListChecks,
  Maximize2,
  Minus,
  Monitor,
  PackageCheck,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  Server,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Square,
  Terminal,
  TrendingUp,
  Video,
  X,
  Zap
} from "lucide-react";
import {
  ApiEnvelope,
  ComplianceIndexPage,
  ComplianceRow,
  ComplianceViolationRow,
  ControlJob,
  ControlJobPage,
  ControlJobResultPreview,
  ControlJobSummary,
  DashboardSummary,
  DesktopRuntimeStatus,
  getJson,
  LogTail,
  OverviewData,
  ProductInformationStatus,
  query,
  QueueDetail,
  QueueLaunchConfig,
  QueuePipelineMode,
  QueueRunRow,
  QueueRunMode,
  QueueVariantMode,
  QueueVodList,
  ScoreDetail,
  ScoreIndexPage,
  ScoreRow,
  sendJson,
  SettingsReadEntry,
  SettingsReadSnapshot,
  SystemStats,
  TrendMediaFiles,
  TrendPageData,
  TrendVideo,
  TikTokOAuthStart,
  VariationPageData,
  VariationPreviewResult,
  VariationProfile,
  VariationVariant,
  WhatsAppDeliveryStatus
} from "./api";
import { boundedJsonPreview } from "./boundedJsonPreview";
import { buildExportOverview } from "./exportOverview";
import { invalidateApiPrefix } from "./queryClient";
import { useApiQuery } from "./useApiQuery";
import { useDebouncedValue } from "./useDebouncedValue";
import { useLiveUpdateStatus } from "./liveUpdates";
import {
  defaultTrendHashtagId,
  completedTrendDownloadCount,
  displayedTrendHashtags,
  TREND_HASHTAG_DISPLAY_LIMIT,
  toggleTrendVideoSelection,
  trendDownloadDisabledReason,
  trendRecommendationRows,
  trendVideoCountsByHashtag,
  trendVideoIsSelectable,
  trendVideoShortageMessage,
  trendVideosForHashtag
} from "./trendSelection";
import {
  baselineMatchesServer,
  clampSelectedVariantIndex,
  copyVariationProfile,
  createPreviewRequestSignature,
  isDraftDirty,
  patchVariationVariant,
  resizeVariationProfile,
  shouldInvalidatePreview
} from "./variants/variantModel";
import { VariantCommandBar } from "./variants/VariantCommandBar";
import { VariantEditorTabs } from "./variants/VariantEditorTabs";
import { VariantNavigator } from "./variants/VariantNavigator";
import { VariantPreviewPanel } from "./variants/VariantPreviewPanel";
import { VariantWorkspace } from "./variants/VariantWorkspace";
import { ModularScannerPage } from "./modules/ModularScannerPage";
import type {
  PresetPanelFeedback,
  VariantCommandStatus,
  VariantPreviewFeedback
} from "./variants/variantTypes";
import "./styles.css";
import "./variants/variants.css";

type BadgeKind = "good" | "bad" | "warn" | "info" | "neutral";
type ActionMessage = { kind: BadgeKind; text: string };
type SortDirection = "asc" | "desc";
type HealthPayload = { status: string; mode: string };
type CatalogStatus = {
  mode: string;
  schema_version: number;
  integrity: string;
  queue_storage_mode: string;
  dirty_source_count: number;
  database_size: number;
  wal_size: number;
  table_counts: Record<string, number>;
  revisions: Record<string, number>;
  backfill?: { status?: string; duration_seconds?: number; error?: string };
  shadow_comparison?: { mismatch_count?: number; checked?: boolean };
};
type WindowControlAction = "get-state" | "minimize" | "toggle-maximize" | "close";
type ScoreGroup = {
  key: string;
  main: ScoreRow;
  variants: ScoreRow[];
  hasBase: boolean;
};

declare global {
  interface Window {
    clipperDesktop?: {
      getStatus?: () => Promise<DesktopRuntimeStatus>;
      windowControl?: (action: WindowControlAction) => Promise<{ maximized: boolean }>;
      restartApp?: () => Promise<void>;
      openOAuth?: (targetUrl: string) => Promise<boolean>;
    };
  }
}

type NavItem = {
  label: string;
  path: string;
  match: string;
  icon: LucideIcon;
  detail: string;
};

const mainNav: NavItem[] = [
  { label: "Home", path: "/overview", match: "/overview", icon: LayoutDashboard, detail: "What needs attention and recent production work." },
  { label: "Production", path: "/production/live", match: "/production", icon: Play, detail: "Run your pipeline to generate and process clips." },
  { label: "Review", path: "/review/clips", match: "/review", icon: Video, detail: "Review and approve clips from your production runs." },
  { label: "Variants", path: "/variants", match: "/variants", icon: SlidersHorizontal, detail: "Configure how clips are transformed and rendered." },
  { label: "Deliveries", path: "/deliveries", match: "/deliveries", icon: PackageCheck, detail: "Manage and track clip deliveries." }
];

const toolNav: NavItem[] = [
  { label: "Trends", path: "/trends", match: "/trends", icon: TrendingUp, detail: "TikTok discovery and editing recommendations." },
  { label: "Modules", path: "/modules", match: "/modules", icon: Library, detail: "Scan VODs for reusable product moments." }
];

const secondaryNav: NavItem[] = [
  { label: "Activity", path: "/activity/jobs", match: "/activity", icon: Activity, detail: "Background jobs and pipeline logs" },
  { label: "Settings", path: "/settings/configuration", match: "/settings", icon: Settings, detail: "Configuration and local diagnostics" }
];

const allNav = [...mainNav, ...toolNav, ...secondaryNav];

const contextTabs: Array<{ match: string; items: Array<{ label: string; path: string; icon: LucideIcon }> }> = [
  {
    match: "/production",
    items: [
      { label: "Live", path: "/production/live", icon: Gauge },
      { label: "Queue", path: "/production/queue", icon: ListChecks }
    ]
  },
  {
    match: "/review",
    items: [
      { label: "Clips", path: "/review/clips", icon: Video },
      { label: "Compliance", path: "/review/compliance", icon: ShieldCheck }
    ]
  },
  {
    match: "/activity",
    items: [
      { label: "Jobs", path: "/activity/jobs", icon: Activity },
      { label: "Logs", path: "/activity/logs", icon: Terminal }
    ]
  },
  {
    match: "/settings",
    items: [
      { label: "Configuration", path: "/settings/configuration", icon: Settings },
      { label: "Diagnostics", path: "/settings/diagnostics", icon: Cpu }
    ]
  }
];

function navItemIsActive(item: NavItem, pathname: string): boolean {
  return pathname === item.match || pathname.startsWith(`${item.match}/`);
}

function statusClass(value?: string | null): BadgeKind {
  const normalized = String(value ?? "").toLowerCase();
  if (["completed", "complete", "strong", "ready", "passed", "sent", "success", "healthy", "approved", "downloaded"].some((item) => normalized.includes(item))) {
    return "good";
  }
  if (["failed", "blocked", "critical", "stalled", "rejected", "interrupted", "error", "outside"].some((item) => normalized.includes(item))) {
    return "bad";
  }
  if (["running", "processing", "active", "rendering", "scanning", "downloading", "in_progress", "in progress"].some((item) => normalized.includes(item))) {
    return "info";
  }
  if (["review", "attention", "waiting", "partial", "paused", "queued", "pending"].some((item) => normalized.includes(item))) {
    return "warn";
  }
  if (!normalized || normalized === "none" || normalized === "-" || ["idle", "stopped", "unknown", "disabled", "not started"].some((item) => normalized.includes(item))) {
    return "neutral";
  }
  return "neutral";
}

function healthText(summary?: DashboardSummary): string {
  const health = summary?.queue_health ?? {};
  const label = health["status_label"];
  if (typeof label === "string" && label) {
    return label;
  }
  return summary?.queue_status || "Unknown";
}

function healthSummary(summary?: DashboardSummary): string {
  const text = summary?.queue_health?.["summary"];
  return typeof text === "string" && text ? text : "No queue summary yet.";
}

function numberText(value: number | undefined | null, digits = 0): string {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(value ?? 0);
}

function byteSizeText(value: number | undefined | null): string {
  const bytes = Math.max(0, value ?? 0);
  if (bytes < 1024) return `${numberText(bytes)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let amount = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && amount >= 1024; index += 1) {
    amount /= 1024;
    unit = units[index];
  }
  return `${numberText(amount, amount >= 10 ? 1 : 2)} ${unit}`;
}

function scoreText(value?: number | null): string {
  return value === undefined || value === null ? "-" : value.toFixed(1);
}

function reviewFlagLabel(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (letter) => letter.toUpperCase()) || "Review signal";
}

function reviewRangeText(total: number, limit: number, offset: number, count: number): string {
  if (total <= 0 || count <= 0) {
    return "0 of 0";
  }
  const start = offset + 1;
  const end = Math.min(total, offset + count);
  return `${numberText(start)}-${numberText(end)} of ${numberText(total)}`;
}

function groupedScoreRows(rows: ScoreRow[]): ScoreGroup[] {
  const grouped = new Map<string, ScoreRow[]>();
  rows.forEach((row) => {
    const key = row.base_score_key || row.score_key;
    const bucket = grouped.get(key) ?? [];
    bucket.push(row);
    grouped.set(key, bucket);
  });

  return Array.from(grouped.entries()).map(([key, groupRows]) => {
    const base = groupRows.find((row) => row.row_type === "base");
    const main = base ?? groupRows[0];
    const variants = groupRows.filter((row) => row.row_type === "variant" && row.score_key !== main.score_key);
    return {
      key,
      main,
      variants,
      hasBase: Boolean(base)
    };
  });
}

function parentDir(path: string): string {
  const index = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return index > 0 ? path.slice(0, index) : path;
}

function compactJson(value?: Record<string, unknown> | null): string {
  if (!value || Object.keys(value).length === 0) {
    return "-";
  }
  return JSON.stringify(value, null, 2);
}

function operationLabel(value: string): string {
  if (value === "module_assembly" || value === "module_review") {
    return `${value.replace(/_/g, " ")} (legacy unsupported)`;
  }
  return value.replace(/_/g, " ");
}

function uniqueOptions(values: Array<string | undefined | null>): string[] {
  return Array.from(new Set(values.map((value) => String(value ?? "").trim()).filter(Boolean))).sort((a, b) => a.localeCompare(b));
}

const runModeOptions: Array<{ value: QueueRunMode; label: string }> = [
  { value: "folder_repeat", label: "Folder Repeat" },
  { value: "folder_once", label: "Folder Once" },
  { value: "single_video", label: "Single Video" }
];

const pipelineModeOptions: Array<{ value: QueuePipelineMode; label: string }> = [
  { value: "full", label: "Full Pipeline" },
  { value: "clips_only", label: "Clips Only" },
  { value: "raw_cuts_only", label: "Raw Cuts Only" }
];

const variantModeOptions: Array<{ value: QueueVariantMode; label: string }> = [
  { value: "all", label: "All Variants" },
  { value: "original", label: "Original Only" },
  { value: "custom", label: "Custom Count" }
];

type OperationStageKey = "transcribe" | "llm" | "yolo" | "ffmpeg";
type OperationStageState = "done" | "running" | "waiting";

const operationStages: Array<{ key: OperationStageKey; label: string; icon: LucideIcon }> = [
  { key: "transcribe", label: "Transcription", icon: FileText },
  { key: "llm", label: "Sales Moment Detection", icon: ListChecks },
  { key: "yolo", label: "Product/Face Scan", icon: Boxes },
  { key: "ffmpeg", label: "Clip Rendering", icon: Clock }
];

function launchSummary(config?: Partial<QueueLaunchConfig>, fallback = "Folder Repeat • Full Pipeline • All Variants • Unlimited"): string {
  if (!config?.run_mode || !config.pipeline_mode) {
    return fallback;
  }
  const run = runModeOptions.find((item) => item.value === config.run_mode)?.label ?? config.run_mode;
  const pipeline = config.pipeline_mode === "modules_only"
    ? "Modules Only (legacy unsupported)"
    : pipelineModeOptions.find((item) => item.value === config.pipeline_mode)?.label ?? config.pipeline_mode;
  const variantMode = config.pipeline_mode === "raw_cuts_only" ? "original" : (config.variant_mode ?? "all");
  const variants = variantMode === "custom"
    ? `${config.variant_count ?? 1} Variants`
    : variantModeOptions.find((item) => item.value === variantMode)?.label ?? variantMode;
  const maxClips = config.max_clips == null ? "Unlimited" : `${config.max_clips} clip${config.max_clips === 1 ? "" : "s"}`;
  return [run, pipeline, variants, maxClips].filter(Boolean).join(" • ");
}

function isQueueStatusActive(value?: string | null): boolean {
  return ["running", "paused", "pause_requested", "restart_pending", "start_requested", "continue_requested"].includes(
    String(value ?? "").toLowerCase()
  );
}

function isQueuePaused(queue?: QueueDetail): boolean {
  return [queue?.control_status, queue?.queue_status].some((value) => String(value ?? "").toLowerCase().includes("paused"));
}

function isQueueActive(queue?: QueueDetail): boolean {
  return isQueueStatusActive(queue?.control_status) || isQueueStatusActive(queue?.queue_status);
}

function isRunActive(row?: QueueRunRow | null): boolean {
  const status = String(row?.status ?? "").toLowerCase();
  return ["running", "processing", "active", "in_progress", "in progress"].some((item) => status.includes(item));
}

function isTerminalRun(row?: QueueRunRow | null): boolean {
  const status = String(row?.status ?? "").toLowerCase();
  return ["completed", "failed", "stopped", "interrupted", "cancelled", "canceled", "skipped"].some((item) => status.includes(item));
}

function runTime(row: QueueRunRow): number {
  const value = row.started_at || row.completed_at;
  const parsed = value ? Date.parse(value) : Number.NaN;
  return Number.isNaN(parsed) ? 0 : parsed;
}

function newestRun(rows: QueueRunRow[]): QueueRunRow | undefined {
  return [...rows].sort((a, b) => runTime(b) - runTime(a))[0];
}

function pickCurrentRun(rows: QueueRunRow[], queueStatus?: string | null): QueueRunRow | undefined {
  const active = rows.filter(isRunActive);
  if (active.length > 0) {
    return newestRun(active);
  }

  if (isQueueStatusActive(queueStatus)) {
    return newestRun(rows.filter((row) => !isTerminalRun(row) && row.progress > 0 && row.progress < 100));
  }

  return undefined;
}

function isQueuedVideo(row: QueueRunRow): boolean {
  return !isTerminalRun(row) && row.progress < 100;
}

function clampProgress(value: number | undefined | null): number {
  return Math.max(0, Math.min(100, value ?? 0));
}

function averageProgress(rows: QueueRunRow[]): number {
  if (rows.length === 0) {
    return 0;
  }
  return Math.round(rows.reduce((total, row) => total + clampProgress(row.progress), 0) / rows.length);
}

function runStatusKind(value?: string | null): BadgeKind {
  return statusClass(value);
}

function stageKeyForRun(row?: QueueRunRow | null): OperationStageKey | undefined {
  const raw = `${row?.current_stage ?? ""} ${row?.current_step ?? ""}`.toLowerCase();
  if (!raw.trim()) {
    return undefined;
  }
  if (["transcribe", "transcription", "whisper"].some((item) => raw.includes(item))) {
    return "transcribe";
  }
  if (["llm", "sales", "moment", "detect"].some((item) => raw.includes(item))) {
    return "llm";
  }
  if (["yolo", "product", "face", "scan"].some((item) => raw.includes(item))) {
    return "yolo";
  }
  if (["ffmpeg", "render", "clip"].some((item) => raw.includes(item))) {
    return "ffmpeg";
  }
  return undefined;
}

function operationStageState(
  stage: OperationStageKey,
  activeStage: OperationStageKey | undefined,
  row: QueueRunRow | undefined,
  summary?: DashboardSummary
): OperationStageState {
  const running = summary?.stage_running?.[stage] ?? 0;
  if (running > 0 || stage === activeStage) {
    return "running";
  }
  const activeIndex = operationStages.findIndex((item) => item.key === activeStage);
  const stageIndex = operationStages.findIndex((item) => item.key === stage);
  if (row && activeIndex >= 0 && stageIndex >= 0 && stageIndex < activeIndex) {
    return "done";
  }
  if (row && row.progress >= 100) {
    return "done";
  }
  return "waiting";
}

function operationStageProgress(
  state: OperationStageState,
  stage: OperationStageKey,
  activeStage: OperationStageKey | undefined,
  row: QueueRunRow | undefined
): number {
  if (state === "done") {
    return 100;
  }
  if (state === "running") {
    if (stage === activeStage && row) {
      return Math.max(8, Math.min(100, row.progress));
    }
    return 64;
  }
  return 0;
}

function displayTime(value?: string | null): string {
  if (!value) {
    return "-";
  }
  const parsed = new Date(value);
  if (!Number.isNaN(parsed.getTime())) {
    return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(parsed);
  }
  return value;
}

type ComplianceOverview = {
  scanned: number;
  passed: number;
  blocked: number;
  rate: number;
};

function asRecord(value: unknown): Record<string, unknown> | undefined {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return undefined;
}

function numericValue(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function recordNumber(record: Record<string, unknown> | undefined, key: string): number | undefined {
  return numericValue(record?.[key]);
}

function exportReadyRows(rows: ScoreRow[]): ScoreRow[] {
  return rows.filter((row) => {
    const status = String(row.status ?? "").toLowerCase();
    return !row.compliance_blocked && !status.includes("blocked") && !status.includes("review");
  });
}

function buildComplianceOverview(data: ComplianceIndexPage | undefined, scoreRows: ScoreRow[]): ComplianceOverview {
  const summary = data?.summary ?? {};
  let scanned = recordNumber(summary, "scanned") ?? data?.total ?? data?.rows.length ?? 0;
  let passed = recordNumber(summary, "passed") ?? data?.rows.filter((row) => row.passed).length ?? 0;
  let blocked = recordNumber(summary, "blocked") ?? data?.rows.filter((row) => row.blocked).length ?? 0;

  if (scanned === 0 && scoreRows.length > 0) {
    scanned = scoreRows.length;
    blocked = scoreRows.filter((row) => row.compliance_blocked).length;
    passed = scanned - blocked;
  }

  const rate = scanned > 0 ? (passed / scanned) * 100 : 0;
  return { scanned, passed, blocked, rate };
}

function usePageInfo(): NavItem {
  const location = useLocation();
  return (
    allNav.find((item) => navItemIsActive(item, location.pathname)) ??
    mainNav[0]
  );
}

function ContextTabs() {
  const location = useLocation();
  const group = contextTabs.find((item) => location.pathname === item.match || location.pathname.startsWith(`${item.match}/`));
  if (!group) {
    return null;
  }
  return (
    <nav className="context-tabs" aria-label="Page sections">
      {group.items.map((item) => (
        <NavLink className={({ isActive }) => `context-tab ${isActive ? "active" : ""}`} to={item.path} key={item.path}>
          <item.icon size={15} aria-hidden="true" />
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}

async function submitMutation(
  run: () => Promise<ApiEnvelope<ControlJob>>,
  setMessage: (message: ActionMessage) => void,
  refreshJobs: () => void,
  refreshViews: Array<() => void> = []
): Promise<void> {
  try {
    const envelope = await run();
    const job = envelope.data;
    setMessage({
      kind: statusClass(job.status),
      text: `Job ${job.job_id.slice(0, 8)} ${job.status}: ${operationLabel(job.operation)}`
    });
    refreshJobs();
    refreshViews.forEach((refresh) => refresh());
  } catch (caught: unknown) {
    setMessage({ kind: "bad", text: caught instanceof Error ? caught.message : String(caught) });
  }
}

function refreshJobQueries(): void {
  void invalidateApiPrefix("/api/control/jobs");
}

function dashboardPollingInterval(summary?: DashboardSummary): number {
  const status = String(summary?.queue_status ?? "").toLowerCase();
  return ["running", "active", "starting", "paused", "stopping"].some((value) => status.includes(value))
    ? 2_000
    : 15_000;
}

function jobPollingInterval(page?: ControlJobPage): number {
  const activeCount = page?.active_count ?? page?.jobs.filter((job) => ["queued", "running"].includes(job.status)).length ?? 0;
  return activeCount > 0 ? 2_000 : 15_000;
}

function AppShell({ children }: { children: ReactNode }) {
  const dashboard = useApiQuery<DashboardSummary>("/api/dashboard", dashboardPollingInterval, true);
  const summary = dashboard.envelope?.data;
  const page = usePageInfo();
  const location = useLocation();
  const topbarDetail = page.path === "/overview" ? "Welcome back. Here’s what’s happening with your pipeline." : page.detail;
  const variantsOwnsPageIdentity = location.pathname === "/variants";
  return (
    <div className="app-shell">
      <div className="desktop-drag-strip" aria-hidden="true" />
      <WindowControls />
      <aside className="side-rail">
        <Link className="brand-block" to="/overview" aria-label="Clipper overview home">
          <div className="brand-mark" aria-hidden="true">C</div>
          <div className="brand-title">Clipper</div>
        </Link>

        <nav className="nav-list" aria-label="Main navigation">
          {mainNav.map((item) => (
            <NavLink className={() => `nav-item ${navItemIsActive(item, location.pathname) ? "active" : ""}`} key={item.path} to={item.path} aria-label={item.label} title={item.label}>
              <item.icon aria-hidden="true" size={18} />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>

        <nav className="nav-list secondary-nav" aria-label="Support navigation">
          {secondaryNav.map((item) => (
            <NavLink className={() => `nav-item ${navItemIsActive(item, location.pathname) ? "active" : ""}`} key={item.path} to={item.path} aria-label={item.label} title={item.label}>
              <item.icon aria-hidden="true" size={18} />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <details className="sidebar-tools" open={toolNav.some((item) => navItemIsActive(item, location.pathname)) || undefined}>
          <summary>More</summary>
          <nav className="nav-list" aria-label="Additional tools">
            {toolNav.map((item) => (
              <NavLink className={() => `nav-item ${navItemIsActive(item, location.pathname) ? "active" : ""}`} key={item.path} to={item.path}>
                <item.icon aria-hidden="true" size={17} />
                <span>{item.label}</span>
              </NavLink>
            ))}
          </nav>
        </details>
      </aside>

      <nav className="mobile-bottom-nav" aria-label="Mobile navigation">
        {mainNav.slice(0, 3).map((item) => (
          <Link className={navItemIsActive(item, location.pathname) ? "active" : ""} to={item.path} key={item.path}>
            <item.icon size={18} aria-hidden="true" />
            <span>{item.label}</span>
          </Link>
        ))}
        <details className="mobile-more-nav">
          <summary><Settings size={18} aria-hidden="true" /><span>More</span></summary>
          <div>
            {[...mainNav.slice(3), ...toolNav, ...secondaryNav].map((item) => (
              <Link to={item.path} key={item.path}><item.icon size={17} aria-hidden="true" />{item.label}</Link>
            ))}
          </div>
        </details>
      </nav>

      <main className={`main-panel ${variantsOwnsPageIdentity ? "variants-main-panel" : ""}`}>
        <header className={`topbar ${variantsOwnsPageIdentity ? "command-page-topbar" : ""}`}>
          {!variantsOwnsPageIdentity && (
            <div>
              <h1>{page.label}</h1>
              <p>{topbarDetail}</p>
            </div>
          )}
          <div className="topbar-actions">
            <QueueHealthPill summary={summary} />
          </div>
        </header>
        <ContextTabs />
        {children}
      </main>
    </div>
  );
}

function QueueHealthPill({ summary }: { summary?: DashboardSummary }) {
  const value = healthText(summary);
  return (
    <Link className={`queue-health-pill ${statusClass(value)}`} to="/settings/diagnostics" aria-label="Open queue and system health">
      <span className="status-dot" aria-hidden="true" />
      <span>Queue Health</span>
      <strong>{value}</strong>
    </Link>
  );
}

function WindowControls() {
  const [maximized, setMaximized] = useState(false);
  const hasDesktopBridge = typeof window !== "undefined" && Boolean(window.clipperDesktop?.windowControl);
  const desktopLaunch = typeof window !== "undefined" && (
    new URLSearchParams(window.location.search).get("desktop") === "1"
    || window.sessionStorage.getItem("clipper:desktop-shell") === "1"
  );
  const canControlWindow = hasDesktopBridge || desktopLaunch || (typeof navigator !== "undefined" && navigator.userAgent.includes("Electron"));

  useEffect(() => {
    if (desktopLaunch) window.sessionStorage.setItem("clipper:desktop-shell", "1");
  }, [desktopLaunch]);

  useEffect(() => {
    if (!hasDesktopBridge) return;
    const sync = () => {
      void window.clipperDesktop?.windowControl?.("get-state").then((result) => setMaximized(Boolean(result?.maximized)));
    };
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, [hasDesktopBridge]);

  if (!canControlWindow) {
    return null;
  }

  async function send(action: WindowControlAction) {
    const result = await window.clipperDesktop?.windowControl?.(action);
    if (result && action !== "close") {
      setMaximized(result.maximized);
    }
  }

  return (
    <div className="window-controls" aria-label="Window controls">
      <button className="window-control-button" onClick={() => void send("minimize")} aria-label="Minimize window">
        <Minus size={15} aria-hidden="true" />
      </button>
      <button className="window-control-button" onClick={() => void send("toggle-maximize")} aria-label={maximized ? "Restore window" : "Maximize window"}>
        <Maximize2 size={14} aria-hidden="true" />
      </button>
      <button className="window-control-button close" onClick={() => void send("close")} aria-label="Close window">
        <X size={15} aria-hidden="true" />
      </button>
    </div>
  );
}

function PageTitle({
  title,
  detail,
  onRefresh,
  children
}: {
  title: string;
  detail: string;
  onRefresh?: () => void;
  children?: ReactNode;
}) {
  return (
    <div className="page-title">
      <div>
        <h2>{title}</h2>
        <p>{detail}</p>
      </div>
      <div className="title-actions">
        {children}
        {onRefresh && (
          <button className="secondary-button" onClick={onRefresh}>
            <RefreshCw size={16} aria-hidden="true" />
            Refresh
          </button>
        )}
      </div>
    </div>
  );
}

function Badge({ value, kind }: { value: string; kind?: BadgeKind }) {
  return (
    <span className={`badge ${kind ?? statusClass(value)}`}>
      <span className="status-dot" aria-hidden="true" />
      {value || "Unknown"}
    </span>
  );
}

function StateBlock({
  kind = "info",
  title,
  detail,
  warnings
}: {
  kind?: BadgeKind;
  title?: string;
  detail?: string;
  warnings?: string[];
}) {
  if (!title && !detail && !warnings?.length) {
    return null;
  }
  return (
    <div className={`state-block ${kind}`}>
      {title && <strong>{title}</strong>}
      {detail && <span>{detail}</span>}
      {warnings?.slice(0, 4).map((warning) => (
        <span key={warning}>{warning}</span>
      ))}
    </div>
  );
}

function ActionNotice({ message }: { message?: ActionMessage }) {
  if (!message) {
    return null;
  }
  const urgent = message.kind === "bad" || message.kind === "warn";
  return (
    <div
      className="action-notice"
      role={urgent ? "alert" : "status"}
      aria-live={urgent ? "assertive" : "polite"}
      aria-atomic="true"
    >
      <StateBlock kind={message.kind} detail={message.text} />
    </div>
  );
}

function EmptyState({ icon: Icon, title, detail }: { icon: LucideIcon; title: string; detail: string }) {
  return (
    <div className="empty-state">
      <Icon size={22} aria-hidden="true" />
      <strong>{title}</strong>
      <span>{detail}</span>
    </div>
  );
}

function SkeletonLines({ count = 4 }: { count?: number }) {
  return (
    <div className="skeleton-stack" aria-label="Loading">
      {Array.from({ length: count }).map((_, index) => (
        <span className="skeleton-line" key={index} />
      ))}
    </div>
  );
}

function MetricCard({ label, value, hint, icon: Icon }: { label: string; value: string; hint: string; icon?: LucideIcon }) {
  return (
    <div className="metric-card">
      <div className="metric-head">
        <div className="metric-label">{label}</div>
        {Icon && <Icon size={17} aria-hidden="true" />}
      </div>
      <div className="metric-value">{value}</div>
      <div className="metric-hint">{hint}</div>
    </div>
  );
}

function Progress({ value }: { value: number }) {
  const safe = Math.max(0, Math.min(100, value));
  return (
    <div className="progress-cell">
      <div className="progress" aria-label={`Progress ${safe}%`}>
        <span style={{ width: `${safe}%` }} />
      </div>
      <span>{safe}%</span>
    </div>
  );
}

function Drawer({
  open,
  title,
  detail,
  onClose,
  children
}: {
  open: boolean;
  title: string;
  detail?: string;
  onClose: () => void;
  children: ReactNode;
}) {
  if (!open) {
    return null;
  }
  return (
    <aside className="drawer" aria-label={title}>
      <div className="drawer-head">
        <div>
          <h2>{title}</h2>
          {detail && <p>{detail}</p>}
        </div>
        <button className="icon-button" onClick={onClose} aria-label="Close detail panel">
          <X size={18} aria-hidden="true" />
        </button>
      </div>
      <div className="drawer-body">{children}</div>
    </aside>
  );
}

function ConfirmDialog({
  open,
  title,
  detail,
  confirmLabel,
  danger = false,
  confirmDisabled = false,
  onConfirm,
  onClose,
  children
}: {
  open: boolean;
  title: string;
  detail: string;
  confirmLabel: string;
  danger?: boolean;
  confirmDisabled?: boolean;
  onConfirm: () => void;
  onClose: () => void;
  children?: ReactNode;
}) {
  const confirmRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (!open) {
      return;
    }
    confirmRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);
  if (!open) {
    return null;
  }
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-dialog-title" aria-describedby="confirm-dialog-detail">
        <div className={`confirm-dialog-icon ${danger ? "danger" : "info"}`}>
          {danger ? <AlertTriangle size={22} aria-hidden="true" /> : <CheckCircle2 size={22} aria-hidden="true" />}
        </div>
        <div>
          <h2 id="confirm-dialog-title">{title}</h2>
          <p id="confirm-dialog-detail">{detail}</p>
        </div>
        {children && <div className="confirm-dialog-content">{children}</div>}
        <div className="confirm-dialog-actions">
          <button className="secondary-button" onClick={onClose}>Cancel</button>
          <button ref={confirmRef} disabled={confirmDisabled} className={danger ? "danger-button" : "primary-button"} onClick={() => { onConfirm(); onClose(); }}>
            {confirmLabel}
          </button>
        </div>
      </section>
    </div>
  );
}

const JOB_TRAY_DISMISSED_STORAGE_KEY = "clipper.job-tray.dismissed.v1";

export function jobTrayDismissalKey(job: ControlJobSummary): string {
  return `${job.job_id}:${job.status}`;
}

export function selectJobTrayJobs(jobs: ControlJobSummary[], dismissed: ReadonlySet<string>): ControlJobSummary[] {
  return jobs
    .filter((job) => ["queued", "running", "failed", "rejected"].includes(job.status))
    .slice(0, 3)
    .filter((job) => !dismissed.has(jobTrayDismissalKey(job)));
}

function readDismissedJobTrayItems(): Set<string> {
  try {
    const stored = JSON.parse(globalThis.localStorage?.getItem(JOB_TRAY_DISMISSED_STORAGE_KEY) ?? "[]");
    return new Set(Array.isArray(stored) ? stored.filter((item): item is string => typeof item === "string") : []);
  } catch {
    return new Set();
  }
}

function JobTray() {
  const jobs = useApiQuery<ControlJobPage>("/api/control/jobs?limit=12", jobPollingInterval, true);
  const [dismissed, setDismissed] = useState<Set<string>>(readDismissedJobTrayItems);
  const candidates = (jobs.envelope?.data.jobs ?? [])
    .filter((job) => ["queued", "running", "failed", "rejected"].includes(job.status))
    .slice(0, 3);
  const visible = selectJobTrayJobs(jobs.envelope?.data.jobs ?? [], dismissed);

  function dismiss(): void {
    const next = new Set(dismissed);
    candidates.forEach((job) => next.add(jobTrayDismissalKey(job)));
    const bounded = new Set([...next].slice(-100));
    setDismissed(bounded);
    try {
      globalThis.localStorage?.setItem(JOB_TRAY_DISMISSED_STORAGE_KEY, JSON.stringify([...bounded]));
    } catch {
      // The in-memory dismissal still works when browser storage is unavailable.
    }
  }

  if (visible.length === 0) {
    return null;
  }
  return (
    <aside className="job-tray" aria-label="Background jobs">
      <div className="job-tray-head">
        <span><Activity size={15} aria-hidden="true" /> Background activity</span>
        <div className="job-tray-actions">
          <Link to="/activity/jobs">View all</Link>
          <button type="button" onClick={dismiss} aria-label="Dismiss background activity" title="Dismiss background activity">
            <X size={15} aria-hidden="true" />
          </button>
        </div>
      </div>
      {visible.map((job) => (
        <Link className="job-tray-row" to={`/activity/jobs?job=${encodeURIComponent(job.job_id)}`} key={job.job_id}>
          <span className={`status-dot ${statusClass(job.status)}`} />
          <span>
            <strong>{operationLabel(job.operation)}</strong>
            <small>{job.error || job.status}</small>
          </span>
          <Badge value={job.status} />
        </Link>
      ))}
    </aside>
  );
}

function Pagination({
  total,
  limit,
  offset,
  setOffset
}: {
  total: number;
  limit: number;
  offset: number;
  setOffset: (offset: number) => void;
}) {
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  return (
    <div className="pagination">
      <button className="secondary-button" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - limit))}>
        <ChevronLeft size={16} aria-hidden="true" />
        Previous
      </button>
      <span>Page {page} of {pages}</span>
      <button className="secondary-button" disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>
        Next
        <ChevronRight size={16} aria-hidden="true" />
      </button>
    </div>
  );
}

function FilterField({
  label,
  children
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <label className="filter-field">
      <span>{label}</span>
      {children}
    </label>
  );
}

function SearchInput({
  value,
  onChange,
  placeholder,
  className = "",
  ariaLabel
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  className?: string;
  ariaLabel?: string;
}) {
  return (
    <div className={`search-input ${className}`.trim()}>
      <Search size={16} aria-hidden="true" />
      <input aria-label={ariaLabel} value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} />
    </div>
  );
}

function IndexSelect({
  label,
  icon: Icon,
  value,
  onChange,
  children
}: {
  label: string;
  icon: LucideIcon;
  value: string;
  onChange: (value: string) => void;
  children: ReactNode;
}) {
  return (
    <label className="index-toolbar-select" title={label}>
      <span className="visually-hidden">{label}</span>
      <Icon size={15} aria-hidden="true" />
      <select aria-label={label} value={value} onChange={(event) => onChange(event.target.value)}>
        {children}
      </select>
    </label>
  );
}

function SortDirectionButton({ direction, onToggle }: { direction: SortDirection; onToggle: () => void }) {
  const descending = direction === "desc";
  const label = descending ? "Sort descending. Activate to sort ascending." : "Sort ascending. Activate to sort descending.";
  const Icon = descending ? ArrowDown : ArrowUp;
  return (
    <button className="icon-button toolbar-icon-button" type="button" onClick={onToggle} aria-label={label} title={label}>
      <Icon size={16} aria-hidden="true" />
    </button>
  );
}

function QueueTable({
  rows,
  compact = false,
  selected,
  setSelected
}: {
  rows: QueueRunRow[];
  compact?: boolean;
  selected?: QueueRunRow | null;
  setSelected?: (row: QueueRunRow) => void;
}) {
  if (rows.length === 0) {
    return <EmptyState icon={ListChecks} title="No queue rows" detail="Queue state is empty or not available yet." />;
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Video</th>
            <th>Status</th>
            <th>Step</th>
            <th>Progress</th>
            <th>Clips</th>
            {!compact && <th>Duration</th>}
            {!compact && <th>Attention</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              className={selected?.run_id === row.run_id ? "selected-row" : ""}
              key={row.run_id || `${row.video_name}-${row.started_at}-${row.attempt_number}`}
              onClick={() => setSelected?.(row)}
            >
              <td>
                <div className="strong">{row.video_name}</div>
                <div className="muted">Run {row.attempt_number}</div>
              </td>
              <td><Badge value={row.status} /></td>
              <td>{row.current_step}</td>
              <td><Progress value={row.progress} /></td>
              <td>{numberText(row.clips_generated)}</td>
              {!compact && <td>{row.duration}</td>}
              {!compact && <td className="muted attention-cell">{queueAttentionText(row)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function queueAttentionText(row: QueueRunRow): string {
  const failed = ["failed", "error", "blocked", "rejected"].some((value) => row.status.toLowerCase().includes(value));
  if (failed && (!row.attention || row.attention.toLowerCase() === "clear")) return "—";
  return row.attention || "Clear";
}

function SegmentedControl<T extends string>({
  label,
  value,
  options,
  onChange,
  disabled = false
}: {
  label: string;
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
  disabled?: boolean;
}) {
  return (
    <div className="launcher-field">
      <span>{label}</span>
      <div className="segmented-control">
        {options.map((option) => (
          <button
            type="button"
            className={value === option.value ? "selected" : ""}
            aria-pressed={value === option.value}
            disabled={disabled}
            key={option.value}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function RunLauncher({
  onQueueRefresh,
  surface = "standard"
}: {
  onQueueRefresh?: () => void;
  surface?: "standard" | "operations";
}) {
  const queue = useApiQuery<QueueDetail>("/api/queue?view=attempts", (data) => isQueueActive(data) ? 2_000 : 15_000, true);
  const vods = useApiQuery<QueueVodList>("/api/queue/vods", 8_000, true);
  const [runMode, setRunMode] = useState<QueueRunMode>("folder_repeat");
  const [pipelineMode, setPipelineMode] = useState<QueuePipelineMode>("full");
  const [variantMode, setVariantMode] = useState<QueueVariantMode>("all");
  const [variantCount, setVariantCount] = useState(2);
  const [maxClips, setMaxClips] = useState("0");
  const [videoPath, setVideoPath] = useState("");
  const [message, setMessage] = useState<ActionMessage>();
  const [confirmStop, setConfirmStop] = useState(false);

  const data = queue.envelope?.data;
  const active = isQueueActive(data);
  const paused = isQueuePaused(data);
  const files = vods.envelope?.data?.files ?? [];
  const effectiveVariantMode: QueueVariantMode = pipelineMode === "raw_cuts_only" ? "original" : variantMode;
  const effectiveVariantCount = effectiveVariantMode === "custom" ? variantCount : 1;
  const parsedMaxClips = Math.max(0, Number.parseInt(maxClips || "0", 10) || 0);
  const draftConfig: QueueLaunchConfig = {
    run_mode: runMode,
    pipeline_mode: pipelineMode,
    variant_mode: effectiveVariantMode,
    variant_count: effectiveVariantCount,
    max_clips: parsedMaxClips,
    video_path: runMode === "single_video" ? videoPath : null
  };
  const displayConfig: QueueLaunchConfig = {
    ...draftConfig,
    max_clips: parsedMaxClips === 0 ? null : parsedMaxClips
  };
  const draftSummary = launchSummary(displayConfig);
  const summary = data?.launch_summary || launchSummary(data?.launch_config, draftSummary);
  const needsVod = runMode === "single_video";
  const canStart = !active && (!needsVod || Boolean(videoPath));
  const queueRows = data?.rows ?? [];
  const currentRun = pickCurrentRun(queueRows, active ? "running" : data?.queue_status);
  const activeStage = stageKeyForRun(currentRun);
  const activeStageMeta = operationStages.find((stage) => stage.key === activeStage) ?? {
    key: undefined,
    label: "Processing",
    icon: Activity
  };
  const ActiveStageIcon = activeStageMeta.icon;
  const currentProgress = clampProgress(currentRun?.progress);

  useEffect(() => {
    if (pipelineMode === "raw_cuts_only") {
      setVariantMode("original");
      setVariantCount(1);
    }
  }, [pipelineMode]);

  useEffect(() => {
    if (runMode === "single_video" && !videoPath && files.length > 0) {
      setVideoPath(files[0].path);
    }
  }, [runMode, videoPath, files]);

  function refreshAll() {
    queue.refresh();
    onQueueRefresh?.();
  }

  function startQueue() {
    void submitMutation(
      () => sendJson<ControlJob>("POST", "/api/control/queue", {
        action: "start",
        launch_config: draftConfig
      }),
      setMessage,
      refreshJobQueries,
      [refreshAll]
    );
  }

  function stopQueue() {
    void submitMutation(
      () => sendJson<ControlJob>("POST", "/api/control/queue", { action: "stop" }),
      setMessage,
      refreshJobQueries,
      [refreshAll]
    );
  }

  function pauseQueue() {
    void submitMutation(
      () => sendJson<ControlJob>("POST", "/api/control/queue", { action: "pause" }),
      setMessage,
      refreshJobQueries,
      [refreshAll]
    );
  }

  function continueQueue() {
    void submitMutation(
      () => sendJson<ControlJob>("POST", "/api/control/queue", { action: "continue" }),
      setMessage,
      refreshJobQueries,
      [refreshAll]
    );
  }

  if (surface === "operations") {
    return (
      <>
        <article className="operation-panel current-run-panel">
          {currentRun ? (
            <>
              <div className="current-run-head">
                <div><h2>Active run</h2><p>{summary}</p></div>
                <Badge value={currentRun.status} kind={runStatusKind(currentRun.status)} />
              </div>
              <div className="current-run-main">
                <h3>{currentRun.video_name}</h3>
                <div className="current-stage">
                  <ActiveStageIcon size={20} aria-hidden="true" />
                  <span>
                    <strong>{activeStageMeta.label}</strong>
                    {!activeStage && currentRun.current_step && <small>{currentRun.current_step}</small>}
                  </span>
                </div>
                <div className="run-progress-line" aria-label={`Current run progress ${currentProgress}%`}>
                  <div className="run-progress-track">
                    <span style={{ width: `${currentProgress}%` }} />
                  </div>
                  <strong>{currentProgress}%</strong>
                </div>
              </div>

              <div className="current-run-meta">
                <div className="run-meta-item">
                  <span>Clips generated</span>
                  <strong>{numberText(currentRun.clips_generated)}</strong>
                </div>
                <div className="run-meta-item wide">
                  <span>Current step</span>
                  <strong>{currentRun.current_step || activeStageMeta.label}</strong>
                </div>
                <div className="run-meta-item">
                  <span>Elapsed</span>
                  <strong>{currentRun.duration || "-"}</strong>
                </div>
              </div>

              <div className="current-run-footer">
                <div className={`run-attention ${currentRun.attention ? "warn" : "good"}`}>
                  {currentRun.attention ? <AlertTriangle size={20} aria-hidden="true" /> : <CheckCircle2 size={20} aria-hidden="true" />}
                  <span>{currentRun.attention || "No issues"}</span>
                </div>
                <div className="run-control-actions">
                  <button className="secondary-button" disabled={!active} onClick={paused ? continueQueue : pauseQueue}>
                    {paused ? <Play size={16} aria-hidden="true" /> : <Clock size={16} aria-hidden="true" />}
                    {paused ? "Continue" : "Pause"}
                  </button>
                  <button className="danger-button" disabled={!active} onClick={() => setConfirmStop(true)}>
                    <Square size={16} aria-hidden="true" />
                    Stop run
                  </button>
                </div>
              </div>
            </>
          ) : (
            <div className="operation-empty current-run-empty">
              <span className="operation-empty-mark">
                <Play size={28} aria-hidden="true" />
              </span>
              <strong>No active run</strong>
              <span>You don’t have any runs in progress.</span>
              <button className="primary-button" onClick={() => document.getElementById("new-run-form")?.scrollIntoView({ behavior: "smooth" })}>Start a run</button>
            </div>
          )}
        </article>

        {!active && <article className="operation-panel next-run-panel" id="new-run-form">
          <div className="next-run-head">
            <h2>Configure a new run</h2>
          </div>
          <div className="run-config-surface">
          <div className="run-form-rows">
            <label className="run-form-row">
              <span>Source</span>
              <select value={runMode} onChange={(event) => setRunMode(event.target.value as QueueRunMode)}>
                {runModeOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
              <small>{runMode === "folder_repeat" ? "Process all folders and keep watching for new work." : runMode === "folder_once" ? "Process the source folder once." : "Process one selected video."}</small>
            </label>
            {needsVod && (
              <label className="run-form-row">
                <span>Video</span>
                <select value={videoPath} onChange={(event) => setVideoPath(event.target.value)}>
                  <option value="">Select video</option>
                  {files.map((file) => <option value={file.path} key={file.path}>{file.name}</option>)}
                </select>
                <small>Select a video from the configured source folder.</small>
              </label>
            )}
            <label className="run-form-row">
              <span>Pipeline</span>
              <select value={pipelineMode} onChange={(event) => setPipelineMode(event.target.value as QueuePipelineMode)}>
                {pipelineModeOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
              <small>{pipelineMode === "full" ? "Include transcription, curation, scanning, and rendering." : "Run only the selected pipeline stages."}</small>
            </label>
            <label className="run-form-row">
              <span>Variants</span>
              <select value={effectiveVariantMode} disabled={pipelineMode === "raw_cuts_only"} onChange={(event) => setVariantMode(event.target.value as QueueVariantMode)}>
                {variantModeOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
              <small>{pipelineMode === "raw_cuts_only" ? "Raw cuts use the original output only." : "Choose which configured variants to render."}</small>
            </label>
            <label className="run-form-row">
              <span>Max clips</span>
              <input type="number" min={0} value={maxClips} onChange={(event) => setMaxClips(event.target.value)} />
              <small>0 means unlimited.</small>
            </label>
            {effectiveVariantMode === "custom" && (
              <label className="run-form-row">
                <span>Variant count</span>
                <select value={variantCount} onChange={(event) => setVariantCount(Number.parseInt(event.target.value, 10))}>
                  {[1, 2, 3, 4, 5, 6].map((count) => <option value={count} key={count}>{count}</option>)}
                </select>
                <small>Render this many variants for each clip.</small>
              </label>
            )}
          </div>
          <details className="advanced-options">
            <summary>Advanced options</summary>
            <div className="advanced-options-body"><span>Run setup</span><strong>{draftSummary}</strong></div>
          </details>
          <div className="next-run-action-row">
            <button className="secondary-button" type="button" onClick={() => { setRunMode("folder_repeat"); setPipelineMode("full"); setVariantMode("all"); setVariantCount(2); setMaxClips("0"); setVideoPath(""); }}>Reset</button>
            <button className="primary-button" disabled={!canStart} onClick={startQueue}><Play size={16} aria-hidden="true" />Start run</button>
          </div>
          </div>
          {needsVod && vods.error && <StateBlock kind="bad" detail={vods.error} />}
          {needsVod && !vods.loading && files.length === 0 && <StateBlock kind="warn" detail="No supported video files found." />}
          <ActionNotice message={message} />
        </article>}
        <ConfirmDialog
          open={confirmStop}
          title="Stop the production queue?"
          detail="The current queue pass will be stopped. Completed outputs are preserved, but in-progress work may need to be resumed or rerun."
          confirmLabel="Stop queue"
          danger
          onConfirm={stopQueue}
          onClose={() => setConfirmStop(false)}
        />
      </>
    );
  }

  return (
    <article className="panel action-panel launcher-panel">
      <div className="panel-head">
        <div>
          <h2>Run launcher</h2>
          <p>{active ? summary : "Choose the next queue run."}</p>
        </div>
        <Badge value={data?.control_status || data?.queue_status || "idle"} />
      </div>

      {active ? (
        <div className="launcher-running">
          <div className="launcher-summary">
            <Badge value={data?.queue_status ?? "running"} />
            <strong>{summary}</strong>
          </div>
          <div className="run-control-actions">
            <button className="secondary-button" onClick={paused ? continueQueue : pauseQueue}>
              {paused ? <Play size={16} aria-hidden="true" /> : <Clock size={16} aria-hidden="true" />}
              {paused ? "Continue" : "Pause"}
            </button>
            <button className="danger-button" onClick={() => setConfirmStop(true)}>
              <Square size={16} aria-hidden="true" />
              Stop Queue
            </button>
          </div>
        </div>
      ) : (
        <>
          <div className="launcher-grid">
            <SegmentedControl label="Run mode" value={runMode} options={runModeOptions} onChange={setRunMode} />
            {needsVod && (
              <FilterField label="VOD">
                <select value={videoPath} onChange={(event) => setVideoPath(event.target.value)}>
                  <option value="">Select VOD</option>
                  {files.map((file) => (
                    <option value={file.path} key={file.path}>{file.name}</option>
                  ))}
                </select>
              </FilterField>
            )}
            <SegmentedControl label="Pipeline" value={pipelineMode} options={pipelineModeOptions} onChange={setPipelineMode} />
            <SegmentedControl
              label="Variants"
              value={effectiveVariantMode}
              options={variantModeOptions}
              onChange={setVariantMode}
              disabled={pipelineMode === "raw_cuts_only"}
            />
            {effectiveVariantMode === "custom" && (
              <FilterField label="Variant count">
                <select value={variantCount} onChange={(event) => setVariantCount(Number.parseInt(event.target.value, 10))}>
                  {[1, 2, 3, 4, 5, 6].map((count) => (
                    <option value={count} key={count}>{count}</option>
                  ))}
                </select>
              </FilterField>
            )}
            <FilterField label="Max clips">
              <input type="number" min={0} value={maxClips} onChange={(event) => setMaxClips(event.target.value)} />
            </FilterField>
          </div>
          <div className="launcher-footer">
            <div className="launcher-summary">
              <Badge value={parsedMaxClips === 0 ? "Unlimited" : `${parsedMaxClips} max`} kind="info" />
              <strong>{launchSummary(draftConfig)}</strong>
            </div>
            <button className="primary-button" disabled={!canStart} onClick={startQueue}>
              <Play size={16} aria-hidden="true" />
              Start Queue
            </button>
          </div>
          {needsVod && vods.error && <StateBlock kind="bad" detail={vods.error} />}
          {needsVod && !vods.loading && files.length === 0 && <StateBlock kind="warn" detail="No supported VOD files found." />}
        </>
      )}
      <ActionNotice message={message} />
      <ConfirmDialog
        open={confirmStop}
        title="Stop the production queue?"
        detail="The current queue pass will be stopped. Completed outputs are preserved, but in-progress work may need to be resumed or rerun."
        confirmLabel="Stop queue"
        danger
        onConfirm={stopQueue}
        onClose={() => setConfirmStop(false)}
      />
    </article>
  );
}

function OperationsPage() {
  const dashboard = useApiQuery<DashboardSummary>("/api/dashboard", dashboardPollingInterval, true);
  const jobsQuery = useApiQuery<ControlJobPage>("/api/control/jobs?limit=12", jobPollingInterval, true);
  const summary = dashboard.envelope?.data;
  const jobs = jobsQuery.envelope?.data;
  const rows = summary?.rows ?? [];
  const queuedVideos = rows.filter(isQueuedVideo);
  const queuedRows = queuedVideos.slice(0, 6);
  const queuedVideoCount = queuedVideos.length;
  const queueProgress = averageProgress(queuedVideos);
  const recentJobs = jobs?.jobs ?? [];
  const currentRun = pickCurrentRun(rows, summary?.queue_status);
  const activeStage = stageKeyForRun(currentRun);
  const activeProduction = Boolean(currentRun) || isQueueStatusActive(summary?.queue_status);

  return (
    <section className="page-stack operations-page">
      {dashboard.loading && <SkeletonLines count={4} />}
      {dashboard.error && <StateBlock kind="bad" title="Dashboard read failed" detail={dashboard.error} />}
      <StateBlock kind="warn" warnings={dashboard.envelope?.warnings} />
      <RunLauncher onQueueRefresh={dashboard.refresh} surface="operations" />

      {activeProduction ? <>
      <article className="operation-panel pipeline-progress-panel">
        <h2>Pipeline Progress</h2>
        <div className="operation-stage-grid">
          {operationStages.map((stage) => {
            const state = operationStageState(stage.key, activeStage, currentRun, summary);
            const progress = operationStageProgress(state, stage.key, activeStage, currentRun);
            const StageIcon = stage.icon;
            const status = state === "done" ? "Done" : state === "running" ? "Running" : "Waiting";
            return (
              <div className={`operation-stage-card ${state}`} key={stage.key}>
                <div className="operation-stage-head">
                  <span className="operation-stage-icon">
                    <StageIcon size={26} aria-hidden="true" />
                  </span>
                  <strong>{stage.label}</strong>
                  <span className="stage-status-pill">{status}</span>
                </div>
                <div className="stage-progress-track" aria-label={`${stage.label} ${status}`}>
                  <span style={{ width: `${progress}%` }} />
                </div>
              </div>
            );
          })}
        </div>
      </article>

      <div className="operation-bottom-grid">
        <article className="operation-panel queue-progress-panel">
          <div className="queue-progress-head">
            <h2>Queue Progress</h2>
            <Badge value={`${numberText(queuedVideoCount)} video${queuedVideoCount === 1 ? "" : "s"}`} kind={queuedVideoCount > 0 ? "warn" : "neutral"} />
          </div>
          <div className="queue-progress-summary">
            <div>
              <strong>{numberText(queuedVideoCount)}</strong>
              <span>Videos in queue</span>
            </div>
            <div className="queue-progress-overall">
              <span>Average progress</span>
              <strong>{queueProgress}%</strong>
              <div className="queue-progress-track" aria-label={`Queue average progress ${queueProgress}%`}>
                <span style={{ width: `${queueProgress}%` }} />
              </div>
            </div>
          </div>
          <div className="queue-progress-list">
            {queuedRows.map((row) => {
              const progress = clampProgress(row.progress);
              return (
                <Link className="queue-progress-row" to="/production/queue" key={`${row.video_name}-${row.started_at}`}>
                  <Video size={16} aria-hidden="true" />
                  <div>
                    <strong>{row.video_name}</strong>
                    <span>{row.current_step || row.status}</span>
                    <div className="queue-progress-track" aria-label={`${row.video_name} progress ${progress}%`}>
                      <span style={{ width: `${progress}%` }} />
                    </div>
                  </div>
                  <strong className="queue-progress-percent">{progress}%</strong>
                  <Badge value={row.status} />
                </Link>
              );
            })}
            {queuedRows.length === 0 && (
              <div className="operation-empty">
                <span className="operation-empty-mark">
                  <ListChecks size={30} aria-hidden="true" />
                </span>
                <span>No videos in queue</span>
              </div>
            )}
          </div>
        </article>

        <article className="operation-panel activity-panel">
          <h2>Recent Activity</h2>
          <div className="operation-activity-list">
            {recentJobs.slice(0, 6).map((job) => (
              <Link className="activity-row" to={`/activity/jobs?job=${encodeURIComponent(job.job_id)}`} key={job.job_id}>
                <span className="activity-icon">
                  <Activity size={17} aria-hidden="true" />
                </span>
                <div>
                  <strong>{operationLabel(job.operation)}</strong>
                  <span>{job.error || job.conflict_key || job.actor}</span>
                </div>
                <Badge value={job.status} />
                <time>{displayTime(job.updated_at)}</time>
              </Link>
            ))}
            {recentJobs.length === 0 && (
              <div className="operation-empty">
                <span className="operation-empty-mark">
                  <Activity size={30} aria-hidden="true" />
                </span>
                <span>No recent activity</span>
              </div>
            )}
          </div>
        </article>
      </div>
      </> : (
        <section className="home-section production-recent-runs">
          <h2>Recent runs</h2>
          {rows.length ? <div className="table-wrap"><table><thead><tr><th>Run</th><th>Status</th><th>Started</th><th>Duration</th></tr></thead><tbody>
            {[...rows].sort((left, right) => runTime(right) - runTime(left)).slice(0, 5).map((row) => (
              <tr key={row.run_id || `${row.video_name}-${row.started_at}-${row.attempt_number}`}><td><div className="strong">{row.video_name}</div><div className="muted">Run {row.attempt_number}</div></td><td><Badge value={row.status} /></td><td>{displayTime(row.started_at)}</td><td>{row.duration || "—"}</td></tr>
            ))}
          </tbody></table></div> : <EmptyState icon={ListChecks} title="No runs yet" detail="Completed production runs will appear here." />}
        </section>
      )}
    </section>
  );
}

function QueuePage() {
  const [offset, setOffset] = useState(0);
  const queue = useApiQuery<QueueDetail>(`/api/queue${query({ view: "attempts", limit: 100, offset })}`, (data) => isQueueActive(data) ? 2_000 : 15_000, true);
  const [selected, setSelected] = useState<QueueRunRow | null>(null);
  const data = queue.envelope?.data;

  return (
    <section className="page-stack">
      <PageTitle title="Queue history" detail="Inspect active, waiting, completed, and failed production runs." onRefresh={queue.refresh} />
      {queue.loading && <SkeletonLines count={4} />}
      {queue.error && <StateBlock kind="bad" title="Queue read failed" detail={queue.error} />}
      <StateBlock kind="warn" warnings={queue.envelope?.warnings} />
      <QueueTable rows={data?.rows ?? []} selected={selected} setSelected={setSelected} />
      {(data?.total ?? 0) > 0 && (
        <div className="queue-pagination" aria-label="Queue history pagination">
          <span>{numberText((data?.offset ?? 0) + 1)}–{numberText(Math.min((data?.offset ?? 0) + (data?.rows.length ?? 0), data?.total ?? 0))} of {numberText(data?.total ?? 0)} run attempts</span>
          <div>
            <button className="secondary-button" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous</button>
            <button className="secondary-button" disabled={offset + 100 >= (data?.total ?? 0)} onClick={() => setOffset(offset + 100)}>Next</button>
          </div>
        </div>
      )}
      <Drawer
        open={Boolean(selected)}
        title={selected?.video_name ?? "Queue run"}
        detail={selected?.current_step}
        onClose={() => setSelected(null)}
      >
        {selected && (
          <div className="detail-list">
            <DetailItem label="Status" value={<Badge value={selected.status} />} />
            <DetailItem label="Attempt" value={`Run ${selected.attempt_number}`} />
            <DetailItem label="Progress" value={<Progress value={selected.progress} />} />
            <DetailItem label="Video path" value={selected.video_path || "-"} />
            <DetailItem label="Output dir" value={selected.output_dir || "-"} />
            <DetailItem label="Working dir" value={selected.working_dir || "-"} />
            <DetailItem label="Started" value={selected.started_at || "-"} />
            <DetailItem label="Completed" value={selected.completed_at || "-"} />
            <DetailItem label="Attention" value={queueAttentionText(selected)} />
          </div>
        )}
      </Drawer>
    </section>
  );
}

function DetailItem({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="detail-item">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function DashboardPage() {
  const dashboard = useApiQuery<DashboardSummary>("/api/dashboard", dashboardPollingInterval, true);
  const overview = useApiQuery<OverviewData>("/api/overview", 30_000, true);
  const summary = dashboard.envelope?.data;
  const overviewData = overview.envelope?.data;
  const exportOverview = buildExportOverview(overviewData?.export);
  const rows = summary?.rows ?? [];
  const recentRuns = [...rows].sort((left, right) => runTime(right) - runTime(left)).slice(0, 6);
  const reviewNeeded = overviewData?.review_needed_count ?? 0;
  const stoppedRuns = Object.entries(summary?.status_counts ?? {}).reduce(
    (count, [status, total]) => ["stopped", "failed", "interrupted"].some((value) => status.toLowerCase().includes(value)) ? count + total : count,
    0,
  );
  const waitingDeliveries = exportOverview.available ? exportOverview.pending : 0;
  const hasAttention = reviewNeeded > 0 || waitingDeliveries > 0 || stoppedRuns > 0;
  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  return (
    <section className="page-stack overview-page">
      {(dashboard.loading || overview.loading) && <SkeletonLines count={4} />}
      {dashboard.error && <StateBlock kind="bad" title="Dashboard read failed" detail={dashboard.error} />}
      {overview.error && <StateBlock kind="bad" title="Overview read failed" detail={overview.error} />}
      <StateBlock kind="warn" warnings={[...(dashboard.envelope?.warnings ?? []), ...(overview.envelope?.warnings ?? [])]} />

      <section className="home-hero">
        <div><h2>{greeting}</h2><p>You’re all set to create and process clips.</p></div>
        <Link className="primary-button" to="/production/live"><Play size={16} aria-hidden="true" />Start production</Link>
      </section>

      <section className="home-section">
        <h2>Needs attention</h2>
        {hasAttention ? <div className="attention-list">
          {reviewNeeded > 0 && <Link to="/review/clips"><Video size={18} aria-hidden="true" /><span><strong>{numberText(reviewNeeded)} clips</strong> need review</span><ChevronRight size={17} /></Link>}
          {waitingDeliveries > 0 && <Link to="/deliveries"><PackageCheck size={18} aria-hidden="true" /><span><strong>{numberText(waitingDeliveries)} deliveries</strong> are waiting</span><ChevronRight size={17} /></Link>}
          {stoppedRuns > 0 && <Link to="/activity/jobs?status=failed"><Activity size={18} aria-hidden="true" /><span><strong>{numberText(stoppedRuns)} runs</strong> are stopped or failed</span><ChevronRight size={17} /></Link>}
        </div> : <p className="attention-clear">You’re all caught up.</p>}
      </section>

      <section className="home-section recent-runs-section">
        <h2>Recent runs</h2>
        {recentRuns.length ? (
          <div className="table-wrap"><table><thead><tr><th>Run</th><th>Clips</th><th>Status</th><th>Started</th><th>Duration</th></tr></thead><tbody>
            {recentRuns.map((row) => <tr key={row.run_id || `${row.video_name}-${row.started_at}-${row.attempt_number}`}><td><div className="strong">{row.video_name}</div><div className="muted">Run {row.attempt_number}</div></td><td>{numberText(row.clips_generated)}</td><td><Badge value={row.status} /></td><td>{displayTime(row.started_at)}</td><td>{row.duration || "—"}</td></tr>)}
          </tbody></table></div>
        ) : <EmptyState icon={Video} title="No recent runs" detail="Your completed and active production runs will appear here." />}
      </section>
    </section>
  );
}

function OverviewStatLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="overview-stat-line">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ClipReviewPage({ active }: { active: boolean }) {
  const limit = 50;
  const initialScore = new URLSearchParams(window.location.search).get("score") ?? "";
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [product, setProduct] = useState("");
  const [sort, setSort] = useState("scored_at");
  const [direction, setDirection] = useState<SortDirection>("desc");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string>(initialScore);
  const [outputDir, setOutputDir] = useState("");
  const [forceRescore, setForceRescore] = useState(false);
  const [rescoreConfirmOpen, setRescoreConfirmOpen] = useState(false);
  const [message, setMessage] = useState<ActionMessage>();
  const debouncedSearch = useDebouncedValue(search, 300);

  useEffect(() => {
    setOffset(0);
  }, [search, status, product, sort, direction]);

  const path = `/api/scores${query({ limit, offset, search: debouncedSearch, status, product, sort, direction })}`;
  const scores = useApiQuery<ScoreIndexPage>(path, 10_000, active);
  const detail = useApiQuery<ScoreDetail>(
    `/api/scores/${encodeURIComponent(selected)}`,
    false,
    active && Boolean(selected)
  );
  const page = scores.envelope?.data;
  const rows = page?.rows ?? [];
  const productOptions = page?.filter_options.product ?? uniqueOptions(rows.map((row) => row.product));
  const pageLimit = page?.limit ?? limit;
  const pageOffset = page?.offset ?? offset;
  const detailOpen = Boolean(selected);

  useEffect(() => {
    const row = rows.find((item) => item.score_key === selected);
    if (row?.clip_path) {
      setOutputDir(parentDir(row.clip_path));
    }
  }, [rows, selected]);

  function submitRescore() {
    void submitMutation(
      () => sendJson<ControlJob>("POST", "/api/operations/rescore", {
        output_dir: outputDir,
        force_rescore: forceRescore
      }),
      setMessage,
      refreshJobQueries,
      [scores.refresh, detail.refresh]
    );
  }

  return (
    <section className={`page-stack clip-review-page ${detailOpen ? "has-detail" : ""}`}>
      {scores.loading && <SkeletonLines count={5} />}
      {scores.error && <StateBlock kind="bad" title="Score read failed" detail={scores.error} />}
      <StateBlock kind="warn" warnings={scores.envelope?.warnings} />
      <ScoreTable
        rows={rows}
        selected={selected}
        setSelected={setSelected}
        total={page?.total ?? 0}
        limit={pageLimit}
        offset={pageOffset}
        search={search}
        setSearch={setSearch}
        status={status}
        setStatus={setStatus}
        product={product}
        setProduct={setProduct}
        productOptions={productOptions}
        sort={sort}
        setSort={setSort}
        direction={direction}
        setDirection={setDirection}
        setOffset={setOffset}
        onRefresh={scores.refresh}
      />

      <details className="review-rescore-panel">
        <summary>
          <span><SlidersHorizontal size={16} aria-hidden="true" /> Advanced actions</span>
          <small>Rescore output folders and access operator tools.</small>
        </summary>
        <div className="review-rescore-content">
          <div className="advanced-action-intro">
            <strong>Rescore an output directory</strong>
            <p>Selecting a clip prefills its output folder.</p>
          </div>
          <div className="action-row">
            <FilterField label="Output directory">
              <input value={outputDir} onChange={(event) => setOutputDir(event.target.value)} placeholder="D:\output_clips\vod__run_001" />
            </FilterField>
            <label className="confirm-check">
              <input type="checkbox" checked={forceRescore} onChange={(event) => setForceRescore(event.target.checked)} />
              Force rescore
            </label>
            <button className="primary-button" disabled={!outputDir} onClick={() => setRescoreConfirmOpen(true)}>
              <RotateCcw size={16} aria-hidden="true" />
              Create rescore job
            </button>
          </div>
          <ActionNotice message={message} />
        </div>
      </details>

      <ScoreDetailPanel
        detail={detail.envelope?.data}
        loading={detail.loading && Boolean(selected)}
        error={detail.error}
        selectedKey={selected}
        onClose={() => setSelected("")}
        onSelect={setSelected}
      />
      <ConfirmDialog
        open={rescoreConfirmOpen}
        title="Rescore this output?"
        detail={`${forceRescore ? "Force a fresh score for" : "Score"} ${outputDir}. The job will remain visible in Activity.`}
        confirmLabel="Create rescore job"
        onClose={() => setRescoreConfirmOpen(false)}
        onConfirm={() => {
          setRescoreConfirmOpen(false);
          submitRescore();
        }}
      />
    </section>
  );
}

function ScoreTable({
  rows,
  selected,
  setSelected,
  total,
  limit,
  offset,
  search,
  setSearch,
  status,
  setStatus,
  product,
  setProduct,
  productOptions,
  sort,
  setSort,
  direction,
  setDirection,
  setOffset,
  onRefresh
}: {
  rows: ScoreRow[];
  selected: string;
  setSelected: (key: string) => void;
  total: number;
  limit: number;
  offset: number;
  search: string;
  setSearch: (value: string) => void;
  status: string;
  setStatus: (value: string) => void;
  product: string;
  setProduct: (value: string) => void;
  productOptions: string[];
  sort: string;
  setSort: (value: string) => void;
  direction: SortDirection;
  setDirection: (value: SortDirection) => void;
  setOffset: (value: number) => void;
  onRefresh: () => void;
}) {
  const groups = useMemo(() => groupedScoreRows(rows), [rows]);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());

  useEffect(() => {
    const visibleKeys = new Set(groups.map((group) => group.key));
    setExpandedGroups((current) => {
      const next = new Set(Array.from(current).filter((key) => visibleKeys.has(key)));
      return next.size === current.size ? current : next;
    });
  }, [groups]);

  function toggleGroup(key: string) {
    setExpandedGroups((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  return (
    <article className="panel review-score-panel">
      <div className="panel-head review-panel-head">
        <div>
          <h2>Clips</h2>
          <p>{numberText(total)} scored {total === 1 ? "item" : "items"}</p>
        </div>
      </div>
      <div className="index-toolbar review-index-toolbar">
        <SearchInput className="review-index-search" ariaLabel="Search clips" value={search} onChange={setSearch} placeholder="Search clips..." />
        <IndexSelect label="Status" icon={ShieldCheck} value={status} onChange={setStatus}>
          <option value="">All statuses</option>
          {["Strong", "Okay", "Review", "Blocked"].map((item) => <option value={item} key={item}>{item}</option>)}
        </IndexSelect>
        <IndexSelect label="Product" icon={PackageCheck} value={product} onChange={setProduct}>
          <option value="">All products</option>
          {productOptions.map((item) => <option value={item} key={item}>{item}</option>)}
        </IndexSelect>
        <IndexSelect label="Sort" icon={SlidersHorizontal} value={sort} onChange={setSort}>
          <option value="scored_at">Scored time</option>
          <option value="total_score">Total score</option>
          <option value="quality_score">Quality score</option>
          <option value="similarity_score">Similarity score</option>
          <option value="product">Product</option>
          <option value="status">Status</option>
        </IndexSelect>
        <SortDirectionButton direction={direction} onToggle={() => setDirection(direction === "desc" ? "asc" : "desc")} />
        <button className="icon-button toolbar-icon-button" type="button" onClick={onRefresh} aria-label="Refresh clips" title="Refresh clips">
          <RefreshCw size={16} aria-hidden="true" />
        </button>
      </div>
      {rows.length === 0 ? (
        <EmptyState icon={Video} title="No clips available yet" detail="Clips will appear after they are rendered and scored." />
      ) : (
        <div className="table-wrap review-score-table-wrap">
          <table className="review-score-table">
            <thead>
              <tr>
                <th>Clip</th>
                <th>Status</th>
                <th>Product</th>
                <th>Score</th>
                <th>Quality</th>
                <th>Flags</th>
                <th>Scored</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((group) => {
                const row = group.main;
                const hasSelectedVariant = group.variants.some((variant) => variant.score_key === selected);
                const isOpen = expandedGroups.has(group.key) || hasSelectedVariant;
                return (
                  <Fragment key={group.key}>
                    <tr
                      className={`review-main-row ${selected === row.score_key ? "selected-row" : ""} ${hasSelectedVariant ? "review-group-active" : ""}`}
                      onClick={() => setSelected(row.score_key)}
                    >
                      <td className="review-clip-cell">
                        <button
                          className={`review-expand-button ${isOpen ? "open" : ""}`}
                          type="button"
                          disabled={group.variants.length === 0}
                          onClick={(event) => {
                            event.stopPropagation();
                            toggleGroup(group.key);
                          }}
                          aria-label={`${isOpen ? "Collapse" : "Expand"} variants for ${row.clip_id || row.source_video}`}
                          aria-expanded={isOpen}
                        >
                          {isOpen ? <ChevronDown size={15} aria-hidden="true" /> : <ChevronRight size={15} aria-hidden="true" />}
                        </button>
                        <span className={`review-row-selector ${selected === row.score_key ? "selected" : ""}`} aria-hidden="true">
                          {selected === row.score_key && <CheckCircle2 size={13} />}
                        </span>
                        <div>
                          <div className="strong">{row.clip_id || row.source_video}</div>
                          <div className="muted">
                            {group.hasBase ? "base" : "variant match"}
                            {group.variants.length > 0 ? ` - ${numberText(group.variants.length)} variant${group.variants.length === 1 ? "" : "s"}` : ""}
                          </div>
                        </div>
                      </td>
                      <td><ReviewStatusBadge value={row.status} /></td>
                      <td className="review-product-cell">{row.product || "-"}</td>
                      <td className="review-number-cell">{scoreText(row.total_score)}</td>
                      <td className="review-number-cell">{scoreText(row.quality_score)}</td>
                      <td className="review-number-cell">{row.flag_count}</td>
                      <td className="muted">{row.scored_at ? displayTime(row.scored_at) : "-"}</td>
                    </tr>
                    {isOpen && group.variants.map((variant) => (
                      <tr
                        className={`review-variant-table-row ${selected === variant.score_key ? "selected-row" : ""}`}
                        key={variant.score_key}
                        onClick={() => setSelected(variant.score_key)}
                      >
                        <td className="review-clip-cell review-variant-clip-cell">
                          <span className={`review-row-selector ${selected === variant.score_key ? "selected" : ""}`} aria-hidden="true">
                            {selected === variant.score_key && <CheckCircle2 size={13} />}
                          </span>
                          <div>
                            <div className="strong">{variant.clip_id || variant.source_video}</div>
                            <div className="muted">variant</div>
                          </div>
                        </td>
                        <td><ReviewStatusBadge value={variant.status} /></td>
                        <td className="review-product-cell">{variant.product || "-"}</td>
                        <td className="review-number-cell">{scoreText(variant.total_score)}</td>
                        <td className="review-number-cell">{scoreText(variant.quality_score)}</td>
                        <td className="review-number-cell">{variant.flag_count}</td>
                        <td className="muted">{variant.scored_at ? displayTime(variant.scored_at) : "-"}</td>
                      </tr>
                    ))}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <ReviewPagination total={total} limit={limit} offset={offset} count={rows.length} setOffset={setOffset} />
    </article>
  );
}

function ReviewStatusBadge({ value, compact = false }: { value: string; compact?: boolean }) {
  const kind = statusClass(value);
  const Icon = kind === "good" ? CheckCircle2 : kind === "warn" || kind === "bad" ? AlertTriangle : undefined;
  return (
    <span className={`review-status-badge ${kind} ${compact ? "compact" : ""}`}>
      {Icon ? <Icon size={compact ? 12 : 14} aria-hidden="true" /> : <span className="status-dot" aria-hidden="true" />}
      {value || "Unknown"}
    </span>
  );
}

function ReviewMetricTile({
  label,
  value,
  unit,
  icon: Icon
}: {
  label: string;
  value: string;
  unit?: string;
  icon?: LucideIcon;
}) {
  return (
    <div className="review-metric-tile">
      <div className="review-metric-label">
        <span>{label}</span>
        {Icon && <Icon size={14} aria-hidden="true" />}
      </div>
      <strong>
        {value}
        {unit && <small>{unit}</small>}
      </strong>
    </div>
  );
}

function ReviewPagination({
  total,
  limit,
  offset,
  count,
  setOffset
}: {
  total: number;
  limit: number;
  offset: number;
  count: number;
  setOffset: (offset: number) => void;
}) {
  const currentPage = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  const firstPage = Math.max(1, Math.min(currentPage - 2, Math.max(1, pages - 4)));
  const visiblePages = Array.from({ length: Math.min(5, pages) }, (_, index) => firstPage + index).filter((page) => page <= pages);

  return (
    <div className="review-pagination" aria-label="Clips pagination">
      <div className="review-page-size">
        <span>{numberText(limit)} per page</span>
      </div>
      <div className="review-page-controls">
        <button className="icon-button small" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - limit))} aria-label="Previous score page">
          <ChevronLeft size={16} aria-hidden="true" />
        </button>
        {visiblePages.map((page) => (
          <button
            className={`review-page-button ${page === currentPage ? "active" : ""}`}
            key={page}
            onClick={() => setOffset((page - 1) * limit)}
            aria-current={page === currentPage ? "page" : undefined}
          >
            {page}
          </button>
        ))}
        <button className="icon-button small" disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)} aria-label="Next score page">
          <ChevronRight size={16} aria-hidden="true" />
        </button>
      </div>
      <span className="review-page-range">{reviewRangeText(total, limit, offset, count)}</span>
    </div>
  );
}

function ScoreArtifactPreview({ row }: { row: ScoreRow }) {
  const artifact = row.artifact;
  if (artifact?.exists && artifact.kind === "video") {
    return <video className="review-preview-media" controls preload="metadata" src={artifact.url} />;
  }
  if (artifact?.exists && artifact.kind === "image") {
    return <img className="review-preview-media" src={artifact.url} alt={row.clip_id || "Selected clip preview"} />;
  }
  return (
    <div className="review-preview-placeholder">
      <Video size={26} aria-hidden="true" />
      <strong>Preview unavailable</strong>
      <span>{row.output_file || row.clip_path || "No artifact path"}</span>
    </div>
  );
}

function VariantPreviewThumb({ row }: { row: ScoreRow }) {
  const artifact = row.artifact;
  if (artifact?.exists && artifact.kind === "video") {
    return <video className="review-variant-thumb" muted preload="metadata" src={artifact.url} />;
  }
  if (artifact?.exists && artifact.kind === "image") {
    return <img className="review-variant-thumb" src={artifact.url} alt="" />;
  }
  return (
    <span className="review-variant-thumb placeholder">
      <Video size={15} aria-hidden="true" />
    </span>
  );
}

function ScoreDetailPanel({
  detail,
  loading,
  error,
  selectedKey,
  onClose,
  onSelect
}: {
  detail?: ScoreDetail;
  loading: boolean;
  error?: string;
  selectedKey: string;
  onClose: () => void;
  onSelect: (key: string) => void;
}) {
  const selected = detail?.selected;
  const variants = detail?.variants ?? [];
  const open = Boolean(selectedKey) || loading || Boolean(error);
  if (!open) {
    return null;
  }
  return (
    <aside className="clip-review-detail" aria-label="Selected clip">
      <div className="clip-review-detail-head">
        <div>
          <h2>Selected clip</h2>
          {selected?.source_video && <p>{selected.source_video}</p>}
        </div>
        <button className="icon-button small" onClick={onClose} aria-label="Close selected clip">
          <X size={17} aria-hidden="true" />
        </button>
      </div>
      <div className="clip-review-detail-body">
        {loading && <SkeletonLines count={5} />}
        {error && <StateBlock kind="bad" title="Clip detail failed" detail={error} />}
        {selected && (
          <>
            <div className="selected-clip-head">
              <div>
                <h3>{selected.clip_id || selected.source_video}</h3>
                <p>{selected.row_type} clip</p>
                <span>{selected.scored_at ? `Scored ${displayTime(selected.scored_at)}` : "Not scored yet"}</span>
              </div>
              <ReviewStatusBadge value={selected.status} />
            </div>

            <ScoreArtifactPreview row={selected} />

            <div className="review-metric-grid">
              <ReviewMetricTile label="Total" value={scoreText(selected.total_score)} unit="/10" icon={BadgeCheck} />
              <ReviewMetricTile label="Content" value={scoreText(selected.content_score)} unit="/10" icon={FileText} />
              <ReviewMetricTile label="Hook" value={scoreText(selected.hook_score)} unit="/10" icon={Zap} />
              <ReviewMetricTile label="Host focus" value={scoreText(selected.host_focus_score)} unit="/10" icon={Eye} />
              <ReviewMetricTile label="Quality" value={scoreText(selected.quality_score)} unit="/10" icon={Eye} />
              <ReviewMetricTile label="Engagement" value={scoreText(selected.engagement_score)} unit="/10" icon={TrendingUp} />
              <ReviewMetricTile label="Similarity" value={scoreText(selected.similarity_score)} unit="/10" icon={Layers3} />
              <ReviewMetricTile label="Flags" value={numberText(selected.flag_count)} icon={AlertTriangle} />
            </div>

            <section className="review-detail-section">
              <div className="review-section-head">
                <h3>Flags</h3>
                <span className="review-count-pill">{numberText(selected.flag_count)}</span>
              </div>
              <div className="review-flag-list">
                {selected.flags.length ? selected.flags.map((flag) => (
                  <div className={`review-flag-item ${statusClass(selected.flag_severity)}`} key={flag}>
                    <AlertTriangle size={17} aria-hidden="true" />
                    <div>
                      <strong>{reviewFlagLabel(flag)}</strong>
                      <span>{selected.flag_severity && selected.flag_severity !== "none" ? `${selected.flag_severity} severity` : "Quality signal"}</span>
                    </div>
                  </div>
                )) : <span className="muted">No flags on this clip.</span>}
              </div>
            </section>

            <section className="review-detail-section">
              <div className="review-section-head">
                <h3>Variants</h3>
                <span className="review-count-pill">{numberText(variants.length)}</span>
              </div>
              <div className="review-variant-list">
                {variants.slice(0, 5).map((variant) => (
                  <button
                    className={`review-variant-row ${variant.score_key === selected.score_key ? "is-selected" : ""}`}
                    key={variant.score_key}
                    onClick={() => onSelect(variant.score_key)}
                    type="button"
                  >
                    <span className={`review-row-selector ${variant.score_key === selected.score_key ? "selected" : ""}`} aria-hidden="true">
                      {variant.score_key === selected.score_key && <CheckCircle2 size={12} />}
                    </span>
                    <VariantPreviewThumb row={variant} />
                    <span className="review-variant-copy">
                      <strong>{variant.clip_id || variant.row_type}</strong>
                      <span>{variant.row_type}</span>
                    </span>
                    <ReviewStatusBadge value={variant.status} compact />
                    <strong className="review-variant-score">{scoreText(variant.total_score)}</strong>
                  </button>
                ))}
                {variants.length > 5 && <span className="review-more-row">+{numberText(variants.length - 5)} more variants</span>}
                {variants.length === 0 && <span className="muted">No sibling variants found.</span>}
              </div>
            </section>

            <section className="review-detail-section">
              <h3>Summary</h3>
              <p className="review-summary">{selected.summary || "No score summary was provided for this clip."}</p>
            </section>

            <details className="review-raw-details">
              <summary>Raw summary</summary>
              <pre className="json-panel">{compactJson(detail?.raw)}</pre>
            </details>
          </>
        )}
      </div>
    </aside>
  );
}

function CompliancePage({ active }: { active: boolean }) {
  const limit = 50;
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [product, setProduct] = useState("");
  const [sort, setSort] = useState("checked_at");
  const [direction, setDirection] = useState<SortDirection>("desc");
  const [offset, setOffset] = useState(0);
  const [selectedOutput, setSelectedOutput] = useState("");
  const [scanOutputDir, setScanOutputDir] = useState("");
  const [force, setForce] = useState(true);
  const [scanConfirmOpen, setScanConfirmOpen] = useState(false);
  const [message, setMessage] = useState<ActionMessage>();
  const debouncedSearch = useDebouncedValue(search, 300);

  useEffect(() => {
    setOffset(0);
  }, [search, status, product, sort, direction]);

  const path = `/api/compliance${query({ limit, offset, search: debouncedSearch, status, product, sort, direction })}`;
  const compliance = useApiQuery<ComplianceIndexPage>(path, 10_000, active);
  const detailPath = `/api/compliance/detail${query({ output_dir: selectedOutput })}`;
  const detail = useApiQuery<ComplianceIndexPage>(detailPath, false, active && Boolean(selectedOutput));
  const data = compliance.envelope?.data;
  const rows = data?.rows ?? [];
  const detailData = detail.envelope?.data;
  const visibleViolations = detailData?.violations.length ? detailData.violations : data?.violations ?? [];
  const summary = detailData?.summary ?? data?.summary ?? {};
  const productOptions = data?.filter_options.product ?? uniqueOptions(rows.map((row) => row.product));

  function submitScan() {
    void submitMutation(
      () => sendJson<ControlJob>("POST", "/api/operations/compliance-scan", {
        output_dir: scanOutputDir,
        force
      }),
      setMessage,
      refreshJobQueries,
      [compliance.refresh, detail.refresh]
    );
  }

  return (
    <section className="page-stack">
      <PageTitle title="Compliance" detail="Review policy status, inspect violations, and launch scans." onRefresh={compliance.refresh} />
      <div className="metric-grid compact">
        <MetricCard label="Scanned" value={numberText(summary.scanned)} hint="Filtered rows" icon={ClipboardCheck} />
        <MetricCard label="Passed" value={numberText(summary.passed)} hint="Policy clear" icon={CheckCircle2} />
        <MetricCard label="Blocked" value={numberText(summary.blocked)} hint="Needs action" icon={AlertTriangle} />
        <MetricCard label="Violations" value={numberText(summary.violation_count)} hint="Visible manifest count" icon={ShieldCheck} />
      </div>

      <article className="panel action-panel">
        <div className="panel-head">
          <div>
            <h2>Compliance scan</h2>
            <p>Select a row to fill the output directory, or paste a target under the output root.</p>
          </div>
          <Badge value={force ? "Force scan" : "Incremental"} kind={force ? "warn" : "info"} />
        </div>
        <div className="action-row">
          <FilterField label="Output directory">
            <input value={scanOutputDir} onChange={(event) => setScanOutputDir(event.target.value)} placeholder="D:\output_clips\vod__run_001" />
          </FilterField>
          <button className="secondary-button" disabled={!selectedOutput} onClick={() => setScanOutputDir(selectedOutput)}>
            Use selected output
          </button>
          <label className="confirm-check">
            <input type="checkbox" checked={force} onChange={(event) => setForce(event.target.checked)} />
            Force scan
          </label>
          <button className="primary-button" disabled={!scanOutputDir} onClick={() => setScanConfirmOpen(true)}>
            <ShieldCheck size={16} aria-hidden="true" />
            Create scan job
          </button>
        </div>
        <ActionNotice message={message} />
      </article>

      <div className="index-toolbar">
        <SearchInput value={search} onChange={setSearch} placeholder="Search clips, products, sources..." />
        <FilterField label="Status">
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">All statuses</option>
            {["passed", "blocked", "auto_fixed"].map((item) => <option value={item} key={item}>{item}</option>)}
          </select>
        </FilterField>
        <FilterField label="Product">
          <select value={product} onChange={(event) => setProduct(event.target.value)}>
            <option value="">All products</option>
            {productOptions.map((item) => <option value={item} key={item}>{item}</option>)}
          </select>
        </FilterField>
        <FilterField label="Sort">
          <select value={sort} onChange={(event) => setSort(event.target.value)}>
            <option value="checked_at">Checked time</option>
            <option value="violation_count">Violations</option>
            <option value="source_video">Source video</option>
            <option value="product">Product</option>
            <option value="status">Status</option>
          </select>
        </FilterField>
        <FilterField label="Direction">
          <select value={direction} onChange={(event) => setDirection(event.target.value as SortDirection)}>
            <option value="desc">Descending</option>
            <option value="asc">Ascending</option>
          </select>
        </FilterField>
      </div>

      {compliance.loading && <SkeletonLines count={5} />}
      {compliance.error && <StateBlock kind="bad" title="Compliance read failed" detail={compliance.error} />}
      <StateBlock kind="warn" warnings={compliance.envelope?.warnings} />
      <ComplianceTable rows={rows} selectedOutput={selectedOutput} setSelectedOutput={setSelectedOutput} />
      <Pagination total={data?.total ?? 0} limit={limit} offset={offset} setOffset={setOffset} />
      <ViolationPanel violations={visibleViolations} loading={detail.loading && Boolean(selectedOutput)} error={detail.error} />
      <ConfirmDialog
        open={scanConfirmOpen}
        title="Start compliance scan?"
        detail={`${force ? "Force a fresh scan for" : "Scan new or changed results in"} ${scanOutputDir}.`}
        confirmLabel="Create scan job"
        onClose={() => setScanConfirmOpen(false)}
        onConfirm={() => {
          setScanConfirmOpen(false);
          submitScan();
        }}
      />
    </section>
  );
}

function ComplianceTable({
  rows,
  selectedOutput,
  setSelectedOutput
}: {
  rows: ComplianceRow[];
  selectedOutput: string;
  setSelectedOutput: (value: string) => void;
}) {
  if (rows.length === 0) {
    return <EmptyState icon={ShieldCheck} title="No compliance rows" detail="Compliance results will appear after scans run." />;
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Clip</th>
            <th>Status</th>
            <th>Product</th>
            <th>Violations</th>
            <th>Checked</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              className={selectedOutput === row.output_dir ? "selected-row" : ""}
              key={`${row.output_dir}-${row.clip_id}-${row.checked_at}`}
              onClick={() => setSelectedOutput(row.output_dir)}
            >
              <td>
                <div className="strong">{row.clip_id || row.source_video}</div>
                <div className="muted">{row.summary || row.source_video}</div>
              </td>
              <td><Badge value={row.blocked ? "Blocked" : row.auto_fixed ? "Auto fixed" : row.passed ? "Passed" : "Unknown"} /></td>
              <td>{row.product}</td>
              <td>{row.violation_count}</td>
              <td>{row.checked_at || "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ViolationPanel({
  violations,
  loading,
  error
}: {
  violations: ComplianceViolationRow[];
  loading: boolean;
  error?: string;
}) {
  return (
    <article className="panel">
      <div className="panel-head">
        <div>
          <h2>Violation review</h2>
          <p>Severity, source field, original text, and suggested replacement.</p>
        </div>
      </div>
      {loading && <SkeletonLines count={4} />}
      {error && <StateBlock kind="bad" title="Violation detail failed" detail={error} />}
      {violations.length === 0 ? (
        <EmptyState icon={CheckCircle2} title="No visible violations" detail="Select another output directory or run a fresh scan." />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Clip</th>
                <th>Severity</th>
                <th>Type</th>
                <th>Original</th>
                <th>Suggested</th>
              </tr>
            </thead>
            <tbody>
              {violations.map((row, index) => (
                <tr key={`${row.compliance_file}-${row.clip_id}-${row.field}-${index}`}>
                  <td>
                    <div className="strong">{row.clip_id || row.source_video}</div>
                    <div className="muted">{row.field}</div>
                  </td>
                  <td><Badge value={row.severity || "Review"} /></td>
                  <td>
                    <div>{row.violation_type || "-"}</div>
                    {(row.start != null || row.end != null) && <div className="muted">Position {row.start ?? "?"}-{row.end ?? "?"}</div>}
                  </td>
                  <td className="wide-cell">{row.original_text || "-"}</td>
                  <td className="wide-cell">{row.suggested_replacement || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </article>
  );
}

function ExportsPage() {
  const [outputRoot, setOutputRoot] = useState("");
  const [batchSize, setBatchSize] = useState("");
  const [dryRun, setDryRun] = useState(true);
  const [packagingConfirmOpen, setPackagingConfirmOpen] = useState(false);
  const [selectedAssignmentId, setSelectedAssignmentId] = useState("");
  const [message, setMessage] = useState<ActionMessage>();
  const overview = useApiQuery<OverviewData>("/api/overview", 30_000, true);
  const whatsappDelivery = useApiQuery<WhatsAppDeliveryStatus>(
    "/api/whatsapp-delivery/status",
    30_000,
    true
  );
  const exportHistory = useApiQuery<ControlJobPage>("/api/control/jobs?limit=100&operation=export_batches", jobPollingInterval, true);
  const exportJobs = exportHistory.envelope?.data.jobs ?? [];
  const assignments = whatsappDelivery.envelope?.data.assignments ?? [];
  const selectedAssignment = assignments.find((item) => item.affiliate_assignment_id === selectedAssignmentId) ?? assignments[0];
  const exportOverview = buildExportOverview(overview.envelope?.data.export);
  const statusLabel = exportOverview.available
    ? reviewFlagLabel(exportOverview.status || (exportOverview.dryRun ? "preflight" : "completed"))
    : "Awaiting reconciliation";

  function submitExport() {
    void submitMutation(
      () => sendJson<ControlJob>("POST", "/api/operations/export-batches", {
        output_root: outputRoot || null,
        batch_size: batchSize ? Number(batchSize) : null,
        dry_run: dryRun
      }),
      setMessage,
      refreshJobQueries,
      [overview.refresh, exportHistory.refresh]
    );
  }

  return (
    <section className="page-stack">
      <PageTitle
        title="Deliveries"
        detail="Monitor automatic export batching and reconcile only when attention is required."
        onRefresh={() => {
          void overview.refresh();
          void exportHistory.refresh();
          void whatsappDelivery.refresh();
        }}
      />
      {overview.error && <StateBlock kind="bad" title="Automatic packaging status failed" detail={overview.error} />}
      {exportHistory.error && <StateBlock kind="bad" title="Delivery history failed" detail={exportHistory.error} />}
      {whatsappDelivery.error && <StateBlock kind="bad" title="WhatsApp delivery state failed" detail={whatsappDelivery.error} />}
      {whatsappDelivery.envelope?.data.cutover && !whatsappDelivery.envelope.data.cutover.claims_enabled && (
        <StateBlock
          kind="warn"
          title="Direct PC delivery is locked"
          detail={whatsappDelivery.envelope.data.cutover.blocking_reason || "Complete the legacy workflow cutover before claiming batches."}
        />
      )}
      <section className="delivery-summary" aria-label="Delivery status summary">
        <div><Clock size={19} aria-hidden="true" /><span>Ready<strong>{numberText(whatsappDelivery.envelope?.data.counts.ready_batches ?? 0)}</strong><small>Ready for assignment</small></span></div>
        <div><CheckCircle2 size={19} aria-hidden="true" /><span>Sent<strong>{numberText(whatsappDelivery.envelope?.data.counts.sent ?? 0)}</strong><small>Delivered successfully</small></span></div>
        <div><X size={19} aria-hidden="true" /><span>Failed<strong>{numberText(whatsappDelivery.envelope?.data.counts.delivery_failed ?? 0)}</strong><small>Delivery failed</small></span></div>
      </section>

      <section className={`delivery-workspace ${assignments.length ? "has-detail" : ""}`}>
        <article className="panel delivery-list-panel">
          <div className="panel-head"><div><h2>Deliveries</h2><p>{numberText(assignments.length)} canonical WhatsApp batches</p></div></div>
          {assignments.length ? <div className="table-wrap"><table><thead><tr><th>Folder</th><th>Recipient</th><th>Channel</th><th>Status</th><th>Sent at</th></tr></thead><tbody>
            {assignments.map((assignment) => <tr className={selectedAssignment?.affiliate_assignment_id === assignment.affiliate_assignment_id ? "selected-row" : ""} key={assignment.affiliate_assignment_id} onClick={() => setSelectedAssignmentId(assignment.affiliate_assignment_id)}>
              <td className="strong">Batch {assignment.batch_number}</td><td>{assignment.affiliate_name || assignment.affiliate_identifier}</td><td>WhatsApp</td><td><Badge value={assignment.delivery_status} /></td><td>{assignment.sent_at ? displayTime(assignment.sent_at) : "—"}</td>
            </tr>)}
          </tbody></table></div> : <EmptyState icon={PackageCheck} title="No deliveries yet" detail="Assigned delivery batches will appear here." />}
        </article>
        {assignments.length > 0 && <aside className="panel delivery-detail-panel">
          <div className="panel-head"><div><h2>Delivery details</h2><p>{selectedAssignment ? `Batch ${selectedAssignment.batch_number}` : "Select a delivery"}</p></div></div>
          {selectedAssignment ? <div className="detail-list">
            <DetailItem label="Folder" value={selectedAssignment.canonical_folder_path} />
            <DetailItem label="Recipient" value={selectedAssignment.affiliate_name || selectedAssignment.affiliate_identifier} />
            <DetailItem label="Identifier" value={selectedAssignment.affiliate_identifier} />
            <DetailItem label="Channel" value="WhatsApp" />
            <DetailItem label="Status" value={<Badge value={selectedAssignment.delivery_status} />} />
            <DetailItem label="Assigned" value={displayTime(selectedAssignment.assigned_at)} />
            <DetailItem label="Sent" value={selectedAssignment.sent_at ? displayTime(selectedAssignment.sent_at) : "—"} />
            {selectedAssignment.delivery_error && <StateBlock kind="bad" title="Delivery error" detail={selectedAssignment.delivery_error} />}
          </div> : <p className="muted-copy">Select a delivery to inspect its current state.</p>}
        </aside>}
      </section>
      <article className="panel delivery-status-panel">
        <div className="panel-head">
          <div>
            <h2>Automatic export batching</h2>
            <p>The pipeline packages actionable clips automatically after each completed run.</p>
          </div>
          <Badge
            value={statusLabel}
            kind={!exportOverview.available ? "neutral" : exportOverview.errorCount > 0 ? "bad" : exportOverview.pending > 0 ? "warn" : "good"}
          />
        </div>
        {exportOverview.available ? <div className="overview-export-stats delivery-status-stats">
          <OverviewStatLine label="Actionable at last pass" value={numberText(exportOverview.actionable)} />
          <OverviewStatLine label="Moved last pass" value={numberText(exportOverview.packagedLastRun)} />
          <OverviewStatLine label="Remaining now" value={numberText(exportOverview.pending)} />
          <OverviewStatLine label="Cumulative assignments" value={numberText(exportOverview.packagedTotal)} />
          <OverviewStatLine label="Last update" value={exportOverview.updatedAt ? new Date(exportOverview.updatedAt).toLocaleString() : "Unknown"} />
        </div> : <div className="delivery-empty-history">
          <strong>No export batching history yet.</strong>
          <p>The next automatic packaging pass will create the first operational snapshot.</p>
        </div>}
      </article>
      <details className="panel delivery-recovery-panel">
        <summary>Recovery &amp; Reconciliation</summary>
        <div className="delivery-recovery-content">
          <p className="muted-copy">Use these controls only to inspect or retry packaging after an automatic pass reports pending clips or errors.</p>
          <div className="action-row delivery-recovery-actions">
          <label className="confirm-check">
            <input type="checkbox" checked={dryRun} onChange={(event) => setDryRun(event.target.checked)} />
            Dry run
          </label>
          <button
            className="primary-button"
            onClick={() => dryRun ? submitExport() : setPackagingConfirmOpen(true)}
          >
            <RotateCcw size={16} aria-hidden="true" />
            {dryRun ? "Run reconciliation preflight" : "Retry packaging"}
          </button>
        </div>
          <details className="delivery-advanced-options">
            <summary>Advanced overrides</summary>
            <div className="action-row">
              <FilterField label="Output root">
                <input value={outputRoot} onChange={(event) => setOutputRoot(event.target.value)} placeholder="configured output root" />
              </FilterField>
              <FilterField label="Batch size">
                <input value={batchSize} onChange={(event) => setBatchSize(event.target.value)} placeholder="configured default" inputMode="numeric" />
              </FilterField>
            </div>
          </details>
        <ActionNotice message={message} />
        </div>
      </details>
      <ConfirmDialog
        open={packagingConfirmOpen}
        title="Retry export packaging?"
        detail={`Reconcile actionable clips from ${outputRoot || "the configured output root"}${batchSize ? ` using batch size ${batchSize}` : ""}. Automatic packaging remains the normal workflow.`}
        confirmLabel="Retry packaging"
        danger
        onClose={() => setPackagingConfirmOpen(false)}
        onConfirm={() => {
          setPackagingConfirmOpen(false);
          submitExport();
        }}
      />
      <article className="panel">
        <div className="panel-head">
          <div>
            <h2>Recovery history</h2>
            <p>Manual preflights and retry results from the control job ledger.</p>
          </div>
        </div>
        {exportJobs.length === 0 ? (
          <EmptyState icon={RotateCcw} title="No recovery jobs yet" detail="Automatic packaging has not needed a manual preflight or retry." />
        ) : (
          <JobTable rows={exportJobs} selected="" setSelected={() => undefined} compact />
        )}
      </article>
    </section>
  );
}

function TrendsPage({ active }: { active: boolean }) {
  const [country, setCountry] = useState("ID");
  const [windowRange, setWindowRange] = useState("1DAY");
  const [category, setCategory] = useState("BEAUTY_AND_PERSONAL_CARE");
  const path = `/api/trends?country_code=${encodeURIComponent(country)}&date_range=${encodeURIComponent(windowRange)}&category_name=${encodeURIComponent(category)}`;
  const downloadJobs = useApiQuery<ControlJobPage>("/api/control/jobs?limit=10&operation=trend_download", jobPollingInterval, active);
  const activeDownloadJob = (downloadJobs.envelope?.data.jobs ?? []).find((job) => ["queued", "running"].includes(job.status));
  const trends = useApiQuery<TrendPageData>(path, activeDownloadJob ? 2_000 : 15_000, active);
  const media = useApiQuery<TrendMediaFiles>("/api/trends/media-files", activeDownloadJob ? 3_000 : 30_000, active);
  const data = trends.envelope?.data;
  const [selectedHashtag, setSelectedHashtag] = useState<string>("");
  const [selectedVideo, setSelectedVideo] = useState<string>("");
  const [selectedMedia, setSelectedMedia] = useState<string>("");
  const [analysisSelection, setAnalysisSelection] = useState<string[]>([]);
  const [message, setMessage] = useState<ActionMessage>();
  const [downloadConfirmOpen, setDownloadConfirmOpen] = useState(false);
  const [rightsConfirmed, setRightsConfirmed] = useState(false);

  const videoCounts = useMemo(() => trendVideoCountsByHashtag(data?.videos ?? []), [data?.videos]);
  const visibleHashtags = useMemo(() => displayedTrendHashtags(data?.hashtags ?? []), [data?.hashtags]);
  const rankedFileCount = data?.videos.length ?? 0;
  const completedDownloadCount = useMemo(() => completedTrendDownloadCount(data?.videos ?? []), [data?.videos]);
  const downloadDisabledReason = trendDownloadDisabledReason(Boolean(data?.snapshot), data?.configuration, Boolean(activeDownloadJob));
  const visibleVideos = useMemo(
    () => trendVideosForHashtag(data?.videos ?? [], selectedHashtag),
    [data?.videos, selectedHashtag]
  );
  const activeHashtag = visibleHashtags.find((hashtag) => hashtag.hashtag_id === selectedHashtag);
  const activeVideoDiagnostics = data?.video_diagnostics?.find((item) => item.hashtag_id === selectedHashtag);
  const videoShortageMessage = trendVideoShortageMessage(activeVideoDiagnostics, visibleVideos.length);
  const activeVideo = visibleVideos.find((video) => video.video_id === selectedVideo) ?? visibleVideos[0];

  useEffect(() => {
    if (!data) return;
    if (!selectedHashtag || !visibleHashtags.some((hashtag) => hashtag.hashtag_id === selectedHashtag)) {
      setSelectedHashtag(defaultTrendHashtagId(visibleHashtags, data.videos));
      setSelectedVideo("");
    }
  }, [data, selectedHashtag, visibleHashtags]);

  useEffect(() => {
    if (!data?.snapshot) return;
    console.debug("[trends] Current Trending Hashtags render", {
      snapshotId: data.snapshot.snapshot_id,
      receivedFromBackend: data.hashtags.length,
      rendered: visibleHashtags.length,
      displayLimit: TREND_HASHTAG_DISPLAY_LIMIT
    });
  }, [data?.snapshot?.snapshot_id, data?.hashtags.length, visibleHashtags.length]);

  useEffect(() => {
    if (activeVideo && activeVideo.video_id !== selectedVideo) {
      setSelectedVideo(activeVideo.video_id);
    } else if (!activeVideo && selectedVideo) {
      setSelectedVideo("");
    }
  }, [activeVideo, selectedVideo]);

  useEffect(() => {
    if (!selectedMedia && !activeVideo?.media_status && activeVideo?.downloaded_relative_path) {
      setSelectedMedia(activeVideo.downloaded_relative_path);
    }
  }, [activeVideo?.video_id, activeVideo?.downloaded_relative_path, selectedMedia]);

  function selectHashtag(hashtagId: string) {
    const firstVideo = trendVideosForHashtag(data?.videos ?? [], hashtagId)[0];
    setSelectedHashtag(hashtagId);
    setSelectedVideo(firstVideo?.video_id ?? "");
    setSelectedMedia("");
  }

  function submitRefresh() {
    void submitMutation(
      () => sendJson<ControlJob>("POST", "/api/operations/trend-refresh", {
        country_code: country,
        date_range: windowRange,
        category_name: category,
        top_hashtag_limit: TREND_HASHTAG_DISPLAY_LIMIT
      }),
      setMessage,
      refreshJobQueries,
      [trends.refresh]
    );
  }

  async function connectTikTok() {
    try {
      const envelope = await sendJson<TikTokOAuthStart>("POST", "/api/integrations/tiktok/oauth/start", {});
      if (window.clipperDesktop?.openOAuth) {
        await window.clipperDesktop.openOAuth(envelope.data.authorization_url);
      } else {
        window.open(envelope.data.authorization_url, "_blank", "noopener,noreferrer");
      }
      setMessage({ kind: "info", text: "TikTok authorization opened in your browser. Complete it once, then return here." });
    } catch (caught: unknown) {
      setMessage({ kind: "bad", text: caught instanceof Error ? caught.message : String(caught) });
    }
  }

  async function selectTikTokAdvertiser(advertiserId: string) {
    try {
      await sendJson<Record<string, unknown>>("PUT", "/api/integrations/tiktok/oauth/advertiser", {
        advertiser_id: advertiserId
      });
      setMessage({ kind: "good", text: "TikTok advertiser selection saved." });
      trends.refresh();
    } catch (caught: unknown) {
      setMessage({ kind: "bad", text: caught instanceof Error ? caught.message : String(caught) });
    }
  }

  function submitDownload() {
    if (!data?.snapshot || !rightsConfirmed) return;
    void submitMutation(
      () => sendJson<ControlJob>("POST", "/api/operations/trend-download", {
        snapshot_id: data.snapshot?.snapshot_id,
        rights_confirmed: true,
        retry_failed: true
      }),
      setMessage,
      refreshJobQueries,
      [trends.refresh, media.refresh]
    );
    setRightsConfirmed(false);
  }

  async function linkMedia() {
    if (!activeVideo || !selectedMedia) return;
    try {
      await sendJson<Record<string, unknown>>("PUT", `/api/trends/videos/${encodeURIComponent(activeVideo.video_id)}/media`, {
        relative_path: selectedMedia
      });
      setMessage({ kind: "good", text: `Approved media linked to ${activeVideo.video_id}.` });
      trends.refresh();
    } catch (caught: unknown) {
      setMessage({ kind: "bad", text: caught instanceof Error ? caught.message : String(caught) });
    }
  }

  function toggleAnalysis(video: TrendVideo) {
    setAnalysisSelection((current) => toggleTrendVideoSelection(current, video));
  }

  function submitAnalysis() {
    if (!data?.snapshot) return;
    void submitMutation(
      () => sendJson<ControlJob>("POST", "/api/operations/trend-analysis", {
        snapshot_id: data.snapshot?.snapshot_id,
        video_ids: analysisSelection,
        force: false
      }),
      setMessage,
      refreshJobQueries,
      [trends.refresh]
    );
  }

  return (
    <section className="page-stack trend-page">
      <PageTitle title="TikTok Trends" detail="Discover TikTok-ranked videos, automatically link saved media, and derive editing recommendations." onRefresh={() => { trends.refresh(); media.refresh(); }}>
        {data?.configuration.oauth?.authorization_required && (
          <button
            className="secondary-button"
            onClick={() => void connectTikTok()}
            disabled={!data.configuration.oauth.app_configured || !data.configuration.oauth.redirect_configured || !data.configuration.oauth.callback_supported}
            title={data.configuration.oauth.configuration_error || "Authorize TikTok Business"}
          >
            <ShieldCheck size={16} aria-hidden="true" /> Connect TikTok
          </button>
        )}
        <button
          className="secondary-button"
          onClick={() => setDownloadConfirmOpen(true)}
          disabled={Boolean(downloadDisabledReason)}
          title={downloadDisabledReason ?? "Save every ranked hashtag video in this snapshot"}
        >
          <Download size={16} aria-hidden="true" /> {activeDownloadJob ? "Saving videos" : "Save all videos"}
        </button>
        <button className="primary-button" onClick={submitRefresh} disabled={!data?.configuration.access_configured}>
          <RefreshCw size={16} aria-hidden="true" /> Refresh Discovery
        </button>
      </PageTitle>
      <ActionNotice message={message} />
      {trends.loading && <SkeletonLines count={6} />}
      {trends.error && <StateBlock kind="bad" title="Trend read failed" detail={trends.error} />}
      <StateBlock kind="warn" warnings={[...(trends.envelope?.warnings ?? []), ...(data?.warnings ?? [])]} />
      {data && (
        <>
          <article className="panel trend-toolbar">
            <div className="filter-row">
              <FilterField label="Country"><input value={country} maxLength={2} onChange={(event) => setCountry(event.target.value.toUpperCase())} /></FilterField>
              <FilterField label="Window">
                <select value={windowRange} onChange={(event) => setWindowRange(event.target.value)}>
                  {['1DAY', '7DAY', '30DAY', '120DAY'].map((value) => <option key={value} value={value}>{value}</option>)}
                </select>
              </FilterField>
              <FilterField label="Category"><input value={category} onChange={(event) => setCategory(event.target.value.toUpperCase())} /></FilterField>
              {(data.configuration.oauth?.advertiser_ids?.length ?? 0) > 1 && (
                <FilterField label="TikTok advertiser">
                  <select
                    value={data.configuration.oauth?.selected_advertiser_id ?? ""}
                    onChange={(event) => void selectTikTokAdvertiser(event.target.value)}
                  >
                    <option value="">Select advertiser</option>
                    {data.configuration.oauth?.advertiser_ids?.map((advertiserId) => (
                      <option value={advertiserId} key={advertiserId}>{advertiserId}</option>
                    ))}
                  </select>
                </FilterField>
              )}
            </div>
            <div className="trend-config-grid">
              <DetailItem label="API access" value={data.configuration.access_configured ? "Configured" : "Missing token or advertiser"} />
              <DetailItem label="OAuth" value={data.configuration.oauth?.connected ? "Connected · long-term token" : "Authorization required"} />
              <DetailItem label="Media folder" value={data.configuration.media_dir} />
              <DetailItem label="Qwen-VL" value={data.configuration.qwen_enabled ? "Enabled" : "Deterministic fallback"} />
              <DetailItem label="yt-dlp" value={data.configuration.ytdlp_available ? data.configuration.ytdlp_version : "Unavailable"} />
              <DetailItem label="Free media storage" value={byteSizeText(data.configuration.media_free_bytes)} />
              <DetailItem label="Snapshot" value={data.snapshot ? displayTime(data.snapshot.retrieved_at) : "Not refreshed"} />
            </div>
          </article>

          {data.snapshot && data.download_summary && (
            <article className="panel trend-download-progress">
              <div className="panel-head">
                <div>
                  <h2>Local video saves</h2>
                  <p>{activeDownloadJob ? "Bulk download is active. Valid files become media-ready automatically." : "Successful downloads are automatically linked for analysis."}</p>
                </div>
                <Badge value={activeDownloadJob?.status ?? "idle"} kind={activeDownloadJob ? "info" : "neutral"} />
              </div>
              <progress
                max={Math.max(1, data.download_summary.targets)}
                value={Math.min(data.download_summary.targets, completedDownloadCount)}
              />
              <div className="trend-download-counts">
                <span>{numberText(data.download_summary.targets)} ranked files</span>
                <span>{numberText(data.download_summary.queued)} queued</span>
                <span>{numberText(data.download_summary.downloading)} downloading</span>
                <span>{numberText(data.download_summary.downloaded)} downloaded</span>
                <span>{numberText(data.download_summary.approved)} approved</span>
                <span>{numberText(data.download_summary.failed + data.download_summary.interrupted)} failed/interrupted</span>
              </div>
            </article>
          )}

          {!data.snapshot ? (
            <EmptyState icon={TrendingUp} title="No trend snapshot" detail="Configure TikTok access and refresh Discovery to create the first snapshot." />
          ) : (
            <div className="trend-layout">
              <article className="panel trend-hashtags">
                <div className="panel-head"><div><h2>Current Trending Hashtags</h2><p>Up to 30 skincare, beauty, cosmetics, personal-care, and recognized brand hashtags from {data.snapshot.country_code}.</p></div></div>
                <div className="trend-video-diagnostics" aria-label="Hashtag diagnostics">
                  <strong>Hashtag diagnostics</strong>
                  <span>{numberText(data.hashtag_diagnostics.total_candidates_returned)} TikTok candidates</span>
                  <span>{numberText(data.hashtag_diagnostics.accepted_topical)} topical accepted</span>
                  <span>{numberText(data.hashtag_diagnostics.accepted_brands)} brands accepted</span>
                  <span>{numberText(data.hashtag_diagnostics.excluded)} excluded</span>
                  <span>{numberText(data.hashtag_diagnostics.deduplicated)} deduplicated</span>
                  <span>{numberText(data.hashtag_diagnostics.stored)} stored</span>
                  <span>{numberText(data.hashtag_diagnostics.backend_returned)} returned by backend</span>
                  <span>{numberText(visibleHashtags.length)} rendered</span>
                  <span>{data.hashtag_diagnostics.source} · {data.hashtag_diagnostics.source_category}</span>
                </div>
                {visibleHashtags.length === 0 ? (
                  <EmptyState icon={TrendingUp} title="No relevant trending hashtags" detail="TikTok returned no qualifying topical or recognized beauty and personal-care brand hashtags for this selection. Unrelated trends are never used as filler." />
                ) : <div className="trend-scroll-list">
                  {visibleHashtags.map((hashtag, index) => (
                    <button
                      type="button"
                      className={`trend-hashtag-row ${selectedHashtag === hashtag.hashtag_id ? "active" : ""}`}
                      key={`${hashtag.hashtag_id}-${index}`}
                      aria-pressed={selectedHashtag === hashtag.hashtag_id}
                      onClick={() => selectHashtag(hashtag.hashtag_id)}
                    >
                      <strong>#{numberText(hashtag.original_rank ?? hashtag.rank_position)} {hashtag.hashtag_name}</strong>
                      <span>{numberText(hashtag.views)} views · {numberText(hashtag.posts)} posts</span>
                      <small>{hashtag.matched_brand ? `Brand: ${hashtag.matched_brand} · ` : ""}{videoCounts[hashtag.hashtag_id] ? `${numberText(videoCounts[hashtag.hashtag_id])} video references` : "No video references fetched"}</small>
                    </button>
                  ))}
                </div>}
              </article>

              <article className="panel trend-videos">
                <div className="panel-head"><div><h2>Ranked video references</h2><p>{activeHashtag ? `Showing ${numberText(visibleVideos.length)} playable videos for #${activeHashtag.hashtag_name}.` : "Select a ranked hashtag."} Final video-only ranks are used by the page, database, metadata, and saved filenames.</p></div><Badge value={`${analysisSelection.length}/20 selected`} kind="info" /></div>
                {activeVideoDiagnostics && (
                  <div className="trend-video-diagnostics" aria-label={`Video diagnostics for ${activeVideoDiagnostics.hashtag_name}`}>
                    <strong>#{activeVideoDiagnostics.hashtag_name} diagnostics</strong>
                    <span>{numberText(activeVideoDiagnostics.total_candidates_returned)} candidates returned</span>
                    <span>{numberText(activeVideoDiagnostics.video_posts_detected)} video posts detected</span>
                    <span>{numberText(activeVideoDiagnostics.image_carousel_posts_excluded)} image/carousel excluded</span>
                    <span>{numberText(activeVideoDiagnostics.unknown_posts_excluded)} unknown excluded</span>
                    <span>{numberText(activeVideoDiagnostics.unavailable_posts_excluded)} unavailable excluded</span>
                    <span>{numberText(activeVideoDiagnostics.valid_videos_stored)} valid stored</span>
                    <span>{numberText(activeVideoDiagnostics.sent_to_frontend)} sent to frontend</span>
                    <span>{numberText(visibleVideos.length)} rendered</span>
                    <span>Pagination {activeVideoDiagnostics.pagination_available ? "available" : "unavailable"}</span>
                  </div>
                )}
                {videoShortageMessage && <StateBlock kind="warn" detail={videoShortageMessage} />}
                {visibleVideos.length === 0 ? (
                  <EmptyState icon={Video} title="No playable videos available for this hashtag" detail="TikTok returned no valid playable video posts in this ranked pool. Image, carousel, unknown, private, deleted, and unplayable posts were excluded." />
                ) : (
                  <div className="trend-video-grid">
                    {visibleVideos.map((video, index) => {
                      const selectable = trendVideoIsSelectable(video);
                      const displayStatus = video.media_status || video.download_status || "discovered";
                      const displayKind = statusClass(displayStatus);
                      return (
                        <button type="button" className={`trend-video-row ${activeVideo?.video_id === video.video_id ? "active" : ""}`} key={`${video.video_id}-${index}`} onClick={() => { setSelectedVideo(video.video_id); setSelectedMedia(""); }}>
                          <input type="checkbox" checked={analysisSelection.includes(video.video_id)} disabled={!selectable} onClick={(event) => event.stopPropagation()} onChange={() => toggleAnalysis(video)} aria-label={`Select ${video.video_id} for analysis`} />
                          <span>
                            <strong>#{video.final_rank} · #{video.hashtag_name}</strong>
                            <small>Original TikTok rank #{video.original_provider_rank ?? video.provider_ordinal} · {video.media_type} · {video.video_id}</small>
                            <small>{video.classification_evidence}</small>
                          </span>
                          <Badge value={displayStatus} kind={displayKind} />
                        </button>
                      );
                    })}
                  </div>
                )}
                <button className="primary-button trend-analyze-button" disabled={analysisSelection.length < 5} onClick={submitAnalysis}>
                  <Zap size={16} aria-hidden="true" /> Analyze selected videos
                </button>
              </article>

              <article className="panel trend-inspector">
                <div className="panel-head"><div><h2>Reference and local media</h2><p>{activeVideo ? `#${activeVideo.hashtag_name}` : "Select a video"}</p></div></div>
                {activeVideo?.embed_url ? (
                  <iframe className="trend-embed" src={activeVideo.embed_url.replace("autoplay=1", "autoplay=0")} title={`TikTok ${activeVideo.video_id}`} allow="encrypted-media; picture-in-picture" />
                ) : <EmptyState icon={Video} title="No reference selected" detail="Choose a discovered video to inspect it." />}
                <FilterField label="Approved file in watched folder">
                  <select value={selectedMedia} onChange={(event) => setSelectedMedia(event.target.value)}>
                    <option value="">Choose a local media file</option>
                    {(media.envelope?.data.files ?? []).map((file) => <option key={file.relative_path} value={file.relative_path}>{file.relative_path}</option>)}
                  </select>
                </FilterField>
                <button className="secondary-button full-width" disabled={!activeVideo || !selectedMedia} onClick={() => void linkMedia()}>
                  <FolderOpen size={16} aria-hidden="true" /> Approve and link media
                </button>
                {activeVideo?.media_error && <StateBlock kind="bad" detail={activeVideo.media_error} />}
                {activeVideo?.download_error && <StateBlock kind="bad" detail={activeVideo.download_error} />}
              </article>
            </div>
          )}

          {data.latest_pattern && (
            <article className="panel trend-pattern">
              <div className="panel-head">
                <div><h2>Suggested Variation profile</h2><p>Read-only suggestion based on {numberText(data.latest_pattern.sample_count)} approved videos.</p></div>
                <Badge value="Not applied" kind="warn" />
              </div>
              <div className="trend-recommendation-grid">
                {trendRecommendationRows(data.latest_pattern).map(([field, recommendation]) => (
                  <div className={`trend-recommendation ${recommendation.applied_to_suggestion ? "accepted" : "retained"}`} key={field}>
                    <span>{reviewFlagLabel(field)}</span>
                    <strong>{String(recommendation.value)}</strong>
                    <small>{Math.round(recommendation.confidence * 100)}% confidence · {recommendation.support_count}/{recommendation.sample_count} support</small>
                    <Badge value={recommendation.applied_to_suggestion ? "Suggested" : "Baseline retained"} kind={recommendation.applied_to_suggestion ? "good" : "neutral"} />
                  </div>
                ))}
              </div>
            </article>
          )}
        </>
      )}
      <ConfirmDialog
        open={downloadConfirmOpen}
        title="Save all TikTok videos?"
        detail={`This explicit run targets ${numberText(rankedFileCount)} ranked hashtag files. Every valid saved or reused file will be linked automatically for analysis.`}
        confirmLabel="Save all videos"
        confirmDisabled={!rightsConfirmed}
        onClose={() => { setDownloadConfirmOpen(false); setRightsConfirmed(false); }}
        onConfirm={submitDownload}
      >
        <div className="trend-download-confirm">
          <span>Already downloaded: {numberText(data?.download_summary?.downloaded)}</span>
          <span>Already approved: {numberText(data?.download_summary?.approved)}</span>
          <span>Free storage: {byteSizeText(data?.configuration.media_free_bytes)}</span>
          <label>
            <input type="checkbox" checked={rightsConfirmed} onChange={(event) => setRightsConfirmed(event.target.checked)} />
            I confirm I have permission to download and store this content for internal analysis.
          </label>
        </div>
      </ConfirmDialog>
    </section>
  );
}

export function VariationsPage({ active }: { active: boolean }) {
  const variations = useApiQuery<VariationPageData>("/api/variations", 30_000, active);
  const productInformation = useApiQuery<ProductInformationStatus>("/api/product-information", 30_000, active);
  const data = variations.envelope?.data;
  const informationData = productInformation.envelope?.data;
  const normalizedServerProfile = useMemo(
    () => data?.profile ? normalizeUiProfile(data.profile) : null,
    [data?.profile]
  );
  const [draft, setDraft] = useState<VariationProfile | null>(null);
  const [draftBaseline, setDraftBaseline] = useState<VariationProfile | null>(null);
  const [selectedVariantIndex, setSelectedVariantIndex] = useState(0);
  const [message, setMessage] = useState<ActionMessage>();
  const [revisionConflict, setRevisionConflict] = useState("");
  const [presetName, setPresetName] = useState("");
  const [selectedPreset, setSelectedPreset] = useState("");
  const [presetFeedback, setPresetFeedback] = useState<PresetPanelFeedback>();
  const [presetLoadConfirmOpen, setPresetLoadConfirmOpen] = useState(false);
  const [previewProduct, setPreviewProduct] = useState("");
  const [previewInformationProduct, setPreviewInformationProduct] = useState("");
  const [busy, setBusy] = useState("");
  const [renderedPreview, setRenderedPreview] = useState<VariationPreviewResult>();
  const [renderedPreviewSignature, setRenderedPreviewSignature] = useState("");
  const [previewFeedback, setPreviewFeedback] = useState<VariantPreviewFeedback>();
  const [previewMediaError, setPreviewMediaError] = useState("");
  const previewRequestIdRef = useRef(0);
  const currentPreviewSignatureRef = useRef("");

  useEffect(() => {
    if (!normalizedServerProfile) {
      return;
    }
    if (!draft || !draftBaseline) {
      acceptServerProfile(normalizedServerProfile);
      return;
    }
    if (baselineMatchesServer(draftBaseline, normalizedServerProfile)) {
      return;
    }
    if (isDraftDirty(draft, draftBaseline)) {
      setRevisionConflict(
        "A newer variation profile is available. Your unsaved draft was preserved; applying it may require resolving the revision conflict."
      );
      return;
    }
    acceptServerProfile(normalizedServerProfile);
  }, [normalizedServerProfile?.revision]);

  useEffect(() => {
    const products = data?.product_broll?.products ?? [];
    if (products.length === 0) {
      return;
    }
    if (previewProduct && products.some((item) => item.product_key === previewProduct)) {
      return;
    }
    const firstWithPreview = products.find((item) => item.preview?.exists) ?? products[0];
    setPreviewProduct(firstWithPreview.product_key);
    invalidateRenderedPreview();
  }, [data?.product_broll?.root, data?.product_broll?.products.length, previewProduct]);

  useEffect(() => {
    const products = informationData?.products ?? [];
    if (products.length === 0) {
      return;
    }
    if (previewInformationProduct && products.some((item) => item.product_key === previewInformationProduct)) {
      return;
    }
    const firstWithFacts = products.find((item) => item.eligible_fact_count > 0) ?? products[0];
    setPreviewInformationProduct(firstWithFacts.product_key);
    invalidateRenderedPreview();
  }, [informationData?.revision, informationData?.products.length, previewInformationProduct]);

  const dirty = isDraftDirty(draft, draftBaseline);
  const visibleVariants = draft?.variants.slice(0, draft.variant_count) ?? [];
  const limits = data?.limits ?? { min_variants: 1, max_variants: 6 };
  const previewIndex = clampSelectedVariantIndex(selectedVariantIndex, visibleVariants.length);
  const previewVariant = visibleVariants[previewIndex];
  const currentPreviewSignature = draft
    ? createPreviewRequestSignature(draft, previewIndex, previewInformationProduct, previewProduct)
    : "";
  currentPreviewSignatureRef.current = currentPreviewSignature;
  const dynamicTextRoles = data?.dynamic_text_roles ?? ["ingredients", "benefits", "usage", "cta"];
  const featureFlags = data?.global_feature_flags ?? {
    sfx: true,
    bgm: true,
    before_after: true,
    broll_intro: true,
    transitional_hook: true,
    host_face_zoom: true
  };
  const commandStatus: VariantCommandStatus = busy === "save" ? "saving" : dirty ? "unsaved" : "saved";

  useEffect(() => {
    if (renderedPreview && shouldInvalidatePreview(renderedPreviewSignature, currentPreviewSignature)) {
      setRenderedPreview(undefined);
      setRenderedPreviewSignature("");
    }
  }, [currentPreviewSignature, renderedPreview, renderedPreviewSignature]);

  useEffect(() => {
    if (!dirty) {
      return;
    }
    const protectDraft = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", protectDraft);
    return () => window.removeEventListener("beforeunload", protectDraft);
  }, [dirty]);

  function invalidateRenderedPreview() {
    previewRequestIdRef.current += 1;
    setRenderedPreview(undefined);
    setRenderedPreviewSignature("");
    setPreviewFeedback(undefined);
    setPreviewMediaError("");
    setBusy((current) => current === "preview" ? "" : current);
  }

  function acceptServerProfile(profile: VariationProfile) {
    const accepted = copyVariationProfile(profile);
    setDraft(accepted);
    setDraftBaseline(copyVariationProfile(accepted));
    setSelectedVariantIndex((current) => clampSelectedVariantIndex(current, accepted.variant_count));
    setRevisionConflict("");
    invalidateRenderedPreview();
  }

  function updateDraft(next: VariationProfile) {
    setDraft(next);
    invalidateRenderedPreview();
  }

  function updateVariant(index: number, patch: Partial<VariationVariant>) {
    if (!draft) {
      return;
    }
    updateDraft(patchVariationVariant(draft, index, patch));
  }

  function updateDynamicTextRole(
    index: number,
    role: VariationVariant["dynamic_text_roles"][number],
    enabled: boolean
  ) {
    const current = visibleVariants[index];
    if (!current) {
      return;
    }
    const selected = new Set(current.dynamic_text_roles ?? []);
    if (enabled) {
      selected.add(role);
    } else {
      selected.delete(role);
    }
    updateVariant(index, {
      dynamic_text_roles: dynamicTextRoles.filter((item) => selected.has(item))
    });
  }

  function updateDynamicTextSetting(
    index: number,
    role: VariationVariant["dynamic_text_roles"][number],
    patch: Partial<VariationVariant["dynamic_text_settings"]["ingredients"]>
  ) {
    const current = visibleVariants[index];
    if (!current) {
      return;
    }
    updateVariant(index, {
      dynamic_text_settings: {
        ...current.dynamic_text_settings,
        [role]: { ...current.dynamic_text_settings[role], ...patch }
      }
    });
  }

  function applyTextStyle(index: number, textStyleId: VariationVariant["text_style_id"]) {
    const style = data?.text_styles.find((item) => item.id === textStyleId);
    updateVariant(index, {
      ...(style?.defaults ?? {}),
      text_style_id: textStyleId
    });
  }

  function selectPreviewVariant(index: number) {
    const nextIndex = clampSelectedVariantIndex(index, visibleVariants.length);
    setSelectedVariantIndex(nextIndex);
    invalidateRenderedPreview();
  }

  function updateSubtitleY(index: number, value: number) {
    const subtitle_y_frac = clampNumber(value, 0.08, 0.92);
    updateVariant(index, {
      subtitle_y_frac,
      subtitle_position: subtitlePositionFromY(subtitle_y_frac)
    });
  }

  function updateLetterboxEnabled(index: number, enabled: boolean) {
    const current = visibleVariants[index];
    if (!current) {
      return;
    }
    updateVariant(index, {
      letterbox_enabled: enabled,
      letterbox_top_frac: enabled && current.letterbox_top_frac <= 0 ? 0.2 : current.letterbox_top_frac,
      letterbox_bottom_frac: enabled && current.letterbox_bottom_frac <= 0 ? 0.2 : current.letterbox_bottom_frac
    });
  }

  function updateLetterboxHookEnabled(index: number, enabled: boolean) {
    const current = visibleVariants[index];
    if (!current) {
      return;
    }
    updateVariant(index, {
      letterbox_hook_enabled: enabled,
      letterbox_hook_font_id: current.letterbox_hook_font_id || current.font_id
    });
  }

  function updateVariantCount(value: number) {
    if (!draft) {
      return;
    }
    const next = resizeVariationProfile(
      draft,
      value,
      limits.min_variants,
      limits.max_variants,
      createUiVariant
    );
    updateDraft(next);
    setSelectedVariantIndex((current) => clampSelectedVariantIndex(current, next.variant_count));
  }

  async function saveProfile() {
    if (!draft || !draftBaseline) {
      return;
    }
    setBusy("save");
    try {
      const envelope = await sendJson<VariationPageData>("PUT", "/api/variations", {
        profile: draft,
        expected_revision: draftBaseline.revision
      });
      acceptServerProfile(normalizeUiProfile(envelope.data.profile));
      setMessage({ kind: "good", text: "Variation profile saved for future renders." });
      variations.refresh();
    } catch (caught) {
      const detail = caught instanceof Error ? caught.message : String(caught);
      if (/\b409\b|conflict/i.test(detail)) {
        setRevisionConflict(
          "The variation profile changed before your draft could be applied. Your draft is still intact; no server data was loaded over it."
        );
      }
      setMessage({ kind: "bad", text: detail });
    } finally {
      setBusy("");
    }
  }

  async function savePreset() {
    if (!draft || !presetName.trim()) {
      return;
    }
    setBusy("preset");
    try {
      await sendJson<Record<string, unknown>>("POST", "/api/variations/presets", {
        name: presetName,
        profile: draft
      });
      setPresetName("");
      setPresetFeedback({ kind: "success", text: "Preset saved." });
      setMessage({ kind: "good", text: "Preset saved." });
      variations.refresh();
    } catch (caught) {
      const detail = caught instanceof Error ? caught.message : String(caught);
      setPresetFeedback({ kind: "error", text: detail });
      setMessage({ kind: "bad", text: detail });
    } finally {
      setBusy("");
    }
  }

  function requestPresetLoad() {
    if (!selectedPreset) {
      return;
    }
    if (dirty) {
      setPresetLoadConfirmOpen(true);
      return;
    }
    void loadPreset();
  }

  async function loadPreset() {
    const presetId = selectedPreset;
    if (!presetId) {
      return;
    }
    setBusy("load");
    try {
      const envelope = await getJson<VariationProfile>(`/api/variations/presets/${encodeURIComponent(presetId)}`);
      const loadedDraft = normalizeUiProfile(envelope.data);
      setDraft(loadedDraft);
      setSelectedVariantIndex((current) => clampSelectedVariantIndex(current, loadedDraft.variant_count));
      invalidateRenderedPreview();
      setPresetFeedback({ kind: "info", text: "Preset loaded into the draft. Apply separately when ready." });
      setMessage({ kind: "info", text: "Preset loaded into the editor. Save to apply it." });
    } catch (caught) {
      const detail = caught instanceof Error ? caught.message : String(caught);
      setPresetFeedback({ kind: "error", text: detail });
      setMessage({ kind: "bad", text: detail });
    } finally {
      setBusy("");
    }
  }

  async function renderPreview() {
    if (!draft || !previewVariant) {
      return;
    }
    const requestId = previewRequestIdRef.current + 1;
    previewRequestIdRef.current = requestId;
    const requestSignature = createPreviewRequestSignature(
      draft,
      previewIndex,
      previewInformationProduct,
      previewProduct
    );
    setBusy("preview");
    setPreviewFeedback(undefined);
    setPreviewMediaError("");
    try {
      const envelope = await sendJson<VariationPreviewResult>("POST", "/api/variations/previews", {
        profile: draft,
        variant_index: previewIndex,
        product_key: previewInformationProduct
      });
      if (
        requestId !== previewRequestIdRef.current
        || requestSignature !== currentPreviewSignatureRef.current
      ) {
        return;
      }
      setRenderedPreview(envelope.data);
      setRenderedPreviewSignature(requestSignature);
      const hasRenderedPreview = envelope.data.previews.some((preview) => preview.exists);
      const feedback = {
        kind: hasRenderedPreview ? "success" : "warning",
        text: envelope.data.message || (hasRenderedPreview ? "Rendered preview ready." : "No rendered preview was produced.")
      } as VariantPreviewFeedback;
      setPreviewFeedback(feedback);
      setMessage({ kind: hasRenderedPreview ? "good" : "warn", text: feedback.text });
    } catch (caught) {
      if (
        requestId !== previewRequestIdRef.current
        || requestSignature !== currentPreviewSignatureRef.current
      ) {
        return;
      }
      const detail = caught instanceof Error ? caught.message : String(caught);
      setPreviewFeedback({ kind: "error", text: detail });
      setMessage({ kind: "bad", text: detail });
    } finally {
      if (requestId === previewRequestIdRef.current) {
        setBusy("");
      }
    }
  }

  async function rescanProductInformation() {
    setBusy("information");
    try {
      const envelope = await sendJson<ProductInformationStatus>("POST", "/api/product-information/rescan", {});
      setMessage({
        kind: envelope.data.warnings.length ? "warn" : "good",
        text: `Product information scanned: ${numberText(envelope.data.sources.length)} source file(s).`
      });
      productInformation.refresh();
    } catch (caught) {
      setMessage({ kind: "bad", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="page-stack variants-page">
      <VariantCommandBar
        status={commandStatus}
        conflict={revisionConflict}
        variantCount={draft?.variant_count ?? data?.profile.variant_count ?? 0}
        revision={draftBaseline?.revision ?? data?.profile.revision}
        refreshing={variations.refreshing}
        canApply={Boolean(draft && dirty)}
        presetsDisabled={!draft || !data}
        presetName={presetName}
        selectedPreset={selectedPreset}
        presets={(data?.presets ?? []).map((preset) => ({ presetId: preset.preset_id, name: preset.name }))}
        presetSaving={busy === "preset"}
        presetLoading={busy === "load"}
        presetFeedback={presetFeedback}
        onRefresh={variations.refresh}
        onApply={() => void saveProfile()}
        onPresetNameChange={setPresetName}
        onSelectedPresetChange={setSelectedPreset}
        onSavePreset={() => void savePreset()}
        onLoadPreset={requestPresetLoad}
      />
      {variations.loading && <SkeletonLines count={5} />}
      {variations.error && <StateBlock kind="bad" title="Variation profile read failed" detail={variations.error} />}
      <ActionNotice message={message} />
      {draft && data && (
        <>
        <VariantWorkspace
          navigator={(
            <VariantNavigator
              variants={visibleVariants}
              selectedIndex={previewIndex}
              minimumCount={limits.min_variants}
              maximumCount={limits.max_variants}
              onSelect={selectPreviewVariant}
              onCountChange={updateVariantCount}
            />
          )}
          editor={(
            <article className="panel variation-editor">
              <VariantEditorTabs
                variant={previewVariant}
                variantIndex={previewIndex}
                data={data}
                informationData={informationData}
                variationWarnings={variations.envelope?.warnings}
                productInformationLoading={productInformation.loading}
                productInformationError={productInformation.error}
                productInformationScanning={busy === "information"}
                onRescanProductInformation={() => void rescanProductInformation()}
                featureFlags={featureFlags}
                previewProduct={previewProduct}
                previewInformationProduct={previewInformationProduct}
                updateVariant={(patch) => updateVariant(previewIndex, patch)}
                applyTextStyle={(textStyleId) => applyTextStyle(previewIndex, textStyleId)}
                updateSubtitleY={(value) => updateSubtitleY(previewIndex, value)}
                updateLetterboxEnabled={(enabled) => updateLetterboxEnabled(previewIndex, enabled)}
                updateLetterboxHookEnabled={(enabled) => updateLetterboxHookEnabled(previewIndex, enabled)}
                updateDynamicTextRole={(role, enabled) => updateDynamicTextRole(previewIndex, role, enabled)}
                updateDynamicTextSetting={(role, patch) => updateDynamicTextSetting(previewIndex, role, patch)}
                updatePreviewProduct={(productKey) => {
                  setPreviewProduct(productKey);
                  invalidateRenderedPreview();
                }}
                updatePreviewInformationProduct={(productKey) => {
                  setPreviewInformationProduct(productKey);
                  invalidateRenderedPreview();
                }}
                hookTypeAvailable={(hookType) => hookTypeAvailable(hookType, featureFlags)}
                beforeAfterRelevant={usesBeforeAfterImage(previewVariant)}
              />
            </article>
          )}
          preview={(
            <VariantPreviewPanel
              variant={previewVariant}
              variantIndex={previewIndex}
              data={data}
              informationData={informationData}
              previewProduct={previewProduct}
              renderedPreview={renderedPreview}
              rendering={busy === "preview"}
              feedback={previewFeedback}
              mediaError={previewMediaError}
              onRender={() => void renderPreview()}
              onMediaError={setPreviewMediaError}
              onMediaLoaded={() => setPreviewMediaError("")}
            />
          )}
        />
        <ConfirmDialog
          open={presetLoadConfirmOpen}
          title="Replace unsaved variation draft?"
          detail="Loading this preset will replace your current unsaved edits. It will not apply the preset to future clips."
          confirmLabel="Load preset"
          danger
          onConfirm={() => void loadPreset()}
          onClose={() => setPresetLoadConfirmOpen(false)}
        />
        </>
      )}
    </section>
  );
}

function copyProfile(profile: VariationProfile): VariationProfile {
  return JSON.parse(JSON.stringify(profile)) as VariationProfile;
}

function defaultDynamicTextSettings(
  headlineFont: string,
  bodyFont: string
): VariationVariant["dynamic_text_settings"] {
  return {
    ingredients: { headline_font_id: headlineFont, body_font_id: bodyFont, font_size: 35, animation: "current", duration_seconds: 2.6 },
    benefits: { headline_font_id: headlineFont, body_font_id: bodyFont, font_size: 35, animation: "current", duration_seconds: 2.6 },
    usage: { headline_font_id: headlineFont, body_font_id: bodyFont, font_size: 35, animation: "current", duration_seconds: 2.6 },
    cta: { headline_font_id: headlineFont, body_font_id: bodyFont, font_size: 50, animation: "current", duration_seconds: 1.3 }
  };
}

function normalizeDynamicTextSettings(variant: VariationVariant): VariationVariant["dynamic_text_settings"] {
  const defaults = defaultDynamicTextSettings(
    variant.headline_font_id || variant.font_id || "",
    variant.caption_font_id || variant.font_id || ""
  );
  const roles = Object.keys(defaults) as VariationVariant["dynamic_text_roles"];
  return Object.fromEntries(roles.map((role) => {
    const raw = variant.dynamic_text_settings?.[role];
    const isCta = role === "cta";
    return [role, {
      ...defaults[role],
      ...raw,
      font_size: clampNumber(raw?.font_size ?? defaults[role].font_size, isCta ? 24 : 20, isCta ? 96 : 72),
      duration_seconds: Math.round(clampNumber(raw?.duration_seconds ?? defaults[role].duration_seconds, 1, 6) * 10) / 10
    }];
  })) as VariationVariant["dynamic_text_settings"];
}

function normalizeUiProfile(profile: VariationProfile): VariationProfile {
  const copy = copyProfile(profile);
  copy.variants = copy.variants.map((variant) => ({
    ...variant,
    before_after_mode: "fullscreen",
    random_broll_enabled: variant.random_broll_enabled ?? false,
    text_style_id: variant.text_style_id ?? "current",
    headline_font_id: variant.headline_font_id || variant.font_id || "",
    caption_font_id: variant.caption_font_id || variant.font_id || "",
    subtitle_size: variant.subtitle_size ?? "medium",
    subtitle_stroke_color: variant.subtitle_stroke_color ?? "#000000",
    subtitle_stroke_width: clampNumber(variant.subtitle_stroke_width ?? 3, 0, 12),
    subtitle_highlight_enabled: variant.subtitle_highlight_enabled ?? true,
    subtitle_animation: variant.subtitle_animation ?? "current",
    headline_animation: variant.headline_animation ?? "current",
    caption_animation: variant.caption_animation ?? "current",
    headline_stroke_width: clampNumber(variant.headline_stroke_width ?? 5, 0, 12),
    headline_shadow_color: variant.headline_shadow_color ?? "#000000",
    headline_shadow_x: clampNumber(variant.headline_shadow_x ?? 3, -20, 20),
    headline_shadow_y: clampNumber(variant.headline_shadow_y ?? 3, -20, 20),
    headline_rotation_degrees: clampNumber(variant.headline_rotation_degrees ?? 0, -10, 10),
    caption_stroke_width: clampNumber(variant.caption_stroke_width ?? 4, 0, 12),
    dynamic_text_mode: variant.dynamic_text_mode ?? "balanced",
    dynamic_text_roles: variant.dynamic_text_roles
      ?? ["ingredients", "benefits", "usage", "cta"],
    dynamic_text_settings: normalizeDynamicTextSettings(variant),
    letterbox_hook_enabled: variant.letterbox_hook_enabled ?? false,
    letterbox_hook_font_id: variant.letterbox_hook_font_id || variant.font_id || "",
    letterbox_hook_font_color: variant.letterbox_hook_font_color ?? "#FFFFFF",
    letterbox_hook_font_size: clampNumber(variant.letterbox_hook_font_size ?? 72, 24, 160),
    letterbox_hook_x_frac: clampNumber(variant.letterbox_hook_x_frac ?? 0.5, 0, 1),
    letterbox_hook_y_frac: clampNumber(variant.letterbox_hook_y_frac ?? 0.5, 0, 1)
  }));
  return copy;
}

function clampNumber(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) {
    return min;
  }
  return Math.max(min, Math.min(max, value));
}

function subtitleYDefault(position: VariationVariant["subtitle_position"]): number {
  if (position === "top") {
    return 0.34;
  }
  if (position === "center") {
    return 0.58;
  }
  return 0.84;
}

function subtitlePositionFromY(value: number): VariationVariant["subtitle_position"] {
  if (value < 0.46) {
    return "top";
  }
  if (value < 0.70) {
    return "center";
  }
  return "bottom";
}

function createUiVariant(index: number, base?: VariationVariant): VariationVariant {
  const dynamicTextSettings = base
    ? normalizeDynamicTextSettings(base)
    : defaultDynamicTextSettings("", "");
  return {
    name: `Variant ${index + 1}`,
    hook_type: base?.hook_type ?? "text",
    visual_mode: base?.visual_mode ?? "host",
    random_broll_enabled: base?.visual_mode === "broll_audio" ? false : base?.random_broll_enabled ?? false,
    before_after_mode: base?.before_after_mode ?? "fullscreen",
    text_style_id: base?.text_style_id ?? "current",
    font_id: base?.font_id ?? "",
    headline_font_id: base?.headline_font_id || base?.font_id || "",
    caption_font_id: base?.caption_font_id || base?.font_id || "",
    font_color: base?.font_color ?? "#FFFFFF",
    highlight_color: base?.highlight_color ?? "#FFD600",
    subtitle_position: base?.subtitle_position ?? "bottom",
    subtitle_size: base?.subtitle_size ?? "medium",
    subtitle_stroke_color: base?.subtitle_stroke_color ?? "#000000",
    subtitle_stroke_width: base?.subtitle_stroke_width ?? 3,
    subtitle_highlight_enabled: base?.subtitle_highlight_enabled ?? true,
    subtitle_animation: base?.subtitle_animation ?? "current",
    headline_animation: base?.headline_animation ?? "current",
    caption_animation: base?.caption_animation ?? "current",
    headline_stroke_width: base?.headline_stroke_width ?? 5,
    headline_shadow_color: base?.headline_shadow_color ?? "#000000",
    headline_shadow_x: base?.headline_shadow_x ?? 3,
    headline_shadow_y: base?.headline_shadow_y ?? 3,
    headline_rotation_degrees: base?.headline_rotation_degrees ?? 0,
    caption_stroke_width: base?.caption_stroke_width ?? 4,
    color_grade: base?.color_grade ?? "original",
    bgm_mode: base?.bgm_mode ?? "auto",
    bgm_path: base?.bgm_path ?? "",
    sfx_enabled: base?.sfx_enabled ?? true,
    zoom_intensity: base?.zoom_intensity ?? "normal",
    product_zoom_enabled: base?.product_zoom_enabled ?? true,
    subtitle_enabled: base?.subtitle_enabled ?? true,
    dynamic_text_mode: base?.dynamic_text_mode ?? "balanced",
    dynamic_text_roles: base?.dynamic_text_roles
      ? [...base.dynamic_text_roles]
      : ["ingredients", "benefits", "usage", "cta"],
    dynamic_text_settings: JSON.parse(JSON.stringify(dynamicTextSettings)) as VariationVariant["dynamic_text_settings"],
    letterbox_enabled: false,
    mirror_enabled: base?.mirror_enabled ?? false,
    subtitle_y_frac: base?.subtitle_y_frac ?? subtitleYDefault(base?.subtitle_position ?? "bottom"),
    letterbox_top_frac: 0,
    letterbox_bottom_frac: 0,
    letterbox_hook_enabled: false,
    letterbox_hook_font_id: base?.letterbox_hook_font_id || base?.font_id || "",
    letterbox_hook_font_color: base?.letterbox_hook_font_color ?? "#FFFFFF",
    letterbox_hook_font_size: base?.letterbox_hook_font_size ?? 72,
    letterbox_hook_x_frac: base?.letterbox_hook_x_frac ?? 0.5,
    letterbox_hook_y_frac: base?.letterbox_hook_y_frac ?? 0.5
  };
}

function usesBeforeAfterImage(variant: Pick<VariationVariant, "hook_type">): boolean {
  return variant.hook_type === "before_after_image" || variant.hook_type === "text_before_after_image";
}

function hookTypeAvailable(
  hookType: string,
  flags: NonNullable<VariationPageData["global_feature_flags"]>
): boolean {
  if (hookType === "before_after_image" || hookType === "text_before_after_image") {
    return flags.before_after;
  }
  if (hookType === "b_roll" || hookType === "text_b_roll") {
    return flags.broll_intro;
  }
  if (hookType === "transitional_hook") {
    return flags.transitional_hook;
  }
  return true;
}

function JobsPage({ active }: { active: boolean }) {
  const params = new URLSearchParams(window.location.search);
  const initialJob = params.get("job") ?? "";
  const [status, setStatus] = useState("");
  const [operation, setOperation] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(initialJob);
  const jobsPath = `/api/control/jobs${query({ limit: 50, offset, operation, status })}`;
  const jobs = useApiQuery<ControlJobPage>(jobsPath, jobPollingInterval, active);
  const detail = useApiQuery<ControlJob>(
    `/api/control/jobs/${encodeURIComponent(selected)}?include_result=false`,
    (job) => job && ["queued", "running"].includes(job.status) ? 2_000 : false,
    active && Boolean(selected),
    { cache: false }
  );
  const resultPreview = useApiQuery<ControlJobResultPreview>(
    `/api/control/jobs/${encodeURIComponent(selected)}/result-preview`,
    false,
    active && Boolean(selected) && Boolean(detail.envelope?.data.result_metadata?.available),
    { cache: false }
  );
  const rows = jobs.envelope?.data.jobs ?? [];
  const operations: ControlJob["operation"][] = ["queue_control", "settings_update", "settings_delete", "settings_reset", "rescore", "compliance_scan", "module_assembly", "export_batches", "module_review", "trend_refresh", "trend_download", "trend_analysis"];

  useEffect(() => { setOffset(0); }, [status, operation]);

  return (
    <section className="page-stack">
      <PageTitle title="Jobs" detail="Audit control operations, conflicts, errors, and results." onRefresh={jobs.refresh} />
      <div className="index-toolbar">
        <FilterField label="Operation">
          <select value={operation} onChange={(event) => setOperation(event.target.value)}>
            <option value="">All operations</option>
            {operations.map((item) => <option value={item} key={item}>{operationLabel(item)}</option>)}
          </select>
        </FilterField>
        <FilterField label="Status">
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">All statuses</option>
            {["queued", "running", "completed", "failed", "interrupted", "rejected"].map((item) => <option value={item} key={item}>{item}</option>)}
          </select>
        </FilterField>
      </div>
      {jobs.loading && <SkeletonLines count={5} />}
      {jobs.error && <StateBlock kind="bad" title="Jobs read failed" detail={jobs.error} />}
      <JobTable rows={rows} selected={selected} setSelected={setSelected} />
      <Pagination total={jobs.envelope?.data.total ?? 0} limit={jobs.envelope?.data.limit ?? 50} offset={jobs.envelope?.data.offset ?? offset} setOffset={setOffset} />
      <JobDetailDrawer
        job={detail.envelope?.data}
        loading={detail.loading && Boolean(selected)}
        error={detail.error}
        resultPreview={resultPreview.envelope?.data}
        resultLoading={resultPreview.loading}
        resultError={resultPreview.error}
        onClose={() => setSelected("")}
      />
    </section>
  );
}

function JobTable({
  rows,
  selected,
  setSelected,
  compact = false
}: {
  rows: ControlJobSummary[];
  selected: string;
  setSelected: (id: string) => void;
  compact?: boolean;
}) {
  if (rows.length === 0) {
    return <EmptyState icon={Activity} title="No jobs match" detail="Change filters or run an operation to create a job." />;
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Job</th>
            <th>Operation</th>
            <th>Status</th>
            <th>Updated</th>
            {!compact && <th>Actor</th>}
            {!compact && <th>Error</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((job) => (
            <tr className={selected === job.job_id ? "selected-row" : ""} key={job.job_id} onClick={() => setSelected(job.job_id)}>
              <td>
                <div className="strong">{job.job_id.slice(0, 12)}</div>
                <div className="muted">{job.conflict_key || "no conflict key"}</div>
              </td>
              <td>{operationLabel(job.operation)}</td>
              <td><Badge value={job.status} /></td>
              <td>{job.updated_at}</td>
              {!compact && <td>{job.actor}</td>}
              {!compact && <td className="wide-cell muted">{job.error || "-"}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function JobDetailDrawer({
  job,
  loading,
  error,
  resultPreview,
  resultLoading,
  resultError,
  onClose
}: {
  job?: ControlJob;
  loading: boolean;
  error?: string;
  resultPreview?: ControlJobResultPreview;
  resultLoading: boolean;
  resultError?: string;
  onClose: () => void;
}) {
  const requestPreview = useMemo(
    () => job ? boundedJsonPreview(job.request) : undefined,
    [job?.request]
  );
  const renderedResultPreview = useMemo(
    () => job?.result != null
      ? boundedJsonPreview(job.result)
      : resultPreview
        ? { text: resultPreview.preview, truncated: resultPreview.truncated, circular: false }
        : job ? boundedJsonPreview(null) : undefined,
    [job?.result, resultPreview]
  );
  return (
    <Drawer open={Boolean(job) || loading || Boolean(error)} title={job ? operationLabel(job.operation) : "Job detail"} detail={job?.job_id} onClose={onClose}>
      {loading && <SkeletonLines count={5} />}
      {error && <StateBlock kind="bad" title="Job detail failed" detail={error} />}
      {job && (
        <>
          <div className="detail-grid">
            <MetricCard label="Status" value={job.status} hint={job.updated_at} icon={Activity} />
            <MetricCard label="Actor" value={job.actor} hint="Submitted by" icon={BadgeCheck} />
            <MetricCard label="Started" value={job.started_at ? "Yes" : "No"} hint={job.started_at || "-"} icon={Clock} />
            <MetricCard label="Finished" value={job.finished_at ? "Yes" : "No"} hint={job.finished_at || "-"} icon={CheckCircle2} />
          </div>
          {job.result_metadata && (
            <div className="detail-list">
              <DetailItem label="Stored result" value={job.result_metadata.available ? "Available" : "Unavailable"} />
              <DetailItem label="Result size" value={job.result_metadata.stored_bytes == null ? "-" : `${numberText(job.result_metadata.stored_bytes)} bytes`} />
              <DetailItem label="Expires" value={job.result_metadata.expires_at || "-"} />
            </div>
          )}
          {job.error && <StateBlock kind="bad" title="Error" detail={job.error} />}
          <section className="drawer-section">
            <h3>Request</h3>
            <pre className="json-panel">{requestPreview?.text ?? "-"}</pre>
            {requestPreview?.truncated && (
              <p className="json-preview-note">
                Preview truncated to protect the renderer{requestPreview.circular ? "; circular values were replaced" : ""}.
              </p>
            )}
          </section>
          <section className="drawer-section">
            <h3>Result</h3>
            {resultLoading && <SkeletonLines count={2} />}
            {resultError && <StateBlock kind="warn" title="Result preview unavailable" detail={resultError} />}
            {!resultLoading && !resultError && <pre className="json-panel">{renderedResultPreview?.text ?? "-"}</pre>}
            {renderedResultPreview?.truncated && (
              <p className="json-preview-note">
                Preview truncated to protect the renderer{renderedResultPreview.circular ? "; circular values were replaced" : ""}.
              </p>
            )}
            {job.result_metadata?.available && (
              <a className="secondary-button" href={`/api/control/jobs/${encodeURIComponent(job.job_id)}/result`}>
                <Download size={16} aria-hidden="true" />
                Download raw result
              </a>
            )}
          </section>
        </>
      )}
    </Drawer>
  );
}

function LogsPage({ active }: { active: boolean }) {
  const [lines, setLines] = useState(200);
  const [search, setSearch] = useState("");
  const [follow, setFollow] = useState(true);
  const [wrap, setWrap] = useState(true);
  const logs = useApiQuery<LogTail>(`/api/logs?lines=${lines}`, follow ? 2_000 : false, active);
  const visible = (logs.envelope?.data.lines ?? [])
    .map((line, sourceIndex) => ({ ...line, sourceIndex }))
    .filter((line) => !search || line.text.toLowerCase().includes(search.toLowerCase()));
  const totalLines = logs.envelope?.data.total_lines;
  const returnedLines = logs.envelope?.data.returned_lines ?? 0;
  return (
    <section className="page-stack">
      <PageTitle title="Pipeline logs" detail="Follow current pipeline output or pause to investigate an issue." onRefresh={logs.refresh} />
      <div className="index-toolbar">
        <SearchInput value={search} onChange={setSearch} placeholder="Search visible log lines..." />
        <FilterField label="Lines">
          <select value={lines} onChange={(event) => setLines(Number(event.target.value))}>
            {[100, 200, 500, 1000].map((value) => <option value={value} key={value}>{value}</option>)}
          </select>
        </FilterField>
        <button className={`secondary-button ${follow ? "active" : ""}`} onClick={() => setFollow((value) => !value)}>
          {follow ? <Clock size={15} aria-hidden="true" /> : <Play size={15} aria-hidden="true" />}
          {follow ? "Pause follow" : "Follow latest"}
        </button>
        <button className="secondary-button" onClick={() => setWrap((value) => !value)}>{wrap ? "No wrap" : "Wrap lines"}</button>
      </div>
      {logs.loading && <SkeletonLines count={4} />}
      {logs.error && <StateBlock kind="bad" title="Log read failed" detail={logs.error} />}
      <StateBlock kind="warn" warnings={logs.envelope?.warnings} />
      <div className="log-meta">
        <span>{logs.envelope?.data.path || "pipeline.log"}</span>
        <span>{totalLines == null ? `Latest ${numberText(returnedLines)} lines` : `Latest ${numberText(returnedLines)} of ${numberText(totalLines)} lines`}</span>
        <span>{logs.envelope?.generated_at ? `Updated ${displayTime(logs.envelope.generated_at)}` : "Waiting for log data"}</span>
      </div>
      {visible.length === 0 && !logs.loading && <EmptyState icon={Terminal} title={search ? "No matching log lines" : "No log lines yet"} detail={search ? "Change the search text or line range." : "Pipeline output will appear here when a run starts."} />}
      <div className={`log-panel ${wrap ? "wrap" : "nowrap"}`} role="log" aria-live={follow ? "polite" : "off"}>
        {visible.map((line) => (
          <div key={line.line_number == null ? `recent-${line.sourceIndex}` : `line-${line.line_number}`}><span>{line.line_number ?? "\u00b7"}</span>{line.text}</div>
        ))}
      </div>
    </section>
  );
}

function SystemPage({ active }: { active: boolean }) {
  const health = useApiQuery<HealthPayload>("/api/health", 5_000, active);
  const system = useApiQuery<SystemStats>("/api/system", 5_000, active);
  const catalog = useApiQuery<CatalogStatus>("/api/catalog/status", 30_000, active);
  const liveUpdates = useLiveUpdateStatus();
  const data = system.envelope?.data;
  const catalogData = catalog.envelope?.data;
  const [desktop, setDesktop] = useState<DesktopRuntimeStatus>();
  const [copyMessage, setCopyMessage] = useState("");

  function refreshDesktop() {
    if (window.clipperDesktop?.getStatus) {
      void window.clipperDesktop.getStatus().then(setDesktop).catch(() => setDesktop(undefined));
    }
  }

  useEffect(() => {
    if (active) {
      refreshDesktop();
    }
  }, [active]);

  function copyDiagnostics() {
    const payload = JSON.stringify({ health: health.envelope?.data, system: data, catalog: catalogData, liveUpdates, desktop }, null, 2);
    void navigator.clipboard.writeText(payload).then(() => setCopyMessage("Diagnostics copied.")).catch(() => setCopyMessage("Could not copy diagnostics."));
  }
  return (
    <section className="page-stack">
      <PageTitle title="Diagnostics" detail="API, catalog, live updates, desktop runtime, and local machine resource status." onRefresh={() => { health.refresh(); system.refresh(); catalog.refresh(); refreshDesktop(); }}>
        <button className="secondary-button" onClick={copyDiagnostics}><ClipboardCheck size={15} aria-hidden="true" /> Copy diagnostics</button>
      </PageTitle>
      {copyMessage && <StateBlock kind={copyMessage.startsWith("Diagnostics") ? "good" : "bad"} detail={copyMessage} />}
      <div className="metric-grid">
        <MetricCard label="API" value={health.envelope?.data.status ?? "Unknown"} hint={health.envelope?.data.mode ?? "control"} icon={Server} />
        <MetricCard label="CPU" value={data?.cpu_percent == null ? "-" : `${data.cpu_percent.toFixed(0)}%`} hint="Current utilization" icon={Cpu} />
        <MetricCard label="RAM" value={data?.ram_percent == null ? "-" : `${data.ram_percent.toFixed(0)}%`} hint={data?.ram_label ?? "Unavailable"} icon={Monitor} />
        <MetricCard label="Disk" value={data?.disk_percent == null ? "-" : `${data.disk_percent.toFixed(0)}%`} hint={data?.disk_label ?? "Unavailable"} icon={HardDrive} />
      </div>
      <article className="panel">
        <div className="panel-head">
          <div>
            <h2>GPU</h2>
            <p>{data?.gpu_label ?? "Unavailable"}</p>
          </div>
          <Badge value={data?.gpu_label ?? "Unavailable"} kind={data?.gpu_label ? "info" : "neutral"} />
        </div>
        <div className="detail-grid">
          <MetricCard label="GPU load" value={data?.gpu_percent == null ? "-" : `${data.gpu_percent.toFixed(0)}%`} hint="Utilization" icon={Gauge} />
          <MetricCard label="GPU memory" value={data?.gpu_mem_percent == null ? "-" : `${data.gpu_mem_percent.toFixed(0)}%`} hint="Memory usage" icon={Monitor} />
        </div>
      </article>
      <article className="panel">
        <div className="panel-head">
          <div>
            <h2>Catalog and live updates</h2>
            <p>Indexed read model, queue persistence, and renderer invalidation status.</p>
          </div>
          <Badge
            value={liveUpdates.mode === "live" ? "Live" : liveUpdates.mode === "connecting" ? "Connecting" : "Polling fallback"}
            kind={liveUpdates.mode === "live" ? "good" : liveUpdates.mode === "connecting" ? "info" : "warn"}
          />
        </div>
        <div className="detail-list">
          <DetailItem label="Catalog mode" value={catalogData?.mode ?? "Unavailable"} />
          <DetailItem label="Queue storage" value={catalogData?.queue_storage_mode ?? "Unavailable"} />
          <DetailItem label="Integrity" value={catalogData?.integrity ?? "Unavailable"} />
          <DetailItem label="Backfill" value={catalogData?.backfill?.status ?? "Unavailable"} />
          <DetailItem label="Dirty sources" value={catalogData?.dirty_source_count ?? 0} />
          <DetailItem label="Shadow mismatches" value={catalogData?.shadow_comparison?.mismatch_count ?? 0} />
          <DetailItem label="Event reconnects" value={liveUpdates.reconnects} />
          <DetailItem label="Last event" value={liveUpdates.lastEventAt ? displayTime(new Date(liveUpdates.lastEventAt).toISOString()) : "None received"} />
        </div>
        {catalogData?.backfill?.error && <StateBlock kind="bad" title="Catalog backfill failed" detail={catalogData.backfill.error} />}
      </article>
      <article className="panel diagnostics-runtime-panel">
        <div className="panel-head">
          <div>
            <h2>Desktop runtime</h2>
            <p>Portable app connection and backend launch context.</p>
          </div>
          <Badge value={desktop ? desktop.backend_running ? "Backend running" : "Backend stopped" : "Browser mode"} kind={desktop?.backend_running ? "good" : "neutral"} />
        </div>
        <div className="detail-list">
          <DetailItem label="Backend port" value={desktop?.backend_port ?? "Unavailable"} />
          <DetailItem label="Project root" value={desktop?.project_root || "Unavailable outside Electron"} />
          <DetailItem label="Python" value={desktop?.python_exe || "Unavailable outside Electron"} />
          <DetailItem label="Backend command" value={desktop?.backend_command || "Unavailable outside Electron"} />
        </div>
        {desktop?.last_error && <StateBlock kind="bad" title="Last desktop error" detail={desktop.last_error} />}
        {desktop?.recent_log?.length ? (
          <details className="review-raw-details">
            <summary>Recent backend startup log</summary>
            <pre className="json-panel">{desktop.recent_log.join("\n")}</pre>
          </details>
        ) : null}
      </article>
      {(health.error || system.error || catalog.error) && <StateBlock kind="bad" title="System read failed" detail={health.error || system.error || catalog.error} />}
      <StateBlock kind="warn" warnings={[...(health.envelope?.warnings ?? []), ...(system.envelope?.warnings ?? []), ...(catalog.envelope?.warnings ?? [])]} />
    </section>
  );
}

const settingCopy: Record<string, { label: string; description: string; unit?: string }> = {
  OUTPUT_DIR: { label: "Clip output folder", description: "Where rendered clips and run artifacts are written." },
  WORKING_DIR: { label: "Working data folder", description: "Queue state, caches, logs, and temporary processing data." },
  QUEUE_INPUT_DIR: { label: "VOD input folder", description: "Folder scanned for livestream videos." },
  MIN_SCORE: { label: "Minimum clip score", description: "Lowest score a detected moment must reach to continue.", unit: "/10" },
  MIN_CLIP_DURATION: { label: "Minimum clip duration", description: "Shortest allowed selected clip.", unit: "seconds" },
  MAX_CLIP_DURATION: { label: "Maximum clip duration", description: "Longest allowed selected clip.", unit: "seconds" },
  OUTPUT_FPS: { label: "Output frame rate", description: "Frames per second used for rendered clips.", unit: "fps" },
  OUTPUT_CQ: { label: "Output quality value", description: "Encoder quality setting; lower values usually produce larger, higher-quality files." },
  BGM_VOLUME: { label: "Background music volume", description: "Relative music volume from 0 to 1." },
  BGM_ENABLED: { label: "Global background music", description: "Master switch. When off, individual variants cannot enable background music." },
  SFX_ENABLED: { label: "Global sound effects", description: "Master switch. When off, individual variants cannot enable sound effects." },
  BEFORE_AFTER_ENABLED: { label: "Global before/after images", description: "Master prerequisite for before/after hooks configured on the Variants page." },
  LM_STUDIO_MOMENT_MODEL_ID: { label: "Text model ID", description: "Canonical LM Studio model used for moment detection and text operations." },
  SCORER_VISION_MODEL_ID: { label: "Vision model ID", description: "Canonical LM Studio vision model used for optional visual scoring." },
  WHISPERX_DEVICE: { label: "WhisperX alignment device", description: "Device used for word alignment; intentionally independent from the transcription device." },
  OUTPUT_NVENC_PRESET: { label: "NVENC encoder preset", description: "NVIDIA encoder preset p1 through p7; used whenever the selected codec ends in _nvenc." },
  OUTPUT_PRESET: { label: "CPU encoder preset", description: "Encoder preset used only for non-NVENC codecs such as libx264." },
  SCORER_EXPORT_READY_THRESHOLD: { label: "Export-ready score", description: "Score required for a clip to be considered delivery-ready.", unit: "/10" },
  SCORER_REVIEW_THRESHOLD: { label: "Review score threshold", description: "Clips below this score are highlighted for review.", unit: "/10" },
  QUEUE_MAX_INFLIGHT_VIDEOS: { label: "Concurrent videos", description: "Maximum videos processed at the same time." },
  QUEUE_FFMPEG_MAX_PARALLEL_CLIPS: { label: "Parallel clip renders", description: "Maximum FFmpeg clip renders running together." },
  QUEUE_BETWEEN_RUNS_DELAY_SECONDS: { label: "Between-run delay", description: "Time to wait before starting the next queued video.", unit: "seconds" },
  QUEUE_DASHBOARD_QUEUED_STALL_SECONDS: { label: "Queued alert threshold", description: "Time a queued video may wait before it is flagged for attention.", unit: "seconds" },
  QUEUE_DASHBOARD_RUNNING_STALL_SECONDS: { label: "Running alert threshold", description: "Time without progress before a running video is flagged for attention.", unit: "seconds" },
  QUEUE_MAX_RETRIES: { label: "Retry limit", description: "Maximum automatic retry attempts for a failed queue step." }
};

function settingLabel(name: string): string {
  if (settingCopy[name]) {
    return settingCopy[name].label;
  }
  return name.toLowerCase().replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function settingCategoryLabel(category: string): string {
  const labels: Record<string, string> = {
    selection: "Processing",
    paths: "Paths",
    queue: "Queue",
    render: "Render",
    models: "Models",
    scoring: "Scoring",
    compliance: "Compliance",
    delivery: "Delivery",
    advanced: "Advanced"
  };
  return labels[category] ?? settingLabel(category);
}

function settingDescription(entry: SettingsReadEntry): string {
  if (settingCopy[entry.name]) {
    const copy = settingCopy[entry.name];
    const restart = entry.category === "queue" ? " Applies on the next queue start." : "";
    return `${copy.description}${copy.unit ? ` Unit: ${copy.unit}.` : ""}${restart}`;
  }
  const scope: Record<string, string> = {
    paths: "Local path used by the production pipeline.",
    queue: "Controls queue scheduling, capacity, or health detection.",
    models: "Controls local transcription or language-model behavior.",
    selection: "Controls how candidate moments become clips.",
    render: "Controls video, audio, and encoder output.",
    scoring: "Controls automated quality scoring and review thresholds.",
    compliance: "Controls policy scanning and automatic corrections."
  };
  const description = scope[entry.category] || "Operator-safe pipeline setting.";
  return entry.category === "queue" ? `${description} Applies on the next queue start.` : description;
}

function SettingsPage({ active }: { active: boolean }) {
  const settings = useApiQuery<SettingsReadSnapshot>("/api/settings/effective", 30_000, active);
  const groups = settings.envelope?.data.groups ?? {};
  const revision = settings.envelope?.data.revision ?? "";
  const entries = Object.values(groups).flat();
  const categoryOrder = ["selection", "queue", "render", "models", "scoring", "compliance", "delivery", "paths", "advanced"];
  const categories = Object.keys(groups).sort((left, right) => {
    const leftIndex = categoryOrder.indexOf(left);
    const rightIndex = categoryOrder.indexOf(right);
    return (leftIndex < 0 ? categoryOrder.length : leftIndex) - (rightIndex < 0 ? categoryOrder.length : rightIndex)
      || left.localeCompare(right);
  });
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<ActionMessage>();
  const [search, setSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("queue");
  const [deleteTarget, setDeleteTarget] = useState("");

  useEffect(() => {
    const next: Record<string, string> = {};
    Object.values(groups).flat().forEach((entry) => {
      next[entry.name] = String(entry.value ?? "");
    });
    setDraft(next);
  }, [revision]);

  function isInvalid(entry: SettingsReadEntry): boolean {
    const raw = draft[entry.name] ?? "";
    if (entry.value_type === "int" || entry.value_type === "float") {
      const numeric = entry.value_type === "int" ? Number.parseInt(raw, 10) : Number.parseFloat(raw);
      return Number.isNaN(numeric)
        || (entry.minimum != null && numeric < entry.minimum)
        || (entry.maximum != null && numeric > entry.maximum);
    }
    return false;
  }

  function parseEntry(entry: SettingsReadEntry): boolean | number | string {
    const raw = draft[entry.name] ?? "";
    if (entry.value_type === "bool") {
      return raw === "true";
    }
    if (entry.value_type === "int") {
      return Number.parseInt(raw, 10);
    }
    if (entry.value_type === "float") {
      return Number.parseFloat(raw);
    }
    return raw;
  }

  const invalidEntries = entries.filter((entry) => entry.editable !== false && isInvalid(entry));
  const changedEntries = entries.filter((entry) => entry.editable !== false && !isInvalid(entry) && String(parseEntry(entry)) !== String(entry.value ?? ""));
  const restartRequiredChanges = changedEntries.filter(
    (entry) => entry.category === "queue" || ["WORKING_DIR", "QUEUE_INPUT_DIR", "QUEUE_STATE_FILE", "QUEUE_FOREVER_STATE_FILE", "QUEUE_CONTROL_FILE"].includes(entry.name)
  );
  const visibleGroups = Object.fromEntries(
    Object.entries(groups)
      .filter(([category]) => !categoryFilter || category === categoryFilter)
      .map(([category, groupEntries]) => [
        category,
        groupEntries.filter((entry) => {
          const needle = search.trim().toLowerCase();
          return !needle || `${entry.name} ${settingLabel(entry.name)} ${settingDescription(entry)}`.toLowerCase().includes(needle);
        })
      ])
      .filter(([, groupEntries]) => (groupEntries as SettingsReadEntry[]).length > 0)
  ) as Record<string, SettingsReadEntry[]>;

  useEffect(() => {
    if (changedEntries.length === 0) {
      return;
    }
    const protectDraft = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", protectDraft);
    return () => window.removeEventListener("beforeunload", protectDraft);
  }, [changedEntries.length]);

  function saveChanges() {
    const overrides: Record<string, boolean | number | string> = {};
    changedEntries.forEach((entry) => {
      overrides[entry.name] = parseEntry(entry);
    });
    void submitMutation(
      () => sendJson<ControlJob>("PUT", "/api/settings/overrides", {
        overrides,
        expected_revision: revision
      }),
      setMessage,
      refreshJobQueries,
      [settings.refresh]
    );
  }

  function deleteOverride(name: string) {
    void submitMutation(
      () => sendJson<ControlJob>("DELETE", `/api/settings/overrides/${encodeURIComponent(name)}${query({ expected_revision: revision })}`),
      setMessage,
      refreshJobQueries,
      [settings.refresh]
    );
  }


  function revertChanges() {
    const next: Record<string, string> = {};
    entries.forEach((entry) => { next[entry.name] = String(entry.value ?? ""); });
    setDraft(next);
    setMessage({ kind: "info", text: "Unsaved setting changes were reverted." });
  }

  return (
    <section className="page-stack">
      <PageTitle title="Configuration" detail="Edit registry-backed operator-safe settings with validated values." onRefresh={settings.refresh}>
        <button className="secondary-button" disabled={changedEntries.length === 0} onClick={revertChanges}>Revert</button>
        <button className="primary-button" disabled={!revision || invalidEntries.length > 0 || changedEntries.length === 0} onClick={saveChanges}>
          <Settings size={16} aria-hidden="true" />
          Save {changedEntries.length ? `${changedEntries.length} change(s)` : "changes"}
        </button>
      </PageTitle>
      {settings.loading && <SkeletonLines count={5} />}
      {settings.error && <StateBlock kind="bad" title="Settings read failed" detail={settings.error} />}
      <StateBlock kind="warn" warnings={settings.envelope?.warnings} />
      {invalidEntries.length > 0 && <StateBlock kind="bad" title="Invalid values" detail={`${invalidEntries.length} setting(s) need numeric values before saving.`} />}
      {restartRequiredChanges.length > 0 && (
        <StateBlock
          kind="warn"
          title="Queue restart required"
          detail="These queue or path changes apply to the next queue start. The active run keeps the settings snapshot it started with."
        />
      )}
      <ActionNotice message={message} />
      <div className="settings-toolbar">
        <SearchInput value={search} onChange={setSearch} placeholder="Search settings by name or purpose..." />
      </div>
      <div className="settings-layout">
        <aside className="settings-subnav" aria-label="Settings categories">
          <button className={!categoryFilter ? "active" : ""} onClick={() => setCategoryFilter("")}>All settings</button>
          {categories.map((category) => <button className={categoryFilter === category ? "active" : ""} onClick={() => setCategoryFilter(category)} key={category}>{settingCategoryLabel(category)}</button>)}
          <details className="settings-revision"><summary>Configuration details</summary><small>Revision {revision ? revision.slice(0, 12) : "loading"}</small></details>
        </aside>
      <div className="settings-grid">
        {Object.entries(visibleGroups).map(([category, groupEntries]) => (
          <article className="panel" key={category}>
            <div className="panel-head">
              <div>
                <h2>{settingCategoryLabel(category)}</h2>
                <p>{groupEntries.length} {groupEntries.length === 1 ? "setting" : "settings"}</p>
              </div>
            </div>
            <div className="settings-list">
              {groupEntries.map((entry) => (
                <div className={`setting-row editable-setting ${entry.editable !== false && isInvalid(entry) ? "invalid" : ""}`} key={entry.name}>
                  <div>
                    <strong>{settingLabel(entry.name)}</strong>
                    <span>{settingDescription(entry)}</span>
                    {entry.editable === false && <span>{entry.read_only_reason || "Managed by operator configuration; restart required after external changes."}</span>}
                    <details className="setting-technical-details">
                      <summary>Details</summary>
                      <code>{entry.name}</code>
                      <span>Type: {entry.value_type}</span>
                      <span>Source: {entry.source}</span>
                      {(entry.minimum !== null || entry.maximum !== null) && <span>Bounds: {entry.minimum ?? "none"} to {entry.maximum ?? "none"}</span>}
                    </details>
                  </div>
                  {entry.value_type === "bool" ? (
                    <label className="setting-toggle">
                      <input
                        type="checkbox"
                        disabled={entry.editable === false}
                        checked={(draft[entry.name] ?? "false") === "true"}
                        onChange={(event) => setDraft((current) => ({ ...current, [entry.name]: String(event.target.checked) }))}
                        aria-label={settingLabel(entry.name)}
                      />
                      <span aria-hidden="true" />
                    </label>
                  ) : (
                    <input
                      type={entry.value_type === "int" || entry.value_type === "float" ? "number" : "text"}
                      min={entry.minimum ?? undefined}
                      max={entry.maximum ?? undefined}
                      step={entry.value_type === "int" ? 1 : entry.value_type === "float" ? "any" : undefined}
                      disabled={entry.editable === false}
                      value={draft[entry.name] ?? ""}
                      onChange={(event) => setDraft((current) => ({ ...current, [entry.name]: event.target.value }))}
                    />
                  )}
                  {entry.editable !== false && entry.source === "settings_override"
                    ? <button className="tiny-button" onClick={() => setDeleteTarget(entry.name)}>Reset override</button>
                    : <span className="setting-reset-slot" aria-hidden="true" />}
                </div>
              ))}
            </div>
          </article>
        ))}
      </div>
      </div>
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="Reset this setting override?"
        detail={`${settingLabel(deleteTarget)} will return to its configured default value.`}
        confirmLabel="Reset override"
        danger
        onConfirm={() => deleteTarget && deleteOverride(deleteTarget)}
        onClose={() => setDeleteTarget("")}
      />
    </section>
  );
}

function RoutedApp() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Navigate to="/overview" replace />} />
        <Route path="/overview" element={<DashboardPage />} />
        <Route path="/production" element={<Navigate to="/production/live" replace />} />
        <Route path="/production/live" element={<OperationsPage />} />
        <Route path="/production/queue" element={<QueuePage />} />
        <Route path="/review" element={<Navigate to="/review/clips" replace />} />
        <Route path="/review/clips" element={<ClipReviewPage active />} />
        <Route path="/review/compliance" element={<CompliancePage active />} />
        <Route path="/trends" element={<TrendsPage active />} />
        <Route path="/variants" element={<VariationsPage active />} />
        <Route path="/modules" element={<ModularScannerPage />} />
        <Route path="/deliveries" element={<ExportsPage />} />
        <Route path="/activity" element={<Navigate to="/activity/jobs" replace />} />
        <Route path="/activity/jobs" element={<JobsPage active />} />
        <Route path="/activity/logs" element={<LogsPage active />} />
        <Route path="/settings" element={<Navigate to="/settings/configuration" replace />} />
        <Route path="/settings/configuration" element={<SettingsPage active />} />
        <Route path="/settings/diagnostics" element={<SystemPage active />} />
        <Route path="/dashboard" element={<Navigate to="/overview" replace />} />
        <Route path="/operations" element={<Navigate to="/production/live" replace />} />
        <Route path="/queue" element={<Navigate to="/production/queue" replace />} />
        <Route path="/clips" element={<Navigate to="/review/clips" replace />} />
        <Route path="/compliance" element={<Navigate to="/review/compliance" replace />} />
        <Route path="/variations" element={<Navigate to="/variants" replace />} />
        <Route path="/exports" element={<Navigate to="/deliveries" replace />} />
        <Route path="/jobs" element={<Navigate to="/activity/jobs" replace />} />
        <Route path="/logs" element={<Navigate to="/activity/logs" replace />} />
        <Route path="/system" element={<Navigate to="/settings/diagnostics" replace />} />
        <Route path="*" element={<Navigate to="/overview" replace />} />
      </Routes>
      <JobTray />
    </AppShell>
  );
}

export function App() {
  return (
    <BrowserRouter>
      <RoutedApp />
    </BrowserRouter>
  );
}
