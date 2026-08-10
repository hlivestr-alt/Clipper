import { describe, expect, it } from "vitest";
import type { VariationProfile, VariationVariant } from "../api";
import {
  baselineMatchesServer,
  clampSelectedVariantIndex,
  createPreviewRequestSignature,
  deriveVariantDependencies,
  deriveVariantSummary,
  isDraftDirty,
  patchVariationVariant,
  profilesMatch,
  resizeVariationProfile,
  shouldInvalidatePreview
} from "./variantModel";

function makeVariant(index = 0): VariationVariant {
  const roleSetting = {
    headline_font_id: "headline.ttf",
    body_font_id: "body.ttf",
    font_size: 35,
    animation: "fade_up" as const,
    duration_seconds: 2.6
  };
  return {
    name: `Variant ${index + 1}`,
    hook_type: "text",
    visual_mode: "host",
    random_broll_enabled: true,
    before_after_mode: "fullscreen",
    text_style_id: "creator_bold_pop",
    font_id: "subtitle.ttf",
    headline_font_id: "headline.ttf",
    caption_font_id: "caption.ttf",
    font_color: "#FFFFFF",
    highlight_color: "#FFD600",
    subtitle_position: "bottom",
    subtitle_size: "medium",
    subtitle_stroke_color: "#101010",
    subtitle_stroke_width: 7,
    subtitle_highlight_enabled: true,
    subtitle_animation: "phrase_cut",
    headline_animation: "pop_overshoot",
    caption_animation: "staggered_reveal",
    headline_stroke_width: 8,
    headline_shadow_color: "#222222",
    headline_shadow_x: 5,
    headline_shadow_y: 6,
    headline_rotation_degrees: -2,
    caption_stroke_width: 6,
    color_grade: "warm",
    bgm_mode: "selected",
    bgm_path: "music.mp3",
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
      cta: { ...roleSetting, font_size: 50, duration_seconds: 1.3 }
    },
    letterbox_enabled: true,
    mirror_enabled: false,
    subtitle_y_frac: 0.84,
    letterbox_top_frac: 0.2,
    letterbox_bottom_frac: 0.2,
    letterbox_hook_enabled: true,
    letterbox_hook_font_id: "hook.ttf",
    letterbox_hook_font_color: "#F0F0F0",
    letterbox_hook_font_size: 72,
    letterbox_hook_x_frac: 0.5,
    letterbox_hook_y_frac: 0.4
  };
}

function makeProfile(count = 1, revision = "rev-1"): VariationProfile {
  return {
    schema_version: 12,
    revision,
    variant_count: count,
    updated_at: "2026-07-29T00:00:00Z",
    variants: Array.from({ length: count }, (_, index) => makeVariant(index))
  };
}

describe("variant state model", () => {
  it("compares a draft to its accepted baseline instead of an unrelated server snapshot", () => {
    const baseline = makeProfile();
    const draft = structuredClone(baseline);
    const refreshedServer = makeProfile(1, "rev-2");
    refreshedServer.variants[0].name = "Server update";

    expect(profilesMatch(draft, baseline)).toBe(true);
    expect(isDraftDirty(draft, baseline)).toBe(false);
    expect(baselineMatchesServer(baseline, refreshedServer)).toBe(false);

    draft.variants[0].name = "Local edit";
    expect(isDraftDirty(draft, baseline)).toBe(true);
    expect(isDraftDirty(draft, refreshedServer)).toBe(true);
  });

  it("supports variant counts from one through six and clamps the selected index", () => {
    let profile = makeProfile();
    for (let count = 1; count <= 6; count += 1) {
      profile = resizeVariationProfile(profile, count, 1, 6, makeVariant);
      expect(profile.variant_count).toBe(count);
      expect(profile.variants).toHaveLength(count);
    }

    expect(clampSelectedVariantIndex(5, 6)).toBe(5);
    expect(clampSelectedVariantIndex(5, 2)).toBe(1);
    expect(clampSelectedVariantIndex(-3, 2)).toBe(0);
    expect(resizeVariationProfile(profile, 99, 1, 6, makeVariant).variant_count).toBe(6);
  });

  it("preserves hidden style-driven fields when applying a full-variant patch", () => {
    const profile = makeProfile();
    const original = profile.variants[0];
    const updated = patchVariationVariant(profile, 0, {
      name: "Renamed",
      visual_mode: "broll_audio"
    });

    expect(updated.variants[0]).toMatchObject({
      name: "Renamed",
      visual_mode: "broll_audio",
      random_broll_enabled: false,
      subtitle_stroke_color: original.subtitle_stroke_color,
      subtitle_stroke_width: original.subtitle_stroke_width,
      subtitle_animation: original.subtitle_animation,
      headline_animation: original.headline_animation,
      caption_animation: original.caption_animation,
      headline_stroke_width: original.headline_stroke_width,
      headline_shadow_color: original.headline_shadow_color,
      headline_shadow_x: original.headline_shadow_x,
      headline_shadow_y: original.headline_shadow_y,
      headline_rotation_degrees: original.headline_rotation_degrees,
      caption_stroke_width: original.caption_stroke_width
    });
  });

  it("derives summaries, dependencies, and all four dynamic-text roles from actual state", () => {
    const variant = makeVariant();
    expect(deriveVariantSummary(variant).dynamicTextRoles).toEqual([
      "ingredients",
      "benefits",
      "usage",
      "cta"
    ]);
    expect(Object.keys(variant.dynamic_text_settings)).toEqual([
      "ingredients",
      "benefits",
      "usage",
      "cta"
    ]);
    expect(deriveVariantDependencies(variant)).toMatchObject({
      usesBrollAudio: false,
      randomBrollDisabled: false,
      productZoomDisabled: false,
      subtitleDetailsDisabled: false,
      dynamicTextRolesDisabled: false,
      letterboxDetailsDisabled: false,
      topBarHookDetailsDisabled: false
    });
  });

  it("invalidates preview signatures for draft, selection, or preview-product changes", () => {
    const profile = makeProfile(2);
    const initial = createPreviewRequestSignature(profile, 0, "serum", "serum-broll");
    const changedDraft = structuredClone(profile);
    changedDraft.variants[0].font_color = "#ABCDEF";

    const signatures = [
      createPreviewRequestSignature(changedDraft, 0, "serum", "serum-broll"),
      createPreviewRequestSignature(profile, 1, "serum", "serum-broll"),
      createPreviewRequestSignature(profile, 0, "cleanser", "serum-broll"),
      createPreviewRequestSignature(profile, 0, "serum", "cleanser-broll")
    ];

    for (const signature of signatures) {
      expect(signature).not.toBe(initial);
      expect(shouldInvalidatePreview(initial, signature)).toBe(true);
    }
    expect(shouldInvalidatePreview(initial, initial)).toBe(false);
  });
});
