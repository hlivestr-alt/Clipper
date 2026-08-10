// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { VariationVariant } from "../api";
import { VariantNavigator } from "./VariantNavigator";
import { VariantWorkspace } from "./VariantWorkspace";

afterEach(cleanup);

function makeVariant(name: string): VariationVariant {
  return {
    name,
    hook_type: "text",
    visual_mode: "host",
    subtitle_enabled: true,
    dynamic_text_mode: "balanced",
    dynamic_text_roles: ["ingredients", "benefits", "usage", "cta"],
    bgm_mode: "auto",
    sfx_enabled: true
  } as VariationVariant;
}

describe("Phase 6 Variants layout foundations", () => {
  it("keeps navigator, editor, and preview in the intended accessible DOM order", () => {
    render(
      <VariantWorkspace
        navigator={<nav aria-label="Test navigator">Navigator</nav>}
        editor={<div>Editor</div>}
        preview={<div>Preview</div>}
      />
    );

    const workspace = screen.getByText("Navigator").closest(".variant-workspace");
    expect(workspace).not.toBeNull();
    expect(Array.from(workspace!.children).map((node) => node.className)).toEqual([
      "variant-workspace-navigator",
      "variant-workspace-editor",
      "variant-workspace-preview"
    ]);
    expect(screen.getByRole("region", { name: "Selected variant editor" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Selected variant preview" })).toBeTruthy();
  });

  it("keeps long navigator names discoverable and cards keyboard-operable", () => {
    const longName = "A deliberately long variant name that must truncate without becoming inaccessible";
    render(
      <VariantNavigator
        variants={[makeVariant(longName)]}
        selectedIndex={0}
        minimumCount={1}
        maximumCount={6}
        onSelect={vi.fn()}
        onCountChange={vi.fn()}
      />
    );

    const navigator = screen.getByRole("complementary", { name: "Variant navigator" });
    const card = within(navigator).getByRole("button", { name: /V1/ });
    expect(card.getAttribute("title")).toContain(longName);
    expect(card.getAttribute("aria-pressed")).toBe("true");
    expect(card.tabIndex).toBe(0);
    expect(within(card).getByTitle(longName)).toBeTruthy();
  });

  it("jumps to and focuses the existing preview while respecting reduced motion", () => {
    const scrollIntoView = vi.fn();
    const originalMatchMedia = window.matchMedia;
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockReturnValue({ matches: true })
    });

    render(
      <VariantWorkspace
        navigator={<div>Navigator</div>}
        editor={<div>Editor</div>}
        preview={(
          <aside id="variant-preview-panel" tabIndex={-1}>
            Preview
          </aside>
        )}
      />
    );

    const preview = screen.getByText("Preview");
    Object.defineProperty(preview, "scrollIntoView", { configurable: true, value: scrollIntoView });
    fireEvent.click(screen.getByRole("button", { name: "Jump to preview" }));

    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "auto", block: "start" });
    expect(document.activeElement).toBe(preview);
    Object.defineProperty(window, "matchMedia", { configurable: true, value: originalMatchMedia });
  });

});
