---
name: PROYA VOD Processing Dashboard
description: Calm operations UI for turning long PROYA livestream VODs into short commerce clips.
colors:
  operations-bg: "#08111f"
  app-gradient-top: "#07101c"
  app-gradient-bottom: "#0a1220"
  video-black: "#020617"
  panel: "#101a29"
  panel-soft: "#111e30"
  panel-rail: "#0b1422"
  text: "#edf3ff"
  text-strong: "#f8fafc"
  muted: "#94a3b8"
  table-text: "#e2e8f0"
  primary-blue: "#3b82f6"
  primary-blue-strong: "#2563eb"
  primary-blue-soft: "#60a5fa"
  success-green: "#22c55e"
  success-green-strong: "#16a34a"
  warning-yellow: "#fbbf24"
  warning-orange: "#f59e0b"
  danger-red: "#ef4444"
  danger-red-strong: "#dc2626"
  review-violet: "#8b5cf6"
  review-violet-strong: "#7c3aed"
typography:
  display:
    fontFamily: "Source Sans Pro, system-ui, sans-serif"
    fontSize: "1.9rem"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "normal"
  headline:
    fontFamily: "Source Sans Pro, system-ui, sans-serif"
    fontSize: "1.3rem"
    fontWeight: 700
    lineHeight: 1.18
    letterSpacing: "normal"
  title:
    fontFamily: "Source Sans Pro, system-ui, sans-serif"
    fontSize: "1.05rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "normal"
  body:
    fontFamily: "Source Sans Pro, system-ui, sans-serif"
    fontSize: "0.92rem"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  label:
    fontFamily: "Source Sans Pro, system-ui, sans-serif"
    fontSize: "0.76rem"
    fontWeight: 800
    lineHeight: 1.1
    letterSpacing: "0.08em"
rounded:
  xs: "4px"
  sm: "6px"
  md: "8px"
  lg: "10px"
  xl: "12px"
  panel: "16px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.primary-blue}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    padding: "10px 16px"
    height: "40px"
  button-secondary:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    padding: "10px 16px"
    height: "40px"
  status-badge:
    backgroundColor: "{colors.panel-soft}"
    textColor: "{colors.text}"
    typography: "{typography.label}"
    rounded: "{rounded.lg}"
    padding: "5px 10px"
  panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.panel}"
    padding: "16px"
  nav-item-active:
    backgroundColor: "{colors.primary-blue-strong}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.xl}"
    padding: "14px 16px"
  input-field:
    backgroundColor: "{colors.panel-rail}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    padding: "10px 12px"
    height: "40px"
  progress-track:
    backgroundColor: "{colors.panel-soft}"
    rounded: "{rounded.pill}"
    height: "10px"
---

# Design System: PROYA VOD Processing Dashboard

## 1. Overview

**Creative North Star: "Production Control Room"**

This system is a calm, status-first operations interface for office operators and editors running a high-throughput local video clipping pipeline. The physical scene is a remote PC or office workstation during long PROYA livestream processing runs: the operator needs to see queue health, failures, reviews, and ready assets without feeling pushed into a noisy debug console.

The dashboard is a product surface, so design serves the task. Familiar Streamlit controls are refined into a dark, legible, control-room vocabulary: dense tables, compact badges, restrained panels, and direct queue controls. The mood is composed, trustworthy, and efficient.

It explicitly rejects the anti-references from PRODUCT.md: flashy SaaS marketing patterns, toy-like controls, excessive decoration, decorative gradients, hero-scale type inside operational screens, dense technical log walls as the primary experience, and status colors that require memorization.

**Key Characteristics:**

- Calm dark operational surfaces with clear tonal steps.
- Status language that combines text, icon or shape, color, and hierarchy.
- Compact tables for desktop review and record cards for mobile.
- Restrained borders, limited shadows, and predictable controls.
- Semantic color reserved for action, health, waiting, failure, and review.

## 2. Colors

The palette is a restrained dark operations palette: blue is the primary command color, green/yellow/red/violet are semantic signals, and cool slate neutrals carry almost every surface.

### Primary

- **Command Blue** (`primary-blue`): Used for primary actions, active tabs, progress fills, selected navigation, and "processing" state. Blue must tell the operator that something can be acted on, is selected, or is moving.
- **Command Blue Strong** (`primary-blue-strong`): Used for primary button depth and active navigation interiors.
- **Command Blue Soft** (`primary-blue-soft`): Used for hover borders, chart points, and focus visibility.

### Secondary

- **Ready Green** (`success-green`): Used for healthy queue state, completed status, strong clips, passed compliance, and positive readiness.
- **Attention Yellow** (`warning-yellow`): Used for waiting, partial readiness, needs-review states, queue attention, and low/medium severity flags.
- **Blocker Red** (`danger-red`): Used for failed videos, blocked compliance, high severity flags, and critical queue state.

### Tertiary

- **Review Violet** (`review-violet`): Used for review-specific selection, score detail emphasis, and variant comparison context. It should never replace blue as the global primary action color.

### Neutral

- **Operations Background** (`operations-bg`): The base app field behind panels.
- **Panel Surface** (`panel`): The main grouped region surface for dashboards, cards, controls, and data regions.
- **Soft Panel Surface** (`panel-soft`): The slightly lifted neutral for nested stats, form controls, and progress tracks.
- **Rail Surface** (`panel-rail`): The darker rail and field surface for navigation and inputs.
- **Operator Text** (`text`): Default high-contrast text.
- **Operator Text Strong** (`text-strong`): Titles, numeric KPIs, and detail panel emphasis.
- **Muted Slate** (`muted`): Labels, metadata, axes, secondary copy, and table headers.
- **Table Text** (`table-text`): Dense row content where pure emphasis would be too loud.

### Named Rules

**The Status Redundancy Rule.** Color never carries status alone. Every important state must include visible text plus icon, dot, progress shape, or badge shape.

**The Accent Budget Rule.** Blue, green, yellow, red, and violet are operational signals, not decoration. If a color does not explain state, selection, risk, or action, remove it.

**The Tinted Neutral Rule.** Avoid pure black and pure white in normal UI. Use the slate text and surface scale so long sessions remain readable.

## 3. Typography

**Display Font:** Source Sans Pro with system-ui fallback.
**Body Font:** Source Sans Pro with system-ui fallback.
**Label/Mono Font:** Source Sans Pro with tabular numerals where numbers must align.

**Character:** The type system is compact and native-feeling. It uses weight, size, and tabular numbers for operational hierarchy rather than decorative font choices.

### Hierarchy

- **Display** (700, `1.9rem`, line-height `1.1`): App title and rare identity-level headings only.
- **Headline** (700, `1.3rem`, line-height `1.18`): Page titles such as Overview, Scores, Compliance, Modules, Focus Debug, and Queues.
- **Title** (700, `1.05rem`, line-height `1.2`): Panel titles, KPI labels, score detail titles, and module card names.
- **Body** (400-600, `0.88rem` to `0.95rem`, line-height `1.35` to `1.55`): Tables, queue rows, explanatory captions, transcript blocks, and form labels. Long prose should stay under 65-75ch, while dense tables may run wider.
- **Label** (700-800, `0.68rem` to `0.82rem`, letter-spacing `0.08em` to `0.11em` for uppercase labels): Section kickers, compact badges, table headers, mobile stat labels, and status chips.
- **Numbers** (700-800, `1.35rem` to `2rem`, line-height `1`): KPIs, score totals, progress values, and counts. Use tabular numerals in badges, score rows, and dimension values.

### Named Rules

**The Scan First Rule.** Numbers, status labels, and next actions get typographic priority over descriptive prose.

**The No Decorative Type Rule.** Do not introduce expressive display fonts, gradient text, or oversized hero typography inside this product UI.

## 4. Elevation

The system uses tonal layering first, borders second, and shadows last. Most surfaces are separated by cool slate backgrounds, low-alpha borders, and compact spacing. Shadows appear only on major grouped regions, selected navigation, and tiny status glows that are paired with text.

### Shadow Vocabulary

- **Panel Ambient** (`box-shadow: 0 18px 48px rgba(0, 0, 0, 0.18)`): Major Streamlit bordered regions only.
- **Active Navigation Lift** (`box-shadow: 0 10px 24px rgba(37, 99, 235, 0.22)`): Selected navigation or primary active context.
- **Status Glow** (`box-shadow: 0 0 10px rgba(34, 197, 94, 0.5)`): Small live status dots only, always paired with a readable label.
- **Inset Track Line** (`box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.05)`): Circular stage indicators and contained progress surfaces.

### Named Rules

**The Tonal Layer Rule.** Reach for a darker or lighter surface step and a border before adding a shadow.

**The Shadow Restraint Rule.** If every panel casts a shadow, no panel is important. Shadows are reserved for hierarchy and state.

## 5. Components

### Buttons

- **Shape:** Gently squared product controls with a consistent radius (`10px`) and minimum height (`40px`, `44px` on mobile).
- **Primary:** Command Blue gradient treatment from `primary-blue` into `primary-blue-strong`, text in `text`, border using soft blue opacity, and compact padding (`10px 16px`).
- **Hover / Focus:** Hover increases blue border clarity. Focus must be visibly outlined with a high-contrast blue ring. Motion is limited to quick state feedback.
- **Secondary / Ghost:** Secondary buttons use `panel` and `panel-rail` tonal fills with slate borders. They remain quiet until hover or active selection.

### Chips

- **Style:** Badges are rounded (`10px` for status, `999px` for score and module chips), compact, and text-forward.
- **State:** Processing, Completed, Waiting, Failed, Needs Attention, Strong, Review, Blocked, Ready, Partial, and Empty states must show text plus color plus shape. Icons or dots are required when the badge is used as a primary status cue.
- **Severity:** High severity may use filled red, medium yellow/orange, low blue, and none/clear green or slate.

### Cards / Containers

- **Corner Style:** Major panels use `16px`; cards and mobile records use `8px` to `10px`; nav items use `12px`.
- **Background:** Panels use layered cool slate gradients. Mobile cards and table shells use darker translucent slate fills.
- **Shadow Strategy:** Major grouped panels may use Panel Ambient. Small stat cards should rely on border and fill.
- **Border:** Low-alpha slate borders are standard. Avoid colored side stripes. Existing score KPI side accents should not be copied into new components.
- **Internal Padding:** Compact operational padding: `12px` to `16px` for panels, `8px` to `12px` for dense review cards.

### Inputs / Fields

- **Style:** Streamlit text inputs, select boxes, and base inputs use dark translucent slate backgrounds with low-alpha slate borders and `10px` radius.
- **Focus:** Focus must be visually obvious and keyboard-friendly, using blue outline or border escalation.
- **Error / Disabled:** Error states use red text/background/border in the same badge vocabulary. Disabled states stay muted, not invisible.

### Navigation

Desktop navigation is a left rail with icon containers, clear labels, and an active blue filled state. Mobile navigation collapses to a compact section selector above the header. Active state must include fill and label, not color alone.

### Tables And Review Rows

Dense tables are a primary interface pattern. Desktop tables use sticky-feeling review panels, compact headers, clipped two-line cells, and row hover tint. Mobile replaces dense tables with record cards and inline details. Selected rows use background tint and clear selected state, but new patterns must avoid thick colored side stripes.

### Progress And Charts

Progress bars use a slate track with a blue, green, or yellow fill and a nearby numeric label. Altair charts use blue marks, muted axis labels, visible tooltips, and grid lines that stay below the data.

### Video Preview And Transcript Detail

Video preview areas use `video-black` and object-fit containment. Transcript boxes use dark panels, readable body size, and inline highlights for price or compliance issues.

## 6. Do's and Don'ts

### Do:

- **Do** surface the next operational decision first: running, waiting, needs review, failed, blocked, ready, or completed.
- **Do** use status text, icon or dot, color, and badge shape together every time status is a primary cue.
- **Do** keep the palette restrained: cool slate neutrals carry the surface, semantic colors carry meaning.
- **Do** keep desktop density useful with compact tables, short labels, and stable spacing.
- **Do** swap dense desktop tables for mobile record cards below `760px`.
- **Do** keep controls predictable: standard buttons, tabs, select boxes, inputs, and pagination.
- **Do** provide visible focus states for buttons, filters, tabs, dropdowns, table actions, and navigation.
- **Do** support WCAG 2.2 AA, reduced-motion preferences, color-blind-safe status indicators, readable chart labels/tooltips, and mobile touch targets around `44px`.

### Don't:

- **Don't** use flashy SaaS marketing patterns.
- **Don't** use toy-like controls.
- **Don't** use excessive decoration.
- **Don't** use decorative gradients. Functional gradients are allowed only for surfaces, progress fills, and selected controls where they preserve readability.
- **Don't** use hero-scale type inside operational screens.
- **Don't** expose dense technical log walls as the primary experience.
- **Don't** use status colors that require memorization.
- **Don't** make the dashboard look like a generic analytics template, a developer debug panel, or a playful creator tool.
- **Don't** add side-stripe borders thicker than `1px` as accents on cards, rows, callouts, or alerts. Use full borders, background tints, icons, labels, or nothing.
- **Don't** use gradient text, decorative glassmorphism, decorative motion, or display fonts in labels, buttons, tables, or data.
