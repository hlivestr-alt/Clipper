import {
  VariantField,
  VariantSection,
  VariantToggle
} from "../VariantControls";
import type { VariantTabPanelProps } from "../variantTypes";

export function AudioTab({ variant, data, featureFlags, updateVariant }: VariantTabPanelProps) {
  const knownTrack = data.bgm_tracks.some((track) => (track.path ?? "") === variant.bgm_path);
  return (
    <div className="variant-tab-stack">
      <VariantSection title="Background music" detail="The six-second visual preview is silent and does not validate audio.">
        {!featureFlags.bgm && <p className="variant-inline-warning">Background music is globally disabled. This variant’s saved choice is preserved.</p>}
        <div className="variant-field-list">
          <VariantField label="Background music mode" htmlFor="variant-bgm-mode" disabled={!featureFlags.bgm}>
            <select
              id="variant-bgm-mode"
              disabled={!featureFlags.bgm}
              value={variant.bgm_mode}
              onChange={(event) => updateVariant({ bgm_mode: event.target.value as typeof variant.bgm_mode })}
            >
              <option value="auto">Auto</option>
              <option value="none">No BGM</option>
              <option value="selected">Selected track</option>
            </select>
          </VariantField>
          <VariantField label="Selected BGM track" htmlFor="variant-bgm-track" disabled={!featureFlags.bgm || variant.bgm_mode !== "selected"}>
            <select
              id="variant-bgm-track"
              disabled={!featureFlags.bgm || variant.bgm_mode !== "selected"}
              value={variant.bgm_path}
              onChange={(event) => updateVariant({ bgm_path: event.target.value })}
            >
              {!knownTrack && variant.bgm_path && <option value={variant.bgm_path}>{variant.bgm_path}</option>}
              {!variant.bgm_path && <option value="">Choose track</option>}
              {data.bgm_tracks.map((track) => <option value={track.path ?? ""} key={track.path}>{track.label}</option>)}
            </select>
          </VariantField>
        </div>
      </VariantSection>
      <VariantSection title="Sound effects">
        {!featureFlags.sfx && <p className="variant-inline-warning">Sound effects are globally disabled. The saved per-variant choice remains stored.</p>}
        <VariantToggle
          label="Sound effects"
          checked={variant.sfx_enabled}
          disabled={!featureFlags.sfx}
          onChange={(sfx_enabled) => updateVariant({ sfx_enabled })}
        />
      </VariantSection>
    </div>
  );
}
