import {
  VariantField,
  VariantSection,
  VariantSegmented,
  VariantToggle,
  variantUiLabel
} from "../VariantControls";
import type { VariantTabPanelProps } from "../variantTypes";

export function VisualTab({
  variant,
  data,
  featureFlags,
  previewProduct,
  beforeAfterRelevant,
  updateVariant,
  updatePreviewProduct
}: VariantTabPanelProps) {
  const brollAudio = variant.visual_mode === "broll_audio";
  const products = data.product_broll.products;
  return (
    <div className="variant-tab-stack">
      <p className="variant-context-line">
        Current composition: {variantUiLabel(variant.visual_mode)} · {variantUiLabel(variant.color_grade)} grade · {variant.mirror_enabled ? "Mirrored" : "Not mirrored"}
      </p>
      <VariantSection title="Hook visual">
        <div className="variant-field-list">
          {beforeAfterRelevant ? (
            <VariantField label="Before/After mode" htmlFor="variant-before-after-mode">
              <select
                id="variant-before-after-mode"
                value={variant.before_after_mode}
                onChange={(event) => updateVariant({ before_after_mode: event.target.value as "fullscreen" })}
              >
                <option value="fullscreen">Fullscreen</option>
              </select>
            </VariantField>
          ) : (
            <p className="variant-empty-note">Before/After mode becomes available for a Before/After hook type.</p>
          )}
        </div>
      </VariantSection>
      <VariantSection title="B-roll and zoom" detail={brollAudio ? "Host-only B-roll and zoom controls are unavailable in audio-over-B-roll mode." : undefined}>
        {!featureFlags.host_face_zoom && (
          <p className="variant-inline-warning">
            Host-face zoom is disabled globally. This renderer flag is separate from this variant's product-zoom setting.
          </p>
        )}
        <div className="variant-field-list">
          <VariantToggle
            label="Relevant B-roll"
            checked={variant.random_broll_enabled}
            disabled={brollAudio}
            onChange={(random_broll_enabled) => updateVariant({ random_broll_enabled })}
          />
          {brollAudio && products.length > 0 && (
            <VariantField label="Preview sample B-roll product" htmlFor="variant-preview-broll-product" hint="Preview-only; this is not saved in the profile.">
              <select
                id="variant-preview-broll-product"
                value={previewProduct}
                onChange={(event) => updatePreviewProduct(event.target.value)}
              >
                {products.map((product) => (
                  <option value={product.product_key} key={product.product_key}>
                    {product.label} ({product.video_count})
                  </option>
                ))}
              </select>
            </VariantField>
          )}
          <VariantToggle
            label="Product zoom"
            checked={variant.product_zoom_enabled}
            disabled={brollAudio}
            onChange={(product_zoom_enabled) => updateVariant({ product_zoom_enabled })}
          />
          <VariantSegmented
            label="Zoom intensity"
            value={variant.zoom_intensity}
            options={data.zoom_intensities as typeof variant.zoom_intensity[]}
            disabled={brollAudio || !variant.product_zoom_enabled}
            onChange={(zoom_intensity) => updateVariant({ zoom_intensity })}
          />
        </div>
      </VariantSection>
    </div>
  );
}
