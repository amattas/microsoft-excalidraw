# Changelog

## 1.0.0 — 2026-07-18

First stable release.

### Library
- 845 Microsoft product icons converted to hand-drawn-style Excalidraw
  elements, organized by product family: Azure (620, in 24 service
  categories), Generic Fluent glyphs (127), Microsoft Fabric (73),
  Dynamics 365 (16), Power Platform (7), Microsoft 365 (2).
- Full library (`microsoft-icons.excalidrawlib`) plus a standalone
  sub-pack per family (`<family>-icons.excalidrawlib`).
- Every library item drops onto the canvas with its service name as a
  grouped text label beneath the icon.
- Per-icon `.excalidraw` files under `icons/`, mirroring the source tree.
- All icons normalized to a 96 px content bounding box regardless of
  source viewBox units (18–96), so packs are consistently sized relative
  to each other and to their labels.

### Converter (`src/convert.py`)
- Self-contained stdlib-only SVG converter: full path grammar
  (M/L/H/V/C/S/Q/T/A + relative variants), shape primitives including
  rounded rects, element/ancestor transforms, CSS class styles,
  linear/radial gradients (resolved to the midpoint color and opacity,
  honoring `stop-opacity`), adaptive curve flattening.
- Fill-rule holes emulated by painting hole subpaths with the composited
  color beneath (majority-sampled over the hole area); artwork fully
  inside a hole is re-emitted on top so badge-style icons survive.
- Implicitly-closed filled paths are closed explicitly so fills and dark
  outlines never gap.
- Deterministic output: stable element/item ids, seeds, and names across
  regenerations; name collisions disambiguated by size, variant, pack,
  folder, then source id.

### Tooling
- `src/render_readme.py` renders every icon to a flat SVG preview and
  generates the README gallery.
- Test suite (pytest, 90 tests) covering the conversion pipeline,
  labeling, packaging, and size normalization.
