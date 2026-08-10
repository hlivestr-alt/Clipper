// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ApiEnvelope,
  ProductInformationStatus,
  VariationPageData,
  VariationPreviewResult,
  VariationProfile,
  VariationVariant
} from "./api";

const queryState = vi.hoisted(() => ({
  variations: undefined as ApiEnvelope<VariationPageData> | undefined,
  productInformation: undefined as ApiEnvelope<ProductInformationStatus> | undefined
}));

vi.mock("./useApiQuery", () => ({
  useApiQuery: (path: string) => ({
    envelope: path === "/api/variations" ? queryState.variations : queryState.productInformation,
    loading: false,
    refreshing: false,
    error: undefined,
    refresh: vi.fn()
  })
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getJson: vi.fn(),
    sendJson: vi.fn()
  };
});

import { App, VariationsPage } from "./App";
import { getJson, sendJson } from "./api";

const mockedGetJson = vi.mocked(getJson);
const mockedSendJson = vi.mocked(sendJson);

function makeVariant(index = 0): VariationVariant {
  const roleSetting = {
    headline_font_id: "font.ttf",
    body_font_id: "font.ttf",
    font_size: 35,
    animation: "current" as const,
    duration_seconds: 2.6
  };
  return {
    name: `Variant ${index + 1}`,
    hook_type: "text",
    visual_mode: "host",
    random_broll_enabled: false,
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
    bgm_mode: "auto",
    bgm_path: "",
    sfx_enabled: true,
    zoom_intensity: "normal",
    product_zoom_enabled: true,
    subtitle_enabled: true,
    dynamic_text_mode: "balanced",
    dynamic_text_roles: ["ingredients", "benefits", "usage", "cta"],
    dynamic_text_settings: {
      ingredients: { ...roleSetting },
      benefits: { ...roleSetting },
      usage: { ...roleSetting },
      cta: { ...roleSetting, font_size: 50, duration_seconds: 1.3 }
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

function makeProfile(count = 1, revision = "rev-1"): VariationProfile {
  return {
    schema_version: 12,
    revision,
    variant_count: count,
    updated_at: "2026-07-29T00:00:00Z",
    variants: Array.from({ length: count }, (_, index) => makeVariant(index))
  };
}

function makePageData(count = 1, revision = "rev-1"): VariationPageData {
  return {
    profile: makeProfile(count, revision),
    fonts: [{ id: "font.ttf", path: "font.ttf", label: "Test Font" }],
    text_styles: [{ id: "current", label: "Current", description: "Current", defaults: {} }],
    bgm_tracks: [],
    hook_types: ["text"],
    visual_modes: ["host", "broll_audio"],
    before_after_modes: ["fullscreen"],
    subtitle_positions: ["top", "center", "bottom"],
    subtitle_sizes: ["compact", "small", "medium", "large"],
    dynamic_text_modes: ["off", "minimal", "balanced", "high_energy"],
    dynamic_text_roles: ["ingredients", "benefits", "usage", "cta"],
    dynamic_text_animations: ["current", "staggered_reveal", "fade_up", "wipe", "slide_up"],
    color_grades: ["original"],
    bgm_modes: ["auto", "none", "selected"],
    zoom_intensities: ["none", "subtle", "normal", "strong"],
    presets: [{ preset_id: "preset-one", name: "Preset one", revision: "preset-rev" }],
    limits: { min_variants: 1, max_variants: 6 },
    preview_source: { path: "preview.mp4", url: "/preview.mp4", kind: "video", exists: true },
    product_broll: { root: "", exists: false, products: [] },
    global_feature_flags: {
      sfx: true,
      bgm: true,
      before_after: true,
      broll_intro: true,
      transitional_hook: true,
      host_face_zoom: true
    }
  };
}

function envelope<T>(data: T): ApiEnvelope<T> {
  return {
    data,
    generated_at: "2026-07-29T00:00:00Z",
    source_signatures: [],
    warnings: []
  };
}

function makePreviewResult(name: string, url: string): VariationPreviewResult {
  return {
    profile_revision: `${name}-revision`,
    source_clip: "preview.mp4",
    preview_source: { path: "preview.mp4", url: "/preview.mp4", kind: "video", exists: true },
    previews: [{
      variant_index: 0,
      variant_name: name,
      path: url.slice(1),
      url,
      kind: "video",
      exists: true
    }],
    message: `${name} ready.`
  };
}

function makeInformationData(): ProductInformationStatus {
  return {
    schema_version: 1,
    revision: "information-rev",
    scanned_at: "2026-07-29T00:00:00Z",
    root: "C:\\private\\information",
    sources: [{
      path: "C:\\private\\information\\serum.pdf",
      extension: ".pdf",
      size: 100,
      sha256: "abc",
      status: "ok",
      cached: true,
      extraction_method: "rules",
      page_count: 1,
      warnings: [],
      products: ["serum"],
      eligible_fact_count: 4,
      fact_counts: { benefits: 4 },
      unassigned_count: 0
    }],
    products: [{ product_key: "serum", label: "Serum", eligible_fact_count: 4, fact_counts: { benefits: 4 } }],
    unassigned_count: 0,
    conflict_count: 0,
    unassigned: [],
    conflicts: [],
    warnings: []
  };
}

function setPageData(data: VariationPageData) {
  queryState.variations = envelope(data);
}

function variantNameInput(container: HTMLElement): HTMLInputElement {
  return container.querySelector('input[aria-label="Variant name"]') as HTMLInputElement;
}

function countInput(container: HTMLElement): HTMLInputElement {
  return container.querySelector('input[aria-label="Variant count"]') as HTMLInputElement;
}

function selectVariant(container: HTMLElement, index: number) {
  fireEvent.click(container.querySelectorAll(".variant-navigator-card")[index] as HTMLButtonElement);
}

function selectedVariantIndex(container: HTMLElement): number {
  return Array.from(container.querySelectorAll(".variant-navigator-card"))
    .findIndex((card) => card.getAttribute("aria-pressed") === "true");
}

function presetSelect(container: HTMLElement): HTMLSelectElement {
  return container.querySelector("#variant-preset-select") as HTMLSelectElement;
}

function openPresets() {
  fireEvent.click(screen.getByRole("button", { name: "Presets" }));
}

beforeEach(() => {
  setPageData(makePageData());
  queryState.productInformation = undefined;
  mockedGetJson.mockReset();
  mockedSendJson.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("variation draft safety", () => {
  it("renders exactly one Variants page heading inside the shared application shell", async () => {
    window.history.pushState({}, "", "/variants");
    render(<App />);
    await waitFor(() => expect(screen.getAllByRole("heading", { name: "Variants" })).toHaveLength(1));
    expect(screen.getByRole("link", { name: "Open queue and system health" })).toBeTruthy();
  });

  it("uses one Variants heading and synchronizes navigator selection, editor, preview, and draft names", async () => {
    setPageData(makePageData(2));
    const view = render(<VariationsPage active />);
    await waitFor(() => expect(view.container.querySelectorAll(".variant-navigator-card")).toHaveLength(2));

    expect(screen.getAllByRole("heading", { name: "Variants" })).toHaveLength(1);
    expect(screen.getByText("Saved")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Apply to future clips" }) as HTMLButtonElement).disabled).toBe(true);
    expect(view.container.querySelector(".subtitle-drag-handle")).toBeNull();
    expect(view.container.querySelector(".preview-adjustments")).toBeNull();
    expect(view.container.querySelectorAll("#variant-subtitle-y")).toHaveLength(1);
    expect(view.container.querySelectorAll("#variant-letterbox-top")).toHaveLength(1);
    expect(view.container.querySelectorAll("#variant-letterbox-bottom")).toHaveLength(1);

    const secondCard = view.container.querySelectorAll(".variant-navigator-card")[1] as HTMLButtonElement;
    fireEvent.click(secondCard);
    expect(secondCard.getAttribute("aria-pressed")).toBe("true");
    expect(selectedVariantIndex(view.container)).toBe(1);
    expect(variantNameInput(view.container).value).toBe("Variant 2");
    expect(screen.getByRole("heading", { name: "Variant 2" })).toBeTruthy();

    fireEvent.change(variantNameInput(view.container), { target: { value: "Immediate navigator name" } });
    expect(secondCard.textContent).toContain("Immediate navigator name");
    expect(screen.getByRole("heading", { name: "Immediate navigator name" })).toBeTruthy();
    expect(screen.getByText("Unsaved")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Apply to future clips" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("changes variant count through increase, direct entry, and decrease while preserving cloning behavior", async () => {
    const view = render(<VariationsPage active />);
    await waitFor(() => expect(view.container.querySelectorAll(".variant-navigator-card")).toHaveLength(1));

    fireEvent.click(screen.getByRole("button", { name: "Increase variant count" }));
    expect(view.container.querySelectorAll(".variant-navigator-card")).toHaveLength(2);
    expect(view.container.querySelectorAll(".variant-navigator-card")[1].textContent).toContain("Variant 2");

    fireEvent.change(countInput(view.container), { target: { value: "4" } });
    expect(view.container.querySelectorAll(".variant-navigator-card")).toHaveLength(4);
    fireEvent.click(screen.getByRole("button", { name: "Decrease variant count" }));
    expect(view.container.querySelectorAll(".variant-navigator-card")).toHaveLength(3);
  });

  it("saves the current draft as a preset through the command-bar panel", async () => {
    mockedSendJson.mockResolvedValueOnce(envelope({}));
    render(<VariationsPage active />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Presets" })).toBeTruthy());

    openPresets();
    fireEvent.change(screen.getByLabelText("Save current draft"), { target: { value: "Campaign preset" } });
    fireEvent.click(screen.getByRole("button", { name: "Save preset" }));

    await waitFor(() => expect(mockedSendJson).toHaveBeenCalledWith(
      "POST",
      "/api/variations/presets",
      expect.objectContaining({
        name: "Campaign preset",
        profile: expect.objectContaining({ revision: "rev-1" })
      })
    ));
    await waitFor(() => expect(screen.getAllByText("Preset saved.").length).toBeGreaterThan(0));
  });

  it("exposes command-bar saving state until apply completes", async () => {
    let resolveSave: ((value: ApiEnvelope<VariationPageData>) => void) | undefined;
    mockedSendJson.mockImplementationOnce(() => new Promise((resolve) => {
      resolveSave = resolve;
    }));
    const view = render(<VariationsPage active />);
    await waitFor(() => expect(variantNameInput(view.container)).toBeTruthy());

    fireEvent.change(variantNameInput(view.container), { target: { value: "Saving draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply to future clips" }));
    expect(screen.getByRole("button", { name: "Saving" })).toBeTruthy();
    expect(screen.getAllByText("Saving")).toHaveLength(2);

    const saved = makePageData(1, "rev-saved");
    saved.profile.variants[0].name = "Saving draft";
    await act(async () => {
      resolveSave?.(envelope(saved));
    });
    await waitFor(() => expect(screen.getByText("Saved")).toBeTruthy());
  });

  it("adopts a background refetch while clean but preserves the accepted baseline and draft while dirty", async () => {
    const initial = makePageData();
    setPageData(initial);
    const view = render(<VariationsPage active />);
    await waitFor(() => expect(variantNameInput(view.container).value).toBe("Variant 1"));

    const cleanRefresh = makePageData(1, "rev-2");
    cleanRefresh.profile.variants[0].name = "Clean server refresh";
    setPageData(cleanRefresh);
    view.rerender(<VariationsPage active />);
    await waitFor(() => expect(variantNameInput(view.container).value).toBe("Clean server refresh"));
    expect(screen.getByText("Saved")).toBeTruthy();

    fireEvent.change(variantNameInput(view.container), { target: { value: "Unsaved local draft" } });
    expect(screen.getByText("Unsaved")).toBeTruthy();

    const dirtyRefresh = makePageData(1, "rev-3");
    dirtyRefresh.profile.variants[0].name = "Newer server value";
    setPageData(dirtyRefresh);
    view.rerender(<VariationsPage active />);

    await waitFor(() => expect(screen.getByText("Revision conflict")).toBeTruthy());
    expect(variantNameInput(view.container).value).toBe("Unsaved local draft");
    expect(screen.getByText("Unsaved")).toBeTruthy();
  });

  it("preserves the unsaved draft and exposes conflict state after an HTTP 409", async () => {
    mockedSendJson.mockRejectedValueOnce(new Error("409 Conflict: revision changed"));
    const view = render(<VariationsPage active />);
    await waitFor(() => expect(variantNameInput(view.container)).toBeTruthy());

    fireEvent.change(variantNameInput(view.container), { target: { value: "Keep this edit" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply to future clips" }));

    await waitFor(() => expect(screen.getByText("Revision conflict")).toBeTruthy());
    expect(variantNameInput(view.container).value).toBe("Keep this edit");
    expect(screen.getByText("Unsaved")).toBeTruthy();
    expect(mockedSendJson).toHaveBeenCalledWith(
      "PUT",
      "/api/variations",
      expect.objectContaining({ expected_revision: "rev-1" })
    );
  });

  it("loads a preset directly into a clean draft and clamps the selected variant", async () => {
    setPageData(makePageData(3));
    const preset = makeProfile(1, "preset-rev");
    preset.variants[0].name = "Loaded preset";
    mockedGetJson.mockResolvedValueOnce(envelope(preset));
    const view = render(<VariationsPage active />);
    await waitFor(() => expect(view.container.querySelectorAll(".variant-navigator-card")).toHaveLength(3));

    selectVariant(view.container, 2);
    openPresets();
    fireEvent.change(presetSelect(view.container), { target: { value: "preset-one" } });
    fireEvent.click(screen.getByRole("button", { name: "Load" }));

    await waitFor(() => expect(screen.getByDisplayValue("Loaded preset")).toBeTruthy());
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(selectedVariantIndex(view.container)).toBe(0);
    expect(mockedSendJson).not.toHaveBeenCalled();
  });

  it("requires confirmation before a dirty preset load; cancel preserves state and confirm replaces it", async () => {
    setPageData(makePageData(2));
    const preset = makeProfile(1, "preset-rev");
    preset.variants[0].name = "Confirmed preset";
    mockedGetJson.mockResolvedValueOnce(envelope(preset));
    const view = render(<VariationsPage active />);
    await waitFor(() => expect(view.container.querySelectorAll(".variant-navigator-card")).toHaveLength(2));

    selectVariant(view.container, 1);
    fireEvent.change(variantNameInput(view.container), { target: { value: "Dirty second variant" } });
    openPresets();
    fireEvent.change(presetSelect(view.container), { target: { value: "preset-one" } });
    fireEvent.click(screen.getByRole("button", { name: "Load" }));

    const firstDialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(firstDialog).getByRole("button", { name: "Cancel" }));
    expect(variantNameInput(view.container).value).toBe("Dirty second variant");
    expect(selectedVariantIndex(view.container)).toBe(1);
    expect(mockedGetJson).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Load" }));
    const secondDialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(secondDialog).getByRole("button", { name: "Load preset" }));

    await waitFor(() => expect(screen.getByDisplayValue("Confirmed preset")).toBeTruthy());
    expect(selectedVariantIndex(view.container)).toBe(0);
    expect(mockedSendJson).not.toHaveBeenCalled();
  });

  it("clamps the unified selected variant after reducing the variant count", async () => {
    setPageData(makePageData(6));
    const view = render(<VariationsPage active />);
    await waitFor(() => expect(view.container.querySelectorAll(".variant-navigator-card")).toHaveLength(6));

    selectVariant(view.container, 5);
    fireEvent.change(countInput(view.container), { target: { value: "2" } });

    expect(view.container.querySelectorAll(".variant-navigator-card")).toHaveLength(2);
    expect(selectedVariantIndex(view.container)).toBe(1);
  });

  it("submits the full selected draft and exposes the render busy state", async () => {
    queryState.productInformation = envelope(makeInformationData());
    let resolvePreview: ((value: ApiEnvelope<VariationPreviewResult>) => void) | undefined;
    mockedSendJson.mockImplementationOnce(() => new Promise((resolve) => {
      resolvePreview = resolve;
    }));
    render(<VariationsPage active />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Render 6-second preview" })).toBeTruthy());
    await waitFor(() => expect(screen.getByDisplayValue("Serum (4)")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Render 6-second preview" }));
    const busyButton = screen.getByRole("button", { name: "Rendering 6-second preview" }) as HTMLButtonElement;
    expect(busyButton.disabled).toBe(true);
    expect(busyButton.getAttribute("aria-busy")).toBe("true");
    expect(mockedSendJson).toHaveBeenCalledWith(
      "POST",
      "/api/variations/previews",
      expect.objectContaining({
        profile: expect.objectContaining({
          revision: "rev-1",
          variants: expect.arrayContaining([expect.objectContaining({ name: "Variant 1" })])
        }),
        variant_index: 0,
        product_key: "serum"
      })
    );

    await act(async () => {
      resolvePreview?.(envelope(makePreviewResult("Rendered payload", "/rendered-payload.mp4")));
    });
    await waitFor(() => expect(screen.getByText("Rendered")).toBeTruthy());
  });

  it("shows a local no-result warning and network failure without changing the draft", async () => {
    mockedSendJson
      .mockResolvedValueOnce(envelope({
        ...makePreviewResult("Empty", "/unused.mp4"),
        previews: [],
        message: "No supported preview output was produced."
      }))
      .mockRejectedValueOnce(new Error("Preview service unavailable"));
    const view = render(<VariationsPage active />);
    await waitFor(() => expect(variantNameInput(view.container)).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Render 6-second preview" }));
    await waitFor(() => expect(screen.getByText("No result")).toBeTruthy());
    expect(screen.getAllByText("No supported preview output was produced.").length).toBeGreaterThan(0);
    expect(variantNameInput(view.container).value).toBe("Variant 1");

    fireEvent.click(screen.getByRole("button", { name: "Render 6-second preview" }));
    await waitFor(() => expect(screen.getByText("Error")).toBeTruthy());
    expect(screen.getAllByText("Preview service unavailable").length).toBeGreaterThan(0);
    expect(variantNameInput(view.container).value).toBe("Variant 1");
  });

  it("invalidates a rendered preview after draft and preview-product changes", async () => {
    const data = makePageData();
    data.profile.variants[0].visual_mode = "broll_audio";
    data.product_broll = {
      root: "broll",
      exists: true,
      products: [
        {
          product_key: "serum",
          label: "Serum",
          folder: "serum",
          exists: true,
          video_count: 1,
          preview: { path: "serum.mp4", url: "/serum.mp4", kind: "video", exists: true }
        },
        {
          product_key: "cleanser",
          label: "Cleanser",
          folder: "cleanser",
          exists: true,
          video_count: 1,
          preview: { path: "cleanser.mp4", url: "/cleanser.mp4", kind: "video", exists: true }
        }
      ]
    };
    setPageData(data);
    mockedSendJson.mockResolvedValue(envelope(makePreviewResult("Rendered", "/rendered.mp4")));
    const view = render(<VariationsPage active />);
    fireEvent.click(await screen.findByRole("tab", { name: "Visual" }));
    await waitFor(() => expect(screen.getByDisplayValue("Serum (1)")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Render 6-second preview" }));
    await waitFor(() => expect(view.container.querySelector(".generated-variation-preview")).toBeTruthy());
    fireEvent.click(screen.getByRole("tab", { name: "Basics" }));
    fireEvent.change(variantNameInput(view.container), { target: { value: "Relevant draft edit" } });
    expect(view.container.querySelector(".generated-variation-preview")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Render 6-second preview" }));
    await waitFor(() => expect(view.container.querySelector(".generated-variation-preview")).toBeTruthy());
    fireEvent.click(screen.getByRole("tab", { name: "Visual" }));
    fireEvent.change(screen.getByDisplayValue("Serum (1)"), { target: { value: "cleanser" } });
    expect(view.container.querySelector(".generated-variation-preview")).toBeNull();
  });

  it("rejects an older preview response after a relevant change and newer render", async () => {
    let resolvePreview: ((value: ApiEnvelope<VariationPreviewResult>) => void) | undefined;
    mockedSendJson.mockImplementationOnce(() => new Promise((resolve) => {
      resolvePreview = resolve;
    }));
    const view = render(<VariationsPage active />);
    await waitFor(() => expect(variantNameInput(view.container)).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Render 6-second preview" }));
    fireEvent.change(variantNameInput(view.container), { target: { value: "Changed during render" } });
    mockedSendJson.mockResolvedValueOnce(envelope(makePreviewResult("New response", "/new.mp4")));
    fireEvent.click(screen.getByRole("button", { name: "Render 6-second preview" }));
    await waitFor(() => {
      const preview = view.container.querySelector(".generated-variation-preview") as HTMLVideoElement;
      expect(preview?.getAttribute("src")).toBe("/new.mp4");
    });

    await act(async () => {
      resolvePreview?.(envelope(makePreviewResult("Old response", "/old.mp4")));
    });

    const preview = view.container.querySelector(".generated-variation-preview") as HTMLVideoElement;
    expect(preview.getAttribute("src")).toBe("/new.mp4");
    expect(screen.getAllByText("New response ready.").length).toBeGreaterThan(0);
    expect(screen.queryByText("Old response ready.")).toBeNull();
    expect(screen.getByRole("button", { name: "Render 6-second preview" })).toBeTruthy();
  });
});
