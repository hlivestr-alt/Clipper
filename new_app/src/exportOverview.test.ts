import { describe, expect, it } from "vitest";

import type { OverviewData } from "./api";
import { buildExportOverview } from "./exportOverview";

function exportData(overrides: Partial<OverviewData["export"]> = {}): OverviewData["export"] {
  return {
    available: true,
    actionable: 0,
    ready: 0,
    packaged_last_run: 0,
    packaged: 0,
    pending: 0,
    packaged_total: 0,
    error_count: 0,
    batch_size: 15,
    progress: 0,
    status: "completed",
    updated_at: "2026-07-13T12:00:00+08:00",
    trigger: "automatic",
    dry_run: false,
    ...overrides
  };
}

describe("buildExportOverview", () => {
  it("marks a missing snapshot unavailable instead of inventing a count", () => {
    expect(buildExportOverview()).toEqual({
      available: false,
      actionable: 0,
      packagedLastRun: 0,
      pending: 0,
      packagedTotal: 0,
      errorCount: 0,
      batchSize: 0,
      status: "",
      updatedAt: "",
      trigger: "",
      dryRun: false
    });
  });

  it("represents a confirmed healthy zero", () => {
    const result = buildExportOverview(exportData({ packaged_total: 120 }));
    expect(result.available).toBe(true);
    expect(result.pending).toBe(0);
    expect(result.packagedTotal).toBe(120);
  });

  it("keeps actionable and pending counts distinct", () => {
    const result = buildExportOverview(exportData({ actionable: 8, packaged_last_run: 5, pending: 3 }));
    expect(result.actionable).toBe(8);
    expect(result.packagedLastRun).toBe(5);
    expect(result.pending).toBe(3);
  });

  it("surfaces reconciliation errors", () => {
    const result = buildExportOverview(exportData({ status: "completed_with_errors", error_count: 2 }));
    expect(result.status).toBe("completed_with_errors");
    expect(result.errorCount).toBe(2);
  });
});
