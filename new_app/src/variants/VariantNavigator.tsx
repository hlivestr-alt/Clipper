import { Captions, Minus, Music2, Plus, Sparkles, Type, Video } from "lucide-react";
import type { VariationVariant } from "../api";
import { deriveVariantSummary } from "./variantModel";

function conciseLabel(value: string): string {
  const labels: Record<string, string> = {
    broll_audio: "B-roll",
    high_energy: "High energy",
    off: "Off",
    none: "None"
  };
  return labels[value] ?? value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function audioLabel(variant: VariationVariant): string {
  if (variant.bgm_mode === "none" && !variant.sfx_enabled) {
    return "Audio off";
  }
  if (variant.bgm_mode === "none") {
    return "SFX only";
  }
  return variant.sfx_enabled ? "Music + SFX" : "Music";
}

export function VariantNavigator({
  variants,
  selectedIndex,
  minimumCount,
  maximumCount,
  onSelect,
  onCountChange
}: {
  variants: VariationVariant[];
  selectedIndex: number;
  minimumCount: number;
  maximumCount: number;
  onSelect: (index: number) => void;
  onCountChange: (count: number) => void;
}) {
  const count = variants.length;
  return (
    <aside className="variant-navigator" aria-label="Variant navigator">
      <div className="variant-navigator-head">
        <div>
          <h2>Variant list</h2>
          <span>{count} of {maximumCount}</span>
        </div>
        <div className="variant-count-stepper" role="group" aria-label="Variant count">
          <button
            type="button"
            disabled={count <= minimumCount}
            onClick={() => onCountChange(count - 1)}
            aria-label="Decrease variant count"
          >
            <Minus size={14} aria-hidden="true" />
          </button>
          <input
            type="number"
            min={minimumCount}
            max={maximumCount}
            value={count}
            onChange={(event) => {
              const value = Number.parseInt(event.target.value, 10);
              if (Number.isFinite(value)) {
                onCountChange(value);
              }
            }}
            aria-label="Variant count"
          />
          <button
            type="button"
            disabled={count >= maximumCount}
            onClick={() => onCountChange(count + 1)}
            aria-label="Increase variant count"
          >
            <Plus size={14} aria-hidden="true" />
          </button>
        </div>
      </div>
      <div className="variant-navigator-list">
        {variants.map((variant, index) => {
          const summary = deriveVariantSummary(variant);
          const selected = selectedIndex === index;
          return (
            <button
              type="button"
              className={`variant-navigator-card ${selected ? "selected" : ""}`}
              key={`variant-navigator-${index}`}
              title={`V${index + 1}: ${variant.name || `Variant ${index + 1}`}`}
              aria-pressed={selected}
              aria-current={selected ? "true" : undefined}
              onClick={() => onSelect(index)}
            >
              <span className="variant-navigator-card-title">
                <span className="variant-navigator-number">V{index + 1}</span>
                <strong title={variant.name || `Variant ${index + 1}`}>{variant.name || `Variant ${index + 1}`}</strong>
              </span>
              <span className="variant-navigator-summary">
                <span role="img" aria-label={`Hook: ${conciseLabel(summary.hookType)}`} title={`Hook: ${conciseLabel(summary.hookType)}`}>
                  <Type size={14} aria-hidden="true" />
                </span>
                <span role="img" aria-label={`Visual: ${conciseLabel(summary.visualMode)}`} title={`Visual: ${conciseLabel(summary.visualMode)}`}>
                  <Video size={14} aria-hidden="true" />
                </span>
                <span role="img" aria-label={`Subtitles: ${summary.subtitlesEnabled ? "On" : "Off"}`} title={`Subtitles: ${summary.subtitlesEnabled ? "On" : "Off"}`}>
                  <Captions size={14} aria-hidden="true" />
                </span>
                <span role="img" aria-label={`Dynamic text: ${conciseLabel(summary.dynamicTextMode)}`} title={`Dynamic text: ${conciseLabel(summary.dynamicTextMode)}`}>
                  <Sparkles size={14} aria-hidden="true" />
                </span>
                <span role="img" aria-label={`Audio: ${audioLabel(variant)}`} title={`Audio: ${audioLabel(variant)}`}>
                  <Music2 size={14} aria-hidden="true" />
                </span>
              </span>
            </button>
          );
        })}
      </div>
    </aside>
  );
}
