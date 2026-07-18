#!/usr/bin/env python3
"""Render every per-icon .excalidraw file to a plain SVG preview and build the
repository README.md that displays the whole converted library.

The previews paint the same geometry Excalidraw stores (flattened polylines and
native rectangles/ellipses, holes already resolved to opaque fills by the
converter), so z-order painting reproduces each icon faithfully — minus the
hand-drawn wobble Excalidraw adds at draw time.

Run with no arguments after convert.py; writes previews/**.svg and README.md.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import convert as c

PREVIEWS = c.OUT_ROOT / "previews"
README = c.OUT_ROOT / "README.md"
GRID_COLS = 6
IMG_WIDTH = 96

SECTION_TITLES = {
    "azure": "Azure",
    "dynamics-365": "Dynamics 365",
    "fabric": "Microsoft Fabric",
    "generic": "Generic",
    "microsoft-365": "Microsoft 365",
    "power-platform": "Power Platform",
    "ai-machine-learning": "AI + Machine Learning",
    "azure-ecosystem": "Azure Ecosystem",
    "devops": "DevOps",
    "hybrid-multicloud": "Hybrid + Multicloud",
    "iot": "IoT",
    "management-governance": "Management + Governance",
}


def _title(slug: str) -> str:
    return SECTION_TITLES.get(slug, slug.replace("-", " ").title())


# --------------------------------------------------------------------------- #
# Excalidraw elements -> SVG
# --------------------------------------------------------------------------- #
def element_svg(e):
    """One SVG fragment per element; empty string for non-drawable types."""
    op = e.get("opacity", 100) / 100.0
    op_attr = f' opacity="{op:g}"' if op < 1 else ""
    fill = e.get("backgroundColor", "transparent")
    stroke = e.get("strokeColor", "none")
    if e["type"] == "line":
        pts = [(e["x"] + px, e["y"] + py) for px, py in e["points"]]
        if len(pts) < 2:
            return ""
        d = "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in pts)
        closed = len(pts) >= 3 and pts[0] == pts[-1]
        if closed:
            d += " Z"
        return (f'<path d="{d}" fill="{fill if closed else "none"}" '
                f'stroke="{stroke}" stroke-width="1" '
                f'stroke-linejoin="round"{op_attr}/>')
    if e["type"] == "rectangle":
        return (f'<rect x="{e["x"]:.2f}" y="{e["y"]:.2f}" '
                f'width="{e["width"]:.2f}" height="{e["height"]:.2f}" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="1"{op_attr}/>')
    if e["type"] == "ellipse":
        cx = e["x"] + e["width"] / 2.0
        cy = e["y"] + e["height"] / 2.0
        return (f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" '
                f'rx="{e["width"] / 2:.2f}" ry="{e["height"] / 2:.2f}" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="1"{op_attr}/>')
    return ""


def icon_svg(elements, pad=2.0):
    frags = [f for f in (element_svg(e) for e in elements) if f]
    xs0 = [e["x"] for e in elements]
    ys0 = [e["y"] for e in elements]
    xs1 = [e["x"] + e["width"] for e in elements]
    ys1 = [e["y"] + e["height"] for e in elements]
    minx, miny = min(xs0) - pad, min(ys0) - pad
    w = max(xs1) - min(xs0) + 2 * pad
    h = max(ys1) - min(ys0) + 2 * pad
    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="{minx:.2f} {miny:.2f} {w:.2f} {h:.2f}">'
            + "".join(frags) + "</svg>")


# --------------------------------------------------------------------------- #
# README assembly
# --------------------------------------------------------------------------- #
def readme_cell(rel_excalidraw: str, name: str) -> str:
    src = "previews/" + quote(str(Path(rel_excalidraw).with_suffix(".svg")))
    return (f'<td align="center"><img src="{src}" width="{IMG_WIDTH}"><br>'
            f'<sub>{name}</sub></td>')


def readme_table(entries) -> str:
    """entries: list of (rel_excalidraw, name). Fixed-column HTML grid."""
    rows = []
    for i in range(0, len(entries), GRID_COLS):
        chunk = entries[i:i + GRID_COLS]
        rows.append("<tr>" + "".join(readme_cell(rel, name) for rel, name in chunk)
                    + "</tr>")
    return "<table>" + "".join(rows) + "</table>"


def _collect():
    """Group icons: {top: {subgroup or "": [(rel, name), ...]}}."""
    groups = defaultdict(lambda: defaultdict(list))
    for p in sorted(c.ICONS_OUT.rglob("*.excalidraw")):
        rel = p.relative_to(c.ICONS_OUT)
        top = rel.parts[0]
        sub = rel.parts[1] if len(rel.parts) > 2 else ""
        groups[top][sub].append((str(rel), c.humanized_name(rel.stem)))
    return groups


def build_readme(groups) -> str:
    total = sum(len(v) for subs in groups.values() for v in subs.values())
    out = [
        "# Microsoft Icons for Excalidraw",
        "",
        f"Hand-drawn-style [Excalidraw](https://excalidraw.com) versions of "
        f"{total} Microsoft product icons — Azure, Microsoft Fabric, "
        "Dynamics 365, Power Platform, Microsoft 365, and a set of generic "
        "Fluent glyphs — converted from the official SVGs.",
        "",
        "## Usage",
        "",
        "Import [`microsoft-icons.excalidrawlib`](microsoft-icons.excalidrawlib) "
        "into Excalidraw (Library &rarr; Browse &rarr; open the file, or drag it "
        "onto the canvas). Every library item drops with its service name "
        "already labeled beneath the icon. Individual icons are also available "
        "as standalone `.excalidraw` files under [`icons/`](icons/).",
        "",
        "Don't need everything? Each product family also ships as its own "
        "smaller pack: "
        + ", ".join(f"[`{top}-icons.excalidrawlib`]({top}-icons.excalidrawlib)"
                    for top in sorted(groups)) + ".",
        "",
        "To regenerate everything from the SVG sources: "
        "`python src/convert.py && python src/render_readme.py`",
        "",
        "## Icons",
        "",
        "The previews below are rendered from the converted Excalidraw "
        "geometry (Excalidraw adds its hand-drawn wobble at draw time).",
        "",
    ]
    for top in sorted(groups):
        subs = groups[top]
        count = sum(len(v) for v in subs.values())
        out.append(f"### {_title(top)} ({count}) &middot; "
                   f"[`{top}-icons.excalidrawlib`]({top}-icons.excalidrawlib)")
        out.append("")
        for sub in sorted(subs):
            entries = subs[sub]
            summary = f"{_title(sub)} ({len(entries)})" if sub else f"{_title(top)} ({len(entries)})"
            out.append(f"<details><summary><b>{summary}</b></summary>")
            out.append("")
            out.append(readme_table(entries))
            out.append("</details>")
            out.append("")
    return "\n".join(out)


def _library_elements(rel):
    """Fallback for sandbox-unreadable icon files (e.g. *credentials* paths):
    pull the same icon's elements from the library, which is keyed by a
    deterministic id derived from the source path, and drop the name label."""
    lib = json.loads(c.LIB_OUT.read_text())
    want = c._det_uuid(str(rel.with_suffix(".svg")) + "|item")
    for item in lib["libraryItems"]:
        if item["id"] == want:
            return [e for e in item["elements"] if e.get("type") != "text"]
    return []


def main():
    groups = _collect()
    n = 0
    for p in sorted(c.ICONS_OUT.rglob("*.excalidraw")):
        rel = p.relative_to(c.ICONS_OUT)
        try:
            elements = json.loads(p.read_text())["elements"]
        except OSError:
            elements = _library_elements(rel)
        if not elements:
            continue
        out = PREVIEWS / rel.with_suffix(".svg")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(icon_svg(elements))
        n += 1

    # prune previews with no surviving source icon
    valid = {PREVIEWS / p.relative_to(c.ICONS_OUT).with_suffix(".svg")
             for p in c.ICONS_OUT.rglob("*.excalidraw")}
    pruned = 0
    for p in PREVIEWS.rglob("*.svg"):
        if p not in valid:
            try:
                p.unlink()
                pruned += 1
            except OSError:
                pass
    def _is_dir(d):
        try:
            return d.is_dir()
        except OSError:
            return False
    for d in sorted((d for d in PREVIEWS.rglob("*") if _is_dir(d)), reverse=True):
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    README.write_text(build_readme(groups))
    print(f"previews written: {n}")
    print(f"previews pruned: {pruned}")
    print(f"README sections: {len(groups)}")


if __name__ == "__main__":
    main()
