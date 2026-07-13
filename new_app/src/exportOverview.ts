import type { OverviewData } from "./api";

export type ExportOverviewView = {
  available: boolean;
  actionable: number;
  packagedLastRun: number;
  pending: number;
  packagedTotal: number;
  errorCount: number;
  batchSize: number;
  status: string;
  updatedAt: string;
  trigger: string;
  dryRun: boolean;
};

function count(value: number | null | undefined): number {
  return Number.isFinite(value) ? Math.max(0, Math.round(value ?? 0)) : 0;
}

export function buildExportOverview(data?: OverviewData["export"]): ExportOverviewView {
  return {
    available: Boolean(data?.available),
    actionable: count(data?.actionable ?? data?.ready),
    packagedLastRun: count(data?.packaged_last_run ?? data?.packaged),
    pending: count(data?.pending),
    packagedTotal: count(data?.packaged_total),
    errorCount: count(data?.error_count),
    batchSize: count(data?.batch_size),
    status: String(data?.status || ""),
    updatedAt: String(data?.updated_at || ""),
    trigger: String(data?.trigger || ""),
    dryRun: Boolean(data?.dry_run)
  };
}
