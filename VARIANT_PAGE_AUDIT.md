# Variants Workspace Implementation Audit

**Reviewed:** 2026-08-10  
**Route:** `/variants`  
**Purpose:** configure the revisioned profile used to generate future clip variants.

The Variants workspace is not a browser for already-rendered output. Clip review, approval, score/compliance inspection, and export remain on their dedicated pages. Applying a variation profile affects future pipeline renders only and does not rewrite existing clips.

This document replaces the pre-redesign audit. The earlier monolithic accordion/source-panel layout no longer exists: the current implementation has a command bar, compact variant navigator, seven-tab editor, separate preview/readiness panel, and diagnostics tab.

## Primary Workflow

1. Load `GET /api/variations` and `GET /api/product-information`.
2. Select a variant in the left navigator and, if needed, change the profile count from 1 through 6.
3. Edit the selected variant through the grouped tabs.
4. Inspect the immediate approximation or render a six-second preview.
5. Repeat for other variants.
6. Optionally save the draft as a preset.
7. Select **Apply to future clips** to save with the server revision that was originally loaded.

The command bar shows Saved/Unsaved/Saving, variant count, a shortened baseline revision, refresh, presets, and apply. Apply is disabled when the draft matches its baseline.

## Workspace Layout

The main workspace has three coordinated regions on wide screens:

- **Variant navigator:** count stepper and one compact card per visible variant. Each card shows its number/name and icon summaries for hook, visual mode, subtitles, dynamic text, and audio.
- **Selected variant editor:** seven keyboard-accessible tabs. Arrow Left/Right, Home, and End move tab focus. The editor scroll resets when changing tabs, and summary cards in Basics can jump directly to related tabs.
- **Preview/readiness panel:** selected variant identity, visual/grade/zoom summary, render action, portrait approximation/rendered media, preview limitations, and readiness for product information, fixed preview media, and B-roll.

On narrower screens the regions stack. **Jump to preview** scrolls/focuses the preview region and honors reduced-motion preference.

## Editor Tabs

| Tab | Current controls and behavior |
| --- | --- |
| **Basics** | Variant name, hook type, host vs audio-over-B-roll visual mode, coordinated text-style preset/reapply, color grade, mirror/flip, and read-only summary cards linking to Subtitles, Visual, Audio, and Dynamic Text. Hook choices that depend on globally disabled features are shown disabled. |
| **Text & Subtitles** | Subtitle enablement; Top/Center/Bottom placement; exact Y from 8-92%; Compact/Small/Medium/Large size; active-word highlighting; subtitle/headline/product-caption fonts; base and highlight colors; typography sample. Named placement also sets a standard exact Y value. |
| **Visual** | Before/After mode when a matching hook type is selected; relevant B-roll; preview-only B-roll product for audio-over-B-roll mode; product zoom and intensity. Host-only B-roll/zoom controls are disabled in audio-over-B-roll mode. A separate warning explains when global host-face zoom is disabled. |
| **Audio** | BGM mode (`auto`, none, selected), selected local track, and per-variant SFX. Controls remain stored but are disabled when the corresponding global feature flag is off. The preview is silent and does not validate audio. |
| **Dynamic Text** | Intensity (`off`, `minimal`, `balanced`, `high_energy`); preview-only information product; enabled role summary; Ingredients, Benefits, Usage, and CTA cards. Each role has enablement, fonts, size, animation, and 1-6 second duration. CTA uses a single text font; other roles have heading/body fonts. |
| **Advanced** | Letterbox enablement; independent top/bottom bars from 0-40%; automatic top-bar hook; hook font/color/size (24-160 px); X/Y position (0-100%). First enabling bars initializes zero-height bars to 20% each. |
| **Assets & Diagnostics** | Supporting-asset readiness, backend warnings, product-information health/rescan, eligible facts/conflicts/unassigned items/source warnings, fixed preview/B-roll/BGM/font/style inventories, and global feature overrides. Diagnostics are no longer placed above the everyday creative workflow. |

## Profile and Variant Model

`variation_profile.py` currently normalizes profiles to schema version 12. The durable profile contains:

- `schema_version`, `revision`, `variant_count`, `updated_at`, optional name, and ordered `variants`.
- Per-variant identity, hook/visual mode, B-roll and before/after choices.
- Text style, subtitle/headline/caption fonts and colors, subtitle placement/size/highlight/motion fields, and style-controlled stroke/shadow/rotation values.
- Grade, BGM/SFX, zoom, subtitles, mirror, and letterbox/top-hook fields.
- Dynamic-text intensity, ordered roles, and role-specific typography/animation/duration.

The backend clamps and validates values again. Frontend normalization supplies defaults for fields added by newer schemas. Increasing the count copies the preceding variant as a starting point; decreasing the count hides/removes excess visible variants and clamps the selection. Changing visual mode to `broll_audio` forces `random_broll_enabled=false`.

Durable locations:

- Active profile: `working/variation_profile.json`.
- Presets: `working/variation_presets/<slug>.json`.
- Generated previews: `working/variation_previews/`.
- Product fact index: `working/product_information_index.json`.
- Fixed host preview source: `assets/variation_preview/raw_cut_preview.mp4`.

The current backend limits are one to six variants.

## Save, Refresh, Conflicts, and Presets

The frontend keeps three relevant copies: the current server profile, a baseline accepted from the server, and the editable draft.

- If polling/SSE retrieves a newer revision while the draft is clean, the new server profile is accepted.
- If the draft is dirty, it is preserved and the page shows a revision-conflict warning.
- Apply sends the full draft plus `expected_revision` to `PUT /api/variations`.
- A stale revision returns `409`; server data is not loaded over the local draft.
- Dirty drafts register a `beforeunload` guard.
- Refresh refetches server data but does not silently overwrite a dirty draft when the revision differs.

Preset save writes the current draft through `POST /api/variations/presets`. Loading a preset retrieves it into the draft only; the active profile does not change until Apply. If the current draft is dirty, the UI confirms before replacing it. Preset names normalize to safe file identifiers, so an identical normalized name can replace the same preset file.

## Preview Behavior

Before rendering, the panel is explicitly labeled **Approximation** and uses:

- the fixed host preview clip for host mode; or
- a preview-only product B-roll sample for audio-over-B-roll mode.

Approximation reflects mirror, grade class, letterbox bars/top-hook marker, before/after placeholder, subtitle position/size/colors/highlight, and relevant state labels. Preview-only B-roll and information product selections are local UI state and are not saved in the profile.

**Render 6-second preview** posts the whole draft, selected variant index, and selected information product to `/api/variations/previews`. The backend creates/reuses an MP4 keyed by profile revision, preview-render version, variant index, and content revision. Changing any profile/selection in the request signature invalidates the displayed rendered result and prevents a stale asynchronous response from replacing a newer preview state.

The rendered preview validates supported typography, color grade, mirroring, letterbox, subtitle, and dynamic-text behavior against the silent fixed source. It does **not** validate audio, product zoom, actual product-B-roll insertion, real Before/After imagery, or transitional-hook output. Those remain production-render behaviors.

Rendered preview and fallback videos are muted, autoplaying, looping, and inline. Missing/failed media has explicit placeholder/error state. Readiness rows summarize:

- indexed product documents and eligible facts/warnings;
- fixed host preview existence;
- total B-roll clips and products missing a playable preview sample.

## Product Information and Dynamic Text

`product_information.py` recursively scans searchable `.pdf` and `.docx` files beneath `assets/information/`. It fingerprints sources, extracts PDF text or DOCX paragraphs/tables, optionally asks LM Studio to classify only source-supported text, and falls back to rules when needed.

Eligible facts retain product, role, exact text, source file, PDF/DOCX locator, extraction method, and conflict state. Ambiguous/unassigned facts and conflicting product/numeric claims are withheld and exposed in diagnostics. Scanned image-only PDFs are not OCR processed.

`dynamic_text.py` selects/schedules eligible Ingredients, Benefits, Usage, and CTA items using profile intensity/roles, speech/topic evidence, timing constraints, silence/speed remapping, typography/layout, and compliance results. High-risk/unavailable compliance can block the dynamic plan. Rendered manifests retain public evidence metadata.

## Dependencies and Global Feature Flags

`GET /api/variations` returns discovered fonts, text styles, BGM tracks, hook/visual/subtitle/dynamic/grade/zoom choices, presets, preview source, product B-roll inventory, limits, and feature flags.

Global flags currently represent SFX, BGM, Before/After, B-roll intro hooks, transitional hooks, and host-face zoom. The UI preserves a variant's saved choice when a global feature is unavailable but disables or warns on the relevant control. Global host-face zoom is deliberately separate from per-variant product zoom.

Audio-over-B-roll dependency rules disable relevant B-roll insertion, product zoom, and zoom intensity. Disabling subtitles disables subtitle-specific placement/size/font controls. Dynamic role controls are disabled when intensity is off. Letterbox and top-hook details depend on their parent toggles.

## API and Security

Relevant routes:

- `GET /api/variations`.
- `PUT /api/variations`.
- `POST /api/variations/previews`.
- `POST /api/variations/presets`.
- `GET /api/variations/presets/{preset_id}`.
- `GET /api/product-information`.
- `POST /api/product-information/rescan`.
- `GET /api/artifacts?path=...` for approved preview media.

All writes and artifact/sensitive reads require the Clipper Bearer token. Preset IDs are normalized to safe identifiers, profile values are strictly normalized, and artifact paths remain containment-checked.

The variation profile and preset store are file-backed, not catalog-backed. The SQLite catalog is not the source of truth for this page.

## Implementation Map

| File | Responsibility |
| --- | --- |
| `new_app/src/App.tsx` | `VariationsPage` queries, baseline/draft state, save/preset/preview/rescan handlers, conflict protection |
| `new_app/src/variants/VariantCommandBar.tsx` | page identity, status/revision, refresh, presets, apply |
| `new_app/src/variants/VariantNavigator.tsx` | count and selected-variant cards |
| `new_app/src/variants/VariantWorkspace.tsx` | responsive three-region layout and jump-to-preview |
| `new_app/src/variants/VariantEditorTabs.tsx` | seven tabs and keyboard navigation |
| `new_app/src/variants/tabs/*.tsx` | grouped creative/technical controls |
| `new_app/src/variants/VariantPreviewPanel.tsx` | approximation, render state, limitations, readiness |
| `new_app/src/variants/diagnostics/*.tsx` | source/asset/global diagnostics |
| `new_app/src/variants/variantModel.ts` | pure profile comparison, resize, patch, summary, dependencies, preview signature |
| `new_app/src/variants/variantTypes.ts` | tab/editor/feedback contracts |
| `new_app/src/variants/variants.css` | page-specific responsive layout/styles |
| `new_app/src/api.ts` | frontend API/profile/product/preview types |
| `variation_profile.py` | schema/defaults/normalization/persistence/options/presets/preview renderer |
| `variation_engine.py` | converts the active profile into production variant configurations |
| `product_information.py` | document indexing, evidence, conflicts, diagnostics |
| `dynamic_text.py` | evidence-aware selection, compliance, schedule, typography/layout |
| `product_broll.py` | product B-roll discovery and preview references |
| `ffmpeg_editor.py` | production consumer of visual/text/audio profile fields |
| `clipper_app/web_api.py` | routes and request validation |

## Test Coverage

Frontend coverage includes:

- `variantModel.test.ts` for pure model rules.
- `variantsPhase2.test.tsx` through `variantsPhase6.test.tsx` for command bar/presets, tabs/dependencies, preview, diagnostics, and final layout/accessibility.
- `variationsDynamicText.test.tsx` for dynamic role editing.
- `variationsSafety.test.tsx` for conflict, dirty-draft, preset replacement, stale preview, and related safety behavior.

Backend/production coverage includes `test_variation_profile.py`, `test_variation_engine.py`, `test_dynamic_text.py`, `test_product_information.py`, `test_product_broll.py`, `test_ffmpeg_editor.py`, `test_read_api.py`, and render-resume/compliance tests.

On 2026-08-10 all 82 frontend tests and all 500 Python tests passed, and the production frontend build succeeded. These tests do not replace a real FFmpeg/profile smoke with production assets/audio/models.

## Current Known Boundaries

- Preview rendering is intentionally partial and silent; production-only effects require a real render.
- There is no preset delete/rename/duplicate action and no overwrite-specific confirmation.
- There is no variant reorder/duplicate button; count growth copies the preceding variant.
- The page does not review or regenerate existing clips.
- Product-information OCR is not implemented.
- Profile/presets are local files and do not have a multi-user merge model beyond revision conflict detection on the active profile.
- A running pipeline is not mutated when the active profile changes; only future variant expansion reads the new profile.
