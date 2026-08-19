import { useCallback, useRef, useState } from "react";
import { CheckCircle2, RefreshCw, SlidersHorizontal } from "lucide-react";
import { PresetPanel } from "./PresetPanel";
import type {
  PresetPanelFeedback,
  PresetPanelOption,
  VariantCommandStatus
} from "./variantTypes";

export function VariantCommandBar({
  status,
  conflict,
  variantCount,
  revision,
  refreshing,
  canApply,
  presetsDisabled,
  presetName,
  selectedPreset,
  presets,
  presetSaving,
  presetLoading,
  presetFeedback,
  onRefresh,
  onApply,
  onPresetNameChange,
  onSelectedPresetChange,
  onSavePreset,
  onLoadPreset
}: {
  status: VariantCommandStatus;
  conflict?: string;
  variantCount: number;
  revision?: string;
  refreshing: boolean;
  canApply: boolean;
  presetsDisabled?: boolean;
  presetName: string;
  selectedPreset: string;
  presets: PresetPanelOption[];
  presetSaving: boolean;
  presetLoading: boolean;
  presetFeedback?: PresetPanelFeedback;
  onRefresh: () => void;
  onApply: () => void;
  onPresetNameChange: (value: string) => void;
  onSelectedPresetChange: (value: string) => void;
  onSavePreset: () => void;
  onLoadPreset: () => void;
}) {
  const [presetsOpen, setPresetsOpen] = useState(false);
  const presetsButtonRef = useRef<HTMLButtonElement | null>(null);
  const statusLabel = status === "saving" ? "Saving" : status === "unsaved" ? "Unsaved" : "Saved";

  const closePresets = useCallback(() => {
    setPresetsOpen(false);
    window.setTimeout(() => presetsButtonRef.current?.focus(), 0);
  }, []);

  return (
    <header className="variant-command-bar">
      <div className="variant-command-identity">
        <div className="variant-command-title-row">
          <h1>Variants</h1>
          <span className={`variant-command-status ${status}`} role="status" aria-live="polite">
            {statusLabel}
          </span>
          {conflict && <span className="variant-command-status conflict" role="alert">Revision conflict</span>}
        </div>
        <p>Configure how clips are transformed and rendered.</p>
        {conflict && <small className="variant-command-conflict">{conflict}</small>}
      </div>
      <div
        className="variant-command-meta"
        aria-label="Active variation profile"
        title={`Profile revision: ${revision || "new"}`}
      >
        <span>Default profile</span>
        <span>{variantCount} {variantCount === 1 ? "variant" : "variants"}</span>
      </div>
      <div className="variant-command-actions">
        <button
          type="button"
          className="secondary-button"
          onClick={onRefresh}
          disabled={refreshing}
          aria-busy={refreshing}
        >
          <RefreshCw className={refreshing ? "is-spinning" : ""} size={16} aria-hidden="true" />
          {refreshing ? "Refreshing" : "Refresh"}
        </button>
        <div className="variant-presets-anchor">
          <button
            ref={presetsButtonRef}
            type="button"
            className="secondary-button"
            disabled={presetsDisabled}
            aria-haspopup="dialog"
            aria-expanded={presetsOpen}
            aria-controls="variant-presets-panel"
            onClick={() => setPresetsOpen((current) => !current)}
          >
            <SlidersHorizontal size={16} aria-hidden="true" />
            Presets
          </button>
          <PresetPanel
            open={presetsOpen}
            presetName={presetName}
            selectedPreset={selectedPreset}
            presets={presets}
            saving={presetSaving}
            loading={presetLoading}
            feedback={presetFeedback}
            onPresetNameChange={onPresetNameChange}
            onSelectedPresetChange={onSelectedPresetChange}
            onSave={onSavePreset}
            onLoad={onLoadPreset}
            onClose={closePresets}
          />
        </div>
        <button
          type="button"
          className="primary-button"
          disabled={!canApply || status === "saving"}
          onClick={onApply}
        >
          <CheckCircle2 size={16} aria-hidden="true" />
          {status === "saving" ? "Saving" : "Apply to future clips"}
        </button>
      </div>
    </header>
  );
}
