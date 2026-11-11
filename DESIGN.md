---
name: PartHarvester Design System
description: High-density mission-control telemetry and multi-part repack bandwidth bonding console
colors:
  canvas: "#090d16"
  surface: "#0f172a"
  elevated: "#162032"
  hover: "#1e2c45"
  active: "#233452"
  border-default: "rgba(255, 255, 255, 0.08)"
  border-subtle: "rgba(255, 255, 255, 0.04)"
  border-accent: "rgba(2, 132, 199, 0.4)"
  text-primary: "#f8fafc"
  text-secondary: "#cbd5e1"
  text-dim: "#94a3b8"
  accent-cyan: "#0284c7"
  accent-cyan-dark: "#0369a1"
  accent-cyan-light: "#38bdf8"
  accent-cyan-tint: "rgba(2, 132, 199, 0.15)"
  accent-emerald: "#059669"
  accent-emerald-light: "#34d399"
  accent-emerald-tint: "rgba(5, 150, 105, 0.15)"
  accent-amber: "#d97706"
  accent-amber-light: "#fbbf24"
  accent-amber-tint: "rgba(217, 119, 6, 0.15)"
  accent-rose: "#e11d48"
  accent-rose-light: "#fb7185"
  accent-rose-tint: "rgba(225, 29, 72, 0.15)"
typography:
  display:
    fontFamily: "'Outfit', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  headline:
    fontFamily: "'Outfit', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "18px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "0.02em"
  body:
    fontFamily: "'Outfit', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "0em"
  caption:
    fontFamily: "'Outfit', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "11.5px"
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: "0.01em"
  tabular:
    fontFamily: "'JetBrains Mono', SFMono-Regular, Menlo, Monaco, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: "0em"
rounded:
  xs: "3px"
  sm: "4px"
  md: "6px"
  lg: "10px"
  pill: "16px"
  full: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "10px"
  lg: "16px"
  xl: "20px"
components:
  button-primary:
    backgroundColor: "{colors.accent-cyan-dark}"
    textColor: "#ffffff"
    typography: "{typography.caption}"
    rounded: "{rounded.md}"
    padding: "6px 14px"
    height: "34px"
  button-surface:
    backgroundColor: "{colors.elevated}"
    textColor: "{colors.text-primary}"
    typography: "{typography.caption}"
    rounded: "{rounded.md}"
    padding: "5px 12px"
    height: "32px"
  badge:
    backgroundColor: "{colors.elevated}"
    textColor: "{colors.text-secondary}"
    typography: "{typography.caption}"
    rounded: "{rounded.full}"
    padding: "2px 8px"
---

# PartHarvester Design System

## Overview
PartHarvester is an **Operate-mode** desktop dashboard designed for power users downloading massive multi-gigabyte game repacks with multi-adapter bandwidth bonding (Wi-Fi + 5G USB tethering + Ethernet). The interface prioritizes scanability, high data density, spatial stability, and real-time numeric telemetry without requiring vertical page scrolling on standard 1080p displays.

## Colors
The color system utilizes a calibrated dark slate canvas with four functional chromatic accents:
- **Canvas (`#090d16`)**: Deep background providing contrast without pure black clipping.
- **Surface (`#0f172a`)**: Primary container card background with subtle hairline borders (`--border-default`).
- **Elevated (`#162032`)**: Interactive sub-panels, table rows, and secondary buttons.
- **Functional Accents**:
  - **Cyan (`#0284c7` / `#0369a1`)**: Primary action, active downloads, throughput aggregation.
  - **Emerald (`#059669` / `#10b981`)**: Connection established, completed files, active network adapters.
  - **Amber (`#d97706` / `#fbbf24`)**: Hand-off notifications, unpacking archives, queued state.
  - **Rose (`#e11d48` / `#fb7185`)**: Failures, cancellations, dead link errors.

## Typography
- **Primary Typeface**: `Outfit`, with system sans fallbacks (`-apple-system`, `BlinkMacSystemFont`, `Segoe UI`).
- **Telemetry & Metrics**: Monospace numerals (`JetBrains Mono`, `Cascadia Code`, `Consolas`) with `font-variant-numeric: tabular-nums` enforced on all transfer rates, byte counters, percentages, and ETAs to ensure absolute column alignment during rapid WebSocket state updates.
- **Hierarchical Discipline**: Strict avoidance of gradient text or decorative heading kickers. High contrast ratios (exceeding WCAG AAA 7:1 for body and data cells, 5.1:1+ for buttons).

## Layout
- **Single-Screen Density**: Fixed viewport budget designed to fit 1920x1080 displays (~900px inside browser chrome) without window scrollbars.
- **2-Column Top Console**:
  - **Left (55%)**: Metrics banner, bandwidth share progress bar, and real-time adapter tiles.
  - **Right (45%)**: Batch link input textarea, destination folder path selector, auto-extraction toggle, and batch action controls.
- **Full-Width Queue (Remaining Height)**: Scrollable downloads table with fixed headers, explicit column widths, and inline action icons.

## Elevation & Depth
- **Zero Decorative Halos**: Elimination of colored radial blur glows, fuzzy drop-shadow halos, and floating decorative gradients.
- **Hairline Tonal Layering**: Depth is achieved entirely through borders (`1px solid var(--border-default)`) and tonal stepping (`#090d16` -> `#0f172a` -> `#162032`).
- **Inset Wells**: Input fields and destination path boxes use inset backgrounds (`#090d16`) with crisp internal padding.

## Shapes
- **Corner Radii**:
  - `4px` (`--radius-sm`) for badges, tags, and small inputs.
  - `6px` (`--radius-md`) for buttons, sub-cards, and network adapter tiles.
  - `10px` (`--radius-lg`) for primary dashboard containers.
  - `16px` for small toggle sliders.
  - `9999px` (`--radius-full`) for toggle pills and status chips.
- **Vector Icons**: 100% vector SVG icons (`14px` or `16px`, `stroke-width: 2`, `currentColor`). Complete ban on raw OS emojis.

## Components
- **Action Buttons**: Unified height hierarchy (`34px` for primary ingest, `32px` for secondary batch controls, `26px` for inline table actions).
- **Toggle Switch**: Accessible custom toggle switch (`36px` x `20px` track, `14px` thumb) with distinct focus ring and smooth slide animation.
- **Table Data Grid**: Compact table layout with `py-1` density, sticky header, hover highlights, and fixed-width action columns.

## Do's and Don'ts
### Do:
- Always use `tabular-nums` on transfer speeds, percentages, and byte sizes.
- Use vector SVG icons with `currentColor` stroke for consistent rendering across platforms.
- Maintain a single-viewport layout without vertical scrolling on 1080p monitors.
- Provide immediate visual feedback on pause, resume, handoff, and retry actions.

### Don't:
- Do not apply `-webkit-text-fill-color: transparent` gradient text to headings.
- Do not add decorative colored drop-shadow glows or background blur halos.
- Do not use OS emojis in place of functional UI icons.
- Do not introduce horizontal or vertical page scrollbars.
