// @vitest-environment jsdom

import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { VariationVariant } from "../api";
import { VariantCommandBar } from "./VariantCommandBar";
import { VariantNavigator } from "./VariantNavigator";
import type { VariantCommandStatus } from "./variantTypes";

function makeVariant(index = 0): VariationVariant {
  const setting = {
    headline_font_id: "font.ttf",
    body_font_id: "font.ttf",
    font_size: 35,
    animation: "current" as const,
    duration_seconds: 2.6
  };
  return {
    name: `Draft name ${index + 1}`,
    hook_type: index === 0 ? "text" : "b_roll",
    visual_mode: index === 0 ? "host" : "broll_audio",
    random_broll_enabled: index === 0,
    before_after_mode: "fullscreen",
    text_style_id: "current",
    font_id: "font.ttf",
    headline_font_id: "font.ttf",
    caption_font_id: "font.ttf",
    font_color: "#FFFFFF",
    highlight_color: "#FFD600",
    subtitle_position: "bottom",
    subtitle_size: "medium",
    subtitle_stroke_color: "#000000",
    subtitle_stroke_width: 3,
    subtitle_highlight_enabled: true,
    subtitle_animation: "current",
    headline_animation: "current",
    caption_animation: "current",
    headline_stroke_width: 5,
    headline_shadow_color: "#000000",
    headline_shadow_x: 3,
    headline_shadow_y: 3,
    headline_rotation_degrees: 0,
    caption_stroke_width: 4,
    color_grade: "original",
    bgm_mode: index === 0 ? "auto" : "none",
    bgm_path: "",
    sfx_enabled: index === 0,
    zoom_intensity: "normal",
    product_zoom_enabled: true,
    subtitle_enabled: index === 0,
    dynamic_text_mode: index === 0 ? "balanced" : "off",
    dynamic_text_roles: ["ingredients", "benefits", "usage", "cta"],
    dynamic_text_settings: {
      ingredients: { ...setting },
      benefits: { ...setting },
      usage: { ...setting },
      cta: { ...setting, font_size: 50 }
    },
    letterbox_enabled: false,
    mirror_enabled: false,
    subtitle_y_frac: 0.84,
    letterbox_top_frac: 0,
    letterbox_bottom_frac: 0,
    letterbox_hook_enabled: false,
    letterbox_hook_font_id: "font.ttf",
    letterbox_hook_font_color: "#FFFFFF",
    letterbox_hook_font_size: 72,
    letterbox_hook_x_frac: 0.5,
    letterbox_hook_y_frac: 0.5
  };
}

function CommandHarness({
  status = "saved",
  conflict,
  refreshing = false,
  canApply = false,
  onApply = vi.fn(),
  onRefresh = vi.fn(),
  onSavePreset = vi.fn(),
  onLoadPreset = vi.fn()
}: {
  status?: VariantCommandStatus;
  conflict?: string;
  refreshing?: boolean;
  canApply?: boolean;
  onApply?: () => void;
  onRefresh?: () => void;
  onSavePreset?: () => void;
  onLoadPreset?: () => void;
}) {
  const [presetName, setPresetName] = useState("");
  const [selectedPreset, setSelectedPreset] = useState("");
  return (
    <VariantCommandBar
      status={status}
      conflict={conflict}
      variantCount={3}
      revision="revision-1234567890"
      refreshing={refreshing}
      canApply={canApply}
      presetName={presetName}
      selectedPreset={selectedPreset}
      presets={[{ presetId: "preset-one", name: "Preset one" }]}
      presetSaving={false}
      presetLoading={false}
      onRefresh={onRefresh}
      onApply={onApply}
      onPresetNameChange={setPresetName}
      onSelectedPresetChange={setSelectedPreset}
      onSavePreset={onSavePreset}
      onLoadPreset={onLoadPreset}
    />
  );
}

afterEach(cleanup);

describe("Phase 2 Variants command area", () => {
  it("shows the only Variants page heading and reflects saved, unsaved, saving, conflict, and apply states", () => {
    const apply = vi.fn();
    const view = render(<CommandHarness onApply={apply} />);
    expect(screen.getAllByRole("heading", { name: "Variants" })).toHaveLength(1);
    expect(screen.getByText("Saved")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Apply to future clips" }) as HTMLButtonElement).disabled).toBe(true);

    view.rerender(<CommandHarness status="unsaved" canApply onApply={apply} />);
    expect(screen.getByText("Unsaved")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Apply to future clips" }));
    expect(apply).toHaveBeenCalledOnce();

    view.rerender(<CommandHarness status="saving" canApply />);
    expect(screen.getAllByText("Saving")).toHaveLength(2);
    expect((screen.getByRole("button", { name: "Saving" }) as HTMLButtonElement).disabled).toBe(true);

    view.rerender(<CommandHarness status="unsaved" canApply conflict="External revision changed." />);
    expect(screen.getByText("Revision conflict")).toBeTruthy();
    expect(screen.getByText("External revision changed.")).toBeTruthy();
    expect(screen.getByText("Unsaved")).toBeTruthy();
  });

  it("exposes refreshing state and invokes refresh when available", () => {
    const refresh = vi.fn();
    const view = render(<CommandHarness onRefresh={refresh} />);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(refresh).toHaveBeenCalledOnce();

    view.rerender(<CommandHarness refreshing onRefresh={refresh} />);
    const button = screen.getByRole("button", { name: "Refreshing" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button.getAttribute("aria-busy")).toBe("true");
  });

  it("opens, focuses, operates, and closes the presets panel with keyboard support", async () => {
    const save = vi.fn();
    const load = vi.fn();
    render(<CommandHarness onSavePreset={save} onLoadPreset={load} />);
    const trigger = screen.getByRole("button", { name: "Presets" });

    fireEvent.click(trigger);
    expect(await screen.findByRole("dialog", { name: "Presets" })).toBeTruthy();
    const nameInput = screen.getByLabelText("Save current draft");
    await waitFor(() => expect(document.activeElement).toBe(nameInput));
    fireEvent.change(nameInput, { target: { value: "My preset" } });
    fireEvent.click(screen.getByRole("button", { name: "Save preset" }));
    expect(save).toHaveBeenCalledOnce();

    fireEvent.change(screen.getByLabelText("Load into draft"), { target: { value: "preset-one" } });
    fireEvent.click(screen.getByRole("button", { name: "Load" }));
    expect(load).toHaveBeenCalledOnce();

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Presets" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("button", { name: "Close presets" }));
    expect(screen.queryByRole("dialog", { name: "Presets" })).toBeNull();
  });
});

describe("Phase 2 variant navigator", () => {
  it("renders one keyboard-selectable card per variant with state-derived accessible summaries", () => {
    const select = vi.fn();
    const variants = Array.from({ length: 6 }, (_, index) => makeVariant(index));
    const view = render(
      <VariantNavigator
        variants={variants}
        selectedIndex={0}
        minimumCount={1}
        maximumCount={6}
        onSelect={select}
        onCountChange={vi.fn()}
      />
    );

    expect(view.container.querySelectorAll(".variant-navigator-card")).toHaveLength(6);
    expect(screen.getByText("Draft name 1")).toBeTruthy();
    expect(screen.getByText("Host · Normal zoom")).toBeTruthy();
    expect(screen.getByText("Subtitles on · Music + SFX")).toBeTruthy();

    const second = view.container.querySelectorAll(".variant-navigator-card")[1] as HTMLButtonElement;
    fireEvent.click(second);
    expect(select).toHaveBeenCalledWith(1);
    expect(second.tagName).toBe("BUTTON");
  });

  it("supports decrement, direct entry, and increment with disabled limit controls", () => {
    const change = vi.fn();
    const view = render(
      <VariantNavigator
        variants={[makeVariant(0)]}
        selectedIndex={0}
        minimumCount={1}
        maximumCount={6}
        onSelect={vi.fn()}
        onCountChange={change}
      />
    );
    expect((screen.getByRole("button", { name: "Decrease variant count" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Increase variant count" }));
    expect(change).toHaveBeenCalledWith(2);
    fireEvent.change(screen.getByRole("spinbutton", { name: "Variant count" }), { target: { value: "4" } });
    expect(change).toHaveBeenCalledWith(4);

    view.rerender(
      <VariantNavigator
        variants={Array.from({ length: 6 }, (_, index) => makeVariant(index))}
        selectedIndex={0}
        minimumCount={1}
        maximumCount={6}
        onSelect={vi.fn()}
        onCountChange={change}
      />
    );
    expect((screen.getByRole("button", { name: "Increase variant count" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Decrease variant count" }));
    expect(change).toHaveBeenCalledWith(5);
  });
});
