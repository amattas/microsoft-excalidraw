"""Tests for the SVG -> Excalidraw converter.

Parameterized and minimal: each test asserts a distinct behavior of the
conversion pipeline. Icon-level tests exercise real Azure source files.
"""
import math
from pathlib import Path

import pytest

import convert as c

SRC = c.SRC_ROOT


# --------------------------------------------------------------------------- #
# Color / gradient resolution
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    ("#fff", "#ffffff"),
    ("#1E1E1E", "#1e1e1e"),
    ("white", "#ffffff"),
    ("rgb(255,0,0)", "#ff0000"),
    ("none", None),
    ("transparent", None),
    (None, None),
])
def test_parse_color(value, expected):
    assert c.parse_color(value) == expected


@pytest.mark.parametrize("stops,at,expected", [
    ([(0.0, "#000000"), (1.0, "#ffffff")], 0.5, "#808080"),
    ([(0.0, "#ff0000"), (1.0, "#0000ff")], 0.5, "#800080"),
    ([(0.22, "#32d4f5"), (1.0, "#198ab3")], 0.0, "#32d4f5"),  # clamps low
    ([(0.0, "#123456"), (1.0, "#123456")], 0.5, "#123456"),
])
def test_interpolate_gradient(stops, at, expected):
    assert c.interpolate_gradient(stops, at) == expected


def test_resolve_url_gradient_midpoint():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18"><defs>'
           '<linearGradient id="g"><stop offset="0" stop-color="#000000"/>'
           '<stop offset="1" stop-color="#ffffff"/></linearGradient></defs></svg>')
    doc = c.SvgDoc(svg)
    assert doc.resolve_fill("url(#g)") == "#808080"


# --------------------------------------------------------------------------- #
# Transforms
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("transform,pt,expected", [
    ("translate(3,4)", (1, 1), (4, 5)),
    ("scale(2)", (2, 3), (4, 6)),
    ("scale(2,3)", (2, 3), (4, 9)),
    ("matrix(1,0,0,1,5,6)", (0, 0), (5, 6)),
    ("rotate(90)", (1, 0), (0, 1)),
    ("rotate(180 5 5)", (5, 0), (5, 10)),
])
def test_parse_transform(transform, pt, expected):
    m = c.parse_transform(transform)
    got = c.apply_mat(m, *pt)
    assert got[0] == pytest.approx(expected[0], abs=1e-9)
    assert got[1] == pytest.approx(expected[1], abs=1e-9)


# --------------------------------------------------------------------------- #
# Path grammar
# --------------------------------------------------------------------------- #
def test_path_absolute_and_close():
    subs = c.parse_path_d("M1,1 L5,1 L5,5 L1,5 Z")
    assert len(subs) == 1
    pts, closed = subs[0]
    assert closed is True
    assert pts[0] == (1.0, 1.0)
    assert pts[-1] == pts[0]  # Z closes exactly


@pytest.mark.parametrize("d,end", [
    ("M0,0 h10 v10", (10.0, 10.0)),           # H/V absolute-ish
    ("M0,0 l3,4 l3,-4", (6.0, 0.0)),          # relative lineto
    ("m2,2 l1,0", (3.0, 2.0)),                # relative moveto
    ("M0,0 C0,10 10,10 10,0", (10.0, 0.0)),   # cubic
    ("M0,0 Q5,10 10,0", (10.0, 0.0)),         # quadratic
])
def test_path_endpoints(d, end):
    pts, _ = c.parse_path_d(d)[0]
    assert pts[-1][0] == pytest.approx(end[0], abs=1e-6)
    assert pts[-1][1] == pytest.approx(end[1], abs=1e-6)


def test_path_two_subpaths():
    subs = c.parse_path_d("M0,0 L2,0 L2,2 Z M4,4 L6,4 L6,6 Z")
    assert len(subs) == 2


def test_arc_flattens_smoothly():
    # semicircle of radius 5 from (0,0) to (10,0)
    pts, _ = c.parse_path_d("M0,0 A5,5 0 0 1 10,0")[0]
    assert pts[-1][0] == pytest.approx(10.0, abs=1e-6)
    assert pts[-1][1] == pytest.approx(0.0, abs=1e-6)
    assert len(pts) > 8  # curve, not a straight segment
    # apex bulges a full radius away from the chord
    assert max(abs(p[1]) for p in pts) == pytest.approx(5.0, abs=0.1)


def test_cubic_adaptive_point_count():
    straightish = c.parse_path_d("M0,0 C1,0 2,0 3,0")[0][0]
    curvy = c.parse_path_d("M0,0 C0,10 10,10 10,0")[0][0]
    assert len(straightish) < len(curvy)


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def test_point_in_poly_and_interior():
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert c.point_in_poly((5, 5), square)
    assert not c.point_in_poly((15, 5), square)
    ip = c.interior_point(square)
    assert c.point_in_poly(ip, square)


# --------------------------------------------------------------------------- #
# Outline rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("w,h,expect_dark", [
    (20, 20, True),     # large -> dark outline
    (5, 5, False),      # too small
    (40, 6, False),     # too thin (min dim < 8)
    (8, 40, True),      # exactly meets min dim, area ok
])
def test_outline_rule(w, h, expect_dark):
    stroke = c._outline_stroke(w, h, "#abcdef")
    assert (stroke == c.OUTLINE_COLOR) == expect_dark
    if not expect_dark:
        assert stroke == "#abcdef"


# --------------------------------------------------------------------------- #
# Holes / counters
# --------------------------------------------------------------------------- #
def test_hole_takes_underlying_color():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
           '<rect x="0" y="0" width="18" height="18" fill="#0000ff"/>'
           '<path d="M2,2 H16 V16 H2 Z M6,6 H12 V12 H6 Z" fill="#ff0000"/></svg>')
    els, cnt = c.convert_text(svg, "hole-test")
    assert cnt == 3 and len(els) == 3
    background, outer, hole = els
    assert background["backgroundColor"] == "#0000ff"
    assert outer["backgroundColor"] == "#ff0000"
    assert hole["backgroundColor"] == "#0000ff"  # punched through to blue


def test_hole_detected_when_inner_subpath_comes_first():
    """Fabric border rings draw the inner contour before the outer one."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
           '<path fill="#888" fill-rule="evenodd" '
           'd="M5,5 L35,5 L35,35 L5,35 M2,2 L38,2 L38,38 L2,38 Z"/></svg>')  # inner loop implicitly closed
    els, _ = c.convert_text(svg, "ring.svg")
    assert len(els) == 2
    assert els[0]["backgroundColor"] == "#888888"      # outer frame keeps the fill
    assert els[1]["backgroundColor"] == "#ffffff"      # inner hole -> underlying/white
    assert els[1]["width"] < els[0]["width"]


def test_hole_composites_translucent_layers():
    """A 20%-opacity sheen over a light tile must blend, not replace."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
           '<rect x="0" y="0" width="40" height="40" fill="#f0f0f0"/>'
           '<rect x="0" y="0" width="40" height="40" fill="#000000" fill-opacity=".2"/>'
           '<path fill="#888" fill-rule="evenodd" '
           'd="M5,5 L35,5 L35,35 L5,35 M2,2 L38,2 L38,38 L2,38 Z"/></svg>')
    els, _ = c.convert_text(svg, "sheen.svg")
    hole = els[-1]
    # 0.2*#000 + 0.8*#f0f0f0 = #c0c0c0
    assert hole["backgroundColor"] == "#c0c0c0"
    assert hole["opacity"] == 100


def test_unclosed_filled_path_is_closed():
    """SVG fills paths via implicit closure even without Z (e.g. the Web Jobs
    disk); the emitted polygon must close so fills and outlines don't gap."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
           '<path d="M1,1 L17,1 L9,17" fill="#ff0000"/></svg>')
    els, _ = c.convert_text(svg, "open-fill")
    pts = els[0]["points"]
    assert pts[0] == pts[-1]


def test_hole_reemits_covered_interior_art():
    """Badge icons draw interior art first and a ring last; the ring's hole
    fill must not flatten the art — it takes the color beneath the art and the
    art is re-emitted on top of the hole."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
           '<rect x="7" y="7" width="4" height="4" fill="#ff0000"/>'
           '<path d="M2,2 H16 V16 H2 Z M4,4 H14 V14 H4 Z" fill="#0000ff"/></svg>')
    els, cnt = c.convert_text(svg, "ring-over-art")
    assert cnt == 4  # 3 subpaths + 1 re-emitted copy of the red square
    assert len(els) == 4
    art, outer, hole, dup = els
    assert hole["backgroundColor"] == "#ffffff"   # beneath the art, not red
    assert dup["backgroundColor"] == "#ff0000"    # art restored on top
    assert dup["type"] == art["type"] == "rectangle"
    assert dup["id"] != art["id"]                 # distinct element identity


def test_root_fill_none_inherited():
    """Fluent/Fabric icons set fill="none" on the root svg; stroke-only paths
    must not fall back to filled black shapes."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" fill="none">'
           '<path d="M5,5 H35 V35 H5 Z" fill="#ffffff"/>'
           '<path stroke="#888" d="M5,5 H35 V35 H5 Z"/></svg>')
    els, cnt = c.convert_text(svg, "root-fill-none")
    assert cnt == 1 and len(els) == 1        # stroke-only path dropped
    assert els[0]["backgroundColor"] == "#ffffff"


def test_hole_fill_majority_ignores_partial_crossing():
    """Art crossing partially through a hole (orbit rings) must not tint the
    whole hole; the fill takes the majority color of the hole area."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
           '<rect x="8" y="0" width="2" height="18" fill="#ff0000"/>'
           '<path d="M4,4 H14 V14 H4 Z M6,6 H12 V12 H6 Z" fill="#0000ff"/></svg>')
    els, _ = c.convert_text(svg, "ring-crossing")
    hole = els[-1]
    assert hole["backgroundColor"] == "#ffffff"  # not #ff0000 from the centroid


def test_hole_fallback_white():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
           '<path d="M2,2 H16 V16 H2 Z M6,6 H12 V12 H6 Z" fill="#ff0000"/></svg>')
    els, _ = c.convert_text(svg, "hole-white")
    assert els[1]["backgroundColor"] == "#ffffff"


def test_css_class_fill():
    """Azure icons style shapes via <style> classes; class fills must resolve
    instead of defaulting to black."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
           '<defs><style>.cls-1{fill:#0078d4;}</style></defs>'
           '<rect class="cls-1" x="2" y="2" width="10" height="10"/></svg>')
    els, _ = c.convert_text(svg, "css-class")
    assert els[0]["backgroundColor"] == "#0078d4"


@pytest.mark.parametrize("stops,expected_opacity", [
    # uniform 50% stop-opacity -> element at 50%
    ('<stop offset="0" stop-color="#112233" stop-opacity=".5"/>'
     '<stop offset="1" stop-color="#112233" stop-opacity=".5"/>', 50),
    # fabric shading overlay: colorless stops fading .2 -> 0 (mid ~9%)
    ('<stop stop-opacity=".2"/><stop offset="1" stop-opacity="0"/>', 10),
])
def test_gradient_stop_opacity(stops, expected_opacity):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18"><defs>'
           f'<linearGradient id="g">{stops}</linearGradient></defs>'
           '<rect x="2" y="2" width="10" height="10" fill="url(#g)"/></svg>')
    els, _ = c.convert_text(svg, "grad-alpha")
    assert els[0]["opacity"] == expected_opacity


def test_fully_rounded_rect_is_ellipse():
    """rx == w/2 turns a rect into a circle (Language Understanding disk)."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
           '<rect width="18" height="18" rx="9" fill="#0078d4"/></svg>')
    e = c.convert_text(svg, "round-rect-full")[0][0]
    assert e["type"] == "ellipse"
    assert e["width"] == pytest.approx(18 * c.OUTSCALE)


def test_partially_rounded_rect_keeps_corner_radius():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
           '<rect x="1" y="1" width="16" height="10" rx="2" fill="#0078d4"/></svg>')
    e = c.convert_text(svg, "round-rect-part")[0][0]
    assert e["type"] == "line"                     # flattened rounded outline
    assert len(e["points"]) > 12                   # corner arcs sampled
    assert e["width"] == pytest.approx(16 * c.OUTSCALE, abs=0.1)
    # no point may sit in the sharp corner outside the rounding
    assert not any(px < 0.5 and py < 0.5 for px, py in e["points"])


# --------------------------------------------------------------------------- #
# Native primitive scaling
# --------------------------------------------------------------------------- #
def test_rect_becomes_scaled_rectangle():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
           '<rect x="1" y="2" width="4" height="6" fill="#123456"/></svg>')
    els, _ = c.convert_text(svg, "rect-test")
    e = els[0]
    assert e["type"] == "rectangle"
    assert e["x"] == pytest.approx(1 * c.OUTSCALE)
    assert e["width"] == pytest.approx(4 * c.OUTSCALE)
    assert e["height"] == pytest.approx(6 * c.OUTSCALE)


def test_circle_becomes_ellipse():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
           '<circle cx="9" cy="9" r="4" fill="#123456"/></svg>')
    e = c.convert_text(svg, "circle-test")[0][0]
    assert e["type"] == "ellipse"
    assert e["width"] == pytest.approx(8 * c.OUTSCALE)


# --------------------------------------------------------------------------- #
# Real-icon behavior (regression against the known defects)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rel,expected", [
    ("azure/management-governance/Alerts.svg", 3),
    ("azure/iot/Event Hubs.svg", 16),
    ("azure/identity/Users.svg", 3),
])
def test_icon_element_counts(rel, expected):
    els, cnt = c.convert_text((SRC / rel).read_text(), rel)
    assert len(els) == expected
    assert cnt == expected  # no silent drops


def test_users_silhouette_is_smooth():
    """The flattening defect turned a smooth outline into 12 points."""
    rel = "azure/identity/Users.svg"
    els, _ = c.convert_text((SRC / rel).read_text(), rel)
    biggest = max((e for e in els if e["type"] == "line"),
                  key=lambda e: len(e["points"]))
    assert len(biggest["points"]) > 30


@pytest.mark.parametrize("idx,expect_dark", [
    (0, True),    # large single-subpath silhouette keeps the per-element rule
    # The last four elements are the four glyph subpaths of one compound source
    # path. Two siblings fall below the size threshold, so the outline decision
    # is consistent per source path: every glyph gets stroke == its own fill,
    # even the "S"/"D" glyphs that would individually qualify for a dark outline.
    (-4, False),
    (-3, False),
    (-2, False),
    (-1, False),
])
def test_time_series_outline_consistent_per_source_path(idx, expect_dark):
    rel = "azure/iot/Time Series Data Sets.svg"
    els, _ = c.convert_text((SRC / rel).read_text(), rel)
    e = els[idx]
    if expect_dark:
        assert e["strokeColor"] == c.OUTLINE_COLOR
    else:
        assert e["strokeColor"] == e["backgroundColor"]


@pytest.mark.parametrize("rel", [
    "azure/management-governance/Alerts.svg",
    "azure/iot/Event Hubs.svg",
    "azure/identity/Users.svg",
    "azure/iot/Time Series Data Sets.svg",
])
def test_no_silent_drops_invariant(rel):
    """Emitted element count must equal the independently counted subpaths
    plus any re-emitted hole-covered copies."""
    doc = c.SvgDoc((SRC / rel).read_text())
    els, _, dups = c.build_elements(doc, rel)
    assert len(els) == c.count_drawable_subpaths(doc) + dups


def test_element_schema_fields():
    rel = "azure/identity/Users.svg"
    els, _ = c.convert_text((SRC / rel).read_text(), rel)
    required = {"id", "seed", "versionNonce", "version", "updated", "isDeleted",
                "angle", "groupIds", "boundElements", "link", "locked", "frameId",
                "strokeColor", "backgroundColor", "fillStyle", "strokeWidth",
                "strokeStyle", "roughness", "opacity", "roundness", "x", "y",
                "width", "height", "type"}
    for e in els:
        assert required <= set(e), required - set(e)
        assert e["strokeWidth"] == 1
        assert e["roughness"] == c.ROUGHNESS
        assert e["roundness"] is None
        assert e["fillStyle"] == "solid"
    # all elements share one group id
    assert len({e["groupIds"][0] for e in els}) == 1


# --------------------------------------------------------------------------- #
# Library item labels
# --------------------------------------------------------------------------- #
def test_lib_item_has_grouped_label_below_icon():
    rel = Path("azure/identity/Users.svg")
    els, _ = c.convert_text((SRC / rel).read_text(), str(rel))
    item = c._lib_item(rel, "Users (identity)", els)
    labels = [e for e in item["elements"] if e["type"] == "text"]
    assert len(labels) == 1
    label = labels[0]
    assert label["text"] == "Users"  # base name, no disambiguation qualifier
    assert label["groupIds"] == els[0]["groupIds"]  # drags with the icon
    # sits below the icon, horizontally centered on its bbox
    assert label["y"] >= max(e["y"] + e["height"] for e in els)
    minx = min(e["x"] for e in els)
    maxx = max(e["x"] + e["width"] for e in els)
    assert label["x"] + label["width"] / 2 == pytest.approx((minx + maxx) / 2)
    assert label["fontSize"] == c.LABEL_FONT_SIZE
    assert label["containerId"] is None


def test_lib_item_label_is_idempotent():
    """Re-wrapping a labeled item (sandbox-fallback path) must not stack labels."""
    rel = Path("azure/identity/Users.svg")
    els, _ = c.convert_text((SRC / rel).read_text(), str(rel))
    once = c._lib_item(rel, "Users", els)
    twice = c._lib_item(rel, "Users", once["elements"])
    assert twice["elements"] == once["elements"]


# --------------------------------------------------------------------------- #
# Per-family sub-packs
# --------------------------------------------------------------------------- #
def test_pack_items_groups_by_top_level_family():
    pairs = [(Path("azure/identity/Users.svg"), {"id": "a"}),
             (Path("azure/web/Web Jobs.svg"), {"id": "b"}),
             (Path("fabric/report_20_item.svg"), {"id": "c"})]
    packs = c.pack_items(pairs)
    assert set(packs) == {"azure", "fabric"}
    assert [i["id"] for i in packs["azure"]] == ["a", "b"]
    assert [i["id"] for i in packs["fabric"]] == ["c"]


def test_make_lib_schema():
    lib = c.make_lib([{"id": "a"}])
    assert lib["type"] == "excalidrawlib" and lib["version"] == 2
    assert lib["libraryItems"] == [{"id": "a"}]


# --------------------------------------------------------------------------- #
# Library item naming
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stem,expected", [
    ("10230-icon-service-Users", "Users"),                       # azure classic
    ("00002-icon-service-Alerts", "Alerts"),
    ("data_factory_16_color", "Data Factory"),                   # fabric: size+variant stripped
    ("report_20_item", "Report"),
    ("spark_job_direction_24_item", "Spark Job Direction"),
    ("copy_job_24_non-item", "Copy Job"),
    ("copilot_48_color", "Copilot"),
    ("power_bi_48_color", "Power BI"),                           # acronym casing
    ("kql_database_24_item", "KQL Database"),
    ("sql_endpoint_24_item", "SQL Endpoint"),
    ("dataflow_gen2_24_item", "Dataflow Gen2"),                  # gen2 kept, cased
    ("Planner-Green-Icon", "Planner Green Icon"),                # plain dash names untouched beyond spacing
])
def test_humanized_name(stem, expected):
    assert c.humanized_name(stem) == expected


@pytest.mark.parametrize("rels,expected", [
    # size disambiguates; same-size variants get the item/color token too
    (["Fabric/data_factory_16_color.svg", "Fabric/data_factory_20_color.svg",
      "Fabric/data_factory_20_item.svg", "Fabric/report_20_item.svg"],
     ["Data Factory (16)", "Data Factory (20, color)", "Data Factory (20, item)", "Report"]),
    # cross-pack duplicates get the pack folder
    (["Teams Purple/apps.svg", "Microsoft Blue/apps.svg"],
     ["Apps (Teams Purple)", "Apps (Microsoft Blue)"]),
    # same pack, color-variant subfolders: pack then parent folder qualify
    (["Microsoft Blue/48x48 Grey & Blue Icon/Apps.svg",
      "Microsoft Blue/48x48 Light Blue Icon/Apps.svg", "Teams Purple/apps.svg"],
     ["Apps (Microsoft Blue, 48x48 Grey & Blue Icon)",
      "Apps (Microsoft Blue, 48x48 Light Blue Icon)", "Apps (Teams Purple)"]),
    # same top-level pack (azure/): parent category folder disambiguates
    (["azure/iot/Event Hubs.svg", "azure/analytics/Event Hubs.svg"],
     ["Event Hubs (iot)", "Event Hubs (analytics)"]),
    # same folder, same display name, different azure IDs: ID qualifies
    (["networking/02302-icon-service-Load-Balancer-Hub.svg",
      "networking/029029174-icon-service-Load-Balancer-Hub.svg"],
     ["Load Balancer Hub (02302)", "Load Balancer Hub (029029174)"]),
    # no collision, no suffix; non-size trailing digits preserved
    (["Agent 365/agent_365.svg"], ["Agent 365"]),
])
def test_name_collision_resolution(rels, expected):
    assert c.resolve_names(rels) == expected
