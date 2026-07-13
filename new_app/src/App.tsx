import type { PointerEvent as ReactPointerEvent, ReactNode } from "react";
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
  ModuleDetail,
  ModuleLibraryPage,
  ModuleLibraryRow,
  ModuleReadiness,
  ModuleReadinessRow,
  OverviewData,
  OverviewTopClip,
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
  VariationOption,
  VariationPageData,
  VariationPreviewResult,
  VariationProfile,
  VariationVariant
} from "./api";
import { boundedJsonPreview } from "./boundedJsonPreview";
import { buildExportOverview } from "./exportOverview";
import { invalidateApiPrefix } from "./queryClient";
import { useApiQuery } from "./useApiQuery";
import { useDebouncedValue } from "./useDebouncedValue";

type BadgeKind = "good" | "bad" | "warn" | "info" | "neutral";
type ActionMessage = { kind: BadgeKind; text: string };
type SortDirection = "asc" | "desc";
type HealthPayload = { status: string; mode: string };
type WindowControlAction = "minimize" | "toggle-maximize" | "close";
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
  { label: "Overview", path: "/overview", match: "/overview", icon: LayoutDashboard, detail: "Production, quality, compliance, and delivery health" },
  { label: "Production", path: "/production/live", match: "/production", icon: Gauge, detail: "Current run, queue progress, and launch controls" },
  { label: "Review", path: "/review/clips", match: "/review", icon: Video, detail: "Clip quality, variants, and policy review" },
  { label: "Variants", path: "/variants", match: "/variants", icon: SlidersHorizontal, detail: "Global variant profiles and previews" },
  { label: "Modules", path: "/modules", match: "/modules", icon: Library, detail: "Reusable hook, main, and CTA inventory" },
  { label: "Deliveries", path: "/deliveries", match: "/deliveries", icon: PackageCheck, detail: "Automatic batching and recovery" }
];

const secondaryNav: NavItem[] = [
  { label: "Activity", path: "/activity/jobs", match: "/activity", icon: Activity, detail: "Background jobs and pipeline logs" },
  { label: "Settings", path: "/settings/configuration", match: "/settings", icon: Settings, detail: "Configuration and local diagnostics" }
];

const allNav = [...mainNav, ...secondaryNav];

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
  if (["completed", "strong", "ready", "passed", "ok", "healthy", "approved"].some((item) => normalized.includes(item))) {
    return "good";
  }
  if (["failed", "blocked", "critical", "stalled", "rejected", "interrupted", "outside"].some((item) => normalized.includes(item))) {
    return "bad";
  }
  if (["review", "attention", "waiting", "partial", "paused", "queued", "running", "processing", "stopped"].some((item) => normalized.includes(item))) {
    return "warn";
  }
  if (!normalized || normalized === "none" || normalized === "-") {
    return "neutral";
  }
  return "info";
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
  { value: "modules_only", label: "Modules Only" },
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
  const pipeline = pipelineModeOptions.find((item) => item.value === config.pipeline_mode)?.label ?? config.pipeline_mode;
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
  const value = row.completed_at || row.started_at;
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
  const normalized = String(value ?? "").toLowerCase();
  if (["running", "processing", "active", "in_progress", "in progress"].some((item) => normalized.includes(item))) {
    return "good";
  }
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

function dashboardDateText(): string {
  return new Intl.DateTimeFormat("en-US", {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric"
  }).format(new Date());
}

type ProductionDay = {
  key: string;
  label: string;
  count: number;
};

type ScoreTrendPoint = {
  key: string;
  label: string;
  average: number;
  count: number;
};

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

function parseDateValue(value?: string | null): Date | undefined {
  const text = String(value ?? "").trim();
  if (!text) {
    return undefined;
  }
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (dateOnly) {
    const [, year, month, day] = dateOnly;
    return new Date(Number(year), Number(month) - 1, Number(day));
  }
  const parsed = new Date(text);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed;
}

function localDayKey(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function dayWithOffset(offset: number): Date {
  const date = new Date();
  date.setHours(0, 0, 0, 0);
  date.setDate(date.getDate() + offset);
  return date;
}

function shortWeekday(date: Date): string {
  return new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(date);
}

function shortMonthDay(date: Date): string {
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(date);
}

function shortDateText(value?: string | null): string {
  const date = parseDateValue(value);
  return date ? new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(date) : "-";
}

function buildProductionDays(rows: QueueRunRow[], days = 7): ProductionDay[] {
  const dates = Array.from({ length: days }, (_, index) => dayWithOffset(index - (days - 1)));
  const counts = new Map(dates.map((date) => [localDayKey(date), 0]));
  rows.forEach((row) => {
    const date = parseDateValue(row.completed_at || row.started_at);
    if (!date) {
      return;
    }
    const key = localDayKey(date);
    if (!counts.has(key)) {
      return;
    }
    counts.set(key, (counts.get(key) ?? 0) + Math.max(0, row.clips_generated ?? 0));
  });
  return dates.map((date) => {
    const key = localDayKey(date);
    return {
      key,
      label: shortWeekday(date),
      count: counts.get(key) ?? 0
    };
  });
}

function buildScoreTrendPoints(rows: ScoreRow[], days = 14): ScoreTrendPoint[] {
  const earliest = dayWithOffset(-(days - 1)).getTime();
  const groups = new Map<string, { total: number; count: number; date: Date }>();
  rows.forEach((row) => {
    const score = numericValue(row.total_score);
    const date = parseDateValue(row.scored_at || row.source_date);
    if (score === undefined || !date || date.getTime() < earliest) {
      return;
    }
    const key = localDayKey(date);
    const current = groups.get(key) ?? { total: 0, count: 0, date };
    current.total += score;
    current.count += 1;
    groups.set(key, current);
  });
  return Array.from(groups.entries())
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, group]) => ({
      key,
      label: shortMonthDay(group.date),
      average: group.total / Math.max(1, group.count),
      count: group.count
    }));
}

function averageScore(rows: ScoreRow[]): number | undefined {
  const values = rows.map((row) => numericValue(row.total_score)).filter((value): value is number => value !== undefined);
  if (values.length === 0) {
    return undefined;
  }
  return values.reduce((total, value) => total + value, 0) / values.length;
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
  const systemQuery = useApiQuery<SystemStats>("/api/system", 15_000, true);
  const summary = dashboard.envelope?.data;
  const system = systemQuery.envelope?.data;
  const page = usePageInfo();
  const location = useLocation();
  const topbarDetail = page.path === "/overview" ? dashboardDateText() : page.detail;
  return (
    <div className="app-shell">
      <aside className="side-rail">
        <Link className="brand-block" to="/overview" aria-label="Clipper overview home">
          <div className="brand-mark">C</div>
          <div>
            <div className="brand-title">Clipper</div>
            <div className="brand-subtitle">Operations</div>
          </div>
        </Link>

        <div className="nav-section-label">Workspace</div>
        <nav className="nav-list" aria-label="Main navigation">
          {mainNav.map((item) => (
            <NavLink className={() => `nav-item ${navItemIsActive(item, location.pathname) ? "active" : ""}`} key={item.path} to={item.path} aria-label={item.label} title={item.label}>
              <item.icon aria-hidden="true" size={18} />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="nav-section-label utility">Utility</div>
        <nav className="nav-list secondary-nav" aria-label="Support navigation">
          {secondaryNav.map((item) => (
            <NavLink className={() => `nav-item ${navItemIsActive(item, location.pathname) ? "active" : ""}`} key={item.path} to={item.path} aria-label={item.label} title={item.label}>
              <item.icon aria-hidden="true" size={18} />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="rail-metrics" aria-label="Production summary">
          <div>
            <span>Total clips</span>
            <strong>{numberText(summary?.total_clips)}</strong>
          </div>
          <div>
            <span>Videos</span>
            <strong>{numberText(summary?.total_videos)}</strong>
          </div>
        </div>

        <div className="rail-status">
          <span className={`status-dot ${statusClass(healthText(summary))}`} />
          <div>
            <div className="rail-status-main">{healthText(summary)}</div>
            <div className="rail-status-sub">{system?.gpu_label || "System metrics loading"}</div>
          </div>
        </div>
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
            {[...mainNav.slice(3), ...secondaryNav].map((item) => (
              <Link to={item.path} key={item.path}><item.icon size={17} aria-hidden="true" />{item.label}</Link>
            ))}
          </div>
        </details>
      </nav>

      <main className="main-panel">
        <header className="topbar">
          <div>
            <div className="eyebrow">Clipper</div>
            <h1>{page.label}</h1>
            <p>{topbarDetail}</p>
          </div>
          <div className="topbar-actions">
            <QueueHealthPill summary={summary} />
            <WindowControls />
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
  const canControlWindow = typeof window !== "undefined" && Boolean(window.clipperDesktop?.windowControl);

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
  return <StateBlock kind={message.kind} detail={message.text} />;
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
  onConfirm,
  onClose
}: {
  open: boolean;
  title: string;
  detail: string;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
  onClose: () => void;
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
        <div className="confirm-dialog-actions">
          <button className="secondary-button" onClick={onClose}>Cancel</button>
          <button ref={confirmRef} className={danger ? "danger-button" : "primary-button"} onClick={() => { onConfirm(); onClose(); }}>
            {confirmLabel}
          </button>
        </div>
      </section>
    </div>
  );
}

function JobTray() {
  const jobs = useApiQuery<ControlJobPage>("/api/control/jobs?limit=12", jobPollingInterval, true);
  const visible = (jobs.envelope?.data.jobs ?? [])
    .filter((job) => ["queued", "running", "failed", "rejected"].includes(job.status))
    .slice(0, 3);
  if (visible.length === 0) {
    return null;
  }
  return (
    <aside className="job-tray" aria-label="Background jobs">
      <div className="job-tray-head">
        <span><Activity size={15} aria-hidden="true" /> Background activity</span>
        <Link to="/activity/jobs">View all</Link>
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
              className={selected?.video_name === row.video_name && selected?.started_at === row.started_at ? "selected-row" : ""}
              key={`${row.video_name}-${row.started_at}`}
              onClick={() => setSelected?.(row)}
            >
              <td>
                <div className="strong">{row.video_name}</div>
                <div className="muted">{row.runs} run(s), {row.redos} redo(s)</div>
              </td>
              <td><Badge value={row.status} /></td>
              <td>{row.current_step}</td>
              <td><Progress value={row.progress} /></td>
              <td>{numberText(row.clips_generated)}</td>
              {!compact && <td>{row.duration}</td>}
              {!compact && <td className="muted attention-cell">{row.attention || "Clear"}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
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
  const queue = useApiQuery<QueueDetail>("/api/queue", (data) => isQueueActive(data) ? 2_000 : 15_000, true);
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
  const activeStageMeta = operationStages.find((stage) => stage.key === activeStage) ?? operationStages[2];
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
          <div className="current-run-head">
            <h2>Current Run</h2>
            <Badge value={currentRun?.status || "Idle"} kind={currentRun ? runStatusKind(currentRun.status) : "neutral"} />
          </div>

          {currentRun ? (
            <>
              <div className="current-run-main">
                <h3>{currentRun.video_name}</h3>
                <div className="current-stage">
                  <ActiveStageIcon size={28} aria-hidden="true" />
                  <strong>{activeStageMeta.label}</strong>
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
                  <Video size={22} aria-hidden="true" />
                  <span>Clips generated</span>
                  <strong>{numberText(currentRun.clips_generated)}</strong>
                </div>
                <div className="run-meta-item wide">
                  <ListChecks size={22} aria-hidden="true" />
                  <span>Current step</span>
                  <strong>{currentRun.current_step || activeStageMeta.label}</strong>
                </div>
                <div className="run-meta-item">
                  <Clock size={22} aria-hidden="true" />
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
                    Stop Queue
                  </button>
                </div>
              </div>
            </>
          ) : (
            <div className="operation-empty current-run-empty">
              <span className="operation-empty-mark">
                <Clock size={30} aria-hidden="true" />
              </span>
              <strong>No active run</strong>
              <span>Queue activity will appear here when production starts.</span>
            </div>
          )}
        </article>

        <article className="operation-panel next-run-panel">
          <div className="next-run-head">
            <h2>Next Run</h2>
            <p>Set the next queue pass before production starts.</p>
          </div>
          <div className="next-run-options">
            <div className="next-run-control-card">
              <SegmentedControl label="Run mode" value={runMode} options={runModeOptions} onChange={setRunMode} disabled={active} />
            </div>
            {needsVod && (
              <div className="next-run-control-card wide">
                <FilterField label="VOD">
                  <select value={videoPath} disabled={active} onChange={(event) => setVideoPath(event.target.value)}>
                    <option value="">Select VOD</option>
                    {files.map((file) => (
                      <option value={file.path} key={file.path}>{file.name}</option>
                    ))}
                  </select>
                </FilterField>
              </div>
            )}
            <div className="next-run-control-card wide">
              <SegmentedControl label="Pipeline" value={pipelineMode} options={pipelineModeOptions} onChange={setPipelineMode} disabled={active} />
            </div>
            <div className="next-run-control-card">
              <SegmentedControl
                label="Variants"
                value={effectiveVariantMode}
                options={variantModeOptions}
                onChange={setVariantMode}
                disabled={active || pipelineMode === "raw_cuts_only"}
              />
            </div>
            {effectiveVariantMode === "custom" && (
              <div className="next-run-control-card compact">
                <FilterField label="Variant count">
                  <select value={variantCount} disabled={active} onChange={(event) => setVariantCount(Number.parseInt(event.target.value, 10))}>
                    {[1, 2, 3, 4, 5, 6].map((count) => (
                      <option value={count} key={count}>{count}</option>
                    ))}
                  </select>
                </FilterField>
              </div>
            )}
          </div>
          <div className="next-run-action-row">
            <div className="next-run-summary">
              <span>Ready setup</span>
              <strong>{draftSummary}</strong>
            </div>
            <FilterField label="Max clips">
              <input type="number" min={0} value={maxClips} disabled={active} onChange={(event) => setMaxClips(event.target.value)} />
            </FilterField>
            <button className="primary-button" disabled={!canStart} onClick={startQueue}>
              <Play size={16} aria-hidden="true" />
              Start Queue
            </button>
          </div>
          {active && <StateBlock kind="info" detail={summary} />}
          {needsVod && vods.error && <StateBlock kind="bad" detail={vods.error} />}
          {needsVod && !vods.loading && files.length === 0 && <StateBlock kind="warn" detail="No supported VOD files found." />}
          <ActionNotice message={message} />
        </article>
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

  return (
    <section className="page-stack operations-page">
      {dashboard.loading && <SkeletonLines count={4} />}
      {dashboard.error && <StateBlock kind="bad" title="Dashboard read failed" detail={dashboard.error} />}
      <StateBlock kind="warn" warnings={dashboard.envelope?.warnings} />
      <RunLauncher onQueueRefresh={dashboard.refresh} surface="operations" />

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
    </section>
  );
}

function QueuePage() {
  const queue = useApiQuery<QueueDetail>("/api/queue", (data) => isQueueActive(data) ? 2_000 : 15_000, true);
  const [selected, setSelected] = useState<QueueRunRow | null>(null);
  const data = queue.envelope?.data;

  return (
    <section className="page-stack">
      <PageTitle title="Queue history" detail="Inspect active, waiting, completed, and failed production runs." onRefresh={queue.refresh} />
      <RunLauncher onQueueRefresh={queue.refresh} />
      {queue.loading && <SkeletonLines count={4} />}
      {queue.error && <StateBlock kind="bad" title="Queue read failed" detail={queue.error} />}
      <StateBlock kind="warn" warnings={queue.envelope?.warnings} />
      <QueueTable rows={data?.rows ?? []} selected={selected} setSelected={setSelected} />
      <Drawer
        open={Boolean(selected)}
        title={selected?.video_name ?? "Queue run"}
        detail={selected?.current_step}
        onClose={() => setSelected(null)}
      >
        {selected && (
          <div className="detail-list">
            <DetailItem label="Status" value={<Badge value={selected.status} />} />
            <DetailItem label="Progress" value={<Progress value={selected.progress} />} />
            <DetailItem label="Video path" value={selected.video_path || "-"} />
            <DetailItem label="Output dir" value={selected.output_dir || "-"} />
            <DetailItem label="Working dir" value={selected.working_dir || "-"} />
            <DetailItem label="Started" value={selected.started_at || "-"} />
            <DetailItem label="Completed" value={selected.completed_at || "-"} />
            <DetailItem label="Attention" value={selected.attention || "Clear"} />
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
  const overview = useApiQuery<OverviewData>(
    "/api/overview",
    (data) => data?.queue_active ? 10_000 : 30_000,
    true
  );
  const summary = dashboard.envelope?.data;
  const overviewData = overview.envelope?.data;
  const complianceOverview: ComplianceOverview = {
    scanned: overviewData?.compliance.scanned ?? 0,
    passed: overviewData?.compliance.passed ?? 0,
    blocked: overviewData?.compliance.blocked ?? 0,
    rate: overviewData?.compliance.rate ?? 0
  };
  const exportOverview = buildExportOverview(overviewData?.export);
  const exportStatusLabel = exportOverview.available
    ? reviewFlagLabel(exportOverview.status || (exportOverview.dryRun ? "preflight" : "completed"))
    : "Awaiting reconciliation";
  const exportHint = !exportOverview.available
    ? "Awaiting the next packaging pass"
    : exportOverview.pending > 0
      ? `${numberText(exportOverview.pending)} clip(s) require reconciliation`
      : "Automatic batching is current";
  const productionDays = summary?.production_days?.length
    ? summary.production_days.map((point) => {
        const date = new Date(`${point.date}T12:00:00`);
        return { key: point.date, label: shortWeekday(date), count: point.clips };
      })
    : buildProductionDays(summary?.rows ?? []);
  const productionTotal = productionDays.reduce((total, day) => total + day.count, 0);
  const topRows = overviewData?.top_clips ?? [];
  const trendPoints: ScoreTrendPoint[] = (overviewData?.score_trend ?? []).map((point) => ({
    key: point.date,
    label: shortMonthDay(new Date(`${point.date}T12:00:00`)),
    average: point.average_score,
    count: point.scored_count
  }));
  const scoreAverage = overviewData?.average_score ?? undefined;
  const firstTrend = trendPoints[0]?.average;
  const latestTrend = trendPoints[trendPoints.length - 1]?.average;
  const scoreDelta = firstTrend !== undefined && latestTrend !== undefined ? latestTrend - firstTrend : undefined;
  const complianceRateText = complianceOverview.scanned > 0 ? `${complianceOverview.rate.toFixed(1)}%` : "-";
  const scoreHint = scoreDelta === undefined
    ? `${numberText(overviewData?.scored_count)} scored clips`
    : `${scoreDelta >= 0 ? "+" : "-"}${Math.abs(scoreDelta).toFixed(1)} vs chart start`;

  return (
    <section className="page-stack overview-page">
      {(dashboard.loading || overview.loading) && <SkeletonLines count={4} />}
      {dashboard.error && <StateBlock kind="bad" title="Dashboard read failed" detail={dashboard.error} />}
      {overview.error && <StateBlock kind="bad" title="Overview read failed" detail={overview.error} />}
      <StateBlock kind="warn" warnings={[...(dashboard.envelope?.warnings ?? []), ...(overview.envelope?.warnings ?? [])]} />

      <div className="overview-kpi-grid">
        <OverviewKpiCard
          label="Clips Today"
          value={numberText(summary?.clips_today ?? summary?.clips_last_24h)}
          hint={`${numberText(summary?.clips_per_hour, 1)} per hour / ${numberText(summary?.clips_last_24h)} in 24h`}
          icon={TrendingUp}
          kind="good"
        />
        <OverviewKpiCard
          label="Pending Export Packaging"
          value={exportOverview.available ? numberText(exportOverview.pending) : "—"}
          hint={exportHint}
          icon={FolderOpen}
          kind={!exportOverview.available ? "info" : exportOverview.pending > 0 || exportOverview.errorCount > 0 ? "bad" : "good"}
        />
        <OverviewKpiCard
          label="Compliance Rate"
          value={complianceRateText}
          hint={`${numberText(complianceOverview.blocked)} blocked clips`}
          icon={ShieldCheck}
          kind={complianceOverview.blocked > 0 ? "bad" : "good"}
        />
        <OverviewKpiCard
          label="Avg Score"
          value={scoreAverage === undefined ? "-" : scoreAverage.toFixed(1)}
          hint={scoreHint}
          icon={Gauge}
          kind="good"
          highlight
        />
      </div>

      <div className="overview-two-column">
        <article className="overview-panel overview-production-panel">
          <div className="overview-panel-head">
            <div>
              <h2>Last 7 Days Production</h2>
              <p>Completed clips from queue runs</p>
            </div>
          </div>
          <OverviewProductionBars days={productionDays} />
          <div className="overview-panel-footer">
            Total this week: <strong>{numberText(productionTotal)}</strong> clips
          </div>
        </article>

        <article className="overview-panel overview-export-panel">
          <div className="overview-panel-head">
            <div>
              <h2>Export Batches</h2>
              <p>Affiliate distribution packaging</p>
            </div>
            <Badge
              value={exportStatusLabel}
              kind={!exportOverview.available ? "neutral" : exportOverview.errorCount > 0 ? "bad" : exportOverview.pending > 0 ? "warn" : "good"}
            />
          </div>
          <div className="overview-export-stats">
            <OverviewStatLine label="Actionable at last pass" value={exportOverview.available ? numberText(exportOverview.actionable) : "—"} />
            <OverviewStatLine label="Moved last pass" value={exportOverview.available ? numberText(exportOverview.packagedLastRun) : "—"} />
            <OverviewStatLine label="Remaining now" value={exportOverview.available ? numberText(exportOverview.pending) : "—"} />
            <OverviewStatLine label="Cumulative assignments" value={exportOverview.available ? numberText(exportOverview.packagedTotal) : "—"} />
            <OverviewStatLine label="Batch size" value={exportOverview.batchSize > 0 ? numberText(exportOverview.batchSize) : "-"} />
          </div>
          <div className="overview-progress-caption">
            {!exportOverview.available
              ? "No operational snapshot exists yet. The next automatic pass or recovery preflight will create one."
              : `${numberText(exportOverview.packagedLastRun)} of ${numberText(exportOverview.actionable)} actionable clips handled; ${numberText(exportOverview.pending)} remain.${exportOverview.updatedAt ? ` Updated ${new Date(exportOverview.updatedAt).toLocaleString()}.` : ""}`}
          </div>
        </article>
      </div>

      <div className="overview-bottom-grid">
        <article className="overview-panel overview-top-clips-panel">
          <div className="overview-panel-head">
            <div>
              <h2>Top Scoring Clips</h2>
              <p>Highest total scores from the latest score index</p>
            </div>
          </div>
          <OverviewTopClips rows={topRows} loading={overview.loading} />
        </article>

        <article className="overview-panel overview-quality-panel">
          <div className="overview-panel-head">
            <div>
              <h2>Quality Over Time</h2>
              <p>Daily average total score</p>
            </div>
          </div>
          <OverviewQualityChart points={trendPoints} />
          <div className="overview-panel-footer">
            Average score is <strong>{scoreAverage === undefined ? "-" : scoreAverage.toFixed(1)}</strong> across {numberText(overviewData?.scored_count)} scored clips.
          </div>
        </article>
      </div>
    </section>
  );
}

function OverviewKpiCard({
  label,
  value,
  hint,
  icon: Icon,
  kind,
  highlight = false
}: {
  label: string;
  value: string;
  hint: string;
  icon: LucideIcon;
  kind: BadgeKind;
  highlight?: boolean;
}) {
  return (
    <article className={`overview-kpi-card ${kind} ${highlight ? "highlight" : ""}`}>
      <div className="overview-kpi-top">
        <span>{label}</span>
        <Icon size={28} aria-hidden="true" />
      </div>
      <strong>{value}</strong>
      <p>{hint}</p>
    </article>
  );
}

function OverviewProductionBars({ days }: { days: ProductionDay[] }) {
  const max = Math.max(1, ...days.map((day) => day.count));
  return (
    <div className="overview-bar-chart" aria-label="Last 7 days production">
      {days.map((day) => {
        const height = day.count === 0 ? 3 : Math.max(12, Math.round((day.count / max) * 100));
        return (
          <div className="overview-bar-column" key={day.key}>
            <span className="overview-bar-value">{numberText(day.count)}</span>
            <div className="overview-bar-slot">
              <span style={{ height: `${height}%` }} />
            </div>
            <span className="overview-bar-label">{day.label}</span>
          </div>
        );
      })}
    </div>
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

function OverviewTopClips({ rows, loading }: { rows: OverviewTopClip[]; loading: boolean }) {
  if (loading && rows.length === 0) {
    return <SkeletonLines count={5} />;
  }
  if (rows.length === 0) {
    return <EmptyState icon={Video} title="No scored clips yet" detail="Top performers appear after scoring completes." />;
  }
  return (
    <div className="overview-clip-list">
      <div className="overview-clip-header">
        <span />
        <span>Product</span>
        <span>Score</span>
        <span>Status</span>
        <span>Date</span>
      </div>
      {rows.map((row) => (
        <Link className="overview-clip-row" to={`/review/clips?score=${encodeURIComponent(row.score_key)}`} key={row.score_key}>
          <OverviewClipThumb row={row} />
          <div className="overview-clip-product">
            <strong>{row.product || "Product"}</strong>
            <span>{row.clip_id || row.source_video}</span>
          </div>
          <span className="overview-score-pill">{scoreText(row.total_score)}</span>
          <span>{row.status || "scored"}</span>
          <time>{shortDateText(row.scored_at || row.source_date)}</time>
        </Link>
      ))}
    </div>
  );
}

function OverviewClipThumb({ row }: { row: OverviewTopClip }) {
  const artifact = row.artifact;
  const productInitial = (row.product || "C").trim().slice(0, 1).toUpperCase() || "C";
  if (artifact?.exists && artifact.kind === "image") {
    return <img className="overview-clip-thumb" src={artifact.url} alt={row.product || "Clip thumbnail"} />;
  }
  if (artifact?.exists && artifact.kind === "video") {
    return <video className="overview-clip-thumb" src={artifact.url} muted playsInline preload="metadata" aria-label={row.product || "Clip preview"} />;
  }
  return <span className="overview-clip-thumb overview-clip-fallback">{productInitial}</span>;
}

function OverviewQualityChart({ points }: { points: ScoreTrendPoint[] }) {
  if (points.length < 2) {
    return <EmptyState icon={TrendingUp} title="No score trend yet" detail="At least two scored days are needed for this chart." />;
  }

  const width = 680;
  const height = 240;
  const padding = { top: 18, right: 18, bottom: 28, left: 42 };
  const scores = points.map((point) => point.average);
  const minScore = Math.max(0, Math.min(...scores) - 0.4);
  const maxScore = Math.min(10, Math.max(...scores) + 0.4);
  const range = Math.max(1, maxScore - minScore);
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const xFor = (index: number) => padding.left + (plotWidth * index) / Math.max(1, points.length - 1);
  const yFor = (score: number) => padding.top + plotHeight - ((score - minScore) / range) * plotHeight;
  const linePath = points.map((point, index) => `${index === 0 ? "M" : "L"} ${xFor(index).toFixed(1)} ${yFor(point.average).toFixed(1)}`).join(" ");
  const areaPath = `${linePath} L ${xFor(points.length - 1).toFixed(1)} ${height - padding.bottom} L ${padding.left} ${height - padding.bottom} Z`;
  const ticks = [maxScore, (maxScore + minScore) / 2, minScore];
  const labelPoints = [points[0], points[Math.floor(points.length / 2)], points[points.length - 1]];

  return (
    <div className="overview-quality-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Quality score trend">
        <defs>
          <linearGradient id="overviewQualityFill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity="0.28" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0.02" />
          </linearGradient>
        </defs>
        {ticks.map((tick) => (
          <Fragment key={tick}>
            <line className="overview-chart-grid" x1={padding.left} x2={width - padding.right} y1={yFor(tick)} y2={yFor(tick)} />
            <text className="overview-chart-tick" x={4} y={yFor(tick) + 4}>{tick.toFixed(1)}</text>
          </Fragment>
        ))}
        <path className="overview-chart-area" d={areaPath} />
        <path className="overview-chart-line" d={linePath} />
        {points.map((point, index) => (
          <circle className="overview-chart-point" cx={xFor(index)} cy={yFor(point.average)} r={index === points.length - 1 ? 4 : 2.6} key={point.key} />
        ))}
      </svg>
      <div className="overview-chart-labels">
        {labelPoints.map((point) => <span key={point.key}>{point.label}</span>)}
      </div>
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
          <span><RotateCcw size={16} aria-hidden="true" /> Rescore an output directory</span>
          <small>Select a score row to prefill the path.</small>
        </summary>
        <div className="review-rescore-content">
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
          <h2>Score index</h2>
          <p>{numberText(total)} rows available, showing {numberText(groups.length)} main clips from {reviewRangeText(total, limit, offset, rows.length)}.</p>
        </div>
      </div>
      <div className="index-toolbar review-index-toolbar">
        <SearchInput className="review-index-search" ariaLabel="Search score index" value={search} onChange={setSearch} placeholder="Search clips, products, sources..." />
        <IndexSelect label="Filter score status" icon={ShieldCheck} value={status} onChange={setStatus}>
          <option value="">All statuses</option>
          {["Strong", "Okay", "Review", "Blocked"].map((item) => <option value={item} key={item}>{item}</option>)}
        </IndexSelect>
        <IndexSelect label="Filter score product" icon={PackageCheck} value={product} onChange={setProduct}>
          <option value="">All products</option>
          {productOptions.map((item) => <option value={item} key={item}>{item}</option>)}
        </IndexSelect>
        <IndexSelect label="Sort score index" icon={SlidersHorizontal} value={sort} onChange={setSort}>
          <option value="scored_at">Scored time</option>
          <option value="total_score">Total score</option>
          <option value="quality_score">Quality score</option>
          <option value="similarity_score">Similarity score</option>
          <option value="product">Product</option>
          <option value="status">Status</option>
        </IndexSelect>
        <SortDirectionButton direction={direction} onToggle={() => setDirection(direction === "desc" ? "asc" : "desc")} />
        <button className="icon-button toolbar-icon-button" type="button" onClick={onRefresh} aria-label="Refresh score index" title="Refresh score index">
          <RefreshCw size={16} aria-hidden="true" />
        </button>
      </div>
      {rows.length === 0 ? (
        <EmptyState icon={Video} title="No scored clips" detail="Score summaries will appear after clips are rendered and scored." />
      ) : (
        <div className="table-wrap review-score-table-wrap">
          <table className="review-score-table">
            <thead>
              <tr>
                <th>Clip</th>
                <th>Status</th>
                <th>Product</th>
                <th>Total</th>
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
                      <td className="muted">{row.scored_at || "-"}</td>
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
                        <td className="muted">{variant.scored_at || "-"}</td>
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
    <div className="review-pagination" aria-label="Score index pagination">
      <div className="review-page-size">
        <span>Rows per page</span>
        <strong>{numberText(limit)}</strong>
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
                <span>{selected.scored_at ? `Scored ${selected.scored_at}` : "Not scored yet"}</span>
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

function ModulesPage({ active }: { active: boolean }) {
  const limit = 50;
  const readiness = useApiQuery<ModuleReadiness>("/api/modules/readiness", 10_000, active);
  const [search, setSearch] = useState("");
  const [qualityStatus, setQualityStatus] = useState("");
  const [reviewFilter, setReviewFilter] = useState("");
  const [visualStatus, setVisualStatus] = useState("");
  const [product, setProduct] = useState("");
  const [sort, setSort] = useState("product");
  const [direction, setDirection] = useState<SortDirection>("asc");
  const [offset, setOffset] = useState(0);
  const debouncedSearch = useDebouncedValue(search, 300);
  const libraryPath = `/api/modules/library${query({
    limit,
    offset,
    search: debouncedSearch,
    quality_status: qualityStatus,
    review_status: reviewFilter,
    visual_status: visualStatus,
    product,
    sort,
    direction
  })}`;
  const library = useApiQuery<ModuleLibraryPage>(libraryPath, 10_000, active);
  const [assemblyOpen, setAssemblyOpen] = useState(false);
  const [assemblyLimit, setAssemblyLimit] = useState("");
  const [assemblyProduct, setAssemblyProduct] = useState("");
  const [assemblyZoom, setAssemblyZoom] = useState(false);
  const [assemblyConfirmOpen, setAssemblyConfirmOpen] = useState(false);
  const [selectedModule, setSelectedModule] = useState<ModuleLibraryRow | null>(null);
  const [reviewStatus, setReviewStatus] = useState("approved");
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<ActionMessage>();
  const moduleDetail = useApiQuery<ModuleDetail>(
    `/api/modules/${encodeURIComponent(selectedModule?.module_id ?? "")}`,
    false,
    active && Boolean(selectedModule)
  );

  useEffect(() => {
    setOffset(0);
  }, [search, qualityStatus, reviewFilter, visualStatus, product, sort, direction]);

  useEffect(() => {
    if (selectedModule) {
      setReviewStatus(selectedModule.review_status || "approved");
      setNote("");
    }
  }, [selectedModule?.module_id]);

  const libraryData = library.envelope?.data;
  const rows = libraryData?.rows ?? [];
  const selectedModuleWithDetail = selectedModule ? {
    ...selectedModule,
    ...(moduleDetail.envelope?.data.selected ?? {}),
    transcript_text: moduleDetail.envelope?.data.transcript_text ?? selectedModule.transcript_text ?? ""
  } : null;
  const readyProducts = readiness.envelope?.data.rows.filter((row) => row.readiness === "ready") ?? [];
  const productOptions = libraryData?.filter_options.product ?? uniqueOptions(rows.map((row) => row.product_key || row.product));
  const qualityOptions = libraryData?.filter_options.quality_status ?? [];
  const reviewOptions = libraryData?.filter_options.review_status ?? [];
  const visualOptions = libraryData?.filter_options.visual_validation_status ?? [];

  function refreshAll() {
    readiness.refresh();
    library.refresh();
    if (selectedModule) {
      moduleDetail.refresh();
    }
  }

  function openAssembly(productKey?: string) {
    setAssemblyProduct(productKey ?? "");
    setAssemblyOpen(true);
  }

  function submitAssembly() {
    const limitValue = assemblyLimit ? Number(assemblyLimit) : null;
    void submitMutation(
      () => sendJson<ControlJob>("POST", "/api/operations/module-assembly", {
        product: assemblyProduct || null,
        module_assembly_limit: limitValue,
        module_product_zoom: assemblyZoom
      }),
      setMessage,
      refreshJobQueries,
      [refreshAll]
    );
  }

  function submitReview() {
    if (!selectedModule) {
      return;
    }
    void submitMutation(
      () => sendJson<ControlJob>("POST", `/api/modules/${encodeURIComponent(selectedModule.module_id)}/review`, {
        status: reviewStatus,
        note
      }),
      setMessage,
      refreshJobQueries,
      [refreshAll]
    );
  }

  return (
    <section className="page-stack">
      <PageTitle title="Modules" detail="Readiness, inventory, assembly, and module review in one workspace." onRefresh={refreshAll}>
        <button className="primary-button" disabled={readyProducts.length === 0} onClick={() => openAssembly()}>
          <Archive size={16} aria-hidden="true" />
          Assemble
        </button>
      </PageTitle>
      <ActionNotice message={message} />
      <StateBlock kind="warn" warnings={[...(readiness.envelope?.warnings ?? []), ...(library.envelope?.warnings ?? [])]} />
      {(readiness.loading || library.loading) && <SkeletonLines count={4} />}
      {(readiness.error || library.error) && <StateBlock kind="bad" title="Module read failed" detail={readiness.error || library.error} />}

      <div className="module-grid">
        {(readiness.envelope?.data.rows ?? []).map((row) => (
          <ReadinessCard row={row} key={row.product_key} onAssemble={() => openAssembly(row.product_key)} />
        ))}
      </div>

      <div className="index-toolbar">
        <SearchInput value={search} onChange={setSearch} placeholder="Search modules, transcripts, sources..." />
        <FilterField label="Product">
          <select value={product} onChange={(event) => setProduct(event.target.value)}>
            <option value="">All products</option>
            {productOptions.map((item) => <option value={item} key={item}>{item}</option>)}
          </select>
        </FilterField>
        <FilterField label="Quality">
          <select value={qualityStatus} onChange={(event) => setQualityStatus(event.target.value)}>
            <option value="">All quality states</option>
            {qualityOptions.map((item) => <option value={item} key={item}>{item}</option>)}
          </select>
        </FilterField>
        <FilterField label="Review">
          <select value={reviewFilter} onChange={(event) => setReviewFilter(event.target.value)}>
            <option value="">All review states</option>
            {reviewOptions.map((item) => <option value={item} key={item}>{item}</option>)}
          </select>
        </FilterField>
        <FilterField label="Visual">
          <select value={visualStatus} onChange={(event) => setVisualStatus(event.target.value)}>
            <option value="">All visual states</option>
            {visualOptions.map((item) => <option value={item} key={item}>{item}</option>)}
          </select>
        </FilterField>
        <FilterField label="Sort">
          <select value={sort} onChange={(event) => setSort(event.target.value)}>
            <option value="product">Product</option>
            <option value="source_date">Source date</option>
            <option value="duration">Duration</option>
            <option value="confidence">Confidence</option>
            <option value="role">Role</option>
            <option value="status">Status</option>
          </select>
        </FilterField>
        <FilterField label="Direction">
          <select value={direction} onChange={(event) => setDirection(event.target.value as SortDirection)}>
            <option value="asc">Ascending</option>
            <option value="desc">Descending</option>
          </select>
        </FilterField>
      </div>

      <ModuleLibraryTable page={libraryData} selected={selectedModule?.module_id ?? ""} setSelected={setSelectedModule} />
      <Pagination total={libraryData?.total ?? 0} limit={limit} offset={offset} setOffset={setOffset} />

      <Drawer open={assemblyOpen} title="Assemble modules" detail="Build reusable clips from ready module inventory." onClose={() => setAssemblyOpen(false)}>
        <div className="detail-list">
          <DetailItem label="Ready products" value={readyProducts.length ? readyProducts.map((row) => row.product).join(", ") : "None ready"} />
        </div>
        <div className="form-stack">
          <FilterField label="Product">
            <select value={assemblyProduct} onChange={(event) => setAssemblyProduct(event.target.value)}>
              <option value="">All ready products</option>
              {readyProducts.map((row) => <option value={row.product_key} key={row.product_key}>{row.product}</option>)}
            </select>
          </FilterField>
          <FilterField label="Limit">
            <input value={assemblyLimit} onChange={(event) => setAssemblyLimit(event.target.value)} placeholder="optional" inputMode="numeric" />
          </FilterField>
          <label className="confirm-check">
            <input type="checkbox" checked={assemblyZoom} onChange={(event) => setAssemblyZoom(event.target.checked)} />
            Product zoom
          </label>
          <button className="primary-button" disabled={readyProducts.length === 0} onClick={() => setAssemblyConfirmOpen(true)}>
            <Archive size={16} aria-hidden="true" />
            Create assembly job
          </button>
        </div>
      </Drawer>

      <ModuleDetailDrawer
        module={selectedModuleWithDetail}
        detailLoading={moduleDetail.loading && Boolean(selectedModule)}
        detailError={moduleDetail.error}
        reviewStatus={reviewStatus}
        setReviewStatus={setReviewStatus}
        note={note}
        setNote={setNote}
        onSubmit={submitReview}
        onClose={() => setSelectedModule(null)}
      />
      <ConfirmDialog
        open={assemblyConfirmOpen}
        title="Assemble ready modules?"
        detail={`Create an assembly job for ${assemblyProduct || "all ready products"}${assemblyLimit ? ` with a limit of ${assemblyLimit}` : ""}.`}
        confirmLabel="Create assembly job"
        onClose={() => setAssemblyConfirmOpen(false)}
        onConfirm={() => {
          setAssemblyConfirmOpen(false);
          setAssemblyOpen(false);
          submitAssembly();
        }}
      />
    </section>
  );
}

function ReadinessCard({ row, onAssemble }: { row: ModuleReadinessRow; onAssemble: () => void }) {
  return (
    <article className="module-card">
      <div className="panel-head">
        <div>
          <h3>{row.product}</h3>
          <p>{row.total} text modules, {row.visual_total} visual records</p>
        </div>
        <Badge value={row.readiness} />
      </div>
      <div className="stage-counts">
        <span>Hook {row.hook}</span>
        <span>Main {row.main}</span>
        <span>CTA {row.cta}</span>
        <span>Zoom {row.zoom_ready_candidates}</span>
      </div>
      <div className="module-visual-summary">
        <span className="good">{row.visual_passed} visual passed</span>
        <span className={row.visual_failed ? "bad" : "muted"}>{row.visual_failed} failed</span>
        <span className="muted">{row.visual_not_run} not run</span>
      </div>
      <button className="secondary-button module-action" disabled={row.readiness !== "ready"} onClick={onAssemble}>
        <Archive size={16} aria-hidden="true" />
        Assemble
      </button>
    </article>
  );
}

function ModuleLibraryTable({
  page,
  selected,
  setSelected
}: {
  page?: ModuleLibraryPage;
  selected: string;
  setSelected: (row: ModuleLibraryRow) => void;
}) {
  const rows = page?.rows ?? [];
  if (rows.length === 0) {
    return <EmptyState icon={Library} title="No modules indexed" detail="Module inventory will appear after extraction and indexing." />;
  }
  return (
    <article className="panel">
      <div className="panel-head">
        <div>
          <h2>Library inventory</h2>
          <p>{numberText(page?.total)} modules indexed.</p>
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Module</th>
              <th>Product</th>
              <th>Role</th>
              <th>Duration</th>
              <th>Quality</th>
              <th>Review</th>
              <th>Visual</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr className={selected === row.module_id ? "selected-row" : ""} key={row.module_id} onClick={() => setSelected(row)}>
                <td>
                  <div className="strong">{row.module_id}</div>
                  <div className="muted">{row.source_video}</div>
                </td>
                <td>{row.product}</td>
                <td>{row.role}</td>
                <td>{row.duration.toFixed(1)}s</td>
                <td>{row.quality_status || "-"}</td>
                <td>{row.review_status || "-"}</td>
                <td>{row.visual_validation_status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </article>
  );
}

function ModuleDetailDrawer({
  module,
  detailLoading,
  detailError,
  reviewStatus,
  setReviewStatus,
  note,
  setNote,
  onSubmit,
  onClose
}: {
  module: ModuleLibraryRow | null;
  detailLoading: boolean;
  detailError?: string;
  reviewStatus: string;
  setReviewStatus: (value: string) => void;
  note: string;
  setNote: (value: string) => void;
  onSubmit: () => void;
  onClose: () => void;
}) {
  const [blockDialogOpen, setBlockDialogOpen] = useState(false);
  return (
    <>
      <Drawer open={Boolean(module)} title={module?.module_id ?? "Module detail"} detail={module?.product} onClose={onClose}>
        {module && (
          <>
          {module.file_artifact?.exists && (
            <a className="secondary-button full-width" href={module.file_artifact.url} target="_blank" rel="noreferrer">
              <Eye size={16} aria-hidden="true" />
              Open artifact
            </a>
          )}
          <div className="detail-grid">
            <MetricCard label="Role" value={module.role || "-"} hint={module.product} icon={Layers3} />
            <MetricCard label="Duration" value={`${module.duration.toFixed(1)}s`} hint="Module length" icon={Clock} />
            <MetricCard label="Confidence" value={scoreText(module.confidence)} hint="Extraction confidence" icon={Gauge} />
            <MetricCard label="Visual hits" value={numberText(module.visual_product_hits)} hint={module.visual_validation_status} icon={Eye} />
          </div>
          <div className="detail-list">
            <DetailItem label="Boundary mode" value={module.boundary_mode || "-"} />
            <DetailItem label="Visual confidence" value={module.visual_product_confidence_max == null ? "-" : module.visual_product_confidence_max.toFixed(2)} />
            <DetailItem label="Validation reason" value={module.visual_validation_reason || "No validation reason recorded."} />
            <DetailItem label="Source date" value={module.source_date || "-"} />
          </div>
          <section className="drawer-section">
            <h3>Transcript</h3>
            {detailLoading && <SkeletonLines count={2} />}
            {detailError && <StateBlock kind="bad" title="Module detail failed" detail={detailError} />}
            {!detailLoading && !detailError && <p className="transcript-box">{module.transcript_text || "No transcript text available."}</p>}
          </section>
          <section className="drawer-section">
            <h3>Review action</h3>
            <div className="form-stack">
              <FilterField label="Status">
                <select value={reviewStatus} onChange={(event) => setReviewStatus(event.target.value)}>
                  <option value="approved">Approve</option>
                  <option value="needs_review">Needs review</option>
                  <option value="blocked">Block</option>
                </select>
              </FilterField>
              <FilterField label="Note">
                <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="optional" />
              </FilterField>
              <button
                className="primary-button"
                onClick={() => reviewStatus === "blocked" ? setBlockDialogOpen(true) : onSubmit()}
              >
                <BadgeCheck size={16} aria-hidden="true" />
                Submit review
              </button>
            </div>
          </section>
          </>
        )}
      </Drawer>
      <ConfirmDialog
        open={blockDialogOpen}
        title="Block this module?"
        detail={`${module?.module_id ?? "This module"} will be excluded from approved assembly workflows.`}
        confirmLabel="Block module"
        danger
        onClose={() => setBlockDialogOpen(false)}
        onConfirm={() => {
          setBlockDialogOpen(false);
          onSubmit();
        }}
      />
    </>
  );
}

function ExportsPage() {
  const [outputRoot, setOutputRoot] = useState("");
  const [batchSize, setBatchSize] = useState("");
  const [dryRun, setDryRun] = useState(true);
  const [packagingConfirmOpen, setPackagingConfirmOpen] = useState(false);
  const [message, setMessage] = useState<ActionMessage>();
  const overview = useApiQuery<OverviewData>("/api/overview", 30_000, true);
  const exportHistory = useApiQuery<ControlJobPage>("/api/control/jobs?limit=100&operation=export_batches", jobPollingInterval, true);
  const exportJobs = exportHistory.envelope?.data.jobs ?? [];
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
        }}
      />
      {overview.error && <StateBlock kind="bad" title="Automatic packaging status failed" detail={overview.error} />}
      {exportHistory.error && <StateBlock kind="bad" title="Delivery history failed" detail={exportHistory.error} />}
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
        <div className="overview-export-stats delivery-status-stats">
          <OverviewStatLine label="Actionable at last pass" value={exportOverview.available ? numberText(exportOverview.actionable) : "—"} />
          <OverviewStatLine label="Moved last pass" value={exportOverview.available ? numberText(exportOverview.packagedLastRun) : "—"} />
          <OverviewStatLine label="Remaining now" value={exportOverview.available ? numberText(exportOverview.pending) : "—"} />
          <OverviewStatLine label="Cumulative assignments" value={exportOverview.available ? numberText(exportOverview.packagedTotal) : "—"} />
          <OverviewStatLine label="Last update" value={exportOverview.updatedAt ? new Date(exportOverview.updatedAt).toLocaleString() : "—"} />
        </div>
        {!exportOverview.available && (
          <p className="muted-copy">The next automatic packaging pass or a recovery preflight will create the first operational snapshot.</p>
        )}
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

const zoomSteps: Array<VariationVariant["zoom_intensity"]> = ["none", "subtle", "normal", "strong"];
const fallbackSubtitleSizes: Array<VariationVariant["subtitle_size"]> = ["small", "medium", "large"];

function VariationsPage({ active }: { active: boolean }) {
  const variations = useApiQuery<VariationPageData>("/api/variations", 30_000, active);
  const data = variations.envelope?.data;
  const normalizedServerProfile = useMemo(
    () => data?.profile ? normalizeUiProfile(data.profile) : null,
    [data?.profile]
  );
  const [draft, setDraft] = useState<VariationProfile | null>(null);
  const [openVariant, setOpenVariant] = useState(0);
  const [selectedPreviewIndex, setSelectedPreviewIndex] = useState(0);
  const [message, setMessage] = useState<ActionMessage>();
  const [presetName, setPresetName] = useState("");
  const [selectedPreset, setSelectedPreset] = useState("");
  const [previewProduct, setPreviewProduct] = useState("");
  const [busy, setBusy] = useState("");
  const [renderedPreview, setRenderedPreview] = useState<VariationPreviewResult>();
  const previewFrameRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (normalizedServerProfile) {
      setDraft(copyProfile(normalizedServerProfile));
      setSelectedPreviewIndex(0);
    }
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
  }, [data?.product_broll?.root, data?.product_broll?.products.length, previewProduct]);

  const dirty = Boolean(draft && normalizedServerProfile && JSON.stringify(draft) !== JSON.stringify(normalizedServerProfile));
  const visibleVariants = draft?.variants.slice(0, draft.variant_count) ?? [];
  const limits = data?.limits ?? { min_variants: 1, max_variants: 6 };
  const previewIndex = Math.max(0, Math.min(selectedPreviewIndex, Math.max(0, visibleVariants.length - 1)));
  const previewVariant = visibleVariants[previewIndex];
  const visualModes = data?.visual_modes ?? ["host", "broll_audio"];
  const subtitleSizeOptions = data?.subtitle_sizes ?? fallbackSubtitleSizes;
  const beforeAfterModes: Array<VariationVariant["before_after_mode"]> = ["fullscreen"];
  const featureFlags = data?.global_feature_flags ?? {
    sfx: true,
    bgm: true,
    before_after: true,
    broll_intro: true,
    transitional_hook: true,
    host_face_zoom: true
  };
  const disabledFeatureLabels = [
    !featureFlags.sfx && "SFX",
    !featureFlags.bgm && "BGM",
    !featureFlags.before_after && "before/after images",
    !featureFlags.broll_intro && "B-roll hooks",
    !featureFlags.transitional_hook && "transitional hooks"
  ].filter(Boolean) as string[];
  const brollPreviewProducts = data?.product_broll?.products ?? [];
  const selectedBrollProduct = brollPreviewProducts.find((item) => item.product_key === previewProduct) ?? brollPreviewProducts[0];

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

  function updateDraft(next: VariationProfile) {
    setDraft(next);
    setRenderedPreview(undefined);
  }

  function updateVariant(index: number, patch: Partial<VariationVariant>) {
    if (!draft) {
      return;
    }
    const safePatch = patch.visual_mode === "broll_audio"
      ? { ...patch, random_broll_enabled: false }
      : patch;
    const variants = draft.variants.map((variant, itemIndex) => itemIndex === index ? { ...variant, ...safePatch } : variant);
    updateDraft({ ...draft, variants });
  }

  function selectPreviewVariant(index: number) {
    const nextIndex = Math.max(0, Math.min(index, Math.max(0, visibleVariants.length - 1)));
    setSelectedPreviewIndex(nextIndex);
    setOpenVariant(nextIndex);
    setRenderedPreview(undefined);
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

  function moveSubtitleFromPointer(event: ReactPointerEvent<HTMLElement>) {
    if (!previewVariant) {
      return;
    }
    const rect = previewFrameRef.current?.getBoundingClientRect();
    if (!rect || rect.height <= 0) {
      return;
    }
    updateSubtitleY(previewIndex, (event.clientY - rect.top) / rect.height);
  }

  function startSubtitleDrag(event: ReactPointerEvent<HTMLButtonElement>) {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    moveSubtitleFromPointer(event);
  }

  function updateVariantCount(value: number) {
    if (!draft) {
      return;
    }
    const count = Math.max(limits.min_variants, Math.min(limits.max_variants, value));
    const variants = [...draft.variants];
    while (variants.length < count) {
      variants.push(createUiVariant(variants.length, variants[variants.length - 1]));
    }
    updateDraft({ ...draft, variant_count: count, variants });
    setOpenVariant(Math.min(openVariant, count - 1));
    setSelectedPreviewIndex(Math.min(selectedPreviewIndex, count - 1));
  }

  async function saveProfile() {
    if (!draft || !data?.profile) {
      return;
    }
    setBusy("save");
    try {
      const envelope = await sendJson<VariationPageData>("PUT", "/api/variations", {
        profile: draft,
        expected_revision: data.profile.revision
      });
      setDraft(normalizeUiProfile(envelope.data.profile));
      setMessage({ kind: "good", text: "Variation profile saved for future renders." });
      variations.refresh();
    } catch (caught) {
      setMessage({ kind: "bad", text: caught instanceof Error ? caught.message : String(caught) });
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
      setMessage({ kind: "good", text: "Preset saved." });
      variations.refresh();
    } catch (caught) {
      setMessage({ kind: "bad", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      setBusy("");
    }
  }

  async function loadPreset() {
    if (!selectedPreset) {
      return;
    }
    setBusy("load");
    try {
      const envelope = await getJson<VariationProfile>(`/api/variations/presets/${encodeURIComponent(selectedPreset)}`);
      setDraft(normalizeUiProfile(envelope.data));
      setSelectedPreviewIndex(0);
      setMessage({ kind: "info", text: "Preset loaded into the editor. Save to apply it." });
    } catch (caught) {
      setMessage({ kind: "bad", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      setBusy("");
    }
  }

  async function renderPreview() {
    if (!draft || !previewVariant) {
      return;
    }
    setBusy("preview");
    try {
      const envelope = await sendJson<VariationPreviewResult>("POST", "/api/variations/previews", {
        profile: draft,
        variant_index: previewIndex
      });
      setRenderedPreview(envelope.data);
      setMessage({
        kind: envelope.data.previews.length ? "good" : "warn",
        text: envelope.data.message || (envelope.data.previews.length ? "Rendered preview ready." : "No rendered preview was produced.")
      });
    } catch (caught) {
      setMessage({ kind: "bad", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="page-stack">
      <PageTitle title="Variants" detail="Configure and preview global clip variants before the next render." onRefresh={variations.refresh}>
        <button className="primary-button" disabled={!draft || !dirty || busy === "save"} onClick={saveProfile}>
          <CheckCircle2 size={16} aria-hidden="true" />
          {busy === "save" ? "Saving" : "Apply to future clips"}
        </button>
      </PageTitle>
      {variations.loading && <SkeletonLines count={5} />}
      {variations.error && <StateBlock kind="bad" title="Variation profile read failed" detail={variations.error} />}
      <StateBlock kind="warn" warnings={variations.envelope?.warnings} />
      {disabledFeatureLabels.length > 0 && (
        <StateBlock
          kind="warn"
          title="Some variant features are globally disabled"
          detail={`${disabledFeatureLabels.join(", ")} cannot be enabled by an individual variant. Saved choices are preserved and will become active again when the global setting is enabled.`}
        />
      )}
      <ActionNotice message={message} />
      {draft && data && (
        <div className="variation-layout">
          <article className="panel variation-editor">
            <div className="panel-head">
              <div>
                <h2>Profile</h2>
                <p>Revision {data.profile.revision ? data.profile.revision.slice(0, 12) : "new"}.</p>
              </div>
              <Badge value={dirty ? "Unsaved" : "Saved"} kind={dirty ? "warn" : "good"} />
            </div>
            <div className="variation-top-controls">
              <FilterField label="Number of variants">
                <input
                  type="number"
                  min={limits.min_variants}
                  max={limits.max_variants}
                  value={draft.variant_count}
                  onChange={(event) => updateVariantCount(Number.parseInt(event.target.value || "1", 10))}
                />
              </FilterField>
              <span className="variation-count-note">({limits.min_variants}-{limits.max_variants})</span>
            </div>
            <div className="variant-accordion">
              {visibleVariants.map((variant, index) => (
                <section className={`variant-editor-row ${openVariant === index ? "open" : ""}`} key={`variant-editor-${index}`}>
                  <button className="variant-row-head" onClick={() => setOpenVariant(openVariant === index ? -1 : index)}>
                    <span className="variant-index">V{index + 1}</span>
                    <strong>{variant.name || `Variant ${index + 1}`}</strong>
                    {variant.visual_mode === "broll_audio" && <span className="visual-mode-chip">B-roll visual</span>}
                    {variant.mirror_enabled && <span className="flip-chip">Flipped</span>}
                    {usesBeforeAfterImage(variant) && <span className="before-after-chip">{variationLabel(variant.before_after_mode)}</span>}
                    {variant.letterbox_enabled && <span className="letterbox-chip">Letterbox</span>}
                    <ChevronRight size={16} aria-hidden="true" />
                  </button>
                  {openVariant === index && (
                    <div className="variant-row-body">
                      <FilterField label="Name">
                        <input value={variant.name} onChange={(event) => updateVariant(index, { name: event.target.value })} />
                      </FilterField>
                      <FilterField label="Hook type">
                        <select value={variant.hook_type} onChange={(event) => updateVariant(index, { hook_type: event.target.value })}>
                          {data.hook_types.map((item) => (
                            <option value={item} key={item} disabled={!hookTypeAvailable(item, featureFlags)}>
                              {variationLabel(item)}{hookTypeAvailable(item, featureFlags) ? "" : " (globally disabled)"}
                            </option>
                          ))}
                        </select>
                      </FilterField>
                      <SegmentedField
                        label="Visual"
                        value={variant.visual_mode ?? "host"}
                        options={visualModes}
                        onChange={(value) => updateVariant(index, { visual_mode: value as VariationVariant["visual_mode"] })}
                      />
                      <ToggleField label="Flip video" checked={variant.mirror_enabled} onChange={(value) => updateVariant(index, { mirror_enabled: value })} />
                      <FilterField label="Before/After image">
                        <select
                          value={variant.before_after_mode}
                          disabled={!usesBeforeAfterImage(variant)}
                          onChange={(event) => updateVariant(index, { before_after_mode: event.target.value as VariationVariant["before_after_mode"] })}
                        >
                          {beforeAfterModes.map((item) => <option value={item} key={item}>{variationLabel(item)}</option>)}
                        </select>
                      </FilterField>
                      <FilterField label="Font">
                        <select value={variant.font_id} onChange={(event) => updateVariant(index, { font_id: event.target.value })}>
                          {data.fonts.map((font) => <option value={font.id ?? font.path ?? ""} key={font.id ?? font.path}>{font.label}</option>)}
                        </select>
                      </FilterField>
                      <ColorField label="Font color" value={variant.font_color} onChange={(value) => updateVariant(index, { font_color: value })} />
                      <ColorField label="Highlight color" value={variant.highlight_color} onChange={(value) => updateVariant(index, { highlight_color: value })} />
                      <ToggleField label="Subtitles" checked={variant.subtitle_enabled} onChange={(value) => updateVariant(index, { subtitle_enabled: value })} />
                      <SegmentedField label="Subtitle placement" value={variant.subtitle_position} options={data.subtitle_positions} disabled={!variant.subtitle_enabled} onChange={(value) => {
                        const subtitle_position = value as VariationVariant["subtitle_position"];
                        updateVariant(index, {
                          subtitle_position,
                          subtitle_y_frac: subtitleYDefault(subtitle_position)
                        });
                      }} />
                      <SegmentedField
                        label="Subtitle size"
                        value={variant.subtitle_size}
                        options={subtitleSizeOptions}
                        disabled={!variant.subtitle_enabled}
                        onChange={(value) => updateVariant(index, { subtitle_size: value as VariationVariant["subtitle_size"] })}
                      />
                      <FilterField label="Color grade">
                        <select value={variant.color_grade} onChange={(event) => updateVariant(index, { color_grade: event.target.value })}>
                          {data.color_grades.map((item) => <option value={item} key={item}>{variationLabel(item)}</option>)}
                        </select>
                      </FilterField>
                      <FilterField label="BGM">
                        <select disabled={!featureFlags.bgm} value={variant.bgm_mode === "selected" ? variant.bgm_path : variant.bgm_mode} onChange={(event) => {
                          const value = event.target.value;
                          if (value === "auto" || value === "none") {
                            updateVariant(index, { bgm_mode: value, bgm_path: "" });
                          } else {
                            updateVariant(index, { bgm_mode: "selected", bgm_path: value });
                          }
                        }}>
                          <option value="auto">Auto from folder</option>
                          <option value="none">No BGM</option>
                          {data.bgm_tracks.map((track) => <option value={track.path ?? ""} key={track.path}>{track.label}</option>)}
                        </select>
                      </FilterField>
                      <ToggleField label="SFX" checked={variant.sfx_enabled} disabled={!featureFlags.sfx} onChange={(value) => updateVariant(index, { sfx_enabled: value })} />
                      <ToggleField
                        label="Random relevant B-roll"
                        checked={variant.random_broll_enabled}
                        disabled={variant.visual_mode === "broll_audio"}
                        onChange={(value) => updateVariant(index, { random_broll_enabled: value })}
                      />
                      <ToggleField
                        label="Product zoom"
                        checked={variant.product_zoom_enabled}
                        disabled={variant.visual_mode === "broll_audio"}
                        onChange={(value) => updateVariant(index, { product_zoom_enabled: value })}
                      />
                      <ZoomField
                        value={variant.zoom_intensity}
                        disabled={variant.visual_mode === "broll_audio"}
                        onChange={(value) => updateVariant(index, { zoom_intensity: value })}
                      />
                      <ToggleField label="Black bars" checked={variant.letterbox_enabled} onChange={(value) => updateLetterboxEnabled(index, value)} />
                      <div className={`letterbox-hook-settings ${!variant.letterbox_enabled ? "control-disabled" : ""}`}>
                        <ToggleField
                          label="Auto top bar hook"
                          checked={variant.letterbox_hook_enabled}
                          disabled={!variant.letterbox_enabled}
                          onChange={(value) => updateLetterboxHookEnabled(index, value)}
                        />
                        <FilterField label="Hook font">
                          <select
                            value={variant.letterbox_hook_font_id || variant.font_id}
                            disabled={!variant.letterbox_enabled || !variant.letterbox_hook_enabled}
                            onChange={(event) => updateVariant(index, { letterbox_hook_font_id: event.target.value })}
                          >
                            {data.fonts.map((font) => <option value={font.id ?? font.path ?? ""} key={font.id ?? font.path}>{font.label}</option>)}
                          </select>
                        </FilterField>
                        <ColorField
                          label="Hook color"
                          value={variant.letterbox_hook_font_color}
                          disabled={!variant.letterbox_enabled || !variant.letterbox_hook_enabled}
                          onChange={(value) => updateVariant(index, { letterbox_hook_font_color: value })}
                        />
                        <NumberControl
                          label="Hook size"
                          value={variant.letterbox_hook_font_size}
                          min={24}
                          max={160}
                          unit="px"
                          disabled={!variant.letterbox_enabled || !variant.letterbox_hook_enabled}
                          onChange={(value) => updateVariant(index, { letterbox_hook_font_size: value })}
                        />
                        <PercentControl
                          label="Hook X"
                          value={variant.letterbox_hook_x_frac}
                          disabled={!variant.letterbox_enabled || !variant.letterbox_hook_enabled}
                          onChange={(value) => updateVariant(index, { letterbox_hook_x_frac: value })}
                        />
                        <PercentControl
                          label="Hook Y"
                          value={variant.letterbox_hook_y_frac}
                          disabled={!variant.letterbox_enabled || !variant.letterbox_hook_enabled}
                          onChange={(value) => updateVariant(index, { letterbox_hook_y_frac: value })}
                        />
                      </div>
                    </div>
                  )}
                </section>
              ))}
            </div>
            <div className="preset-row">
              <FilterField label="Save preset">
                <input value={presetName} onChange={(event) => setPresetName(event.target.value)} placeholder="Preset name" />
              </FilterField>
              <button className="secondary-button" disabled={!presetName.trim() || busy === "preset"} onClick={savePreset}>Save preset</button>
              <FilterField label="Load preset">
                <select value={selectedPreset} onChange={(event) => setSelectedPreset(event.target.value)}>
                  <option value="">Choose preset</option>
                  {data.presets.map((preset) => <option value={preset.preset_id} key={preset.preset_id}>{preset.name}</option>)}
                </select>
              </FilterField>
              <button className="secondary-button" disabled={!selectedPreset || busy === "load"} onClick={loadPreset}>Load</button>
            </div>
          </article>
          <article className="panel variation-preview-panel">
            <div className="panel-head">
              <div>
                <h2>Preview</h2>
                <p>
                  {previewVariant?.visual_mode === "broll_audio"
                    ? selectedBrollProduct?.exists
                      ? `B-roll ${selectedBrollProduct.label} (${selectedBrollProduct.video_count})`
                      : `Missing ${selectedBrollProduct?.folder ?? data.product_broll.root}`
                    : data.preview_source.exists ? `Source ${parentDir(data.preview_source.path)}` : "Missing assets/variation_preview/raw_cut_preview.mp4"}
                </p>
              </div>
              <Badge
                value={previewVariant?.visual_mode === "broll_audio"
                  ? selectedBrollProduct?.preview?.exists ? "B-roll clip" : "Missing"
                  : data.preview_source.exists ? "Fixed clip" : "Missing"}
                kind={(previewVariant?.visual_mode === "broll_audio" ? selectedBrollProduct?.preview?.exists : data.preview_source.exists) ? "good" : "warn"}
              />
            </div>
            {previewVariant && (
              <div className="single-preview-stack">
                <div className="variation-preview-toolbar">
                  <FilterField label="Preview variant">
                    <select value={previewIndex} onChange={(event) => selectPreviewVariant(Number.parseInt(event.target.value, 10))}>
                      {visibleVariants.map((variant, index) => (
                        <option value={index} key={`preview-variant-${index}`}>
                          V{index + 1} {variant.name || `Variant ${index + 1}`}
                        </option>
                      ))}
                    </select>
                  </FilterField>
                  {previewVariant.visual_mode === "broll_audio" && (
                    <FilterField label="Sample product">
                      <select value={selectedBrollProduct?.product_key ?? ""} onChange={(event) => setPreviewProduct(event.target.value)}>
                        {brollPreviewProducts.map((product) => (
                          <option value={product.product_key} key={product.product_key}>
                            {product.label} ({product.video_count})
                          </option>
                        ))}
                      </select>
                    </FilterField>
                  )}
                  <div className="preview-variant-meta">
                    <span className="variant-index">V{previewIndex + 1}</span>
                    <span>{variationLabel(previewVariant.visual_mode)} / {variationLabel(previewVariant.color_grade)} / {previewVariant.visual_mode === "broll_audio" ? "No host zoom" : previewVariant.product_zoom_enabled ? variationLabel(previewVariant.zoom_intensity) : "No product zoom"}</span>
                  </div>
                  <button className="secondary-button render-preview-button" disabled={busy === "preview"} onClick={renderPreview}>
                    <RefreshCw size={15} aria-hidden="true" />
                    {busy === "preview" ? "Rendering" : "Render preview"}
                  </button>
                </div>
                <div className="single-preview-shell">
                  <div
                    className={`single-preview-frame grade-${previewVariant.color_grade} ${previewVariant.letterbox_enabled ? "has-bars" : ""} ${previewVariant.mirror_enabled ? "is-flipped" : ""}`}
                    ref={previewFrameRef}
                  >
                    {renderedPreview?.previews[0]?.exists ? (
                      <img className="generated-variation-preview" src={renderedPreview.previews[0].url} alt={`Rendered ${renderedPreview.previews[0].variant_name} preview`} />
                    ) : previewVariant.visual_mode === "broll_audio" ? (
                      selectedBrollProduct?.preview?.exists ? (
                        <video src={selectedBrollProduct.preview.url} muted autoPlay loop playsInline />
                      ) : (
                        <div className="preview-placeholder preview-missing">
                          <Video size={34} aria-hidden="true" />
                          <strong>B-roll preview missing</strong>
                          <span>{selectedBrollProduct?.folder ?? data.product_broll.root}</span>
                        </div>
                      )
                    ) : data.preview_source.exists ? (
                      <video src={data.preview_source.url} muted autoPlay loop playsInline />
                    ) : (
                      <div className="preview-placeholder preview-missing">
                        <Video size={34} aria-hidden="true" />
                        <strong>Preview asset missing</strong>
                        <span>assets/variation_preview/raw_cut_preview.mp4</span>
                      </div>
                    )}
                    {previewVariant.letterbox_enabled && (
                      <>
                        <div
                          className="preview-blackbar top"
                          style={{ height: `${clampNumber(previewVariant.letterbox_top_frac, 0, 0.4) * 100}%` }}
                        />
                        <div
                          className="preview-blackbar bottom"
                          style={{ height: `${clampNumber(previewVariant.letterbox_bottom_frac, 0, 0.4) * 100}%` }}
                        />
                        {previewVariant.letterbox_hook_enabled && previewVariant.letterbox_top_frac > 0 && (
                          <div
                            className="preview-letterbox-hook"
                            style={{
                              left: `${clampNumber(previewVariant.letterbox_hook_x_frac, 0, 1) * 100}%`,
                              top: `${clampNumber(previewVariant.letterbox_top_frac, 0, 0.4) * clampNumber(previewVariant.letterbox_hook_y_frac, 0, 1) * 100}%`,
                              color: previewVariant.letterbox_hook_font_color,
                              fontSize: `${letterboxHookPreviewFontSize(previewVariant.letterbox_hook_font_size)}px`
                            }}
                          >
                            Auto hook text
                          </div>
                        )}
                      </>
                    )}
                    {usesBeforeAfterImage(previewVariant) && (
                      <div className={`preview-before-after-card mode-${previewVariant.before_after_mode}`}>
                        <strong>Before</strong>
                        <span />
                        <strong>After</strong>
                      </div>
                    )}
                    {previewVariant.subtitle_enabled && (
                      <button
                        type="button"
                        className="subtitle-drag-handle"
                        style={{
                          top: `${clampNumber(previewVariant.subtitle_y_frac, 0.08, 0.92) * 100}%`,
                          color: previewVariant.font_color,
                          borderColor: previewVariant.highlight_color,
                          fontSize: `${subtitlePreviewFontSize(previewVariant.subtitle_size)}px`
                        }}
                        aria-label="Subtitle position"
                        onPointerDown={startSubtitleDrag}
                        onPointerMove={(event) => {
                          if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                            moveSubtitleFromPointer(event);
                          }
                        }}
                      >
                        <span style={{ color: previewVariant.highlight_color }}>Subtitle</span>
                        <small>{Math.round(clampNumber(previewVariant.subtitle_y_frac, 0.08, 0.92) * 100)}%</small>
                      </button>
                    )}
                  </div>
                </div>
                <div className="preview-adjustments">
                  <PercentControl
                    label="Top bar"
                    value={previewVariant.letterbox_top_frac}
                    max={0.4}
                    disabled={!previewVariant.letterbox_enabled}
                    onChange={(value) => updateVariant(previewIndex, { letterbox_top_frac: value })}
                  />
                  <PercentControl
                    label="Bottom bar"
                    value={previewVariant.letterbox_bottom_frac}
                    max={0.4}
                    disabled={!previewVariant.letterbox_enabled}
                    onChange={(value) => updateVariant(previewIndex, { letterbox_bottom_frac: value })}
                  />
                  <PercentControl
                    label="Subtitle Y"
                    value={previewVariant.subtitle_y_frac}
                    min={0.08}
                    max={0.92}
                    disabled={!previewVariant.subtitle_enabled}
                    onChange={(value) => updateSubtitleY(previewIndex, value)}
                  />
                </div>
              </div>
            )}
          </article>
        </div>
      )}
    </section>
  );
}

function ColorField({ label, value, disabled = false, onChange }: { label: string; value: string; disabled?: boolean; onChange: (value: string) => void }) {
  return (
    <FilterField label={label}>
      <div className="color-field">
        <input type="color" value={value} disabled={disabled} onChange={(event) => onChange(event.target.value.toUpperCase())} />
        <input value={value} disabled={disabled} onChange={(event) => onChange(event.target.value.toUpperCase())} maxLength={7} />
      </div>
    </FilterField>
  );
}

function NumberControl({
  label,
  value,
  min,
  max,
  unit,
  disabled = false,
  onChange
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  unit: string;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  const clamped = Math.round(clampNumber(value, min, max));
  return (
    <div className={`filter-field number-control ${disabled ? "control-disabled" : ""}`}>
      <span>{label}</span>
      <div className="number-control-row">
        <input
          type="range"
          min={min}
          max={max}
          step={1}
          value={clamped}
          disabled={disabled}
          onChange={(event) => onChange(Math.round(clampNumber(Number.parseInt(event.target.value || `${min}`, 10), min, max)))}
        />
        <input
          type="number"
          min={min}
          max={max}
          value={clamped}
          disabled={disabled}
          onChange={(event) => onChange(Math.round(clampNumber(Number.parseInt(event.target.value || `${min}`, 10), min, max)))}
        />
        <span>{unit}</span>
      </div>
    </div>
  );
}

function PercentControl({
  label,
  value,
  min = 0,
  max = 1,
  disabled = false,
  onChange
}: {
  label: string;
  value: number;
  min?: number;
  max?: number;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  const clamped = clampNumber(value, min, max);
  const percent = Math.round(clamped * 100);
  const minPercent = Math.round(min * 100);
  const maxPercent = Math.round(max * 100);
  return (
    <div className={`filter-field percent-control ${disabled ? "control-disabled" : ""}`}>
      <span>{label}</span>
      <div className="percent-control-row">
        <input
          type="range"
          min={minPercent}
          max={maxPercent}
          step={1}
          value={percent}
          disabled={disabled}
          onChange={(event) => onChange(clampNumber(Number.parseInt(event.target.value, 10) / 100, min, max))}
        />
        <input
          type="number"
          min={minPercent}
          max={maxPercent}
          step={1}
          value={percent}
          disabled={disabled}
          aria-label={label}
          onChange={(event) => onChange(clampNumber(Number.parseInt(event.target.value || "0", 10) / 100, min, max))}
        />
        <span>%</span>
      </div>
    </div>
  );
}

function SegmentedField({ label, value, options, disabled = false, onChange }: { label: string; value: string; options: string[]; disabled?: boolean; onChange: (value: string) => void }) {
  return (
    <div className={`filter-field ${disabled ? "control-disabled" : ""}`}>
      <span>{label}</span>
      <div className="segmented-control">
        {options.map((option) => (
          <button className={value === option ? "active" : ""} disabled={disabled} key={option} onClick={() => onChange(option)}>
            {variationLabel(option)}
          </button>
        ))}
      </div>
    </div>
  );
}

function ToggleField({ label, checked, disabled = false, onChange }: { label: string; checked: boolean; disabled?: boolean; onChange: (value: boolean) => void }) {
  return (
    <label className={`toggle-field ${disabled ? "control-disabled" : ""}`}>
      <span>{label}</span>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />
    </label>
  );
}

function ZoomField({ value, disabled = false, onChange }: { value: VariationVariant["zoom_intensity"]; disabled?: boolean; onChange: (value: VariationVariant["zoom_intensity"]) => void }) {
  const index = Math.max(0, zoomSteps.indexOf(value));
  return (
    <div className={`filter-field ${disabled ? "control-disabled" : ""}`}>
      <span>Zoom intensity</span>
      <input type="range" min={0} max={zoomSteps.length - 1} step={1} value={index} disabled={disabled} onChange={(event) => onChange(zoomSteps[Number(event.target.value)] ?? "normal")} />
      <div className="zoom-labels">
        {zoomSteps.map((step) => <span key={step}>{variationLabel(step)}</span>)}
      </div>
    </div>
  );
}

function copyProfile(profile: VariationProfile): VariationProfile {
  return JSON.parse(JSON.stringify(profile)) as VariationProfile;
}

function normalizeUiProfile(profile: VariationProfile): VariationProfile {
  const copy = copyProfile(profile);
  copy.variants = copy.variants.map((variant) => ({
    ...variant,
    before_after_mode: "fullscreen",
    random_broll_enabled: variant.random_broll_enabled ?? false,
    subtitle_size: variant.subtitle_size ?? "medium",
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

function subtitlePreviewFontSize(size: VariationVariant["subtitle_size"]): number {
  if (size === "small") {
    return 14;
  }
  if (size === "large") {
    return 20;
  }
  return 17;
}

function letterboxHookPreviewFontSize(value: number): number {
  return Math.round(clampNumber(value * 0.32, 12, 34));
}

function createUiVariant(index: number, base?: VariationVariant): VariationVariant {
  return {
    name: `Variant ${index + 1}`,
    hook_type: base?.hook_type ?? "text",
    visual_mode: base?.visual_mode ?? "host",
    random_broll_enabled: base?.visual_mode === "broll_audio" ? false : base?.random_broll_enabled ?? false,
    before_after_mode: base?.before_after_mode ?? "fullscreen",
    font_id: base?.font_id ?? "",
    font_color: base?.font_color ?? "#FFFFFF",
    highlight_color: base?.highlight_color ?? "#FFD600",
    subtitle_position: base?.subtitle_position ?? "bottom",
    subtitle_size: base?.subtitle_size ?? "medium",
    color_grade: base?.color_grade ?? "original",
    bgm_mode: base?.bgm_mode ?? "auto",
    bgm_path: base?.bgm_path ?? "",
    sfx_enabled: base?.sfx_enabled ?? true,
    zoom_intensity: base?.zoom_intensity ?? "normal",
    product_zoom_enabled: base?.product_zoom_enabled ?? true,
    subtitle_enabled: base?.subtitle_enabled ?? true,
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

function variationLabel(value: string): string {
  const labels: Record<string, string> = {
    none: "None",
    text: "Text",
    before_after_image: "Before/After image",
    text_before_after_image: "Text + Before/After image",
    b_roll: "B-roll",
    text_b_roll: "Text + B-roll",
    transitional_hook: "Transitional Hook",
    host: "Host",
    broll_audio: "Audio over B-roll",
    fullscreen: "Fullscreen",
    small: "Small",
    medium: "Medium",
    large: "Large"
  };
  return labels[value] ?? String(value || "text").replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
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
  const operations: ControlJob["operation"][] = ["queue_control", "settings_update", "settings_delete", "settings_reset", "rescore", "compliance_scan", "module_assembly", "export_batches", "module_review"];

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
  const data = system.envelope?.data;
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
    const payload = JSON.stringify({ health: health.envelope?.data, system: data, desktop }, null, 2);
    void navigator.clipboard.writeText(payload).then(() => setCopyMessage("Diagnostics copied.")).catch(() => setCopyMessage("Could not copy diagnostics."));
  }
  return (
    <section className="page-stack">
      <PageTitle title="Diagnostics" detail="API, desktop runtime, and local machine resource status." onRefresh={() => { health.refresh(); system.refresh(); refreshDesktop(); }}>
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
      {(health.error || system.error) && <StateBlock kind="bad" title="System read failed" detail={health.error || system.error} />}
      <StateBlock kind="warn" warnings={[...(health.envelope?.warnings ?? []), ...(system.envelope?.warnings ?? [])]} />
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
  QUEUE_FFMPEG_MAX_PARALLEL_CLIPS: { label: "Parallel clip renders", description: "Maximum FFmpeg clip renders running together." }
};

function settingLabel(name: string): string {
  if (settingCopy[name]) {
    return settingCopy[name].label;
  }
  return name.toLowerCase().replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
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
    compliance: "Controls policy scanning and automatic corrections.",
    modules: "Controls reusable-module extraction, validation, and assembly."
  };
  const description = scope[entry.category] || "Operator-safe pipeline setting.";
  return entry.category === "queue" ? `${description} Applies on the next queue start.` : description;
}

function SettingsPage({ active }: { active: boolean }) {
  const settings = useApiQuery<SettingsReadSnapshot>("/api/settings/effective", 30_000, active);
  const groups = settings.envelope?.data.groups ?? {};
  const revision = settings.envelope?.data.revision ?? "";
  const entries = Object.values(groups).flat();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<ActionMessage>();
  const [search, setSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
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
        <FilterField label="Category">
          <select value={categoryFilter} onChange={(event) => setCategoryFilter(event.target.value)}>
            <option value="">All categories</option>
            {Object.keys(groups).map((category) => <option value={category} key={category}>{settingLabel(category)}</option>)}
          </select>
        </FilterField>
      </div>
      <article className="panel">
        <div className="panel-head">
          <div>
            <h2>Override editor</h2>
            <p>Revision {revision ? revision.slice(0, 12) : "loading"}. Values save to the app override file.</p>
          </div>
          <Badge value={changedEntries.length ? `${changedEntries.length} dirty` : "Clean"} kind={changedEntries.length ? "warn" : "good"} />
        </div>
      </article>
      <div className="settings-grid">
        {Object.entries(visibleGroups).map(([category, groupEntries]) => (
          <article className="panel" key={category}>
            <div className="panel-head">
              <div>
                <h2>{category}</h2>
                <p>{groupEntries.length} registered values</p>
              </div>
            </div>
            <div className="settings-list">
              {groupEntries.map((entry) => (
                <div className={`setting-row editable-setting ${entry.editable !== false && isInvalid(entry) ? "invalid" : ""}`} key={entry.name}>
                  <div>
                    <strong>{settingLabel(entry.name)}</strong>
                    <span>{settingDescription(entry)}</span>
                    <code>{entry.name}</code>
                    <span>{entry.value_type} - {entry.source}</span>
                    {entry.editable === false && <span>{entry.read_only_reason || "Managed by operator configuration; restart required after external changes."}</span>}
                    {(entry.minimum !== null || entry.maximum !== null) && (
                      <span>Bounds {entry.minimum ?? "-"} to {entry.maximum ?? "-"}</span>
                    )}
                  </div>
                  {entry.value_type === "bool" ? (
                    <select disabled={entry.editable === false} value={draft[entry.name] ?? "false"} onChange={(event) => setDraft((current) => ({ ...current, [entry.name]: event.target.value }))}>
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </select>
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
                  <button className="tiny-button" disabled={entry.editable === false || entry.source !== "settings_override"} onClick={() => setDeleteTarget(entry.name)}>
                    Reset override
                  </button>
                </div>
              ))}
            </div>
          </article>
        ))}
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
        <Route path="/variants" element={<VariationsPage active />} />
        <Route path="/modules" element={<ModulesPage active />} />
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
