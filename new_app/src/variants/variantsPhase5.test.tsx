// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ApiEnvelope,
  ProductInformationStatus,
  VariationPageData,
  VariationProfile,
  VariationVariant
} from "../api";

const queryState = vi.hoisted(() => ({
  variations: undefined as ApiEnvelope<VariationPageData> | undefined,
  information: undefined as ApiEnvelope<ProductInformationStatus> | undefined,
  informationLoading: false,
  informationError: undefined as string | undefined,
  informationRefresh: vi.fn()
}));

vi.mock("../useApiQuery", () => ({
  useApiQuery: (path: string) => path === "/api/product-information"
    ? {
        envelope: queryState.information,
        loading: queryState.informationLoading,
        refreshing: false,
        error: queryState.informationError,
        refresh: queryState.informationRefresh
      }
    : {
        envelope: queryState.variations,
        loading: false,
        refreshing: false,
        error: undefined,
        refresh: vi.fn()
      }
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, getJson: vi.fn(), sendJson: vi.fn() };
});

import { VariationsPage } from "../App";
import { sendJson } from "../api";
import { AssetsDiagnosticsTab } from "./tabs/AssetsDiagnosticsTab";

const mockedSendJson = vi.mocked(sendJson);

function makeVariant(): VariationVariant {
  const setting = {
    headline_font_id: "headline.ttf",
    body_font_id: "body.ttf",
    font_size: 35,
    animation: "current" as const,
    duration_seconds: 2.6
  };
  return {
    name: "Diagnostic variant",
    hook_type: "text",
    visual_mode: "host",
    random_broll_enabled: true,
    before_after_mode: "fullscreen",
    text_style_id: "current",
    font_id: "subtitle.ttf",
    headline_font_id: "headline.ttf",
    caption_font_id: "caption.ttf",
    font_color: "#FFFFFF",
    highlight_color: "#FFD600",
    subtitle_position: "bottom",
    subtitle_size: "medium",
    subtitle_stroke_color: "#000000",
    subtitle_stroke_width: 3,
    subtitle_highlight_enabled: true,
    subtitle_animation: "current",
    headline_animation: "current",
    caption_animation: "current",
    headline_stroke_width: 5,
    headline_shadow_color: "#000000",
    headline_shadow_x: 3,
    headline_shadow_y: 3,
    headline_rotation_degrees: 0,
    caption_stroke_width: 4,
    color_grade: "original",
    bgm_mode: "auto",
    bgm_path: "C:\\private\\music\\track.mp3",
    sfx_enabled: true,
    zoom_intensity: "normal",
    product_zoom_enabled: true,
    subtitle_enabled: true,
    dynamic_text_mode: "balanced",
    dynamic_text_roles: ["ingredients", "benefits", "usage", "cta"],
    dynamic_text_settings: {
      ingredients: { ...setting },
      benefits: { ...setting },
      usage: { ...setting },
      cta: { ...setting, font_size: 50 }
    },
    letterbox_enabled: false,
    mirror_enabled: false,
    subtitle_y_frac: 0.84,
    letterbox_top_frac: 0,
    letterbox_bottom_frac: 0,
    letterbox_hook_enabled: false,
    letterbox_hook_font_id: "headline.ttf",
    letterbox_hook_font_color: "#FFFFFF",
    letterbox_hook_font_size: 72,
    letterbox_hook_x_frac: 0.5,
    letterbox_hook_y_frac: 0.5
  };
}

function makeProfile(): VariationProfile {
  return {
    schema_version: 12,
    revision: "profile-revision-123456",
    variant_count: 1,
    updated_at: "2026-07-30T01:00:00Z",
    variants: [makeVariant()]
  };
}

function makeData(): VariationPageData {
  const profile = makeProfile();
  return {
    profile,
    fonts: [
      { id: "headline.ttf", path: "C:\\private\\fonts\\headline.ttf", label: "Headline" },
      { id: "body.ttf", path: "C:\\private\\fonts\\body.ttf", label: "Body" }
    ],
    text_styles: [{
      id: "current",
      label: "Current",
      description: "Preserves the current motion, stroke, and shadow defaults.",
      defaults: {}
    }],
    bgm_tracks: [{ label: "Steady Beat", path: "C:\\private\\music\\track.mp3" }],
    hook_types: ["text"],
    visual_modes: ["host", "broll_audio"],
    before_after_modes: ["fullscreen"],
    subtitle_positions: ["top", "center", "bottom"],
    subtitle_sizes: ["compact", "small", "medium", "large"],
    dynamic_text_modes: ["off", "minimal", "balanced", "high_energy"],
    dynamic_text_roles: ["ingredients", "benefits", "usage", "cta"],
    dynamic_text_animations: ["current", "staggered_reveal", "fade_up", "wipe", "slide_up"],
    color_grades: ["original"],
    bgm_modes: ["auto", "none", "selected"],
    zoom_intensities: ["none", "subtle", "normal", "strong"],
    presets: [],
    limits: { min_variants: 1, max_variants: 6 },
    preview_source: {
      path: "C:\\private\\preview\\host.mp4",
      url: "/api/artifacts?host",
      kind: "video",
      exists: true
    },
    product_broll: {
      root: "C:\\private\\broll",
      exists: true,
      products: [{
        product_key: "serum",
        label: "Serum",
        folder: "C:\\private\\broll\\serum",
        exists: true,
        video_count: 3,
        preview: {
          path: "C:\\private\\broll\\serum\\sample.mp4",
          url: "/api/artifacts?sample",
          kind: "video",
          exists: true
        }
      }]
    },
    global_feature_flags: {
      sfx: false,
      bgm: false,
      before_after: false,
      broll_intro: false,
      transitional_hook: false,
      host_face_zoom: false
    }
  };
}

function makeInformation(): ProductInformationStatus {
  return {
    schema_version: 1,
    revision: "information-revision-123456",
    scanned_at: "2026-07-30T02:03:04Z",
    root: "C:\\private\\information",
    sources: [{
      path: "C:\\private\\information\\serum.pdf",
      extension: ".pdf",
      size: 2048,
      sha256: "abc123",
      status: "warning",
      cached: true,
      extraction_method: "rules_fallback",
      page_count: 4,
      warnings: ["One table could not be assigned."],
      products: ["serum"],
      eligible_fact_count: 10,
      fact_counts: { ingredients: 3, benefits: 3, usage: 2, cta: 2 },
      unassigned_count: 1
    }],
    products: [{
      product_key: "serum",
      label: "Serum",
      eligible_fact_count: 10,
      fact_counts: { ingredients: 3, benefits: 3, usage: 2, cta: 2 }
    }],
    unassigned_count: 1,
    conflict_count: 1,
    unassigned: [{
      role: "usage",
      text: "Use as directed.",
      source_file: "C:\\private\\information\\serum.pdf",
      locator: { page: 4 },
      reason: "Missing product heading"
    }],
    conflicts: [{
      product: "serum",
      role: "benefits",
      key: "benefit-strength",
      fact_ids: ["fact-1", "fact-2"],
      reason: "Different values were returned."
    }],
    warnings: ["Review unassigned product facts."]
  };
}

function envelope<T>(data: T): ApiEnvelope<T> {
  return {
    data,
    generated_at: "2026-07-30T02:03:04Z",
    source_signatures: [],
    warnings: []
  };
}

function renderDiagnostics(overrides: Partial<ComponentProps<typeof AssetsDiagnosticsTab>> = {}) {
  const data = makeData();
  const variant = data.profile.variants[0];
  return render(
    <AssetsDiagnosticsTab
      variant={variant}
      variantIndex={0}
      data={data}
      informationData={makeInformation()}
      featureFlags={data.global_feature_flags!}
      previewProduct="serum"
      previewInformationProduct="serum"
      updateVariant={vi.fn()}
      applyTextStyle={vi.fn()}
      updateSubtitleY={vi.fn()}
      updateLetterboxEnabled={vi.fn()}
      updateLetterboxHookEnabled={vi.fn()}
      updateDynamicTextRole={vi.fn()}
      updateDynamicTextSetting={vi.fn()}
      updatePreviewProduct={vi.fn()}
      updatePreviewInformationProduct={vi.fn()}
      hookTypeAvailable={() => true}
      beforeAfterRelevant={false}
      onNavigateTab={vi.fn()}
      onRescanProductInformation={vi.fn()}
      {...overrides}
    />
  );
}

beforeEach(() => {
  queryState.variations = envelope(makeData());
  queryState.information = envelope(makeInformation());
  queryState.informationLoading = false;
  queryState.informationError = undefined;
  queryState.informationRefresh.mockReset();
  mockedSendJson.mockReset();
});

afterEach(cleanup);

describe("Phase 5 Assets & Diagnostics", () => {
  it("shows real readiness, fact roles, sources, warnings, inventories, and global overrides without fictional systems", () => {
    const view = renderDiagnostics();
    expect(screen.getByRole("heading", { name: "Supporting asset readiness" })).toBeTruthy();
    expect(screen.getByText("Product-information health")).toBeTruthy();
    expect(screen.getByText("information-")).toBeTruthy();
    expect(screen.getByText("2026-07-30T02:03:04Z")).toBeTruthy();
    expect(screen.getByText("Ingredients")).toBeTruthy();
    expect(screen.getByText("Benefits")).toBeTruthy();
    expect(screen.getByText("Usage")).toBeTruthy();
    expect(screen.getByText("Cta")).toBeTruthy();
    expect(screen.getByText("One table could not be assigned.")).toBeTruthy();
    expect(screen.getByText("Use as directed.")).toBeTruthy();
    expect(screen.getByText("Different values were returned.")).toBeTruthy();
    expect(screen.getByText("Media and creative-asset inventory")).toBeTruthy();
    expect(screen.getByText("Host-face zoom")).toBeTruthy();
    expect(screen.getAllByText("Disabled globally").length).toBeGreaterThan(0);
    expect(view.container.textContent).not.toMatch(/Notion|Marketing Drive|Brand Kit|Subtitle Presets|help center|Rescan all assets/i);
  });

  it("keeps absolute paths out of primary rows and reveals them only in troubleshooting disclosures", () => {
    const view = renderDiagnostics();
    const readiness = screen.getByRole("heading", { name: "Supporting asset readiness" }).closest("section")!;
    expect(readiness.textContent).not.toContain("C:\\private");
    const sourcesSection = screen.getByRole("heading", { name: "Information sources" }).closest("section")!;
    const sourceSummary = sourcesSection.querySelector("summary")!;
    expect(sourceSummary.textContent).not.toContain("C:\\private");
    const sourceDetails = sourceSummary.closest("details")!;
    expect(sourceDetails.open).toBe(false);
    const technicalSummary = within(sourceDetails).getByText("Technical path and checksum");
    fireEvent.click(technicalSummary);
    expect(technicalSummary.closest("details")?.open).toBe(true);
    expect(Array.from(view.container.querySelectorAll("code")).some(
      (code) => code.textContent?.includes("C:\\private\\information\\serum.pdf")
    )).toBe(true);
  });

  it("presents loading, query error, no-document, scanning, ready and missing inventory states", () => {
    const { rerender } = renderDiagnostics({
      productInformationLoading: true,
      productInformationError: "Index unavailable"
    });
    expect(screen.getByText("Loading product information…")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("Index unavailable");

    const data = makeData();
    data.preview_source.exists = false;
    data.product_broll.exists = false;
    data.product_broll.products = [];
    const emptyInformation = { ...makeInformation(), sources: [], products: [], unassigned: [], conflicts: [], warnings: [], unassigned_count: 0, conflict_count: 0 };
    rerender(
      <AssetsDiagnosticsTab
        variant={data.profile.variants[0]}
        variantIndex={0}
        data={data}
        informationData={emptyInformation}
        productInformationScanning
        featureFlags={data.global_feature_flags!}
        previewProduct=""
        previewInformationProduct=""
        updateVariant={vi.fn()}
        applyTextStyle={vi.fn()}
        updateSubtitleY={vi.fn()}
        updateLetterboxEnabled={vi.fn()}
        updateLetterboxHookEnabled={vi.fn()}
        updateDynamicTextRole={vi.fn()}
        updateDynamicTextSetting={vi.fn()}
        updatePreviewProduct={vi.fn()}
        updatePreviewInformationProduct={vi.fn()}
        hookTypeAvailable={() => true}
        beforeAfterRelevant={false}
        onNavigateTab={vi.fn()}
        onRescanProductInformation={vi.fn()}
      />
    );
    expect(screen.getByText("No product-information documents")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Scanning product information" }).getAttribute("aria-busy")).toBe("true");
    expect(screen.getAllByText("Missing").length).toBeGreaterThan(1);
  });

  it("removes the old leading panel, keeps one rescan action, and reports rescan success in one live action notice", async () => {
    mockedSendJson.mockResolvedValue(envelope(makeInformation()));
    const view = render(<VariationsPage active />);
    expect(view.container.querySelector(".product-information-panel")).toBeNull();
    const command = view.container.querySelector(".variant-command-bar");
    const workspace = view.container.querySelector(".variant-workspace");
    expect(command).toBeTruthy();
    expect(workspace).toBeTruthy();
    expect(command!.compareDocumentPosition(workspace!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "Assets & Diagnostics" }));
    expect(screen.getAllByRole("button", { name: "Rescan product information" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Rescan product information" }));
    await waitFor(() => expect(queryState.informationRefresh).toHaveBeenCalledTimes(1));
    const notices = view.container.querySelectorAll(".action-notice");
    expect(notices).toHaveLength(1);
    expect(notices[0].textContent).toContain("Product information scanned");
  });

  it("reports rescan failure once and keeps the creative editor usable when product information fails", async () => {
    queryState.information = undefined;
    queryState.informationError = "Information endpoint unavailable";
    mockedSendJson.mockRejectedValue(new Error("Rescan failed"));
    const view = render(<VariationsPage active />);
    const nameInput = view.container.querySelector('input[aria-label="Variant name"]') as HTMLInputElement;
    expect(nameInput.disabled).toBe(false);
    fireEvent.click(screen.getByRole("tab", { name: "Assets & Diagnostics" }));
    expect(screen.getByText("Information endpoint unavailable")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Rescan product information" }));
    await waitFor(() => expect(view.container.querySelector(".action-notice")?.textContent).toContain("Rescan failed"));
    expect(view.container.querySelectorAll(".action-notice")).toHaveLength(1);
  });
});
