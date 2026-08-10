import type { ReactNode } from "react";

export function VariantWorkspace({
  navigator,
  editor,
  preview
}: {
  navigator: ReactNode;
  editor: ReactNode;
  preview: ReactNode;
}) {
  function jumpToPreview() {
    const previewRegion = document.getElementById("variant-preview-panel");
    if (!previewRegion) return;
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    previewRegion.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    previewRegion.focus({ preventScroll: true });
  }

  return (
    <div className="variant-workspace">
      <div className="variant-workspace-navigator">{navigator}</div>
      <div className="variant-workspace-editor" role="region" aria-label="Selected variant editor">
        <button
          type="button"
          className="secondary-button variant-jump-preview"
          aria-controls="variant-preview-panel"
          onClick={jumpToPreview}
        >
          Jump to preview
        </button>
        {editor}
      </div>
      <div className="variant-workspace-preview" role="region" aria-label="Selected variant preview">{preview}</div>
    </div>
  );
}
