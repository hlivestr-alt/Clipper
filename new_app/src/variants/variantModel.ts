import type { VariationProfile, VariationVariant } from "../api";

export type VariantFactory = (index: number, previous?: VariationVariant) => VariationVariant;

export type VariantSummary = {
  hookType: string;
  visualMode: VariationVariant["visual_mode"];
  subtitlesEnabled: boolean;
  dynamicTextMode: VariationVariant["dynamic_text_mode"];
  dynamicTextRoles: VariationVariant["dynamic_text_roles"];
  backgroundMusicEnabled: boolean;
  soundEffectsEnabled: boolean;
  relevantBrollEnabled: boolean;
  productZoomEnabled: boolean;
  mirrored: boolean;
  letterboxed: boolean;
};

export type VariantDependencies = {
  usesBrollAudio: boolean;
  randomBrollDisabled: boolean;
  productZoomDisabled: boolean;
  zoomIntensityDisabled: boolean;
  subtitleDetailsDisabled: boolean;
  dynamicTextRolesDisabled: boolean;
  letterboxDetailsDisabled: boolean;
  topBarHookDetailsDisabled: boolean;
};

export function copyVariationProfile(profile: VariationProfile): VariationProfile {
  return JSON.parse(JSON.stringify(profile)) as VariationProfile;
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(stableValue);
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, stableValue(item)])
    );
  }
  return value;
}

export function stableSerialize(value: unknown): string {
  return JSON.stringify(stableValue(value));
}

export function profilesMatch(
  draft: VariationProfile | null | undefined,
  baseline: VariationProfile | null | undefined
): boolean {
  if (!draft || !baseline) {
    return draft === baseline;
  }
  return stableSerialize(draft) === stableSerialize(baseline);
}

export function isDraftDirty(
  draft: VariationProfile | null | undefined,
  baseline: VariationProfile | null | undefined
): boolean {
  return Boolean(draft && baseline && !profilesMatch(draft, baseline));
}

export function baselineMatchesServer(
  baseline: VariationProfile | null | undefined,
  serverProfile: VariationProfile | null | undefined
): boolean {
  return Boolean(baseline && serverProfile && baseline.revision === serverProfile.revision);
}

export function clampSelectedVariantIndex(index: number, variantCount: number): number {
  if (!Number.isFinite(index) || variantCount <= 0) {
    return 0;
  }
  return Math.max(0, Math.min(Math.trunc(index), variantCount - 1));
}

export function resizeVariationProfile(
  profile: VariationProfile,
  requestedCount: number,
  minimumCount: number,
  maximumCount: number,
  createVariant: VariantFactory
): VariationProfile {
  const count = Math.max(minimumCount, Math.min(maximumCount, Math.trunc(requestedCount)));
  const variants = [...profile.variants];
  while (variants.length < count) {
    variants.push(createVariant(variants.length, variants[variants.length - 1]));
  }
  return { ...profile, variant_count: count, variants };
}

export function patchVariationVariant(
  profile: VariationProfile,
  index: number,
  patch: Partial<VariationVariant>
): VariationProfile {
  if (index < 0 || index >= profile.variants.length) {
    return profile;
  }
  const safePatch = patch.visual_mode === "broll_audio"
    ? { ...patch, random_broll_enabled: false }
    : patch;
  return {
    ...profile,
    variants: profile.variants.map((variant, itemIndex) => (
      itemIndex === index ? { ...variant, ...safePatch } : variant
    ))
  };
}

export function deriveVariantSummary(variant: VariationVariant): VariantSummary {
  return {
    hookType: variant.hook_type,
    visualMode: variant.visual_mode,
    subtitlesEnabled: variant.subtitle_enabled,
    dynamicTextMode: variant.dynamic_text_mode,
    dynamicTextRoles: [...variant.dynamic_text_roles],
    backgroundMusicEnabled: variant.bgm_mode !== "none",
    soundEffectsEnabled: variant.sfx_enabled,
    relevantBrollEnabled: variant.random_broll_enabled,
    productZoomEnabled: variant.product_zoom_enabled,
    mirrored: variant.mirror_enabled,
    letterboxed: variant.letterbox_enabled
  };
}

export function deriveVariantDependencies(variant: VariationVariant): VariantDependencies {
  const usesBrollAudio = variant.visual_mode === "broll_audio";
  return {
    usesBrollAudio,
    randomBrollDisabled: usesBrollAudio,
    productZoomDisabled: usesBrollAudio,
    zoomIntensityDisabled: usesBrollAudio || !variant.product_zoom_enabled,
    subtitleDetailsDisabled: !variant.subtitle_enabled,
    dynamicTextRolesDisabled: variant.dynamic_text_mode === "off",
    letterboxDetailsDisabled: !variant.letterbox_enabled,
    topBarHookDetailsDisabled: !variant.letterbox_enabled || !variant.letterbox_hook_enabled
  };
}

export function createPreviewRequestSignature(
  profile: VariationProfile,
  selectedVariantIndex: number,
  informationProduct: string,
  previewProduct: string
): string {
  return stableSerialize({
    profile,
    selectedVariantIndex: clampSelectedVariantIndex(selectedVariantIndex, profile.variant_count),
    informationProduct,
    previewProduct
  });
}

export function shouldInvalidatePreview(
  renderedSignature: string | null | undefined,
  currentSignature: string
): boolean {
  return Boolean(renderedSignature && renderedSignature !== currentSignature);
}
