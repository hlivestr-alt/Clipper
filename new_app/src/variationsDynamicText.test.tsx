// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { VariationPageData, VariationVariant } from "./api";

const dynamicSettings: VariationVariant["dynamic_text_settings"] = {
  ingredients: { headline_font_id: "font.ttf", body_font_id: "font.ttf", font_size: 35, animation: "current", duration_seconds: 2.6 },
  benefits: { headline_font_id: "font.ttf", body_font_id: "font.ttf", font_size: 35, animation: "current", duration_seconds: 2.6 },
  usage: { headline_font_id: "font.ttf", body_font_id: "font.ttf", font_size: 35, animation: "current", duration_seconds: 2.6 },
  cta: { headline_font_id: "font.ttf", body_font_id: "font.ttf", font_size: 50, animation: "current", duration_seconds: 1.3 }
};

const variant = {
  name: "Test variant",
  hook_type: "text",
  visual_mode: "host",
  random_broll_enabled: false,
  before_after_mode: "fullscreen",
  text_style_id: "current",
  font_id: "font.ttf",
  headline_font_id: "font.ttf",
  caption_font_id: "font.ttf",
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
  bgm_path: "",
  sfx_enabled: true,
  zoom_intensity: "normal",
  product_zoom_enabled: true,
  subtitle_enabled: true,
  dynamic_text_mode: "balanced",
  dynamic_text_roles: ["ingredients", "benefits", "usage", "cta"],
  dynamic_text_settings: dynamicSettings,
  letterbox_enabled: false,
  mirror_enabled: false,
  subtitle_y_frac: 0.84,
  letterbox_top_frac: 0,
  letterbox_bottom_frac: 0,
  letterbox_hook_enabled: false,
  letterbox_hook_font_id: "font.ttf",
  letterbox_hook_font_color: "#FFFFFF",
  letterbox_hook_font_size: 72,
  letterbox_hook_x_frac: 0.5,
  letterbox_hook_y_frac: 0.5
} satisfies VariationVariant;

const pageData = {
  profile: { schema_version: 12, revision: "rev-1", variant_count: 1, updated_at: "", variants: [variant] },
  fonts: [{ id: "font.ttf", path: "font.ttf", label: "Test Font" }],
  text_styles: [{ id: "current", label: "Current", description: "Current", defaults: {} }],
  bgm_tracks: [],
  hook_types: ["text"],
  visual_modes: ["host"],
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
  preview_source: { path: "", url: "", kind: "video", exists: false },
  product_broll: { root: "", exists: false, products: [] },
  global_feature_flags: {
    sfx: true,
    bgm: true,
    before_after: true,
    broll_intro: true,
    transitional_hook: true,
    host_face_zoom: true
  }
} satisfies VariationPageData;

vi.mock("./useApiQuery", () => ({
  useApiQuery: (path: string) => path === "/api/variations"
    ? { envelope: { data: pageData, warnings: [] }, loading: false, refreshing: false, refresh: vi.fn() }
    : { loading: false, refreshing: false, refresh: vi.fn() }
}));

import { VariationsPage } from "./App";

describe("variation dynamic-text controls", () => {
  it("renders all role cards and tracks role-setting edits as unsaved", async () => {
    const { container } = render(<VariationsPage active />);

    fireEvent.click(await screen.findByRole("tab", { name: "Dynamic Text" }));
    await waitFor(() => expect(container.querySelectorAll(".variant-dynamic-role-card")).toHaveLength(4));
    expect(screen.getByText("Ingredients")).toBeTruthy();
    expect(screen.getByText("Benefits")).toBeTruthy();
    expect(screen.getByText("Usage")).toBeTruthy();
    expect(screen.getByText("CTA")).toBeTruthy();
    const firstCard = container.querySelector(".variant-dynamic-role-card") as HTMLElement;

    const duration = firstCard.querySelector('input[id$="-duration"]') as HTMLInputElement;
    expect(duration.value).toBe("2.6");
    fireEvent.change(duration, { target: { value: "3.4" } });
    expect(duration.value).toBe("3.4");
    expect((screen.getByRole("button", { name: "Apply to future clips" }) as HTMLButtonElement).disabled).toBe(false);

    const enabled = firstCard.querySelector('input[type="checkbox"]') as HTMLInputElement;
    fireEvent.click(enabled);
    expect(enabled.checked).toBe(false);
  });
});
