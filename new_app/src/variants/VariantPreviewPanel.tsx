import { AlertTriangle, CheckCircle2, Film, LoaderCircle, RefreshCw, Video } from "lucide-react";
import type {
  ProductInformationStatus,
  VariationPageData,
  VariationPreviewMedia,
  VariationPreviewResult,
  VariationVariant
} from "../api";
import type { VariantPreviewFeedback } from "./variantTypes";

type VariantPreviewPanelProps = {
  variant: VariationVariant;
  variantIndex: number;
  data: VariationPageData;
  informationData?: ProductInformationStatus;
  previewProduct: string;
  renderedPreview?: VariationPreviewResult;
  rendering: boolean;
  feedback?: VariantPreviewFeedback;
  mediaError?: string;
  onRender: () => void;
  onMediaError: (message: string) => void;
  onMediaLoaded: () => void;
};

type ReadinessTone = "ready" | "warning" | "missing";

function clamp(value: number, minimum: number, maximum: number) {
  return Math.max(minimum, Math.min(maximum, value));
}

function label(value: string) {
  const labels: Record<string, string> = {
    host: "Host footage",
    broll_audio: "Audio over B-roll",
    original: "Original",
    none: "None",
    subtle: "Subtle",
    normal: "Normal",
    strong: "Strong"
  };
  return labels[value] ?? String(value || "").replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function subtitleFontSize(size: VariationVariant["subtitle_size"]) {
  if (size === "compact") return 12;
  if (size === "small") return 14;
  if (size === "large") return 20;
  return 17;
}

function hookFontSize(value: number) {
  return Math.round(clamp(value * 0.32, 12, 34));
}

function usesBeforeAfter(variant: Pick<VariationVariant, "hook_type">) {
  return variant.hook_type === "before_after_image" || variant.hook_type === "text_before_after_image";
}

function renderedMedia(result?: VariationPreviewResult): VariationPreviewMedia | undefined {
  return result?.previews.find((preview) => preview.exists);
}

function informationReadiness(informationData?: ProductInformationStatus): {
  tone: ReadinessTone;
  status: string;
  detail: string;
} {
  if (!informationData || informationData.sources.length === 0) {
    return { tone: "missing", status: "Missing", detail: "No indexed product documents" };
  }
  const factCount = informationData.products.reduce((total, product) => total + product.eligible_fact_count, 0);
  if (
    informationData.warnings.length > 0
    || informationData.unassigned_count > 0
    || informationData.conflict_count > 0
    || informationData.sources.some((source) => source.status !== "ok")
  ) {
    return { tone: "warning", status: "Warning", detail: `${factCount} eligible facts` };
  }
  return { tone: "ready", status: "Ready", detail: `${factCount} eligible facts` };
}

function ReadinessRow({
  label: rowLabel,
  status,
  detail,
  tone
}: {
  label: string;
  status: string;
  detail: string;
  tone: ReadinessTone;
}) {
  return (
    <div className={`variant-preview-readiness-row ${tone}`}>
      <span className="variant-preview-readiness-icon" aria-hidden="true">
        {tone === "ready" ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}
      </span>
      <span>
        <strong>{rowLabel}</strong>
        <small>{detail}</small>
      </span>
      <b>{status}</b>
    </div>
  );
}

export function VariantPreviewPanel({
  variant,
  variantIndex,
  data,
  informationData,
  previewProduct,
  renderedPreview,
  rendering,
  feedback,
  mediaError,
  onRender,
  onMediaError,
  onMediaLoaded
}: VariantPreviewPanelProps) {
  const selectedBrollProduct = data.product_broll.products.find((product) => product.product_key === previewProduct)
    ?? data.product_broll.products[0];
  const rendered = renderedMedia(renderedPreview);
  const approximation = !rendered;
  const brollMode = variant.visual_mode === "broll_audio";
  const fallbackAvailable = brollMode
    ? Boolean(selectedBrollProduct?.preview?.exists)
    : data.preview_source.exists;
  const missingSource = approximation && !fallbackAvailable;
  const information = informationReadiness(informationData);
  const brollCount = data.product_broll.products.reduce((total, product) => total + product.video_count, 0);
  const brollMissingSamples = data.product_broll.products.filter(
    (product) => product.video_count > 0 && !product.preview?.exists
  ).length;
  const brollTone: ReadinessTone = brollCount === 0
    ? "missing"
    : brollMissingSamples > 0 ? "warning" : "ready";
  const brollStatus = brollCount === 0 ? "Missing" : brollMissingSamples > 0 ? "Warning" : "Ready";
  const brollDetail = brollCount === 0
    ? "No local B-roll clips"
    : `${brollCount} clip${brollCount === 1 ? "" : "s"}${brollMissingSamples ? ` · ${brollMissingSamples} without a sample` : ""}`;
  const previewState = rendering
    ? { label: "Rendering", tone: "rendering" }
    : mediaError || feedback?.kind === "error"
      ? { label: "Error", tone: "error" }
      : rendered
        ? { label: "Rendered", tone: "rendered" }
        : missingSource
          ? { label: "Missing source", tone: "warning" }
          : feedback?.kind === "warning"
            ? { label: "No result", tone: "warning" }
            : { label: "Approximation", tone: "approximation" };
  const mediaLabel = rendered
    ? `Rendered preview for V${variantIndex + 1} ${variant.name}`
    : brollMode
      ? `Approximate B-roll preview for V${variantIndex + 1} ${variant.name}`
      : `Approximate host preview for V${variantIndex + 1} ${variant.name}`;

  return (
    <aside
      className="panel variant-preview-panel"
      id="variant-preview-panel"
      tabIndex={-1}
      aria-labelledby="variant-preview-heading"
    >
      <header className="variant-preview-header">
        <div>
          <span className="variant-preview-eyebrow">V{variantIndex + 1}</span>
          <h2 id="variant-preview-heading" title={variant.name}>{variant.name}</h2>
        </div>
        <div
          className={`variant-preview-state ${previewState.tone}`}
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {rendering && <LoaderCircle className="is-spinning" size={14} aria-hidden="true" />}
          {previewState.label}
        </div>
      </header>

      <div className="variant-preview-meta" aria-label="Selected variant preview metadata">
        <span>{label(variant.visual_mode)}</span>
        <span>{label(variant.color_grade)} grade</span>
        <span>
          {variant.visual_mode === "broll_audio"
            ? "Host zoom unavailable"
            : variant.product_zoom_enabled ? `${label(variant.zoom_intensity)} zoom` : "No zoom"}
        </span>
      </div>

      <button
        type="button"
        className="secondary-button variant-preview-render"
        disabled={rendering}
        aria-busy={rendering}
        onClick={onRender}
      >
        {rendering
          ? <LoaderCircle className="is-spinning" size={15} aria-hidden="true" />
          : <RefreshCw size={15} aria-hidden="true" />}
        {rendering ? "Rendering 6-second preview" : "Render 6-second preview"}
      </button>

      <div className="variant-preview-canvas-shell">
        <div
          className={[
            "variant-preview-canvas",
            approximation ? "is-approximation" : "is-rendered",
            approximation && variant.mirror_enabled ? "is-flipped" : "",
            approximation ? `grade-${variant.color_grade}` : ""
          ].filter(Boolean).join(" ")}
          data-preview-mode={approximation ? "approximation" : "rendered"}
        >
          {mediaError ? (
            <div className="variant-preview-placeholder error" role="img" aria-label={mediaError}>
              <AlertTriangle size={30} aria-hidden="true" />
              <strong>Preview media failed to load</strong>
              <span>{mediaError}</span>
            </div>
          ) : rendered ? (
            rendered.kind === "video" ? (
              <video
                className="variant-preview-media generated-variation-preview"
                src={rendered.url}
                muted
                autoPlay
                loop
                playsInline
                aria-label={mediaLabel}
                onLoadedData={onMediaLoaded}
                onError={() => onMediaError("The rendered preview artifact could not be loaded.")}
              />
            ) : (
              <img
                className="variant-preview-media generated-variation-preview"
                src={rendered.url}
                alt={mediaLabel}
                onLoad={onMediaLoaded}
                onError={() => onMediaError("The rendered preview artifact could not be loaded.")}
              />
            )
          ) : brollMode ? (
            selectedBrollProduct?.preview?.exists ? (
              <video
                className="variant-preview-media"
                src={selectedBrollProduct.preview.url}
                muted
                autoPlay
                loop
                playsInline
                aria-label={mediaLabel}
                onLoadedData={onMediaLoaded}
                onError={() => onMediaError("The selected B-roll sample could not be loaded.")}
              />
            ) : (
              <div className="variant-preview-placeholder warning" role="img" aria-label="B-roll preview source missing">
                <Film size={30} aria-hidden="true" />
                <strong>B-roll sample unavailable</strong>
                <span>
                  {selectedBrollProduct
                    ? `${selectedBrollProduct.label} has no playable preview sample.`
                    : "No preview-only B-roll product is available."}
                </span>
              </div>
            )
          ) : data.preview_source.exists ? (
            <video
              className="variant-preview-media"
              src={data.preview_source.url}
              muted
              autoPlay
              loop
              playsInline
              aria-label={mediaLabel}
              onLoadedData={onMediaLoaded}
              onError={() => onMediaError("The fixed host preview source could not be loaded.")}
            />
          ) : (
            <div className="variant-preview-placeholder warning" role="img" aria-label="Fixed preview source missing">
              <Video size={30} aria-hidden="true" />
              <strong>Fixed preview source missing</strong>
              <span>Add the configured local preview clip to use host-mode approximation.</span>
            </div>
          )}

          {approximation && fallbackAvailable && !mediaError && (
            <div className="variant-preview-approximation-layer" aria-hidden="true">
              {variant.letterbox_enabled && (
                <>
                  <div
                    className="preview-blackbar top"
                    data-testid="preview-top-bar"
                    style={{ height: `${clamp(variant.letterbox_top_frac, 0, 0.4) * 100}%` }}
                  />
                  <div
                    className="preview-blackbar bottom"
                    data-testid="preview-bottom-bar"
                    style={{ height: `${clamp(variant.letterbox_bottom_frac, 0, 0.4) * 100}%` }}
                  />
                  {variant.letterbox_hook_enabled && variant.letterbox_top_frac > 0 && (
                    <div
                      className="preview-letterbox-hook"
                      data-testid="preview-top-hook"
                      style={{
                        left: `${clamp(variant.letterbox_hook_x_frac, 0, 1) * 100}%`,
                        top: `${clamp(variant.letterbox_top_frac, 0, 0.4) * clamp(variant.letterbox_hook_y_frac, 0, 1) * 100}%`,
                        color: variant.letterbox_hook_font_color,
                        fontSize: `${hookFontSize(variant.letterbox_hook_font_size)}px`
                      }}
                    >
                      Auto hook text
                    </div>
                  )}
                </>
              )}
              {usesBeforeAfter(variant) && (
                <div className="preview-before-after-card" data-testid="preview-before-after">
                  <strong>Before</strong>
                  <span />
                  <strong>After</strong>
                </div>
              )}
              {variant.subtitle_enabled && (
                <div
                  className="subtitle-preview-overlay"
                  data-testid="preview-subtitle"
                  style={{
                    top: `${clamp(variant.subtitle_y_frac, 0.08, 0.92) * 100}%`,
                    color: variant.font_color,
                    borderColor: variant.highlight_color,
                    fontSize: `${subtitleFontSize(variant.subtitle_size)}px`
                  }}
                >
                  <span>Preview settings </span>
                  <strong style={{ color: variant.subtitle_highlight_enabled ? variant.highlight_color : variant.font_color }}>
                    update
                  </strong>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="variant-preview-helper">
        <p>Preview updates as settings change.</p>
      </div>

      {mediaError && (
        <div
          className="variant-preview-feedback error"
          role="alert"
        >
          {mediaError}
        </div>
      )}

      <details className="variant-preview-details">
        <summary>Preview details</summary>
        <p>
          Rendering validates supported typography, color grade, mirroring, letterbox, and dynamic text on a silent fixed source.
          Audio, product zoom, B-roll insertion, real Before/After imagery, and transitional hooks require a full render.
        </p>
        <section className="variant-preview-readiness" aria-label="Readiness">
          <h3>Readiness</h3>
          <ReadinessRow label="Product information" {...information} />
          <ReadinessRow
            label="Fixed preview source"
            tone={data.preview_source.exists ? "ready" : "missing"}
            status={data.preview_source.exists ? "Ready" : "Missing"}
            detail={data.preview_source.exists ? "Local host preview available" : "Host approximation unavailable"}
          />
          <ReadinessRow
            label="B-roll inventory"
            tone={brollTone}
            status={brollStatus}
            detail={brollDetail}
          />
        </section>
      </details>
    </aside>
  );
}
