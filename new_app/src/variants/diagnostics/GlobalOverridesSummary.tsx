import type { VariantFeatureFlags } from "../variantTypes";

const overrideRows: Array<{
  key: keyof VariantFeatureFlags;
  label: string;
  detail: string;
}> = [
  { key: "bgm", label: "Background music", detail: "Controls whether saved per-variant BGM choices are active." },
  { key: "sfx", label: "Sound effects", detail: "Controls whether saved per-variant SFX choices are active." },
  { key: "before_after", label: "Before/After images", detail: "Controls availability of Before/After image hooks." },
  { key: "broll_intro", label: "B-roll hooks", detail: "Controls availability of B-roll hook types." },
  { key: "transitional_hook", label: "Transitional hooks", detail: "Controls availability of transitional hook types." },
  {
    key: "host_face_zoom",
    label: "Host-face zoom",
    detail: "This global renderer flag is separate from the per-variant product-zoom setting."
  }
];

export function GlobalOverridesSummary({ featureFlags }: { featureFlags: VariantFeatureFlags }) {
  return (
    <section className="diagnostic-section" aria-labelledby="global-overrides-heading">
      <header className="diagnostic-section-head">
        <div>
          <h3 id="global-overrides-heading">Global configuration overrides</h3>
          <p>Global availability overrides per-variant activation without clearing saved choices.</p>
        </div>
      </header>
      <div className="diagnostic-readiness-list">
        {overrideRows.map((item) => {
          const enabled = featureFlags[item.key];
          return (
            <div className={`diagnostic-readiness-row ${enabled ? "ready" : "disabled"}`} key={item.key}>
              <span className="diagnostic-status-dot" aria-hidden="true" />
              <span className="diagnostic-readiness-copy">
                <strong>{item.label}</strong>
                <small>{item.detail}</small>
              </span>
              <b>{enabled ? "Available" : "Disabled globally"}</b>
            </div>
          );
        })}
      </div>
    </section>
  );
}
