import { AssetReadinessSummary } from "../diagnostics/AssetReadinessSummary";
import { CreativeAssetInventory } from "../diagnostics/CreativeAssetInventory";
import { GlobalOverridesSummary } from "../diagnostics/GlobalOverridesSummary";
import { ProductInformationDiagnostics } from "../diagnostics/ProductInformationDiagnostics";
import type { VariantTabPanelProps } from "../variantTypes";

export function AssetsDiagnosticsTab({
  data,
  informationData,
  variationWarnings,
  productInformationLoading,
  productInformationError,
  productInformationScanning,
  onRescanProductInformation,
  featureFlags,
  previewProduct
}: VariantTabPanelProps) {
  return (
    <div className="variant-tab-stack variant-diagnostics-tab">
      <AssetReadinessSummary
        data={data}
        informationData={informationData}
        informationLoading={productInformationLoading}
        informationError={productInformationError}
        featureFlags={featureFlags}
      />
      {Boolean(variationWarnings?.length) && (
        <section className="diagnostic-section" aria-labelledby="variants-source-warnings-heading">
          <header className="diagnostic-section-head">
            <div>
              <h3 id="variants-source-warnings-heading">Variants source warnings</h3>
              <p>Warnings returned with the current Variants query.</p>
            </div>
          </header>
          <div className="diagnostic-warning-list diagnostic-section-body">
            {variationWarnings?.map((warning, index) => <p key={`${warning}-${index}`}>{warning}</p>)}
          </div>
        </section>
      )}
      <ProductInformationDiagnostics
        informationData={informationData}
        loading={productInformationLoading}
        error={productInformationError}
        scanning={productInformationScanning}
        onRescan={onRescanProductInformation}
      />
      <CreativeAssetInventory data={data} featureFlags={featureFlags} previewProduct={previewProduct} />
      <GlobalOverridesSummary featureFlags={featureFlags} />
    </div>
  );
}
