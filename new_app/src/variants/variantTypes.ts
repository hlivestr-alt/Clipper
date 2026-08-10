import type {
  ProductInformationStatus,
  VariationPageData,
  VariationVariant
} from "../api";

export type VariantCommandStatus = "saved" | "unsaved" | "saving";

export type PresetPanelFeedback = {
  kind: "success" | "info" | "error";
  text: string;
};

export type PresetPanelOption = {
  presetId: string;
  name: string;
};

export type VariantPreviewFeedback = {
  kind: "success" | "warning" | "error";
  text: string;
};

export type VariantEditorTabId =
  | "basics"
  | "text-subtitles"
  | "visual"
  | "audio"
  | "dynamic-text"
  | "advanced"
  | "assets-diagnostics";

export type VariantFeatureFlags = NonNullable<VariationPageData["global_feature_flags"]>;

export type VariantEditorContext = {
  variant: VariationVariant;
  variantIndex: number;
  data: VariationPageData;
  informationData?: ProductInformationStatus;
  variationWarnings?: string[];
  productInformationLoading?: boolean;
  productInformationError?: string;
  productInformationScanning?: boolean;
  onRescanProductInformation?: () => void;
  featureFlags: VariantFeatureFlags;
  previewProduct: string;
  previewInformationProduct: string;
  updateVariant: (patch: Partial<VariationVariant>) => void;
  applyTextStyle: (textStyleId: VariationVariant["text_style_id"]) => void;
  updateSubtitleY: (value: number) => void;
  updateLetterboxEnabled: (enabled: boolean) => void;
  updateLetterboxHookEnabled: (enabled: boolean) => void;
  updateDynamicTextRole: (
    role: VariationVariant["dynamic_text_roles"][number],
    enabled: boolean
  ) => void;
  updateDynamicTextSetting: (
    role: VariationVariant["dynamic_text_roles"][number],
    patch: Partial<VariationVariant["dynamic_text_settings"]["ingredients"]>
  ) => void;
  updatePreviewProduct: (productKey: string) => void;
  updatePreviewInformationProduct: (productKey: string) => void;
  hookTypeAvailable: (hookType: string) => boolean;
  beforeAfterRelevant: boolean;
};

export type VariantTabPanelProps = VariantEditorContext & {
  onNavigateTab: (tab: VariantEditorTabId) => void;
};
