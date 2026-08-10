import { describe, expect, it } from "vitest";
import type { ControlJobSummary, ControlJobStatus } from "./api";
import { jobTrayDismissalKey, selectJobTrayJobs } from "./App";

function job(job_id: string, status: ControlJobStatus): ControlJobSummary {
  return {
    job_id,
    status,
    operation: "trend_analysis",
    created_at: "2026-07-20T00:00:00Z",
    updated_at: "2026-07-20T00:00:00Z",
    actor: "test"
  };
}

describe("background job tray", () => {
  it("hides dismissed failed jobs without revealing older history", () => {
    const jobs = [job("failed-1", "failed"), job("failed-2", "failed"), job("running-1", "running"), job("older", "failed")];
    const dismissed = new Set(jobs.slice(0, 3).map(jobTrayDismissalKey));

    expect(selectJobTrayJobs(jobs, dismissed)).toEqual([]);
  });

  it("surfaces new jobs and meaningful status changes", () => {
    const failed = job("analysis-1", "failed");
    const dismissed = new Set([jobTrayDismissalKey(failed)]);

    expect(selectJobTrayJobs([job("analysis-2", "failed"), failed], dismissed).map((item) => item.job_id)).toEqual(["analysis-2"]);
    expect(selectJobTrayJobs([job("analysis-1", "queued")], dismissed).map((item) => item.status)).toEqual(["queued"]);
  });
});
