// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  ProductInformationStatus,
  VariationPageData,
  VariationPreviewResult,
  VariationVariant
} from "../api";
import { VariantPreviewPanel } from "./VariantPreviewPanel";

function makeVariant(): VariationVariant {
  const roleSetting = {
    headline_font_id: "headline.ttf",
    body_font_id: "body.ttf",
    font_size: 35,
    animation: "current" as const,
    duration_seconds: 2.6
  };
  return {
    name: "Preview variant",
    hook_type: "text_before_after_image",
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
    subtitle_size: "large",
    subtitle_stroke_color: "#000000",
    subtitle_stroke_width: 4,
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
    color_grade: "warm",
    bgm_mode: "auto",
    bgm_path: "",
    sfx_enabled: true,
    zoom_intensity: "strong",
    product_zoom_enabled: true,
    subtitle_enabled: true,
    dynamic_text_mode: "balanced",
    dynamic_text_roles: ["ingredients", "benefits", "usage", "cta"],
    dynamic_text_settings: {
      ingredients: { ...roleSetting },
      benefits: { ...roleSetting },
      usage: { ...roleSetting },
      cta: { ...roleSetting, font_size: 50 }
    },
    letterbox_enabled: true,
    mirror_enabled: true,
    subtitle_y_frac: 0.82,
    letterbox_top_frac: 0.2,
    letterbox_bottom_frac: 0.12,
    letterbox_hook_enabled: true,
    letterbox_hook_font_id: "headline.ttf",
    letterbox_hook_font_color: "#ABCDEF",
    letterbox_hook_font_size: 90,
    letterbox_hook_x_frac: 0.35,
    letterbox_hook_y_frac: 0.4
  };
}

function makeData(): VariationPageData {
  const variant = makeVariant();
  return {
    profile: {
      schema_version: 12,
      revision: "rev-1",
      variant_count: 1,
      updated_at: "",
      variants: [variant]
    },
    fonts: [],
    text_styles: [],
    bgm_tracks: [],
    hook_types: ["text_before_after_image"],
    visual_modes: ["host", "broll_audio"],
    before_after_modes: ["fullscreen"],
    subtitle_positions: ["top", "center", "bottom"],
    subtitle_sizes: ["compact", "small", "medium", "large"],
    dynamic_text_modes: ["off", "minimal", "balanced", "high_energy"],
    dynamic_text_roles: ["ingredients", "benefits", "usage", "cta"],
    dynamic_text_animations: ["current", "staggered_reveal", "fade_up", "wipe", "slide_up"],
    color_grades: ["original", "warm"],
    bgm_modes: ["auto", "none", "selected"],
    zoom_intensities: ["none", "subtle", "normal", "strong"],
    presets: [],
    limits: { min_variants: 1, max_variants: 6 },
    preview_source: {
      path: "C:\\private\\variation_preview\\raw_cut_preview.mp4",
      url: "/api/artifacts?host",
      kind: "video",
      exists: true
    },
    product_broll: {
      root: "C:\\private\\broll",
      exists: true,
      products: [
        {
          product_key: "serum",
          label: "Serum",
          folder: "C:\\private\\broll\\serum",
          exists: true,
          video_count: 3,
          preview: { path: "C:\\private\\broll\\serum\\sample.mp4", url: "/api/artifacts?serum", kind: "video", exists: true }
        },
        {
          product_key: "cleanser",
          label: "Cleanser",
          folder: "C:\\private\\broll\\cleanser",
          exists: true,
          video_count: 2,
          preview: null
        }
      ]
    }
  };
}

function makeInformation(): ProductInformationStatus {
  return {
    schema_version: 1,
    revision: "info-1",
    scanned_at: "",
    root: "C:\\private\\information",
    sources: [{
      path: "C:\\private\\information\\serum.pdf",
      extension: ".pdf",
      size: 20,
      sha256: "hash",
      status: "ok",
      cached: true,
      extraction_method: "rules",
      page_count: 2,
      warnings: [],
      products: ["serum"],
      eligible_fact_count: 8,
      fact_counts: { benefits: 8 },
      unassigned_count: 0
    }],
    products: [{ product_key: "serum", label: "Serum", eligible_fact_count: 8, fact_counts: { benefits: 8 } }],
    unassigned_count: 0,
    conflict_count: 0,
    unassigned: [],
    conflicts: [],
    warnings: []
  };
}

function makeRendered(kind: "video" | "image", url: string): VariationPreviewResult {
  return {
    profile_revision: "rendered-rev",
    source_clip: "C:\\private\\source.mp4",
    preview_source: { path: "C:\\private\\source.mp4", url: "/source.mp4", kind: "video", exists: true },
    previews: [{
      variant_index: 0,
      variant_name: "Preview variant",
      path: `C:\\private\\${kind}`,
      url,
      kind,
      exists: true
    }],
    message: "Rendered preview ready."
  };
}

function renderPanel(overrides: Partial<ComponentProps<typeof VariantPreviewPanel>> = {}) {
  const props: ComponentProps<typeof VariantPreviewPanel> = {
    variant: makeVariant(),
    variantIndex: 0,
    data: makeData(),
    informationData: makeInformation(),
    previewProduct: "serum",
    rendering: false,
    onRender: vi.fn(),
    onMediaError: vi.fn(),
    onMediaLoaded: vi.fn(),
    ...overrides
  };
  return { ...render(<VariantPreviewPanel {...props} />), props };
}

afterEach(cleanup);

describe("Phase 4 persistent variant preview", () => {
  it("follows selected identity, exposes one render action, and contains no editing controls", () => {
    const view = renderPanel();
    expect(screen.getByRole("heading", { name: "Preview variant" })).toBeTruthy();
    expect(screen.getByText("V1")).toBeTruthy();
    expect(screen.getByText("Approximation")).toBeTruthy();
    expect(screen.getByText("Host footage")).toBeTruthy();
    expect(screen.getByText("Warm grade")).toBeTruthy();
    expect(screen.getByText("Strong zoom")).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "Preview variant" })).toBeNull();

    const panel = screen.getByRole("complementary");
    expect(within(panel).getAllByRole("button")).toHaveLength(1);
    expect(within(panel).queryAllByRole("textbox")).toHaveLength(0);
    expect(within(panel).queryAllByRole("spinbutton")).toHaveLength(0);
    expect(within(panel).queryAllByRole("combobox")).toHaveLength(0);
    expect(view.container.querySelector(".subtitle-drag-handle")).toBeNull();
    expect(view.container.querySelector(".preview-adjustments")).toBeNull();

    view.rerender(
      <VariantPreviewPanel
        {...view.props}
        variant={{ ...view.props.variant, name: "Renamed preview" }}
        variantIndex={2}
      />
    );
    expect(screen.getByRole("heading", { name: "Renamed preview" })).toBeTruthy();
    expect(screen.getByText("V3")).toBeTruthy();
  });

  it("uses host and preview-only B-roll fallback media and reports missing sources", () => {
    renderPanel();
    expect(screen.getByLabelText("Approximate host preview for V1 Preview variant").getAttribute("src")).toBe("/api/artifacts?host");
    cleanup();

    const brollVariant = { ...makeVariant(), visual_mode: "broll_audio" as const };
    const broll = renderPanel({ variant: brollVariant, previewProduct: "serum" });
    expect(screen.getByLabelText("Approximate B-roll preview for V1 Preview variant").getAttribute("src")).toBe("/api/artifacts?serum");
    expect(Object.prototype.hasOwnProperty.call(broll.props.variant, "previewProduct")).toBe(false);
    cleanup();

    renderPanel({ variant: brollVariant, previewProduct: "cleanser" });
    expect(screen.getByText("B-roll sample unavailable")).toBeTruthy();
    expect(screen.getByText("Missing source")).toBeTruthy();
    cleanup();

    const missingHostData = makeData();
    missingHostData.preview_source.exists = false;
    renderPanel({ data: missingHostData });
    expect(screen.getByText("Fixed preview source missing")).toBeTruthy();
    expect(screen.getByText("Missing source")).toBeTruthy();
  });

  it("renders video and image results, exposes busy state, and reports artifact failures", () => {
    const onRender = vi.fn();
    const onMediaError = vi.fn();
    const video = renderPanel({
      renderedPreview: makeRendered("video", "/rendered.mp4"),
      onRender,
      onMediaError
    });
    expect(screen.getByText("Rendered")).toBeTruthy();
    expect(screen.getByLabelText("Rendered preview for V1 Preview variant").getAttribute("src")).toBe("/rendered.mp4");
    expect(video.container.querySelector(".variant-preview-approximation-layer")).toBeNull();
    fireEvent.error(screen.getByLabelText("Rendered preview for V1 Preview variant"));
    expect(onMediaError).toHaveBeenCalledWith("The rendered preview artifact could not be loaded.");
    cleanup();

    renderPanel({ renderedPreview: makeRendered("image", "/rendered.png") });
    expect(screen.getByRole("img", { name: "Rendered preview for V1 Preview variant" }).getAttribute("src")).toBe("/rendered.png");
    cleanup();

    renderPanel({ rendering: true });
    const button = screen.getByRole("button", { name: "Rendering 6-second preview" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute("aria-busy")).toBe("true");
    expect(screen.getByRole("status").textContent).toContain("Rendering");
    cleanup();

    renderPanel({ mediaError: "Artifact request failed." });
    expect(screen.getByRole("alert").textContent).toContain("Artifact request failed.");
    expect(screen.getByRole("img", { name: "Artifact request failed." })).toBeTruthy();
  });

  it("shows non-interactive mirror, subtitle, letterbox, hook, grade, and Before/After approximation", () => {
    const view = renderPanel();
    const canvas = view.container.querySelector(".variant-preview-canvas") as HTMLElement;
    expect(canvas.classList.contains("is-flipped")).toBe(true);
    expect(canvas.classList.contains("grade-warm")).toBe(true);
    expect(canvas.getAttribute("data-preview-mode")).toBe("approximation");

    const subtitle = screen.getByTestId("preview-subtitle");
    expect(subtitle.getAttribute("style")).toContain("top: 82%");
    expect(subtitle.getAttribute("style")).toContain("font-size: 20px");
    expect(within(subtitle).getByText("update").getAttribute("style")).toContain("rgb(255, 214, 0)");
    expect(screen.getByTestId("preview-top-bar").getAttribute("style")).toContain("height: 20%");
    expect(screen.getByTestId("preview-bottom-bar").getAttribute("style")).toContain("height: 12%");
    expect(screen.getByTestId("preview-top-hook").textContent).toContain("Auto hook text");
    expect(screen.getByTestId("preview-before-after")).toBeTruthy();
    expect(view.container.querySelector(".variant-preview-approximation-layer")?.getAttribute("aria-hidden")).toBe("true");
    expect(within(screen.getByRole("complementary")).getAllByRole("button")).toHaveLength(1);

    view.rerender(
      <VariantPreviewPanel
        {...view.props}
        variant={{ ...view.props.variant, subtitle_enabled: false }}
      />
    );
    expect(screen.queryByTestId("preview-subtitle")).toBeNull();
  });

  it("derives concise readiness from real data without exposing filesystem paths", () => {
    const ready = renderPanel();
    const readiness = screen.getByRole("region", { name: "Readiness" });
    expect(within(readiness).getByText("Product information")).toBeTruthy();
    expect(within(readiness).getByText("Fixed preview source")).toBeTruthy();
    expect(within(readiness).getByText("B-roll inventory")).toBeTruthy();
    expect(within(readiness).getByText("5 clips · 1 without a sample")).toBeTruthy();
    expect(ready.container.textContent).not.toContain("C:\\private");
    cleanup();

    const warningInformation = makeInformation();
    warningInformation.warnings = ["One source warning"];
    renderPanel({ informationData: warningInformation });
    expect(within(screen.getByRole("region", { name: "Readiness" })).getAllByText("Warning").length).toBeGreaterThan(0);
    cleanup();

    const missingData = makeData();
    missingData.preview_source.exists = false;
    missingData.product_broll = { root: "private", exists: false, products: [] };
    renderPanel({ data: missingData, informationData: undefined });
    const missingReadiness = screen.getByRole("region", { name: "Readiness" });
    expect(within(missingReadiness).getAllByText("Missing")).toHaveLength(3);
  });
});
