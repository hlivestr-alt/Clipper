import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ModularPlannerPage } from "./ModularPlannerPage";

function envelope(data: unknown) {
  return { data, generated_at: "now", source_signatures: [], warnings: [] };
}

function response(data: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(envelope(data)), {
    status, headers: { "Content-Type": "application/json" }
  }));
}

const item = {
  position: 0, segment_id: "cta-1", scan_id: "scan-1", scanner_generation: 1,
  role: "cta", source_id: "source-1", source_filename: "vod.mp4",
  start_seconds: 10, end_seconds: 30, duration_seconds: 20, confidence: 0.9,
  transcript_text: "Hari ini Rp99.000 promo beli 2 gratis 1", reason: "accepted",
  approved_usage_at_selection: 0, current_run_usage_at_selection: 0,
  ranking_metadata: { joinability: { joinability_score: 1, start_quality: "clean", end_quality: "clean", reason_codes: [], hard_unusable: false, boundary_label: "Clean" as const } }
};

const composition = {
  composition_id: "composition-1", ordinal: 1, requested_template: "standard" as const,
  actual_template: "standard" as const, fallback_reason: null, cta_mode: "use_cta" as const,
  target_min_duration: 45, target_max_duration: 75, actual_duration: 60,
  distinct_source_count: 3, selection_score: 92, exact_signature: "exact", near_signature: "near",
  status: "draft" as const,
  selection_metadata: { hook_benefits_continuity: 0.8 },
  items: [
    { ...item, position: 0, role: "hook", segment_id: "hook-1", transcript_text: "Hook" },
    { ...item, position: 1, role: "benefits", segment_id: "benefit-1", transcript_text: "Benefit" },
    { ...item, position: 2 }
  ]
};

const run = {
  planner_run_id: "run-1", production_method: "modular_video" as const, product: "serum" as const,
  requested_template: "standard" as const, ingredient_shortage_policy: "partial" as const,
  cta_mode: "use_cta" as const, requested_count: 2, generated_count: 1, shortfall: 1,
  target_min_duration: 45, target_max_duration: 75, seed: "123456789abcdef", planner_version: "v1",
  status: "draft" as const, revision: 2, warnings: [], search_statistics: {},
  compositions: [composition], created_at: "now", approved_at: null
};

const inventory = {
  product: "serum", snapshot_hash: "hash",
  roles: {
    hook: { segments: 10, distinct_sources: 8 }, benefits: { segments: 20, distinct_sources: 12 },
    ingredients: { segments: 2, distinct_sources: 2 }, cta: { segments: 4, distinct_sources: 4 }
  },
  suggested_durations: []
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}><ModularPlannerPage /></QueryClientProvider>);
}

describe("ModularPlannerPage", () => {
  beforeEach(() => {
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  });

  afterEach(() => {
    cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals();
  });

  it("applies all template defaults without overwriting manual durations", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/inventory")) return response(inventory);
      return response({ runs: [] });
    }));
    renderPage();
    const minimum = screen.getByLabelText("Minimum seconds") as HTMLInputElement;
    const maximum = screen.getByLabelText("Maximum seconds") as HTMLInputElement;
    expect(minimum.value).toBe("45"); expect(maximum.value).toBe("75");
    fireEvent.change(screen.getByLabelText("Template"), { target: { value: "ingredient" } });
    await waitFor(() => expect(minimum.value).toBe("60"));
    expect(maximum.value).toBe("90");
    fireEvent.change(screen.getByLabelText("CTA mode"), { target: { value: "no_cta" } });
    await waitFor(() => expect(minimum.value).toBe("45"));
    expect(maximum.value).toBe("75");
    fireEvent.change(minimum, { target: { value: "52" } });
    fireEvent.change(screen.getByLabelText("Template"), { target: { value: "benefit_focus" } });
    fireEvent.change(screen.getByLabelText("CTA mode"), { target: { value: "use_cta" } });
    expect(minimum.value).toBe("52"); expect(maximum.value).toBe("75");
    fireEvent.click(screen.getByRole("button", { name: /reset to suggested/i }));
    expect(minimum.value).toBe("60"); expect(maximum.value).toBe("90");
  });

  it("previews and submits one balanced All Products production job", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/inventory")) return response(inventory);
      if (path.includes("/api/modular-production/profiles")) return response({ profiles: [{ profile_id: "active", name: "Active", revision: "r1", variant_count: 6 }] });
      if (path.includes("/api/modular-production/jobs") && init?.method !== "POST") return response({ jobs: [] });
      if (path.endsWith("/api/modular-production/jobs") && init?.method === "POST") return response({ detail: "test accepted" }, 500);
      return response({ runs: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    fireEvent.change(screen.getByLabelText("Production product"), { target: { value: "all_products" } });
    fireEvent.change(screen.getByLabelText("Production base videos"), { target: { value: "24" } });
    expect(await screen.findByText("The total base-video count will be distributed as evenly as possible across all 6 products. Each video still contains only one product.")).toBeTruthy();
    expect(await screen.findByText("24 base videos → 144 expected final variants")).toBeTruthy();
    expect(screen.getAllByText(/4 bases → 24 variants/)).toHaveLength(6);
    fireEvent.click(screen.getByRole("button", { name: /start modular production/i }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([path, init]) => String(path).endsWith("/api/modular-production/jobs") && init?.method === "POST")).toBe(true));
    const post = fetchMock.mock.calls.find(([path, init]) => String(path).endsWith("/api/modular-production/jobs") && init?.method === "POST");
    const body = JSON.parse(String(post?.[1]?.body));
    expect(body.product).toBe("all_products");
    expect(body.requested_base_count).toBe(24);
    expect(body.seed).toBeTruthy();
  });

  it("generates, previews source range, regenerates, removes, and approves without rendering", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/inventory")) return response(inventory);
      if (path.includes("/runs?")) return response({ runs: [] });
      if (path.endsWith("/api/modular-planner/runs") && init?.method === "POST") return response(run, 201);
      if (path.includes("/regenerate")) return response({ ...run, revision: 3 });
      if (path.includes("/remove")) return response({ ...run, revision: 3, generated_count: 0, shortfall: 2, compositions: [{ ...composition, status: "removed" }] });
      if (path.endsWith("/approve")) return response({ ...run, revision: 3, status: "approved", compositions: [{ ...composition, status: "approved" }] });
      return response({ runs: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPage();
    fireEvent.change(screen.getByLabelText("Base compositions"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: /generate plans/i }));
    expect(await screen.findByText("Hari ini Rp99.000 promo beli 2 gratis 1")).toBeTruthy();
    expect(screen.getAllByText(/Boundary: Clean/).length).toBe(3);
    expect(screen.getByText(/continuity 80%/)).toBeTruthy();
    expect(screen.getByText("Requested").nextSibling?.textContent).toBe("2");
    const previewButtons = screen.getAllByRole("button", { name: "Preview" });
    fireEvent.click(previewButtons[2]);
    const video = document.querySelector("video") as HTMLVideoElement;
    expect(video.getAttribute("src")).toBe("/api/modular-scanner/media/source-1");
    await waitFor(() => expect(video.currentTime).toBe(10));
    video.currentTime = 30; fireEvent.timeUpdate(video);
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /regenerate/i }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([path]) => String(path).includes("/regenerate"))).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: /^remove$/i }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([path]) => String(path).includes("/remove"))).toBe(true));
    // Restore the generated response so approval remains available after testing removal.
    fireEvent.click(screen.getByRole("button", { name: /generate plans/i }));
    await screen.findByRole("button", { name: /approve 1 composition/i });
    fireEvent.click(screen.getByRole("button", { name: /approve 1 composition/i }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([path]) => String(path).endsWith("/approve"))).toBe(true));
    expect(fetchMock.mock.calls.every(([path]) => !String(path).includes("/api/control/queue"))).toBe(true);
  });

  it("selects approved compositions explicitly, launches a 202 pilot render, and plays the joined output", async () => {
    const approvedRun = { ...run, status: "approved" as const, compositions: [{ ...composition, status: "approved" as const }] };
    const completedRender = {
      render_run_id: "render-1", planner_run_id: "run-1", planner_manifest_id: "manifest-1",
      renderer_version: "modular-renderer-v1", selected_composition_ids: ["composition-1"],
      status: "completed" as const, requested_count: 1, succeeded_count: 1, failed_count: 0,
      current_composition_id: null, items: [{
        render_run_id: "render-1", composition_id: "composition-1", product: "serum" as const,
        template: "standard" as const, ordinal: 1, renderer_version: "modular-renderer-v1",
        expected_duration: 60, rendered_duration: 60.03, duration_delta: 0.03, status: "completed" as const,
        normalization: { target_fps: 30 }, created_at: "now", completed_at: "now"
      }], created_at: "now", completed_at: "now"
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/inventory")) return response(inventory);
      if (path.includes("/api/modular-renderer/runs") && init?.method === "POST") return response(completedRender, 202);
      if (path.includes("/api/modular-renderer/runs?")) return response({ runs: [] });
      if (path.includes("/api/modular-planner/runs?")) return response({ runs: [approvedRun] });
      return response(approvedRun);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPage();
    expect(await screen.findByRole("heading", { name: "Render Pilot" })).toBeTruthy();
    expect((screen.getByLabelText("Select composition 1 for rendering") as HTMLInputElement).checked).toBe(false);
    fireEvent.click(screen.getByLabelText("Select composition 1 for rendering"));
    fireEvent.click(screen.getByRole("button", { name: /render selected/i }));
    await screen.findByRole("button", { name: /play base video/i });
    expect(window.confirm).toHaveBeenCalledWith("Render 1 approved base video?");
    const post = fetchMock.mock.calls.find(([path, init]) => String(path).endsWith("/api/modular-renderer/runs") && init?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ planner_run_id: "run-1", composition_ids: ["composition-1"], manual_rerender: false });
    fireEvent.click(screen.getByRole("button", { name: /play base video/i }));
    const videos = Array.from(document.querySelectorAll("video"));
    expect(videos.some((video) => video.getAttribute("src") === "/api/modular-renderer/runs/render-1/media/composition-1")).toBe(true);
  });

  it("selects completed bases by ID, confirms six each, polls, and exposes authenticated variant playback", async () => {
    const approvedRun = { ...run, status: "approved" as const, compositions: [{ ...composition, status: "approved" as const }] };
    const eligible = { render_run_id: "render-1", composition_id: "composition-1", product: "serum", ordinal: 1, renderer_version: "modular-renderer-v1.1", rendered_duration: 60, base_identity: "sha256" };
    const pilot = {
      run_id: "pilot-1", profile_id: "active", profile_revision: "profile-rev", status: "completed",
      requested_base_count: 1, requested_variant_count: 6, succeeded_base_count: 1, failed_base_count: 0,
      total_expected_outputs: 6, total_completed_outputs: 6, current_render_item_id: null,
      items: [{
        render_run_id: "render-1", modular_render_item_id: "render-1:composition-1", planner_run_id: "run-1",
        composition_id: "composition-1", product: "serum", renderer_version: "modular-renderer-v1.1",
        base_identity: "sha256", ordinal: 1, variant_profile: "active", status: "completed",
        expected_variant_count: 6, produced_variant_count: 6, generation_seconds: 42,
        outputs: Array.from({ length: 6 }, (_, index) => ({ media_id: `media-${index}`, variant_index: index, variant_id: `v${index}`, variant_name: `Variant ${index + 1}`, url: `/api/modular-variant-pilot/media/media-${index}`, duration: 60, width: 1080, height: 1920, has_video: true, has_audio: true, file_size: 1048576, generation_seconds: 7 }))
      }]
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/inventory")) return response(inventory);
      if (path.includes("/api/modular-variant-pilot/profiles")) return response({ profiles: [{ profile_id: "active", name: "Active variation profile", revision: "profile-rev", variant_count: 6 }], required_variant_count: 6 });
      if (path.includes("/api/modular-variant-pilot/eligible")) return response({ bases: [eligible] });
      if (path.endsWith("/api/modular-variant-pilot/runs") && init?.method === "POST") return response(pilot, 202);
      if (path.includes("/api/modular-renderer/runs?")) return response({ runs: [] });
      if (path.includes("/api/modular-planner/runs?")) return response({ runs: [approvedRun] });
      return response(approvedRun);
    });
    vi.stubGlobal("fetch", fetchMock); vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPage();
    expect(await screen.findByRole("heading", { name: "Generate Variants Pilot" })).toBeTruthy();
    const checkbox = await screen.findByLabelText("Select completed base 1 for variants");
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole("button", { name: /^generate variants$/i }));
    await screen.findByText("var5 · Variant 6");
    expect(window.confirm).toHaveBeenCalledWith("1 base videos × 6 variants = 6 outputs. Generate pilot variants?");
    const post = fetchMock.mock.calls.find(([path, init]) => String(path).endsWith("/api/modular-variant-pilot/runs") && init?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ bases: [{ render_run_id: "render-1", composition_id: "composition-1" }], profile_id: "active", manual_rerun: false });
    const sources = Array.from(document.querySelectorAll("video")).map((video) => video.getAttribute("src"));
    expect(sources).toContain("/api/modular-variant-pilot/media/media-5");
    expect(fetchMock.mock.calls.every(([path]) => !String(path).includes("/api/control/queue"))).toBe(true);
  });
});
// @vitest-environment jsdom
