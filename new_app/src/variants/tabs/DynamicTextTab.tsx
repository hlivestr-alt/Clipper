import type { VariationVariant } from "../../api";
import {
  VariantField,
  VariantNumberField,
  VariantSection,
  VariantSegmented,
  VariantToggle,
  variantUiLabel
} from "../VariantControls";
import type { VariantTabPanelProps } from "../variantTypes";

const roles: VariationVariant["dynamic_text_roles"] = ["ingredients", "benefits", "usage", "cta"];
const animations: Array<VariationVariant["dynamic_text_settings"]["ingredients"]["animation"]> = [
  "current",
  "staggered_reveal",
  "fade_up",
  "wipe",
  "slide_up"
];

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}

export function DynamicTextTab({
  variant,
  data,
  informationData,
  previewInformationProduct,
  updateVariant,
  updateDynamicTextRole,
  updateDynamicTextSetting,
  updatePreviewInformationProduct
}: VariantTabPanelProps) {
  const intensityOff = variant.dynamic_text_mode === "off";
  const enabledLabels = roles.filter((role) => variant.dynamic_text_roles.includes(role)).map(variantUiLabel);
  return (
    <div className="variant-tab-stack">
      <VariantSection title="Dynamic text settings">
        {!informationData?.products.length && (
          <p className="variant-inline-warning">
            No indexed information product is available for dynamic-text preview content. Role settings remain editable and stored.
          </p>
        )}
        <div className="variant-field-list">
          <VariantSegmented
            label="Dynamic-text intensity"
            value={variant.dynamic_text_mode}
            options={data.dynamic_text_modes.length
              ? data.dynamic_text_modes
              : ["off", "minimal", "balanced", "high_energy"]}
            onChange={(dynamic_text_mode) => updateVariant({ dynamic_text_mode })}
          />
          {(informationData?.products.length ?? 0) > 0 && (
            <VariantField
              label="Preview information product"
              htmlFor="variant-information-product"
              hint="Preview-only; this selection is not saved in the profile."
            >
              <select
                id="variant-information-product"
                value={previewInformationProduct}
                disabled={intensityOff}
                onChange={(event) => updatePreviewInformationProduct(event.target.value)}
              >
                {informationData?.products.map((product) => (
                  <option value={product.product_key} key={product.product_key}>
                    {product.label} ({product.eligible_fact_count})
                  </option>
                ))}
              </select>
            </VariantField>
          )}
          <p className="variant-enabled-roles">
            <strong>Enabled roles:</strong> {enabledLabels.length ? enabledLabels.join(", ") : "None"}
          </p>
        </div>
      </VariantSection>
      <div className="variant-dynamic-role-grid">
        {roles.map((role) => (
          <DynamicRoleCard
            key={role}
            role={role}
            variant={variant}
            fonts={data.fonts}
            intensityOff={intensityOff}
            onToggle={(enabled) => updateDynamicTextRole(role, enabled)}
            onChange={(patch) => updateDynamicTextSetting(role, patch)}
          />
        ))}
      </div>
    </div>
  );
}

function DynamicRoleCard({
  role,
  variant,
  fonts,
  intensityOff,
  onToggle,
  onChange
}: {
  role: VariationVariant["dynamic_text_roles"][number];
  variant: VariationVariant;
  fonts: Array<{ id?: string; path?: string; label: string }>;
  intensityOff: boolean;
  onToggle: (enabled: boolean) => void;
  onChange: (patch: Partial<VariationVariant["dynamic_text_settings"]["ingredients"]>) => void;
}) {
  const setting = variant.dynamic_text_settings[role];
  const enabled = variant.dynamic_text_roles.includes(role);
  const disabled = intensityOff || !enabled;
  const isCta = role === "cta";
  const idPrefix = `variant-role-${role}`;
  return (
    <section className={`variant-dynamic-role-card ${enabled ? "enabled" : ""}`} aria-labelledby={`${idPrefix}-title`}>
      <div className="variant-dynamic-role-head">
        <div>
          <h3 id={`${idPrefix}-title`}>{variantUiLabel(role)}</h3>
          <p>{roleDescription(role)}</p>
        </div>
        <VariantToggle
          label={`Enable ${variantUiLabel(role)}`}
          checked={enabled}
          disabled={intensityOff}
          onChange={onToggle}
        />
      </div>
      <div className="variant-dynamic-role-fields">
        <VariantField label={isCta ? "Text font" : "Heading font"} htmlFor={`${idPrefix}-heading-font`} disabled={disabled}>
          <select
            id={`${idPrefix}-heading-font`}
            value={setting.headline_font_id}
            disabled={disabled}
            onChange={(event) => onChange({ headline_font_id: event.target.value })}
          >
            {fonts.map((font) => <option value={font.id ?? font.path ?? ""} key={font.id ?? font.path}>{font.label}</option>)}
          </select>
        </VariantField>
        {!isCta && (
          <VariantField label="Body font" htmlFor={`${idPrefix}-body-font`} disabled={disabled}>
            <select
              id={`${idPrefix}-body-font`}
              value={setting.body_font_id}
              disabled={disabled}
              onChange={(event) => onChange({ body_font_id: event.target.value })}
            >
              {fonts.map((font) => <option value={font.id ?? font.path ?? ""} key={font.id ?? font.path}>{font.label}</option>)}
            </select>
          </VariantField>
        )}
        <VariantNumberField
          id={`${idPrefix}-size`}
          label={isCta ? "Text size" : "Body size"}
          value={setting.font_size}
          min={isCta ? 24 : 20}
          max={isCta ? 96 : 72}
          unit="px"
          disabled={disabled}
          onChange={(value) => onChange({ font_size: Math.round(clamp(value, isCta ? 24 : 20, isCta ? 96 : 72)) })}
        />
        <VariantField label="Animation" htmlFor={`${idPrefix}-animation`} disabled={disabled}>
          <select
            id={`${idPrefix}-animation`}
            value={setting.animation}
            disabled={disabled}
            onChange={(event) => onChange({ animation: event.target.value as typeof setting.animation })}
          >
            {animations.map((animation) => <option value={animation} key={animation}>{variantUiLabel(animation)}</option>)}
          </select>
        </VariantField>
        <VariantNumberField
          id={`${idPrefix}-duration`}
          label="Duration"
          value={setting.duration_seconds}
          min={1}
          max={6}
          step={0.1}
          unit="s"
          disabled={disabled}
          onChange={(value) => onChange({ duration_seconds: Math.round(clamp(value, 1, 6) * 10) / 10 })}
        />
      </div>
    </section>
  );
}

function roleDescription(role: VariationVariant["dynamic_text_roles"][number]): string {
  if (role === "ingredients") return "Show key ingredients and concentrations.";
  if (role === "benefits") return "Highlight core benefits and outcomes.";
  if (role === "usage") return "Explain product application and usage.";
  return "Show the call to action or prompt.";
}
