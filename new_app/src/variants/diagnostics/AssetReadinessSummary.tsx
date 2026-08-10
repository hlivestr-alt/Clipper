import type { ProductInformationStatus, VariationPageData } from "../../api";
import type { VariantFeatureFlags } from "../variantTypes";

type ReadinessTone = "ready" | "warning" | "missing" | "disabled" | "error" | "loading";

function ReadinessItem({
  label,
  status,
  detail,
  tone
}: {
  label: string;
  status: string;
  detail: string;
  tone: ReadinessTone;
}) {
  return (
    <div className={`diagnostic-readiness-row ${tone}`}>
      <span className="diagnostic-status-dot" aria-hidden="true" />
      <span className="diagnostic-readiness-copy">
        <strong>{label}</strong>
        <small>{detail}</small>
      </span>
      <b>{status}</b>
    </div>
  );
}

export function AssetReadinessSummary({
  data,
  informationData,
  informationLoading,
  informationError,
  featureFlags
}: {
  data: VariationPageData;
  informationData?: ProductInformationStatus;
  informationLoading?: boolean;
  informationError?: string;
  featureFlags: VariantFeatureFlags;
}) {
  const eligibleFacts = informationData?.products.reduce(
    (total, product) => total + product.eligible_fact_count,
    0
  ) ?? 0;
  const informationWarning = Boolean(
    informationData
    && (
      informationData.warnings.length
      || informationData.unassigned_count
      || informationData.conflict_count
      || informationData.sources.some((source) => source.status !== "ok")
    )
  );
  const brollCount = data.product_broll.products.reduce(
    (total, product) => total + product.video_count,
    0
  );
  const brollWarning = !data.product_broll.exists || data.product_broll.products.some(
    (product) => !product.exists || (product.video_count > 0 && !product.preview?.exists)
  );
  const disabledCount = Object.values(featureFlags).filter((enabled) => !enabled).length;
  const information = informationLoading
    ? { tone: "loading" as const, status: "Loading", detail: "Reading the local product-information index" }
    : informationError
      ? { tone: "error" as const, status: "Error", detail: "Product-information status could not be loaded" }
      : !informationData?.sources.length
        ? { tone: "missing" as const, status: "Missing", detail: "No indexed PDF or DOCX sources" }
        : informationWarning
          ? { tone: "warning" as const, status: "Warning", detail: `${eligibleFacts} eligible fact${eligibleFacts === 1 ? "" : "s"}` }
          : { tone: "ready" as const, status: "Ready", detail: `${eligibleFacts} eligible fact${eligibleFacts === 1 ? "" : "s"}` };

  return (
    <section className="diagnostic-section" aria-labelledby="supporting-readiness-heading">
      <header className="diagnostic-section-head">
        <div>
          <h3 id="supporting-readiness-heading">Supporting asset readiness</h3>
          <p>Current local sources and application inventories used by variant configuration.</p>
        </div>
      </header>
      <div className="diagnostic-readiness-list">
        <ReadinessItem label="Product information" {...information} />
        <ReadinessItem
          label="Fixed preview media"
          tone={data.preview_source.exists ? "ready" : "missing"}
          status={data.preview_source.exists ? "Ready" : "Missing"}
          detail={data.preview_source.exists ? "Configured host preview is available" : "Host-mode approximation is unavailable"}
        />
        <ReadinessItem
          label="Product B-roll"
          tone={brollCount === 0 ? "missing" : brollWarning ? "warning" : "ready"}
          status={brollCount === 0 ? "Missing" : brollWarning ? "Warning" : "Ready"}
          detail={`${brollCount} available clip${brollCount === 1 ? "" : "s"}`}
        />
        <ReadinessItem
          label="Background music"
          tone={!featureFlags.bgm ? "disabled" : data.bgm_tracks.length ? "ready" : "missing"}
          status={!featureFlags.bgm ? "Disabled globally" : data.bgm_tracks.length ? "Ready" : "Missing"}
          detail={`${data.bgm_tracks.length} discovered track${data.bgm_tracks.length === 1 ? "" : "s"}`}
        />
        <ReadinessItem
          label="Discovered fonts"
          tone={data.fonts.length ? "ready" : "missing"}
          status={data.fonts.length ? "Ready" : "Missing"}
          detail={`${data.fonts.length} available font${data.fonts.length === 1 ? "" : "s"}`}
        />
        <ReadinessItem
          label="Text styles"
          tone={data.text_styles.length ? "ready" : "missing"}
          status={data.text_styles.length ? "Ready" : "Missing"}
          detail={`${data.text_styles.length} configured style${data.text_styles.length === 1 ? "" : "s"}`}
        />
        <ReadinessItem
          label="Global feature availability"
          tone={disabledCount ? "warning" : "ready"}
          status={disabledCount ? "Warning" : "Ready"}
          detail={disabledCount ? `${disabledCount} feature${disabledCount === 1 ? "" : "s"} disabled globally` : "All reported features are available"}
        />
      </div>
    </section>
  );
}
