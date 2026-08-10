// @vitest-environment jsdom

import { useState } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type {
  ProductInformationStatus,
  VariationPageData,
  VariationVariant
} from "../api";
import { VariantEditorTabs } from "./VariantEditorTabs";
import type { VariantFeatureFlags } from "./variantTypes";

function makeVariant(name = "Variant one"): VariationVariant {
  const setting = {
    headline_font_id: "headline.ttf",
    body_font_id: "body.ttf",
    font_size: 35,
    animation: "current" as const,
    duration_seconds: 2.6
  };
  return {
    name,
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
    subtitle_stroke_color: "#010101",
    subtitle_stroke_width: 4,
    subtitle_highlight_enabled: true,
    subtitle_animation: "phrase_cut",
    headline_animation: "soft_pop",
    caption_animation: "fade_up",
    headline_stroke_width: 5,
    headline_shadow_color: "#020202",
    headline_shadow_x: 3,
    headline_shadow_y: 4,
    headline_rotation_degrees: 1,
    caption_stroke_width: 4,
    color_grade: "warm",
    bgm_mode: "auto",
    bgm_path: "saved-track.mp3",
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
      cta: { ...setting, font_size: 50, duration_seconds: 1.3 }
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

const pageData: VariationPageData = {
  profile: { schema_version: 12, revision: "rev-1", variant_count: 2, updated_at: "", variants: [makeVariant(), makeVariant("Variant two")] },
  fonts: [
    { id: "subtitle.ttf", path: "subtitle.ttf", label: "Subtitle" },
    { id: "headline.ttf", path: "headline.ttf", label: "Headline" },
    { id: "body.ttf", path: "body.ttf", label: "Body" },
    { id: "caption.ttf", path: "caption.ttf", label: "Caption" }
  ],
  text_styles: [
    { id: "current", label: "Current", description: "Keep current values.", defaults: {} },
    {
      id: "creator_bold_pop",
      label: "Creator Bold Pop",
      description: "Reapply the complete creator style.",
      defaults: {
        font_color: "#EEEEEE",
        subtitle_stroke_width: 9,
        headline_shadow_x: 8,
        caption_animation: "wipe"
      }
    }
  ],
  bgm_tracks: [
    { path: "saved-track.mp3", label: "Saved track" },
    { path: "new-track.mp3", label: "New track" }
  ],
  hook_types: ["text", "before_after_image"],
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
  preview_source: { path: "preview.mp4", url: "/preview.mp4", kind: "video", exists: true },
  product_broll: {
    root: "broll",
    exists: true,
    products: [
      { product_key: "serum", label: "Serum", folder: "serum", exists: true, video_count: 2 },
      { product_key: "cleanser", label: "Cleanser", folder: "cleanser", exists: true, video_count: 1 }
    ]
  },
  global_feature_flags: {
    sfx: true,
    bgm: true,
    before_after: true,
    broll_intro: true,
    transitional_hook: true,
    host_face_zoom: true
  }
};

const informationData: ProductInformationStatus = {
  schema_version: 1,
  revision: "info-1",
  scanned_at: "",
  root: "information",
  sources: [],
  products: [
    { product_key: "serum", label: "Serum", eligible_fact_count: 8, fact_counts: {} },
    { product_key: "cleanser", label: "Cleanser", eligible_fact_count: 3, fact_counts: {} }
  ],
  unassigned_count: 0,
  conflict_count: 0,
  unassigned: [],
  conflicts: [],
  warnings: []
};

function clamp(value: number, minimum: number, maximum: number) {
  return Math.max(minimum, Math.min(maximum, value));
}

function EditorHarness({ featureFlags = pageData.global_feature_flags! }: { featureFlags?: VariantFeatureFlags }) {
  const [variantIndex, setVariantIndex] = useState(0);
  const [variants, setVariants] = useState([makeVariant(), makeVariant("Variant two")]);
  const [previewProduct, setPreviewProduct] = useState("serum");
  const [previewInformationProduct, setPreviewInformationProduct] = useState("serum");
  const variant = variants[variantIndex];
  const updateVariant = (patch: Partial<VariationVariant>) => {
    setVariants((current) => current.map((item, index) => index === variantIndex ? { ...item, ...patch } : item));
  };
  return (
    <>
      <button type="button" onClick={() => setVariantIndex((current) => current === 0 ? 1 : 0)}>Switch variant</button>
      <VariantEditorTabs
        variant={variant}
        variantIndex={variantIndex}
        data={pageData}
        informationData={informationData}
        featureFlags={featureFlags}
        previewProduct={previewProduct}
        previewInformationProduct={previewInformationProduct}
        updateVariant={updateVariant}
        applyTextStyle={(textStyleId) => {
          const style = pageData.text_styles.find((item) => item.id === textStyleId);
          updateVariant({ ...(style?.defaults ?? {}), text_style_id: textStyleId });
        }}
        updateSubtitleY={(value) => {
          const subtitle_y_frac = clamp(value, 0.08, 0.92);
          updateVariant({
            subtitle_y_frac,
            subtitle_position: subtitle_y_frac < 0.46 ? "top" : subtitle_y_frac < 0.70 ? "center" : "bottom"
          });
        }}
        updateLetterboxEnabled={(enabled) => updateVariant({
          letterbox_enabled: enabled,
          letterbox_top_frac: enabled && variant.letterbox_top_frac <= 0 ? 0.2 : variant.letterbox_top_frac,
          letterbox_bottom_frac: enabled && variant.letterbox_bottom_frac <= 0 ? 0.2 : variant.letterbox_bottom_frac
        })}
        updateLetterboxHookEnabled={(enabled) => updateVariant({
          letterbox_hook_enabled: enabled,
          letterbox_hook_font_id: variant.letterbox_hook_font_id || variant.font_id
        })}
        updateDynamicTextRole={(role, enabled) => updateVariant({
          dynamic_text_roles: enabled
            ? pageData.dynamic_text_roles.filter((item) => new Set([...variant.dynamic_text_roles, role]).has(item))
            : variant.dynamic_text_roles.filter((item) => item !== role)
        })}
        updateDynamicTextSetting={(role, patch) => updateVariant({
          dynamic_text_settings: {
            ...variant.dynamic_text_settings,
            [role]: { ...variant.dynamic_text_settings[role], ...patch }
          }
        })}
        updatePreviewProduct={setPreviewProduct}
        updatePreviewInformationProduct={setPreviewInformationProduct}
        hookTypeAvailable={() => true}
        beforeAfterRelevant={variant.hook_type.includes("before_after")}
      />
      <output data-testid="variant-state">{JSON.stringify({ variant, previewProduct, previewInformationProduct })}</output>
    </>
  );
}

function state() {
  return JSON.parse(screen.getByTestId("variant-state").textContent ?? "{}") as {
    variant: VariationVariant;
    previewProduct: string;
    previewInformationProduct: string;
  };
}

afterEach(cleanup);

describe("Phase 3 variant editor tabs", () => {
  it("provides seven related tabs with mouse, arrow-key, summary navigation, and persistent active state", () => {
    const view = render(<EditorHarness />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual([
      "Basics",
      "Text & Subtitles",
      "Visual",
      "Audio",
      "Dynamic Text",
      "Advanced",
      "Assets & Diagnostics"
    ]);
    for (const tab of tabs) {
      const panelId = tab.getAttribute("aria-controls") ?? "";
      expect(view.container.querySelector(`#${panelId}`)).toBeTruthy();
      expect(view.container.querySelector(`#${panelId}`)?.getAttribute("aria-labelledby")).toBe(tab.id);
    }

    fireEvent.click(screen.getByRole("tab", { name: "Audio" }));
    expect(screen.getByRole("tab", { name: "Audio" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(screen.getByRole("tab", { name: "Audio" }), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Dynamic Text" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Switch variant" }));
    expect(screen.getByRole("tab", { name: "Dynamic Text" }).getAttribute("aria-selected")).toBe("true");

    fireEvent.click(screen.getByRole("tab", { name: "Basics" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit subtitles" }));
    expect(screen.getByRole("tab", { name: "Text & Subtitles" }).getAttribute("aria-selected")).toBe("true");
  });

  it("has one owner for migrated fields and no obsolete accordion controls", () => {
    const view = render(<EditorHarness />);
    expect(view.container.querySelectorAll("#variant-subtitle-y")).toHaveLength(1);
    expect(view.container.querySelectorAll("#variant-letterbox-top")).toHaveLength(1);
    expect(view.container.querySelectorAll("#variant-letterbox-bottom")).toHaveLength(1);
    expect(view.container.querySelectorAll("#variant-letterbox-hook-x")).toHaveLength(1);
    expect(view.container.querySelectorAll("#variant-letterbox-hook-y")).toHaveLength(1);
    expect(view.container.querySelectorAll("#variant-bgm-mode")).toHaveLength(1);
    expect(view.container.querySelectorAll(".variant-row-body")).toHaveLength(0);
    expect(view.container.querySelectorAll(".dynamic-content-card")).toHaveLength(0);
    expect(view.container.querySelectorAll(".variant-summary-card input, .variant-summary-card select")).toHaveLength(0);
  });

  it("preserves Basics behavior and reapplies complete style defaults", () => {
    render(<EditorHarness />);
    fireEvent.change(screen.getByLabelText("Variant name"), { target: { value: "Renamed" } });
    expect(state().variant.name).toBe("Renamed");

    fireEvent.change(screen.getByLabelText("Hook type"), { target: { value: "before_after_image" } });
    expect(state().variant.hook_type).toBe("before_after_image");
    fireEvent.click(screen.getByRole("button", { name: "Audio over B-roll" }));
    expect(state().variant.visual_mode).toBe("broll_audio");
    fireEvent.change(screen.getByLabelText("Text style"), { target: { value: "creator_bold_pop" } });
    expect(state().variant).toMatchObject({
      text_style_id: "creator_bold_pop",
      subtitle_stroke_width: 9,
      headline_shadow_x: 8,
      caption_animation: "wipe"
    });
    fireEvent.click(screen.getByRole("button", { name: "Reapply style values" }));
    expect(state().variant.subtitle_stroke_width).toBe(9);
    fireEvent.change(screen.getByLabelText("Color grade"), { target: { value: "original" } });
    fireEvent.click(screen.getByLabelText("Flip video"));
    expect(state().variant).toMatchObject({ color_grade: "original", mirror_enabled: true });
  });

  it("keeps subtitle placement, exact Y, typography, and disabled dependencies in one tab", () => {
    render(<EditorHarness />);
    fireEvent.click(screen.getByRole("tab", { name: "Text & Subtitles" }));
    fireEvent.click(screen.getByRole("button", { name: "Top" }));
    expect((screen.getByLabelText("Exact subtitle Y") as HTMLInputElement).value).toBe("34");
    fireEvent.change(screen.getByLabelText("Exact subtitle Y"), { target: { value: "60" } });
    expect(state().variant).toMatchObject({ subtitle_y_frac: 0.6, subtitle_position: "center" });
    fireEvent.change(screen.getByLabelText("Exact subtitle Y"), { target: { value: "99" } });
    expect(state().variant.subtitle_y_frac).toBe(0.92);
    fireEvent.click(screen.getByRole("button", { name: "Compact" }));
    fireEvent.click(screen.getByLabelText("Active-word highlighting"));
    fireEvent.change(screen.getByLabelText("Subtitle font"), { target: { value: "body.ttf" } });
    fireEvent.change(screen.getByLabelText("Headline font"), { target: { value: "subtitle.ttf" } });
    fireEvent.change(screen.getByLabelText("Product-caption font"), { target: { value: "headline.ttf" } });
    fireEvent.change(screen.getByLabelText("Base font color"), { target: { value: "#123456" } });
    fireEvent.change(screen.getByLabelText("Highlight color"), { target: { value: "#654321" } });
    expect(state().variant).toMatchObject({
      subtitle_size: "compact",
      subtitle_highlight_enabled: false,
      font_id: "body.ttf",
      headline_font_id: "subtitle.ttf",
      caption_font_id: "headline.ttf",
      font_color: "#123456",
      highlight_color: "#654321"
    });
    fireEvent.click(screen.getByLabelText("Subtitles"));
    expect((screen.getByLabelText("Exact subtitle Y") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Bottom" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("preserves Visual and Audio dependencies and preview-only selections", () => {
    render(<EditorHarness />);
    fireEvent.change(screen.getByLabelText("Hook type"), { target: { value: "before_after_image" } });
    fireEvent.click(screen.getByRole("button", { name: "Audio over B-roll" }));
    fireEvent.click(screen.getByRole("tab", { name: "Visual" }));
    expect((screen.getByLabelText("Before/After mode") as HTMLSelectElement).options).toHaveLength(1);
    expect((screen.getByLabelText("Relevant B-roll") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("Product zoom") as HTMLInputElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Preview sample B-roll product"), { target: { value: "cleanser" } });
    expect(state().previewProduct).toBe("cleanser");
    expect(Object.prototype.hasOwnProperty.call(state().variant, "previewProduct")).toBe(false);

    fireEvent.click(screen.getByRole("tab", { name: "Audio" }));
    fireEvent.change(screen.getByLabelText("Background music mode"), { target: { value: "selected" } });
    fireEvent.change(screen.getByLabelText("Selected BGM track"), { target: { value: "new-track.mp3" } });
    fireEvent.change(screen.getByLabelText("Background music mode"), { target: { value: "none" } });
    expect(state().variant).toMatchObject({ bgm_mode: "none", bgm_path: "new-track.mp3" });
    fireEvent.click(screen.getByLabelText("Sound effects"));
    expect(state().variant.sfx_enabled).toBe(false);
  });

  it("exposes global audio overrides without clearing stored per-variant choices", () => {
    render(<EditorHarness featureFlags={{ ...pageData.global_feature_flags!, bgm: false, sfx: false }} />);
    fireEvent.click(screen.getByRole("tab", { name: "Audio" }));
    expect((screen.getByLabelText("Background music mode") as HTMLSelectElement).disabled).toBe(true);
    expect((screen.getByLabelText("Sound effects") as HTMLInputElement).disabled).toBe(true);
    expect(state().variant).toMatchObject({
      bgm_mode: "auto",
      bgm_path: "saved-track.mp3",
      sfx_enabled: true
    });
  });

  it("shows four flat dynamic role cards with supported fields, limits, animations, and Off dependencies", () => {
    const view = render(<EditorHarness />);
    fireEvent.click(screen.getByRole("tab", { name: "Dynamic Text" }));
    expect(view.container.querySelectorAll(".variant-dynamic-role-card")).toHaveLength(4);
    for (const label of ["Ingredients", "Benefits", "Usage", "CTA"]) {
      expect(screen.getByRole("heading", { name: label })).toBeTruthy();
    }
    const ctaCard = screen.getByRole("heading", { name: "CTA" }).closest(".variant-dynamic-role-card") as HTMLElement;
    expect(within(ctaCard).queryByLabelText("Body font")).toBeNull();
    expect((within(ctaCard).getByLabelText("Text size") as HTMLInputElement).min).toBe("24");
    expect((within(ctaCard).getByLabelText("Text size") as HTMLInputElement).max).toBe("96");
    const usageCard = screen.getByRole("heading", { name: "Usage" }).closest(".variant-dynamic-role-card") as HTMLElement;
    expect(within(usageCard).getByLabelText("Heading font")).toBeTruthy();
    expect(within(usageCard).getByLabelText("Body font")).toBeTruthy();
    const animation = within(usageCard).getByLabelText("Animation") as HTMLSelectElement;
    expect(Array.from(animation.options).map((option) => option.value)).toEqual([
      "current", "staggered_reveal", "fade_up", "wipe", "slide_up"
    ]);
    fireEvent.change(within(usageCard).getByLabelText("Duration"), { target: { value: "6.9" } });
    expect(state().variant.dynamic_text_settings.usage.duration_seconds).toBe(6);
    fireEvent.change(screen.getByLabelText("Preview information product"), { target: { value: "cleanser" } });
    expect(state().previewInformationProduct).toBe("cleanser");
    fireEvent.click(within(usageCard).getByLabelText("Enable Usage"));
    expect(state().variant.dynamic_text_roles).not.toContain("usage");
    fireEvent.click(within(usageCard).getByLabelText("Enable Usage"));
    expect(state().variant.dynamic_text_roles).toContain("usage");
    fireEvent.click(screen.getByRole("button", { name: "Off" }));
    expect((within(usageCard).getByLabelText("Duration") as HTMLInputElement).disabled).toBe(true);
    expect((within(usageCard).getByLabelText("Enable Usage") as HTMLInputElement).disabled).toBe(true);
  });

  it("initializes and edits Advanced letterbox and hook settings within documented limits", () => {
    render(<EditorHarness />);
    fireEvent.click(screen.getByRole("tab", { name: "Advanced" }));
    expect((screen.getByLabelText("Top bar height") as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(screen.getByLabelText("Black bars"));
    expect((screen.getByLabelText("Top bar height") as HTMLInputElement).value).toBe("20");
    expect((screen.getByLabelText("Bottom bar height") as HTMLInputElement).value).toBe("20");
    fireEvent.change(screen.getByLabelText("Top bar height"), { target: { value: "45" } });
    fireEvent.change(screen.getByLabelText("Bottom bar height"), { target: { value: "12" } });
    expect(state().variant).toMatchObject({ letterbox_top_frac: 0.4, letterbox_bottom_frac: 0.12 });
    fireEvent.click(screen.getByLabelText("Automatic top-bar hook"));
    fireEvent.change(screen.getByLabelText("Top-bar hook font"), { target: { value: "body.ttf" } });
    fireEvent.change(screen.getByLabelText("Top-bar hook color"), { target: { value: "#ABCDEF" } });
    fireEvent.change(screen.getByLabelText("Top-bar hook size"), { target: { value: "170" } });
    fireEvent.change(screen.getByLabelText("Top-bar hook X position"), { target: { value: "25" } });
    fireEvent.change(screen.getByLabelText("Top-bar hook Y position"), { target: { value: "75" } });
    expect(state().variant).toMatchObject({
      letterbox_hook_enabled: true,
      letterbox_hook_font_id: "body.ttf",
      letterbox_hook_font_color: "#ABCDEF",
      letterbox_hook_font_size: 160,
      letterbox_hook_x_frac: 0.25,
      letterbox_hook_y_frac: 0.75
    });
  });
});
