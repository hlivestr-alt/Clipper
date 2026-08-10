import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import type { PresetPanelFeedback, PresetPanelOption } from "./variantTypes";

export function PresetPanel({
  open,
  presetName,
  selectedPreset,
  presets,
  saving,
  loading,
  feedback,
  onPresetNameChange,
  onSelectedPresetChange,
  onSave,
  onLoad,
  onClose
}: {
  open: boolean;
  presetName: string;
  selectedPreset: string;
  presets: PresetPanelOption[];
  saving: boolean;
  loading: boolean;
  feedback?: PresetPanelFeedback;
  onPresetNameChange: (value: string) => void;
  onSelectedPresetChange: (value: string) => void;
  onSave: () => void;
  onLoad: () => void;
  onClose: () => void;
}) {
  const nameInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    nameInputRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [open, onClose]);

  if (!open) {
    return null;
  }

  return (
    <section
      id="variant-presets-panel"
      className="variant-presets-panel"
      role="dialog"
      aria-modal="false"
      aria-labelledby="variant-presets-title"
    >
      <div className="variant-presets-head">
        <div>
          <h2 id="variant-presets-title">Presets</h2>
          <p>Save this draft or load a preset without applying it.</p>
        </div>
        <button type="button" className="variant-icon-button" onClick={onClose} aria-label="Close presets">
          <X size={16} aria-hidden="true" />
        </button>
      </div>
      <div className="variant-presets-section">
        <label htmlFor="variant-preset-name">Save current draft</label>
        <div className="variant-presets-control-row">
          <input
            id="variant-preset-name"
            ref={nameInputRef}
            value={presetName}
            placeholder="Preset name"
            onChange={(event) => onPresetNameChange(event.target.value)}
          />
          <button
            type="button"
            className="secondary-button"
            disabled={!presetName.trim() || saving}
            onClick={onSave}
          >
            {saving ? "Saving preset" : "Save preset"}
          </button>
        </div>
        <small>Saving with an existing slug may overwrite that preset.</small>
      </div>
      <div className="variant-presets-section">
        <label htmlFor="variant-preset-select">Load into draft</label>
        <div className="variant-presets-control-row">
          <select
            id="variant-preset-select"
            value={selectedPreset}
            onChange={(event) => onSelectedPresetChange(event.target.value)}
          >
            <option value="">Choose preset</option>
            {presets.map((preset) => (
              <option value={preset.presetId} key={preset.presetId}>{preset.name}</option>
            ))}
          </select>
          <button
            type="button"
            className="secondary-button"
            disabled={!selectedPreset || loading}
            onClick={onLoad}
          >
            {loading ? "Loading preset" : "Load"}
          </button>
        </div>
      </div>
      {feedback && (
        <p
          className={`variant-presets-feedback ${feedback.kind}`}
          role={feedback.kind === "error" ? "alert" : "status"}
          aria-live="polite"
        >
          {feedback.text}
        </p>
      )}
    </section>
  );
}
