import type { VariationVariant } from "../../api";
import {
  VariantField,
  VariantSection,
  VariantSegmented,
  VariantToggle,
  variantUiLabel
} from "../VariantControls";
import type { VariantTabPanelProps } from "../variantTypes";

export function BasicsTab({
  variant,
  data,
  featureFlags,
  updateVariant,
  applyTextStyle,
  hookTypeAvailable,
  onNavigateTab
}: VariantTabPanelProps) {
  const style = data.text_styles.find((item) => item.id === variant.text_style_id);
  const audioSummary = variant.bgm_mode === "none"
    ? variant.sfx_enabled ? "Sound effects only" : "Audio enhancements off"
    : `${variant.bgm_mode === "selected" ? "Selected music" : "Automatic music"}${variant.sfx_enabled ? " and sound effects" : ""}`;
  const visualSummary = [
    variant.random_broll_enabled ? "Relevant B-roll" : "No relevant B-roll",
    variant.product_zoom_enabled ? `${variantUiLabel(variant.zoom_intensity)} zoom` : "No product zoom",
    variant.letterbox_enabled ? "Letterbox enabled" : "No letterbox"
  ].join(" · ");

  return (
    <div className="variant-tab-stack">
      <VariantSection title="Creative setup" detail="The most frequently changed settings for this variant.">
        <div className="variant-field-list">
          <VariantField label="Variant name" htmlFor="variant-name">
            <input
              id="variant-name"
              aria-label="Variant name"
              value={variant.name}
              onChange={(event) => updateVariant({ name: event.target.value })}
            />
          </VariantField>
          <VariantField label="Hook type" htmlFor="variant-hook-type">
            <select
              id="variant-hook-type"
              value={variant.hook_type}
              onChange={(event) => updateVariant({ hook_type: event.target.value })}
            >
              {data.hook_types.map((item) => (
                <option value={item} key={item} disabled={!hookTypeAvailable(item)}>
                  {variantUiLabel(item)}{hookTypeAvailable(item) ? "" : " (globally disabled)"}
                </option>
              ))}
            </select>
          </VariantField>
          <VariantSegmented
            label="Visual mode"
            value={variant.visual_mode}
            options={data.visual_modes.length ? data.visual_modes : ["host", "broll_audio"]}
            onChange={(value) => updateVariant({ visual_mode: value })}
          />
          <VariantField label="Text style" htmlFor="variant-text-style">
            <select
              id="variant-text-style"
              value={variant.text_style_id}
              onChange={(event) => applyTextStyle(event.target.value as VariationVariant["text_style_id"])}
            >
              {data.text_styles.map((item) => <option value={item.id} key={item.id}>{item.label}</option>)}
            </select>
          </VariantField>
          <div className="variant-style-description">
            <p>{style?.description || "Use the current profile values without reapplying a style preset."}</p>
            <button
              type="button"
              className="secondary-button"
              disabled={variant.text_style_id === "current"}
              onClick={() => applyTextStyle(variant.text_style_id)}
            >
              Reapply style values
            </button>
          </div>
          <VariantField label="Color grade" htmlFor="variant-color-grade">
            <select
              id="variant-color-grade"
              value={variant.color_grade}
              onChange={(event) => updateVariant({ color_grade: event.target.value })}
            >
              {data.color_grades.map((item) => <option value={item} key={item}>{variantUiLabel(item)}</option>)}
            </select>
          </VariantField>
          <VariantToggle
            label="Flip video"
            checked={variant.mirror_enabled}
            onChange={(mirror_enabled) => updateVariant({ mirror_enabled })}
          />
        </div>
      </VariantSection>

      <VariantSection title="Configuration summary" detail="Read-only shortcuts to detailed settings.">
        <div className="variant-summary-grid">
          <SummaryCard
            title="Subtitles"
            detail={variant.subtitle_enabled
              ? `${variantUiLabel(variant.subtitle_position)} · ${variantUiLabel(variant.subtitle_size)}`
              : "Disabled"}
            action="Edit subtitles"
            onClick={() => onNavigateTab("text-subtitles")}
          />
          <SummaryCard title="Visual enhancements" detail={visualSummary} action="Configure visual" onClick={() => onNavigateTab("visual")} />
          <SummaryCard title="Audio" detail={audioSummary} action="Configure audio" onClick={() => onNavigateTab("audio")} />
          <SummaryCard
            title="Dynamic text"
            detail={variant.dynamic_text_mode === "off"
              ? "Off"
              : `${variantUiLabel(variant.dynamic_text_mode)} · ${variant.dynamic_text_roles.length} role(s)`}
            action="Configure dynamic text"
            onClick={() => onNavigateTab("dynamic-text")}
          />
        </div>
      </VariantSection>
      {(!featureFlags.bgm || !featureFlags.sfx) && (
        <p className="variant-inline-warning">Some audio features are globally disabled. Saved per-variant choices remain stored.</p>
      )}
    </div>
  );
}

function SummaryCard({
  title,
  detail,
  action,
  onClick
}: {
  title: string;
  detail: string;
  action: string;
  onClick: () => void;
}) {
  return (
    <article className="variant-summary-card">
      <strong>{title}</strong>
      <p>{detail}</p>
      <button type="button" className="variant-summary-action" onClick={onClick}>{action}</button>
    </article>
  );
}
