import type { VariationVariant } from "../../api";
import {
  VariantColorField,
  VariantField,
  VariantNumberField,
  VariantSection,
  VariantSegmented,
  VariantToggle
} from "../VariantControls";
import type { VariantTabPanelProps } from "../variantTypes";

const placementDefaults: Record<VariationVariant["subtitle_position"], number> = {
  top: 0.34,
  center: 0.58,
  bottom: 0.84
};

export function TextSubtitlesTab({ variant, data, updateVariant, updateSubtitleY }: VariantTabPanelProps) {
  const disabled = !variant.subtitle_enabled;
  const fontOptions = data.fonts.map((font) => ({
    value: font.id ?? font.path ?? "",
    label: font.label
  }));
  return (
    <div className="variant-tab-stack">
      <VariantSection title="Subtitle behavior" detail="Placement and timing-safe positioning for rendered subtitles.">
        <div className="variant-field-list">
          <VariantToggle
            label="Subtitles"
            checked={variant.subtitle_enabled}
            onChange={(subtitle_enabled) => updateVariant({ subtitle_enabled })}
          />
          <VariantSegmented
            label="Subtitle placement"
            value={variant.subtitle_position}
            options={data.subtitle_positions as VariationVariant["subtitle_position"][]}
            disabled={disabled}
            onChange={(subtitle_position) => updateVariant({
              subtitle_position,
              subtitle_y_frac: placementDefaults[subtitle_position]
            })}
          />
          <VariantNumberField
            id="variant-subtitle-y"
            label="Exact subtitle Y"
            value={Math.round(variant.subtitle_y_frac * 100)}
            min={8}
            max={92}
            unit="%"
            disabled={disabled}
            onChange={(value) => updateSubtitleY(value / 100)}
          />
          <VariantSegmented
            label="Subtitle size"
            value={variant.subtitle_size}
            options={data.subtitle_sizes.length ? data.subtitle_sizes : ["compact", "small", "medium", "large"]}
            disabled={disabled}
            onChange={(subtitle_size) => updateVariant({ subtitle_size })}
          />
          <VariantToggle
            label="Active-word highlighting"
            checked={variant.subtitle_highlight_enabled}
            disabled={disabled}
            onChange={(subtitle_highlight_enabled) => updateVariant({ subtitle_highlight_enabled })}
          />
        </div>
      </VariantSection>

      <VariantSection title="Typography">
        <div className="variant-field-list">
          <FontSelect id="variant-subtitle-font" label="Subtitle font" value={variant.font_id} options={fontOptions} disabled={disabled} onChange={(font_id) => updateVariant({ font_id })} />
          <FontSelect id="variant-headline-font" label="Headline font" value={variant.headline_font_id || variant.font_id} options={fontOptions} onChange={(headline_font_id) => updateVariant({ headline_font_id })} />
          <FontSelect id="variant-caption-font" label="Product-caption font" value={variant.caption_font_id || variant.font_id} options={fontOptions} onChange={(caption_font_id) => updateVariant({ caption_font_id })} />
          <VariantColorField id="variant-font-color" label="Base font color" value={variant.font_color} onChange={(font_color) => updateVariant({ font_color })} />
          <VariantColorField id="variant-highlight-color" label="Highlight color" value={variant.highlight_color} onChange={(highlight_color) => updateVariant({ highlight_color })} />
        </div>
        <div className="variant-text-sample" aria-label="Typography preview">
          <span style={{ color: variant.font_color }}>Here is the key </span>
          <strong style={{ color: variant.highlight_color }}>difference</strong>
        </div>
      </VariantSection>
    </div>
  );
}

function FontSelect({
  id,
  label,
  value,
  options,
  disabled = false,
  onChange
}: {
  id: string;
  label: string;
  value: string;
  options: Array<{ value: string; label: string }>;
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <VariantField label={label} htmlFor={id} disabled={disabled}>
      <select id={id} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
      </select>
    </VariantField>
  );
}
