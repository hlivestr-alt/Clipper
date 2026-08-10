import type { KeyboardEvent } from "react";
import { useRef, useState } from "react";
import { AdvancedTab } from "./tabs/AdvancedTab";
import { AssetsDiagnosticsTab } from "./tabs/AssetsDiagnosticsTab";
import { AudioTab } from "./tabs/AudioTab";
import { BasicsTab } from "./tabs/BasicsTab";
import { DynamicTextTab } from "./tabs/DynamicTextTab";
import { TextSubtitlesTab } from "./tabs/TextSubtitlesTab";
import { VisualTab } from "./tabs/VisualTab";
import type { VariantEditorContext, VariantEditorTabId } from "./variantTypes";

const tabs: Array<{ id: VariantEditorTabId; label: string }> = [
  { id: "basics", label: "Basics" },
  { id: "text-subtitles", label: "Text & Subtitles" },
  { id: "visual", label: "Visual" },
  { id: "audio", label: "Audio" },
  { id: "dynamic-text", label: "Dynamic Text" },
  { id: "advanced", label: "Advanced" },
  { id: "assets-diagnostics", label: "Assets & Diagnostics" }
];

export function VariantEditorTabs(props: VariantEditorContext) {
  const [activeTab, setActiveTab] = useState<VariantEditorTabId>("basics");
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const editorRootRef = useRef<HTMLDivElement | null>(null);
  const activeIndex = tabs.findIndex((tab) => tab.id === activeTab);

  function selectTab(tab: VariantEditorTabId, focus = false) {
    setActiveTab(tab);
    const scrollRegion = editorRootRef.current?.closest<HTMLElement>(".variation-editor");
    if (scrollRegion && tab !== activeTab) {
      scrollRegion.scrollTop = 0;
    }
    if (focus) {
      const index = tabs.findIndex((item) => item.id === tab);
      tabRefs.current[index]?.focus();
    }
  }

  function onTabKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    let nextIndex = activeIndex;
    if (event.key === "ArrowRight") {
      nextIndex = (activeIndex + 1) % tabs.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (activeIndex - 1 + tabs.length) % tabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = tabs.length - 1;
    } else {
      return;
    }
    event.preventDefault();
    selectTab(tabs[nextIndex].id, true);
  }

  const panelProps = { ...props, onNavigateTab: selectTab };
  function renderPanel(tab: VariantEditorTabId) {
    if (tab === "basics") return <BasicsTab {...panelProps} />;
    if (tab === "text-subtitles") return <TextSubtitlesTab {...panelProps} />;
    if (tab === "visual") return <VisualTab {...panelProps} />;
    if (tab === "audio") return <AudioTab {...panelProps} />;
    if (tab === "dynamic-text") return <DynamicTextTab {...panelProps} />;
    if (tab === "advanced") return <AdvancedTab {...panelProps} />;
    return <AssetsDiagnosticsTab {...panelProps} />;
  }
  return (
    <div className="variant-tabbed-editor" ref={editorRootRef}>
      <div className="variant-editor-tablist" role="tablist" aria-label="Variant editor sections">
        {tabs.map((tab, index) => (
          <button
            type="button"
            role="tab"
            id={`variant-tab-${tab.id}`}
            aria-controls={`variant-panel-${tab.id}`}
            aria-selected={activeTab === tab.id}
            tabIndex={activeTab === tab.id ? 0 : -1}
            key={tab.id}
            ref={(node) => { tabRefs.current[index] = node; }}
            onClick={() => selectTab(tab.id)}
            onKeyDown={onTabKeyDown}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {tabs.map((tab) => (
        <section
          className="variant-editor-tabpanel"
          role="tabpanel"
          id={`variant-panel-${tab.id}`}
          aria-labelledby={`variant-tab-${tab.id}`}
          tabIndex={activeTab === tab.id ? 0 : -1}
          hidden={activeTab !== tab.id}
          key={`panel-${tab.id}`}
        >
          {renderPanel(tab.id)}
        </section>
      ))}
    </div>
  );
}
