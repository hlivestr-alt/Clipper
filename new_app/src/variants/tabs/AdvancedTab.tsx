import {
  VariantColorField,
  VariantField,
  VariantNumberField,
  VariantSection,
  VariantToggle
} from "../VariantControls";
import type { VariantTabPanelProps } from "../variantTypes";

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}

export function AdvancedTab({
  variant,
  data,
  updateVariant,
  updateLetterboxEnabled,
  updateLetterboxHookEnabled
}: VariantTabPanelProps) {
  const barsDisabled = !variant.letterbox_enabled;
  const hookDisabled = barsDisabled || !variant.letterbox_hook_enabled;
  return (
    <div className="variant-tab-stack">
      <VariantSection title="Letterbox bars" detail="Independent top and bottom safe-area bars.">
        <div className="variant-field-list">
          <VariantToggle
            label="Black bars"
            checked={variant.letterbox_enabled}
            onChange={updateLetterboxEnabled}
          />
          <VariantNumberField
            id="variant-letterbox-top"
            label="Top bar height"
            value={Math.round(variant.letterbox_top_frac * 100)}
            min={0}
            max={40}
            unit="%"
            disabled={barsDisabled}
            onChange={(value) => updateVariant({ letterbox_top_frac: clamp(value, 0, 40) / 100 })}
          />
          <VariantNumberField
            id="variant-letterbox-bottom"
            label="Bottom bar height"
            value={Math.round(variant.letterbox_bottom_frac * 100)}
            min={0}
            max={40}
            unit="%"
            disabled={barsDisabled}
            onChange={(value) => updateVariant({ letterbox_bottom_frac: clamp(value, 0, 40) / 100 })}
          />
        </div>
      </VariantSection>
      <VariantSection title="Automatic top-bar hook">
        <div className="variant-field-list">
          <VariantToggle
            label="Automatic top-bar hook"
            checked={variant.letterbox_hook_enabled}
            disabled={barsDisabled}
            onChange={updateLetterboxHookEnabled}
          />
          <VariantField label="Top-bar hook font" htmlFor="variant-letterbox-hook-font" disabled={hookDisabled}>
            <select
              id="variant-letterbox-hook-font"
              value={variant.letterbox_hook_font_id || variant.font_id}
              disabled={hookDisabled}
              onChange={(event) => updateVariant({ letterbox_hook_font_id: event.target.value })}
            >
              {data.fonts.map((font) => <option value={font.id ?? font.path ?? ""} key={font.id ?? font.path}>{font.label}</option>)}
            </select>
          </VariantField>
          <VariantColorField
            id="variant-letterbox-hook-color"
            label="Top-bar hook color"
            value={variant.letterbox_hook_font_color}
            disabled={hookDisabled}
            onChange={(letterbox_hook_font_color) => updateVariant({ letterbox_hook_font_color })}
          />
          <VariantNumberField
            id="variant-letterbox-hook-size"
            label="Top-bar hook size"
            value={variant.letterbox_hook_font_size}
            min={24}
            max={160}
            unit="px"
            disabled={hookDisabled}
            onChange={(value) => updateVariant({ letterbox_hook_font_size: Math.round(clamp(value, 24, 160)) })}
          />
          <VariantNumberField
            id="variant-letterbox-hook-x"
            label="Top-bar hook X position"
            value={Math.round(variant.letterbox_hook_x_frac * 100)}
            min={0}
            max={100}
            unit="%"
            disabled={hookDisabled}
            onChange={(value) => updateVariant({ letterbox_hook_x_frac: clamp(value, 0, 100) / 100 })}
          />
          <VariantNumberField
            id="variant-letterbox-hook-y"
            label="Top-bar hook Y position"
            value={Math.round(variant.letterbox_hook_y_frac * 100)}
            min={0}
            max={100}
            unit="%"
            disabled={hookDisabled}
            onChange={(value) => updateVariant({ letterbox_hook_y_frac: clamp(value, 0, 100) / 100 })}
          />
        </div>
      </VariantSection>
    </div>
  );
}
