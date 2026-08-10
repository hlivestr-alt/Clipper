import { RefreshCw } from "lucide-react";
import type { ProductInformationSource, ProductInformationStatus } from "../../api";

function displayLabel(value: string) {
  return String(value || "").replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function numberText(value: number) {
  return new Intl.NumberFormat().format(value);
}

function byteSizeText(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function sourceName(path: string) {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || path;
}

function relativeSourcePath(path: string, root: string) {
  const normalizedPath = path.replace(/\\/g, "/");
  const normalizedRoot = root.replace(/\\/g, "/").replace(/\/+$/, "");
  if (normalizedRoot && normalizedPath.toLowerCase().startsWith(`${normalizedRoot.toLowerCase()}/`)) {
    return normalizedPath.slice(normalizedRoot.length + 1);
  }
  return sourceName(path);
}

function SourceRow({ source, root }: { source: ProductInformationSource; root: string }) {
  const statusTone = source.status === "ok" ? "ready" : "warning";
  return (
    <details className="diagnostic-disclosure information-source-detail">
      <summary>
        <span className={`diagnostic-status-dot ${statusTone}`} aria-hidden="true" />
        <span>
          <strong>{sourceName(source.path)}</strong>
          <small>
            {numberText(source.eligible_fact_count)} eligible facts
            {source.page_count > 0 ? ` · ${numberText(source.page_count)} pages` : ""}
            {` · ${byteSizeText(source.size)}`}
          </small>
        </span>
        <b className={statusTone}>{displayLabel(source.status)}</b>
      </summary>
      <div className="diagnostic-disclosure-body">
        <dl className="diagnostic-metadata">
          <div><dt>Relative source path</dt><dd>{relativeSourcePath(source.path, root)}</dd></div>
          <div><dt>File type</dt><dd>{source.extension || "Unknown"}</dd></div>
          <div><dt>Extraction</dt><dd>{displayLabel(source.extraction_method)}</dd></div>
          <div><dt>Cache status</dt><dd>{source.cached ? "Cached" : "Not cached"}</dd></div>
          <div><dt>Unassigned facts</dt><dd>{numberText(source.unassigned_count)}</dd></div>
          <div><dt>Products</dt><dd>{source.products.map(displayLabel).join(", ") || "None assigned"}</dd></div>
        </dl>
        {Object.keys(source.fact_counts).length > 0 && (
          <p className="diagnostic-inline-detail">
            <strong>Role counts:</strong>{" "}
            {Object.entries(source.fact_counts).map(([role, count]) => `${displayLabel(role)} ${count}`).join(" · ")}
          </p>
        )}
        {source.warnings.length > 0 && (
          <div className="diagnostic-warning-list" aria-label={`Warnings for ${sourceName(source.path)}`}>
            {source.warnings.map((warning, index) => <p key={`${warning}-${index}`}>{warning}</p>)}
          </div>
        )}
        <details className="diagnostic-technical-detail">
          <summary>Technical path and checksum</summary>
          <dl className="diagnostic-metadata">
            <div><dt>Full path</dt><dd><code>{source.path}</code></dd></div>
            <div><dt>SHA-256</dt><dd><code>{source.sha256}</code></dd></div>
          </dl>
        </details>
      </div>
    </details>
  );
}

export function ProductInformationDiagnostics({
  informationData,
  loading,
  error,
  scanning,
  onRescan
}: {
  informationData?: ProductInformationStatus;
  loading?: boolean;
  error?: string;
  scanning?: boolean;
  onRescan?: () => void;
}) {
  const totalFacts = informationData?.products.reduce(
    (total, product) => total + product.eligible_fact_count,
    0
  ) ?? 0;

  return (
    <>
      <section className="diagnostic-section" aria-labelledby="product-information-health-heading">
        <header className="diagnostic-section-head">
          <div>
            <h3 id="product-information-health-heading">Product-information health</h3>
            <p>
              Facts come from supported local PDF and DOCX files. Conflicted facts are excluded from eligible generated facts.
            </p>
          </div>
          <button
            type="button"
            className="secondary-button"
            disabled={scanning || !onRescan}
            aria-busy={scanning}
            onClick={onRescan}
          >
            <RefreshCw className={scanning ? "is-spinning" : undefined} size={15} aria-hidden="true" />
            {scanning ? "Scanning product information" : "Rescan product information"}
          </button>
        </header>
        {loading && <div className="diagnostic-state loading" role="status">Loading product information…</div>}
        {error && (
          <div className="diagnostic-state error" role="alert">
            <strong>Product-information query failed</strong>
            <span>{error}</span>
            <small>Variant editing remains available.</small>
          </div>
        )}
        {informationData && (
          <>
            <dl className="diagnostic-summary-grid">
              <div><dt>Index revision</dt><dd>{informationData.revision ? informationData.revision.slice(0, 12) : "Unavailable"}</dd></div>
              <div><dt>Last scan</dt><dd>{informationData.scanned_at || "Unavailable"}</dd></div>
              <div><dt>Products</dt><dd>{numberText(informationData.products.length)}</dd></div>
              <div><dt>Eligible facts</dt><dd>{numberText(totalFacts)}</dd></div>
            </dl>
            {informationData.root && (
              <details className="diagnostic-technical-detail diagnostic-root-detail">
                <summary>Information index location</summary>
                <code>{informationData.root}</code>
              </details>
            )}
            {informationData.sources.length === 0 && (
              <div className="diagnostic-state warning" role="status">
                <strong>No product-information documents</strong>
                <span>Add searchable PDF or DOCX files to the configured local information folder, then rescan product information.</span>
              </div>
            )}
          </>
        )}
      </section>

      {informationData && (
        <>
          <section className="diagnostic-section" aria-labelledby="product-fact-availability-heading">
            <header className="diagnostic-section-head">
              <div>
                <h3 id="product-fact-availability-heading">Product fact availability</h3>
                <p>Eligible counts are reported by the current product-information index.</p>
              </div>
            </header>
            <div className="diagnostic-product-grid">
              {informationData.products.map((product) => (
                <article className="diagnostic-product-card" key={product.product_key}>
                  <header>
                    <strong>{product.label}</strong>
                    <span>{numberText(product.eligible_fact_count)} eligible</span>
                  </header>
                  <dl>
                    {Object.entries(product.fact_counts).map(([role, count]) => (
                      <div key={role}><dt>{displayLabel(role)}</dt><dd>{numberText(count)}</dd></div>
                    ))}
                  </dl>
                  {Object.keys(product.fact_counts).length === 0 && <p>No indexed role counts returned.</p>}
                </article>
              ))}
              {informationData.products.length === 0 && <p className="diagnostic-empty-note">No indexed products.</p>}
            </div>
          </section>

          <section className="diagnostic-section" aria-labelledby="information-sources-heading">
            <header className="diagnostic-section-head">
              <div>
                <h3 id="information-sources-heading">Information sources</h3>
                <p>Open a source to inspect returned extraction metadata, warnings, and troubleshooting details.</p>
              </div>
            </header>
            <div className="diagnostic-source-list">
              {informationData.sources.map((source) => (
                <SourceRow source={source} root={informationData.root} key={source.path} />
              ))}
              {informationData.sources.length === 0 && <p className="diagnostic-empty-note">No source documents returned.</p>}
            </div>
          </section>

          <section className="diagnostic-section" aria-labelledby="information-warnings-heading">
            <header className="diagnostic-section-head">
              <div>
                <h3 id="information-warnings-heading">Warnings and conflicts</h3>
                <p>Diagnostic output from the current index. These items cannot be edited or dismissed here.</p>
              </div>
            </header>
            <div className="diagnostic-warning-groups">
              <article>
                <header><strong>Unassigned facts</strong><b>{numberText(informationData.unassigned_count)}</b></header>
                {informationData.warnings.map((warning, index) => <p key={`${warning}-${index}`}>{warning}</p>)}
                {informationData.unassigned.length > 0 ? (
                  <details className="diagnostic-disclosure compact">
                    <summary>View returned unassigned examples</summary>
                    <div className="diagnostic-long-list">
                      {informationData.unassigned.map((item, index) => (
                        <div key={`${item.source_file}-${index}`}>
                          <strong>{sourceName(item.source_file)}</strong>
                          <span>{item.text}</span>
                          <small>{displayLabel(item.role)} · {item.reason}</small>
                          <details className="diagnostic-technical-detail">
                            <summary>Source and locator</summary>
                            <code>{item.source_file}</code>
                            <pre>{JSON.stringify(item.locator, null, 2)}</pre>
                          </details>
                        </div>
                      ))}
                    </div>
                  </details>
                ) : <p>No unassigned fact examples returned.</p>}
              </article>
              <article>
                <header><strong>Conflicting facts</strong><b>{numberText(informationData.conflict_count)}</b></header>
                {informationData.conflicts.length > 0 ? (
                  <div className="diagnostic-long-list">
                    {informationData.conflicts.map((item) => (
                      <div key={`${item.product}-${item.role}-${item.key}`}>
                        <strong>{displayLabel(item.product)} · {displayLabel(item.role)}</strong>
                        <span>{item.reason}</span>
                        <small>{item.fact_ids.length} returned fact ID{item.fact_ids.length === 1 ? "" : "s"}</small>
                        <details className="diagnostic-technical-detail">
                          <summary>Returned conflict details</summary>
                          <dl className="diagnostic-metadata">
                            <div><dt>Conflict key</dt><dd><code>{item.key}</code></dd></div>
                            <div><dt>Fact IDs</dt><dd><code>{item.fact_ids.join(", ")}</code></dd></div>
                          </dl>
                        </details>
                      </div>
                    ))}
                  </div>
                ) : <p>No conflicting facts returned.</p>}
              </article>
            </div>
          </section>
        </>
      )}
    </>
  );
}
