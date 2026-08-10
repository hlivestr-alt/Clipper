import type { ReactNode } from "react";

export function variantUiLabel(value: string): string {
  const labels: Record<string, string> = {
    auto: "Auto",
    none: "None",
    host: "Host footage",
    broll_audio: "Audio over B-roll",
    high_energy: "High Energy",
    staggered_reveal: "Staggered Reveal",
    fade_up: "Fade Up",
    slide_up: "Slide Up",
    phrase_cut: "Phrase Cut",
    cta: "CTA"
  };
  return labels[value] ?? String(value).replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function VariantSection({
  title,
  detail,
  children
}: {
  title: string;
  detail?: string;
  children: ReactNode;
}) {
  return (
    <section className="variant-tab-section">
      <div className="variant-tab-section-head">
        <h3>{title}</h3>
        {detail && <p>{detail}</p>}
      </div>
      {children}
    </section>
  );
}

export function VariantField({
  label,
  htmlFor,
  hint,
  disabled = false,
  children
}: {
  label: string;
  htmlFor?: string;
  hint?: string;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <div className={`variant-field ${disabled ? "disabled" : ""}`}>
      <div className="variant-field-copy">
        {htmlFor ? <label htmlFor={htmlFor}>{label}</label> : <span>{label}</span>}
        {hint && <small>{hint}</small>}
      </div>
      <div className="variant-field-control">{children}</div>
    </div>
  );
}

export function VariantToggle({
  label,
  checked,
  disabled = false,
  hint,
  onChange
}: {
  label: string;
  checked: boolean;
  disabled?: boolean;
  hint?: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className={`variant-toggle ${disabled ? "disabled" : ""}`}>
      <span>
        <strong>{label}</strong>
        {hint && <small>{hint}</small>}
      </span>
      <span className="variant-toggle-control">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span aria-hidden="true" />
      </span>
    </label>
  );
}

export function VariantSegmented<T extends string>({
  label,
  value,
  options,
  disabled = false,
  onChange
}: {
  label: string;
  value: T;
  options: readonly T[];
  disabled?: boolean;
  onChange: (value: T) => void;
}) {
  return (
    <div className={`variant-segmented-field ${disabled ? "disabled" : ""}`}>
      <span>{label}</span>
      <div className="variant-segmented" role="group" aria-label={label}>
        {options.map((option) => (
          <button
            type="button"
            key={option}
            disabled={disabled}
            aria-pressed={value === option}
            onClick={() => onChange(option)}
          >
            {variantUiLabel(option)}
          </button>
        ))}
      </div>
    </div>
  );
}

export function VariantNumberField({
  id,
  label,
  value,
  min,
  max,
  step = 1,
  unit,
  disabled = false,
  onChange
}: {
  id: string;
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  unit?: string;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <VariantField label={label} htmlFor={id} disabled={disabled}>
      <div className="variant-number-input">
        <input
          id={id}
          type="number"
          min={min}
          max={max}
          step={step}
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(Number(event.target.value))}
        />
        {unit && <span>{unit}</span>}
      </div>
    </VariantField>
  );
}

export function VariantColorField({
  id,
  label,
  value,
  disabled = false,
  onChange
}: {
  id: string;
  label: string;
  value: string;
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <VariantField label={label} htmlFor={id} disabled={disabled}>
      <div className="variant-color-input">
        <input
          id={id}
          type="color"
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value.toUpperCase())}
        />
        <span>{value}</span>
      </div>
    </VariantField>
  );
}
