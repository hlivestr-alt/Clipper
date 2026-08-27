import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ModularScannerPage } from "./ModularScannerPage";

const currentScan = {
  scan_id: "scan-1", source_id: "source-1", generation: 1, trigger: "scan" as const,
  status: "completed", progress_current: 1, progress_total: 1, accepted_count: 1,
  rejected_count: 0, is_current: true
};

const activeScan = {
  ...currentScan, scan_id: "scan-2", generation: 2, trigger: "rescan" as const,
  status: "analyzing", progress_current: 1, progress_total: 2, accepted_count: 0,
  is_current: false
};

const segment = {
  segment_id: "segment-1", scan_id: "scan-1", source_id: "source-1", vod_filename: "one.mp4",
  product: "serum", role: "benefits", start_seconds: 5, end_seconds: 20,
  duration_seconds: 15, confidence: 0.91, transcript_text: "serum vitamin c membantu mencerahkan kulit",
  reason: "Clear reusable benefit"
};

function envelope(data: unknown) {
  return { data, generated_at: "now", source_signatures: [], warnings: [] };
}

function jsonResponse(data: unknown) {
  return Promise.resolve(new Response(JSON.stringify(envelope(data)), {
    status: 200,
    headers: { "Content-Type": "application/json" }
  }));
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}><ModularScannerPage /></QueryClientProvider>);
}

describe("ModularScannerPage", () => {
  beforeEach(() => {
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("never starts a scan when sources load or selection changes", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/sources")) {
        return jsonResponse({ sources: [
          { source_id: "source-1", filename: "one.mp4", file_size: 100, mtime_ns: 1, duration_seconds: 40, current_scan: null, active_scan: null },
          { source_id: "source-2", filename: "two.mp4", file_size: 200, mtime_ns: 2, duration_seconds: 50, current_scan: null, active_scan: null }
        ] });
      }
      if (path.includes("/scans?")) return jsonResponse({ scans: [] });
      if (path.includes("/segments?")) return jsonResponse({ segments: [] });
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    const selector = await screen.findByLabelText("Source VOD");
    await waitFor(() => expect((selector as HTMLSelectElement).value).toBe("source-1"));
    fireEvent.change(selector, { target: { value: "source-2" } });
    await waitFor(() => expect((selector as HTMLSelectElement).value).toBe("source-2"));
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
  });

  it("keeps the previous successful results visible while a rescan runs", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/sources")) return jsonResponse({ sources: [{
        source_id: "source-1", filename: "one.mp4", file_size: 100, mtime_ns: 1,
        duration_seconds: 40, current_scan: currentScan, active_scan: activeScan
      }] });
      if (path.includes("/scans?")) return jsonResponse({ scans: [activeScan, currentScan] });
      if (path.includes("/segments?")) return jsonResponse({ segments: [segment] });
      return jsonResponse({});
    }));
    renderPage();
    expect(await screen.findByText(segment.transcript_text)).toBeTruthy();
    expect(screen.getAllByText("analyzing").length).toBeGreaterThan(0);
    expect(screen.getByText("Current successful generation")).toBeTruthy();
  });

  it("seeks to preview start, pauses at end, and replays from start", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/sources")) return jsonResponse({ sources: [{
        source_id: "source-1", filename: "one.mp4", file_size: 100, mtime_ns: 1,
        duration_seconds: 40, current_scan: currentScan, active_scan: null
      }] });
      if (path.includes("/scans?")) return jsonResponse({ scans: [currentScan] });
      if (path.includes("/segments?")) return jsonResponse({ segments: [segment] });
      return jsonResponse({});
    }));
    renderPage();
    fireEvent.click(await screen.findByText(segment.transcript_text));
    const media = document.querySelector("video") as HTMLVideoElement;
    await waitFor(() => expect(media.currentTime).toBe(5));
    expect(media.getAttribute("src")).toBe("/api/modular-scanner/media/source-1");
    media.currentTime = 20;
    fireEvent.timeUpdate(media);
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /replay/i }));
    expect(media.currentTime).toBe(5);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();
  });

  it("previews counts, confirms the queue, and polls compact batch progress", async () => {
    const batch = {
      batch_id: "batch-1", status: "running", total_eligible: 5, discovered: 5, checked: 5, checking: 0,
      already_current: 2, already_active: 1, queued: 2, completed: 1,
      failed: 0, remaining: 1,
      currently_running: { source_id: "source-1", filename: "one.mp4", status: "analyzing" }
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("batch-scan-preview")) {
        return jsonResponse({
          total_eligible: 5, already_current: 2, already_active: 1,
          would_queue: 2, needs_check: 0, will_evaluate: 2
        });
      }
      if (path.endsWith("/api/modular-scanner/batches") && init?.method === "POST") {
        return jsonResponse({
          launched: true, reused: false,
          batch: { ...batch, status: "preparing", total_eligible: 0, discovered: 0, checked: 0, queued: 0, remaining: 0 }
        });
      }
      if (path.includes("/batches/batch-1")) return jsonResponse({ batch });
      if (path.includes("/sources")) return jsonResponse({ sources: [{
        source_id: "source-1", filename: "one.mp4", file_size: 100, mtime_ns: 1,
        duration_seconds: 40, current_scan: currentScan, active_scan: null
      }] });
      if (path.includes("/scans?")) return jsonResponse({ scans: [currentScan] });
      if (path.includes("/segments?")) return jsonResponse({ segments: [segment] });
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /scan all unscanned vods/i }));
    const dialog = await screen.findByRole("dialog", { name: "Batch scan confirmation" });
    expect(dialog.textContent).toContain("5 VODs found");
    expect(dialog.textContent).toContain("Already current2");
    expect(dialog.textContent).toContain("Already active1");
    fireEvent.click(screen.getByRole("button", { name: "Evaluate 2 VODs" }));
    expect(await screen.findByText("Preparing batch...")).toBeTruthy();
    const progress = screen.getByLabelText("Batch scan progress");
    await waitFor(() => expect(progress.textContent).toContain("Remaining1"));
    expect(progress.textContent).toContain("Completed1");
    expect(progress.textContent).toContain("one.mp4");
  });

  it("shows the all-current state without launching a batch", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const path = String(input);
      if (path.includes("batch-scan-preview")) {
        return jsonResponse({
          total_eligible: 3, already_current: 3, already_active: 0,
          would_queue: 0, needs_check: 0, will_evaluate: 0
        });
      }
      if (path.includes("/sources")) return jsonResponse({ sources: [{
        source_id: "source-1", filename: "one.mp4", file_size: 100, mtime_ns: 1,
        duration_seconds: 40, current_scan: currentScan, active_scan: null
      }] });
      if (path.includes("/scans?")) return jsonResponse({ scans: [currentScan] });
      if (path.includes("/segments?")) return jsonResponse({ segments: [segment] });
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /scan all unscanned vods/i }));
    expect(await screen.findByText("All eligible VODs already have compatible scans.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /evaluate \d+ vods/i })).toBeNull();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
    expect(screen.getByRole("button", { name: /scan vod/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^rescan$/i })).toBeTruthy();
  });
});
// @vitest-environment jsdom
