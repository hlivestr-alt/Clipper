import type { ReactNode } from "react";
import type { VariationPageData } from "../../api";
import type { VariantFeatureFlags } from "../variantTypes";

function sourceName(path: string) {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || path;
}

function InventoryCard({
  title,
  status,
  tone,
  children
}: {
  title: string;
  status: string;
  tone: "ready" | "warning" | "missing" | "disabled";
  children: ReactNode;
}) {
  return (
    <article className="diagnostic-inventory-card">
      <header>
        <strong>{title}</strong>
        <b className={tone}>{status}</b>
      </header>
      {children}
    </article>
  );
}

export function CreativeAssetInventory({
  data,
  featureFlags,
  previewProduct
}: {
  data: VariationPageData;
  featureFlags: VariantFeatureFlags;
  previewProduct: string;
}) {
  const brollCount = data.product_broll.products.reduce(
    (total, product) => total + product.video_count,
    0
  );
  const selectedPreviewProduct = data.product_broll.products.find(
    (product) => product.product_key === previewProduct
  );

  return (
    <section className="diagnostic-section" aria-labelledby="creative-inventory-heading">
      <header className="diagnostic-section-head">
        <div>
          <h3 id="creative-inventory-heading">Media and creative-asset inventory</h3>
          <p>Read-only inventory discovered by the existing Variants query.</p>
        </div>
      </header>
      <div className="diagnostic-inventory-grid">
        <InventoryCard
          title="Fixed preview source"
          status={data.preview_source.exists ? "Ready" : "Missing"}
          tone={data.preview_source.exists ? "ready" : "missing"}
        >
          <p>{data.preview_source.exists ? `Available ${data.preview_source.kind} source` : "Host-mode approximation is unavailable."}</p>
          {data.preview_source.path && (
            <details className="diagnostic-technical-detail">
              <summary>Troubleshooting details</summary>
              <code>{data.preview_source.path}</code>
            </details>
          )}
        </InventoryCard>

        <InventoryCard
          title="Product B-roll"
          status={brollCount ? `${brollCount} clips` : "Missing"}
          tone={brollCount ? "ready" : "missing"}
        >
          <p>
            Current preview-only sample:{" "}
            {selectedPreviewProduct?.preview?.exists ? selectedPreviewProduct.label : "Unavailable"}
          </p>
          <div className="diagnostic-compact-list">
            {data.product_broll.products.map((product) => (
              <details className="diagnostic-technical-detail" key={product.product_key}>
                <summary>
                  <span>{product.label}</span>
                  <b>{product.video_count} clip{product.video_count === 1 ? "" : "s"} · {product.preview?.exists ? "Sample ready" : "No sample"}</b>
                </summary>
                <dl className="diagnostic-metadata">
                  <div><dt>Product folder available</dt><dd>{product.exists ? "Yes" : "No"}</dd></div>
                  <div><dt>Folder</dt><dd><code>{product.folder}</code></dd></div>
                  {product.preview?.path && <div><dt>Preview sample</dt><dd><code>{product.preview.path}</code></dd></div>}
                </dl>
              </details>
            ))}
            {data.product_broll.products.length === 0 && <p>No product B-roll inventory returned.</p>}
          </div>
        </InventoryCard>

        <InventoryCard
          title="Background music"
          status={!featureFlags.bgm ? "Disabled globally" : data.bgm_tracks.length ? `${data.bgm_tracks.length} tracks` : "Missing"}
          tone={!featureFlags.bgm ? "disabled" : data.bgm_tracks.length ? "ready" : "missing"}
        >
          <p>
            {featureFlags.bgm
              ? "Discovered tracks are available to per-variant BGM selection."
              : "Per-variant track choices remain stored while BGM is globally disabled."}
          </p>
          <details className="diagnostic-technical-detail">
            <summary>Discovered track labels</summary>
            <ul>
              {data.bgm_tracks.map((track) => <li key={track.path ?? track.id ?? track.label}>{track.label}</li>)}
            </ul>
            {data.bgm_tracks.some((track) => track.path) && (
              <details>
                <summary>Technical track paths</summary>
                {data.bgm_tracks.map((track) => track.path && <code key={track.path}>{track.path}</code>)}
              </details>
            )}
          </details>
        </InventoryCard>

        <InventoryCard
          title="Discovered fonts"
          status={data.fonts.length ? `${data.fonts.length} fonts` : "Missing"}
          tone={data.fonts.length ? "ready" : "missing"}
        >
          <p>{data.fonts.length ? data.fonts.map((font) => font.label).join(", ") : "No discovered font options returned."}</p>
          {data.fonts.some((font) => font.id || font.path) && (
            <details className="diagnostic-technical-detail">
              <summary>Font identifiers</summary>
              <ul>
                {data.fonts.map((font) => (
                  <li key={font.id ?? font.path ?? font.label}>
                    {font.label}: <code>{font.id ?? sourceName(font.path ?? "")}</code>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </InventoryCard>

        <InventoryCard
          title="Text styles"
          status={data.text_styles.length ? `${data.text_styles.length} styles` : "Missing"}
          tone={data.text_styles.length ? "ready" : "missing"}
        >
          <p>Text styles also preserve hidden motion, stroke, and shadow properties.</p>
          <div className="diagnostic-compact-list">
            {data.text_styles.map((style) => (
              <div key={style.id}><strong>{style.label}</strong><span>{style.description}</span></div>
            ))}
          </div>
        </InventoryCard>
      </div>
    </section>
  );
}
