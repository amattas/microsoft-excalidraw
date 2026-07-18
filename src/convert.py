#!/usr/bin/env python3
"""Convert Azure service SVG icons to Excalidraw format.

Self-contained, stdlib only. Parses the full SVG path grammar (M/L/H/V/C/S/Q/T/A
and relative variants), rect/circle/ellipse/polygon/polyline/line primitives,
element/ancestor transforms, and linear/radial gradient fills (resolved to the
interpolated color at offset 0.5). Curves are flattened with adaptive
subdivision. Each drawable subpath becomes one Excalidraw element; interior
subpaths (letter counters / holes) are emitted immediately after their outer
contour and filled with the color of whatever sits beneath them, faking a
fill-rule that Excalidraw lacks.

Run with no arguments to regenerate every per-icon .excalidraw file and the
microsoft-icons.excalidrawlib library.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

OUT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = OUT_ROOT / "src" / "icons"
ICONS_OUT = OUT_ROOT / "icons"
LIB_OUT = OUT_ROOT / "microsoft-icons.excalidrawlib"

# 18x18 source viewBox rendered at ~96px to match the existing library scale.
OUTSCALE = 96.0 / 18.0
# Flatness tolerance for curve subdivision, in source units.
# Sketchiness knobs. Excalidraw's wobble is clamped per segment, so denser
# points = cleaner lines. Coarser FLATNESS + higher ROUGHNESS = more scribble.
FLATNESS = 0.02   # 0.02 renders almost clean; 0.3+ starts looking polygonal
ROUGHNESS = 1     # excalidraw: 0 architect, 1 artist, 2 cartoonist
MAX_POINTS = 300
# Fixed timestamp so regeneration is deterministic (no wall-clock calls).
FIXED_TS = 1784320097329

# Outline rule thresholds, in output-space pixels.
OUTLINE_MIN_DIM = 8.0
OUTLINE_MIN_AREA = 300.0
OUTLINE_COLOR = "#1e1e1e"

# Library-item name label, drawn centered under the icon.
LABEL_FONT_SIZE = 16
LABEL_LINE_HEIGHT = 1.25
LABEL_CHAR_WIDTH = 0.6  # width estimate as a fraction of font size per char
LABEL_GAP = 8.0


# --------------------------------------------------------------------------- #
# Color helpers
# --------------------------------------------------------------------------- #
_NAMED_COLORS = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "none": None,
    "transparent": None,
}


def parse_color(value):
    """Parse a CSS/SVG color into an #rrggbb string, or None for none/transparent."""
    if value is None:
        return None
    v = value.strip().lower()
    if not v or v == "none" or v == "transparent":
        return None
    if v.startswith("url("):
        return v  # unresolved gradient ref; caller resolves it
    if v in _NAMED_COLORS:
        rgb = _NAMED_COLORS[v]
        return None if rgb is None else _rgb_to_hex(rgb)
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            return "#" + "".join(c * 2 for c in h)
        if len(h) == 4:  # #rgba -> drop alpha
            return "#" + "".join(c * 2 for c in h[:3])
        if len(h) >= 6:
            return "#" + h[:6]
        return "#000000"
    m = re.match(r"rgba?\(([^)]+)\)", v)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        try:
            r, g, b = (int(round(float(p.rstrip("%")) * (255 / 100)))
                       if p.endswith("%") else int(round(float(p)))
                       for p in parts[:3])
            return _rgb_to_hex((r, g, b))
        except ValueError:
            return "#000000"
    return "#000000"


def _rgb_to_hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(round(c)))) for c in rgb))


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def interpolate_gradient(stops, at=0.5):
    """stops: list of (offset, '#rrggbb'). Return the color at `at`."""
    if not stops:
        return "#000000"
    stops = sorted(stops, key=lambda s: s[0])
    if at <= stops[0][0]:
        return stops[0][1]
    if at >= stops[-1][0]:
        return stops[-1][1]
    for (o0, c0), (o1, c1) in zip(stops, stops[1:]):
        if o0 <= at <= o1:
            t = 0.0 if o1 == o0 else (at - o0) / (o1 - o0)
            r0, g0, b0 = _hex_to_rgb(c0)
            r1, g1, b1 = _hex_to_rgb(c1)
            return _rgb_to_hex((r0 + (r1 - r0) * t,
                                g0 + (g1 - g0) * t,
                                b0 + (b1 - b0) * t))
    return stops[-1][1]


# --------------------------------------------------------------------------- #
# Affine transforms (2x3: a b c d e f mapping x'=a*x+c*y+e, y'=b*x+d*y+f)
# --------------------------------------------------------------------------- #
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mul(m, n):
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def apply_mat(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def parse_transform(text):
    if not text:
        return IDENTITY
    m = IDENTITY
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text):
        nums = [float(x) for x in re.findall(r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?", args)]
        if name == "translate":
            tx = nums[0] if nums else 0.0
            ty = nums[1] if len(nums) > 1 else 0.0
            t = (1, 0, 0, 1, tx, ty)
        elif name == "scale":
            sx = nums[0] if nums else 1.0
            sy = nums[1] if len(nums) > 1 else sx
            t = (sx, 0, 0, sy, 0, 0)
        elif name == "rotate":
            ang = math.radians(nums[0]) if nums else 0.0
            cos, sin = math.cos(ang), math.sin(ang)
            t = (cos, sin, -sin, cos, 0, 0)
            if len(nums) >= 3:
                cx, cy = nums[1], nums[2]
                t = mat_mul((1, 0, 0, 1, cx, cy), mat_mul(t, (1, 0, 0, 1, -cx, -cy)))
        elif name == "matrix" and len(nums) == 6:
            t = tuple(nums)
        elif name == "skewX":
            t = (1, 0, math.tan(math.radians(nums[0])) if nums else 0, 1, 0, 0)
        elif name == "skewY":
            t = (1, math.tan(math.radians(nums[0])) if nums else 0, 0, 1, 0, 0)
        else:
            continue
        m = mat_mul(m, t)
    return m


def mat_is_axis_aligned(m):
    return abs(m[1]) < 1e-9 and abs(m[2]) < 1e-9


# --------------------------------------------------------------------------- #
# SVG path parsing -> flattened subpaths (each a list of (x, y), with closed flag)
# --------------------------------------------------------------------------- #
_PATH_TOKEN = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)")


def _tokenize_path(d):
    for cmd, num in _PATH_TOKEN.findall(d):
        yield ("cmd", cmd) if cmd else ("num", float(num))


class _PathReader:
    def __init__(self, d):
        self.toks = list(_tokenize_path(d))
        self.i = 0

    def peek_cmd(self):
        return self.i < len(self.toks) and self.toks[self.i][0] == "cmd"

    def next_cmd(self):
        t = self.toks[self.i]
        self.i += 1
        return t[1]

    def num(self):
        t = self.toks[self.i]
        if t[0] != "num":
            raise ValueError("expected number in path data")
        self.i += 1
        return t[1]

    def flag(self):
        # arc flags may be packed (e.g. "010" == 0,1,0); handle single digit
        t = self.toks[self.i]
        self.i += 1
        return int(t[1])

    def has_more(self):
        return self.i < len(self.toks)


def parse_path_d(d):
    """Return list of subpaths; each subpath is (points, closed)."""
    r = _PathReader(d)
    subpaths = []
    pts = []
    closed = False
    start = (0.0, 0.0)
    cur = (0.0, 0.0)
    prev_ctrl = None  # for S/T reflection
    prev_cmd = None
    cmd = None

    def flush():
        nonlocal pts, closed
        if len(pts) >= 2:
            subpaths.append((pts, closed))
        pts = []
        closed = False

    while r.has_more():
        if r.peek_cmd():
            cmd = r.next_cmd()
        # else: implicit repeat of previous command
        low = cmd.lower()
        rel = cmd.islower()

        if low == "m":
            x, y = r.num(), r.num()
            if rel and pts:
                x += cur[0]
                y += cur[1]
            elif rel:
                x += cur[0]
                y += cur[1]
            flush()
            cur = (x, y)
            start = cur
            pts = [cur]
            prev_ctrl = None
            # subsequent coordinate pairs are implicit lineto
            cmd = "l" if rel else "L"
        elif low == "l":
            x, y = r.num(), r.num()
            if rel:
                x += cur[0]
                y += cur[1]
            cur = (x, y)
            pts.append(cur)
            prev_ctrl = None
        elif low == "h":
            x = r.num()
            x = x + cur[0] if rel else x
            cur = (x, cur[1])
            pts.append(cur)
            prev_ctrl = None
        elif low == "v":
            y = r.num()
            y = y + cur[1] if rel else y
            cur = (cur[0], y)
            pts.append(cur)
            prev_ctrl = None
        elif low == "c":
            x1, y1, x2, y2, x, y = (r.num() for _ in range(6))
            if rel:
                x1 += cur[0]; y1 += cur[1]; x2 += cur[0]; y2 += cur[1]; x += cur[0]; y += cur[1]
            _flatten_cubic(cur, (x1, y1), (x2, y2), (x, y), pts)
            prev_ctrl = (x2, y2)
            cur = (x, y)
        elif low == "s":
            x2, y2, x, y = (r.num() for _ in range(4))
            if rel:
                x2 += cur[0]; y2 += cur[1]; x += cur[0]; y += cur[1]
            if prev_cmd in ("c", "s"):
                x1 = 2 * cur[0] - prev_ctrl[0]
                y1 = 2 * cur[1] - prev_ctrl[1]
            else:
                x1, y1 = cur
            _flatten_cubic(cur, (x1, y1), (x2, y2), (x, y), pts)
            prev_ctrl = (x2, y2)
            cur = (x, y)
        elif low == "q":
            x1, y1, x, y = (r.num() for _ in range(4))
            if rel:
                x1 += cur[0]; y1 += cur[1]; x += cur[0]; y += cur[1]
            _flatten_quad(cur, (x1, y1), (x, y), pts)
            prev_ctrl = (x1, y1)
            cur = (x, y)
        elif low == "t":
            x, y = r.num(), r.num()
            if rel:
                x += cur[0]; y += cur[1]
            if prev_cmd in ("q", "t"):
                x1 = 2 * cur[0] - prev_ctrl[0]
                y1 = 2 * cur[1] - prev_ctrl[1]
            else:
                x1, y1 = cur
            _flatten_quad(cur, (x1, y1), (x, y), pts)
            prev_ctrl = (x1, y1)
            cur = (x, y)
        elif low == "a":
            rx, ry = r.num(), r.num()
            rot = r.num()
            large = r.flag()
            sweep = r.flag()
            x, y = r.num(), r.num()
            if rel:
                x += cur[0]; y += cur[1]
            _flatten_arc(cur, rx, ry, rot, large, sweep, (x, y), pts)
            prev_ctrl = None
            cur = (x, y)
        elif low == "z":
            closed = True
            if pts and pts[0] != cur:
                pts.append(pts[0])
            cur = start
            flush()
            # a new subpath may follow with implicit moveto to start
            pts = [cur]
            prev_ctrl = None
        else:
            raise ValueError(f"unknown path command {cmd!r}")
        prev_cmd = low

    flush()
    # drop the trailing empty [start] subpath left by a closing Z
    return [sp for sp in subpaths if len(sp[0]) >= 2]


def _flatten_cubic(p0, p1, p2, p3, out, depth=0):
    if depth > 24:
        out.append(p3)
        return
    # flatness: max distance of control points from the chord p0-p3
    d1 = _point_line_dist(p1, p0, p3)
    d2 = _point_line_dist(p2, p0, p3)
    if max(d1, d2) <= FLATNESS:
        out.append(p3)
        return
    p01 = _mid(p0, p1); p12 = _mid(p1, p2); p23 = _mid(p2, p3)
    p012 = _mid(p01, p12); p123 = _mid(p12, p23)
    m = _mid(p012, p123)
    _flatten_cubic(p0, p01, p012, m, out, depth + 1)
    _flatten_cubic(m, p123, p23, p3, out, depth + 1)


def _flatten_quad(p0, p1, p2, out, depth=0):
    if depth > 24:
        out.append(p2)
        return
    if _point_line_dist(p1, p0, p2) <= FLATNESS:
        out.append(p2)
        return
    p01 = _mid(p0, p1); p12 = _mid(p1, p2); m = _mid(p01, p12)
    _flatten_quad(p0, p01, m, out, depth + 1)
    _flatten_quad(m, p12, p2, out, depth + 1)


def _mid(a, b):
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def _point_line_dist(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    seg = math.hypot(dx, dy)
    if seg < 1e-12:
        return math.hypot(px - ax, py - ay)
    return abs((px - ax) * dy - (py - ay) * dx) / seg


def _flatten_arc(p0, rx, ry, phi_deg, large, sweep, p1, out):
    """Endpoint -> center parameterization, then sample the arc."""
    x0, y0 = p0
    x1, y1 = p1
    rx, ry = abs(rx), abs(ry)
    if rx < 1e-9 or ry < 1e-9 or (x0 == x1 and y0 == y1):
        out.append(p1)
        return
    phi = math.radians(phi_deg)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx2 = (x0 - x1) / 2.0
    dy2 = (y0 - y1) / 2.0
    x1p = cos_p * dx2 + sin_p * dy2
    y1p = -sin_p * dx2 + cos_p * dy2
    # correct out-of-range radii
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx *= s
        ry *= s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den > 0 else 0.0
    if large == sweep:
        co = -co
    cxp = co * (rx * y1p) / ry
    cyp = -co * (ry * x1p) / rx
    cx = cos_p * cxp - sin_p * cyp + (x0 + x1) / 2.0
    cy = sin_p * cxp + cos_p * cyp + (y0 + y1) / 2.0

    def angle(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        length = math.hypot(ux, uy) * math.hypot(vx, vy)
        a = math.acos(max(-1.0, min(1.0, dot / length))) if length else 0.0
        if ux * vy - uy * vx < 0:
            a = -a
        return a

    theta1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = angle((x1p - cxp) / rx, (y1p - cyp) / ry,
                   (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi

    # sample count from arc length vs flatness
    r_avg = (rx + ry) / 2.0
    max_step = 2 * math.acos(max(0.0, 1 - FLATNESS / max(r_avg, FLATNESS))) if r_avg > 0 else math.pi / 8
    max_step = max(max_step, math.pi / 90)
    n = max(2, int(math.ceil(abs(dtheta) / max_step)))
    for i in range(1, n + 1):
        t = theta1 + dtheta * (i / n)
        ex = cos_p * rx * math.cos(t) - sin_p * ry * math.sin(t) + cx
        ey = sin_p * rx * math.cos(t) + cos_p * ry * math.sin(t) + cy
        out.append((ex, ey))


# --------------------------------------------------------------------------- #
# SVG document parsing
# --------------------------------------------------------------------------- #
def _localname(tag):
    return tag.split("}", 1)[-1]


def _style_dict(style):
    out = {}
    if style:
        for item in style.split(";"):
            if ":" in item:
                k, v = item.split(":", 1)
                out[k.strip()] = v.strip()
    return out


class SvgDoc:
    def __init__(self, text):
        self.root = ET.fromstring(text)
        self.gradients = {}
        self._collect_gradients(self.root)

    def _collect_gradients(self, node):
        for el in node.iter():
            name = _localname(el.tag)
            if name in ("linearGradient", "radialGradient"):
                gid = el.get("id")
                if gid:
                    self.gradients[gid] = el

    def _gradient_stops(self, el, _seen=None):
        _seen = _seen or set()
        gid = el.get("id")
        if gid in _seen:
            return []
        _seen.add(gid)
        stops = []
        for child in el:
            if _localname(child.tag) == "stop":
                off = child.get("offset", "0").strip()
                offv = float(off[:-1]) / 100.0 if off.endswith("%") else float(off)
                style = _style_dict(child.get("style"))
                col = child.get("stop-color") or style.get("stop-color") or "#000000"
                hexc = parse_color(col) or "#000000"
                stops.append((offv, hexc))
        if not stops:
            href = el.get("{http://www.w3.org/1999/xlink}href") or el.get("href")
            if href and href.startswith("#"):
                ref = self.gradients.get(href[1:])
                if ref is not None:
                    return self._gradient_stops(ref, _seen)
        return stops

    def resolve_fill(self, value):
        """Resolve a fill value (possibly url(#id)) to #rrggbb or None."""
        col = parse_color(value)
        if col is None:
            return None
        if col.startswith("url("):
            m = re.match(r"url\(#([^)]+)\)", col)
            if not m:
                return "#000000"
            grad = self.gradients.get(m.group(1))
            if grad is None:
                return "#000000"
            return interpolate_gradient(self._gradient_stops(grad), 0.5)
        return col


DRAWABLE_TAGS = {"path", "rect", "circle", "ellipse", "polygon", "polyline", "line"}
SKIP_SUBTREE_TAGS = {"defs", "clipPath", "mask", "symbol", "title", "style"}


def iter_drawables(doc):
    """Yield drawable primitives in document order with inherited context.

    Each item: dict(kind, node, matrix, opacity, fill_attr).
    Elements inside <defs>/<clipPath>/<mask> are skipped.
    """
    results = []

    def walk(node, matrix, opacity, fill_inherited):
        for child in node:
            name = _localname(child.tag)
            if name in SKIP_SUBTREE_TAGS:
                continue
            style = _style_dict(child.get("style"))
            cmat = mat_mul(matrix, parse_transform(child.get("transform")))
            cop = opacity
            op_attr = child.get("opacity") or style.get("opacity")
            if op_attr is not None:
                try:
                    cop = opacity * float(op_attr)
                except ValueError:
                    pass
            fill = child.get("fill")
            if fill is None:
                fill = style.get("fill")
            fill = fill if fill is not None else fill_inherited
            if name == "g":
                walk(child, cmat, cop, fill)
            elif name in DRAWABLE_TAGS:
                results.append({
                    "kind": name,
                    "node": child,
                    "matrix": cmat,
                    "opacity": cop,
                    "fill": fill,
                    "fill_opacity": child.get("fill-opacity") or style.get("fill-opacity"),
                })
            else:
                # unknown container (e.g. <a>): descend
                walk(child, cmat, cop, fill)

    walk(doc.root, IDENTITY, 1.0, None)
    return results


def _num(node, attr, default=0.0):
    v = node.get(attr)
    if v is None:
        return default
    return float(re.sub(r"[a-z%]+$", "", v.strip()))


def primitive_subpaths(item):
    """Return (subpaths, native_shape) for a drawable.

    subpaths: list of (points_in_source_space, closed) already transformed by the
    element matrix. native_shape: None, or ('rectangle'|'ellipse', geometry) when
    the primitive is axis-aligned and can stay a native Excalidraw shape.
    """
    kind = item["kind"]
    node = item["node"]
    m = item["matrix"]
    axis = mat_is_axis_aligned(m)

    def tp(x, y):
        return apply_mat(m, x, y)

    if kind == "path":
        raw = parse_path_d(node.get("d", ""))
        subs = [([tp(x, y) for (x, y) in pts], closed) for pts, closed in raw]
        return subs, None

    if kind == "rect":
        x, y = _num(node, "x"), _num(node, "y")
        w, h = _num(node, "width"), _num(node, "height")
        corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
        tc = [tp(px, py) for px, py in corners]
        if axis:
            xs = [p[0] for p in tc]; ys = [p[1] for p in tc]
            return [(tc, True)], ("rectangle", (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)))
        return [(tc, True)], None

    if kind in ("circle", "ellipse"):
        cx, cy = _num(node, "cx"), _num(node, "cy")
        if kind == "circle":
            rx = ry = _num(node, "r")
        else:
            rx, ry = _num(node, "rx"), _num(node, "ry")
        pts = []
        n = 64
        for i in range(n + 1):
            t = 2 * math.pi * i / n
            pts.append(tp(cx + rx * math.cos(t), cy + ry * math.sin(t)))
        if axis:
            sx = math.hypot(m[0], m[1])
            sy = math.hypot(m[2], m[3])
            ccx, ccy = tp(cx, cy)
            rxo, ryo = rx * sx, ry * sy
            return [(pts, True)], ("ellipse", (ccx - rxo, ccy - ryo, 2 * rxo, 2 * ryo))
        return [(pts, True)], None

    if kind in ("polygon", "polyline"):
        nums = [float(x) for x in re.findall(r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?", node.get("points", ""))]
        raw = list(zip(nums[0::2], nums[1::2]))
        pts = [tp(x, y) for x, y in raw]
        closed = kind == "polygon"
        if closed and pts and pts[0] != pts[-1]:
            pts = pts + [pts[0]]
        return [(pts, closed)], None

    if kind == "line":
        x1, y1 = _num(node, "x1"), _num(node, "y1")
        x2, y2 = _num(node, "x2"), _num(node, "y2")
        return [([tp(x1, y1), tp(x2, y2)], False)], None

    return [], None


# --------------------------------------------------------------------------- #
# Geometry helpers for holes
# --------------------------------------------------------------------------- #
def point_in_poly(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi):
            inside = not inside
        j = i
    return inside


def interior_point(poly):
    """A point guaranteed inside a simple polygon (for point-in-poly tests)."""
    n = len(poly)
    cx = sum(p[0] for p in poly) / n
    cy = sum(p[1] for p in poly) / n
    if point_in_poly((cx, cy), poly):
        return (cx, cy)
    # scanline through the centroid's y: take the midpoint of the first span
    xs = []
    j = n - 1
    for i in range(n):
        yi = poly[i][1]; yj = poly[j][1]
        if (yi > cy) != (yj > cy):
            xi = poly[i][0]; xj = poly[j][0]
            xs.append(xi + (xj - xi) * (cy - yi) / (yj - yi + 1e-30))
        j = i
    xs.sort()
    if len(xs) >= 2:
        return ((xs[0] + xs[1]) / 2.0, cy)
    return (cx, cy)


def poly_area(poly):
    a = 0.0
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2.0


# --------------------------------------------------------------------------- #
# Element assembly
# --------------------------------------------------------------------------- #
def _bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _scale_pts(points):
    return [(x * OUTSCALE, y * OUTSCALE) for x, y in points]


def _det_int(seed_str, mod):
    h = hashlib.sha256(seed_str.encode()).hexdigest()
    return int(h[:12], 16) % mod


def _det_uuid(seed_str):
    h = hashlib.sha256(seed_str.encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def build_elements(doc, rel_key):
    """Return the list of Excalidraw element dicts for one icon."""
    drawables = iter_drawables(doc)
    group_id = _det_uuid(rel_key + "|group")
    elements = []
    # emitted polygons (output coords) + fill, for hole color lookup
    emitted_polys = []
    idx = 0

    for item in drawables:
        fill_hex = doc.resolve_fill(item["fill"] if item["fill"] is not None else "#000000")
        fo = item.get("fill_opacity")
        if fill_hex is None:
            continue  # fill:none -> not a drawable
        subs, native = primitive_subpaths(item)
        subs = [(pts, closed) for pts, closed in subs if len(pts) >= 2]
        if not subs:
            continue

        op = item["opacity"]
        if fo is not None:
            try:
                op *= float(fo)
            except ValueError:
                pass
        opacity = max(0, min(100, int(round(op * 100))))

        # snapshot of everything strictly beneath this source element
        below = list(emitted_polys)

        # classify holes: a closed subpath contained in an odd number of the
        # element's other closed subpaths
        out_polys = [_scale_pts(pts) for pts, _ in subs]
        interiors = [interior_point(p) for p in out_polys]

        # Outline decision is consistent per source path: a compound path (more
        # than one subpath) gets a dark outline only if EVERY one of its
        # subpath-elements passes the size threshold; if any fails, all of its
        # elements use stroke == fill. Single-subpath line elements reduce to the
        # per-element rule. Native primitives keep the per-element rule below.
        item_dark = all(
            _outline_qualifies(maxx - minx, maxy - miny)
            for minx, miny, maxx, maxy in (_bbox(p) for p in out_polys)
        )
        # SVG fills implicitly close every subpath, so containment ignores the
        # explicit closed flag (fabric border rings leave the inner loop open).
        areas = [abs(poly_area(p)) for p in out_polys]
        containment = []
        for i in range(len(subs)):
            containers = []
            for jdx in range(len(subs)):  # any other subpath can enclose a hole,
                if jdx == i:
                    continue
                if areas[jdx] <= areas[i]:
                    continue  # ...but only a strictly larger one (centroid samples
                              # of ring-shaped regions can land inside their hole)
                if point_in_poly(interiors[i], out_polys[jdx]):
                    containers.append(jdx)
            is_hole = len(containers) % 2 == 1
            parent = min(containers, key=lambda k: poly_area(out_polys[k])) if containers else None
            containment.append((is_hole, parent))

        order = []  # emission order among this element's subpaths
        for i in range(len(subs)):
            if not containment[i][0]:
                order.append(i)
                for k in range(len(subs)):
                    if containment[k][0] and containment[k][1] == i:
                        order.append(k)
        # any hole whose parent wasn't emitted (defensive) -> append
        for i in range(len(subs)):
            if i not in order:
                order.append(i)

        for i in order:
            pts, closed = subs[i]
            is_hole = containment[i][0]
            out_pts = out_polys[i]
            if is_hole:
                fill = _hole_fill(interiors[i], below)
                elem = _make_line_element(out_pts, closed, fill, opacity, group_id, rel_key, idx, item_dark)
            elif native is not None and len(subs) == 1:
                shape, geom = native
                sgeom = tuple(v * OUTSCALE for v in geom)
                elem = _make_native_element(shape, sgeom, fill_hex, opacity, group_id, rel_key, idx)
            else:
                elem = _make_line_element(out_pts, closed, fill_hex, opacity, group_id, rel_key, idx, item_dark)
            elements.append(elem)
            emitted_polys.append((out_pts,
                                  fill_hex if not is_hole else elem["backgroundColor"],
                                  elem["opacity"]))
            idx += 1

    return elements, group_id


def _hole_fill(sample, below):
    """Composite every layer covering `sample` bottom-up over white, honoring
    each layer's opacity (a 20% sheen must blend, not replace)."""
    color = (255.0, 255.0, 255.0)
    for poly, layer_color, layer_op in below:
        if point_in_poly(sample, poly):
            a = layer_op / 100.0
            rgb = _hex_to_rgb(layer_color)
            color = tuple(a * f + (1 - a) * c for f, c in zip(rgb, color))
    return _rgb_to_hex(color)


def _outline_qualifies(w, h):
    return min(w, h) >= OUTLINE_MIN_DIM and (w * h) >= OUTLINE_MIN_AREA


def _outline_stroke(w, h, fill):
    return OUTLINE_COLOR if _outline_qualifies(w, h) else fill


def _common_fields(rel_key, idx):
    return {
        "version": 1,
        "versionNonce": _det_int(f"{rel_key}|{idx}|nonce", 2 ** 31),
        "isDeleted": False,
        "id": _det_uuid(f"{rel_key}|{idx}|id"),
        "fillStyle": "solid",
        "strokeWidth": 1,
        "strokeStyle": "solid",
        "roughness": ROUGHNESS,
        "angle": 0,
        "seed": _det_int(f"{rel_key}|{idx}|seed", 2 ** 31),
        "roundness": None,
        "boundElements": [],
        "updated": FIXED_TS,
        "link": None,
        "locked": False,
        "frameId": None,
    }


def _make_line_element(out_pts, closed, fill, opacity, group_id, rel_key, idx, dark):
    minx, miny, maxx, maxy = _bbox(out_pts)
    w, h = maxx - minx, maxy - miny
    points = [[x - minx, y - miny] for x, y in out_pts]
    if closed and points and points[0] != points[-1]:
        points.append([points[0][0], points[0][1]])
    if closed and len(points) >= 2:
        points[-1] = [points[0][0], points[0][1]]
    if len(points) > MAX_POINTS:
        points = _decimate(points, MAX_POINTS, closed)
    el = _common_fields(rel_key, idx)
    el.update({
        "type": "line",
        "x": minx,
        "y": miny,
        "width": w,
        "height": h,
        "strokeColor": OUTLINE_COLOR if dark else fill,
        "backgroundColor": fill,
        "opacity": opacity,
        "groupIds": [group_id],
        "points": points,
        "lastCommittedPoint": None,
        "startBinding": None,
        "endBinding": None,
        "startArrowhead": None,
        "endArrowhead": None,
    })
    return el


def _make_native_element(shape, geom, fill, opacity, group_id, rel_key, idx):
    x, y, w, h = geom
    el = _common_fields(rel_key, idx)
    el.update({
        "type": shape,
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "strokeColor": _outline_stroke(w, h, fill),
        "backgroundColor": fill,
        "opacity": opacity,
        "groupIds": [group_id],
    })
    return el


def _decimate(points, cap, closed):
    if closed:
        body = points[:-1]
        step = len(body) / (cap - 1)
        keep = [body[int(i * step)] for i in range(cap - 1)]
        keep.append([keep[0][0], keep[0][1]])
        return keep
    step = (len(points) - 1) / (cap - 1)
    keep = [points[min(len(points) - 1, int(round(i * step)))] for i in range(cap)]
    return keep


# --------------------------------------------------------------------------- #
# Independent subpath counter (guards against silent drops)
# --------------------------------------------------------------------------- #
def count_drawable_subpaths(doc):
    n = 0
    for item in iter_drawables(doc):
        fill_hex = doc.resolve_fill(item["fill"] if item["fill"] is not None else "#000000")
        if fill_hex is None:
            continue
        subs, _ = primitive_subpaths(item)
        n += sum(1 for pts, _ in subs if len(pts) >= 2)
    return n


# --------------------------------------------------------------------------- #
# File / library assembly
# --------------------------------------------------------------------------- #
_FABRIC_STEM_RE = re.compile(r"^(?P<base>.+?)_(?P<size>16|20|24|28|32|40|48|64)(?:_(?:non-)?item|_color)?$")
_VARIANT_RE = re.compile(r"_((?:non-)?item|color)$")
_ACRONYMS = {"bi": "BI", "ai": "AI", "ml": "ML", "sql": "SQL", "kql": "KQL",
             "api": "API", "iot": "IoT", "gen2": "Gen2"}


def humanized_name(stem):
    m = re.match(r"^\d+-icon-(?:service-)?(.*)$", stem)
    if m:
        return m.group(1).replace("-", " ").strip()
    m = _FABRIC_STEM_RE.match(stem)
    base = m.group("base") if m else stem
    words = [w for w in base.replace("_", " ").replace("-", " ").split() if w]
    # capitalize lowercase words (with acronym casing); leave mixed-case as-is
    return " ".join(_ACRONYMS.get(w, w.capitalize() if w.islower() else w) for w in words)


def resolve_names(rels):
    """Humanize each icon's stem; disambiguate same-name collisions by
    progressively appending qualifiers: size token, item/color variant,
    then top-level pack folder."""
    rels = [Path(r) for r in rels]

    def size_of(r):
        m = _FABRIC_STEM_RE.match(r.stem)
        return m.group("size") if m else None

    def variant_of(r):
        m = _VARIANT_RE.search(r.stem)
        return m.group(1) if m else None

    def pack_of(r):
        return r.parts[0] if len(r.parts) > 1 else None

    def parent_of(r):
        return r.parts[-2] if len(r.parts) > 2 else None

    def id_of(r):
        m = re.match(r"^(\d+)-icon", r.stem)
        return m.group(1) if m else None

    qualifiers = [size_of, variant_of, pack_of, parent_of, id_of]
    names = [humanized_name(r.stem) for r in rels]
    quals = [[] for _ in rels]
    for qualifier in qualifiers:
        groups = defaultdict(list)
        for i, n in enumerate(names):
            groups[n].append(i)
        for idxs in groups.values():
            if len(idxs) < 2:
                continue
            vals = {i: qualifier(rels[i]) for i in idxs}
            if len({v for v in vals.values() if v is not None}) < 2:
                continue  # qualifier doesn't discriminate within this group
            for i in idxs:
                if vals[i] is not None:
                    quals[i].append(str(vals[i]))
                    names[i] = f"{humanized_name(rels[i].stem)} ({', '.join(quals[i])})"
    return names


def make_icon_json(elements):
    return {
        "type": "excalidraw",
        "version": 2,
        "source": "fluentui-icons-to-excalidraw",
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }


def convert_text(text, rel_key):
    doc = SvgDoc(text)
    elements, _ = build_elements(doc, rel_key)
    return elements, count_drawable_subpaths(doc)


def make_lib(items):
    return {
        "type": "excalidrawlib",
        "version": 2,
        "source": "fluentui-icons-to-excalidraw",
        "libraryItems": items,
    }


def pack_items(rel_item_pairs):
    """Group library items by top-level family folder, preserving order."""
    packs = defaultdict(list)
    for rel, item in rel_item_pairs:
        packs[Path(rel).parts[0]].append(item)
    return packs


def main():
    svgs = sorted(SRC_ROOT.rglob("*.svg"))
    existing_lib = json.loads(LIB_OUT.read_text()) if LIB_OUT.exists() else None
    existing_items = existing_lib["libraryItems"] if existing_lib else []

    entries = []  # (rel, library item)
    failures = []
    blocked = []
    total_elements = 0

    names = resolve_names([svg.relative_to(SRC_ROOT) for svg in svgs])

    for svg, name in zip(svgs, names):
        rel = svg.relative_to(SRC_ROOT)
        rel_key = str(rel)
        try:
            text = svg.read_text()
        except (PermissionError, OSError) as e:
            blocked.append((rel_key, str(e)))
            # fall back to the existing library item (matched by name, never by
            # position) so the icon still ships when the sandbox blocks the source.
            prior = next((it for it in existing_items
                          if it.get("name") == name and it.get("elements")), None)
            # drop the library label: per-icon files carry only the icon itself
            elements = [e for e in prior["elements"] if e.get("type") != "text"] if prior else []
            if not elements:
                # Shipping an empty item would poison the fallback source for
                # every future run; refuse instead.
                failures.append((rel_key, "unreadable and no prior library data "
                                          "— re-run unsandboxed"))
                continue
            _write_icon(rel, elements)
            entries.append((rel, _lib_item(rel, name, elements)))
            total_elements += len(elements)
            continue

        try:
            elements, subpath_count = convert_text(text, rel_key)
        except Exception as e:  # noqa: BLE001
            failures.append((rel_key, repr(e)))
            continue

        if len(elements) != subpath_count:
            failures.append((rel_key, f"element count {len(elements)} != subpaths {subpath_count}"))
            continue
        if not elements:
            failures.append((rel_key, "no elements emitted"))
            continue

        _write_icon(rel, elements)
        entries.append((rel, _lib_item(rel, name, elements)))
        total_elements += len(elements)

    if failures:
        print(f"FAILURES ({len(failures)}):")
        for k, msg in failures[:50]:
            print(f"  {k}: {msg}")
        raise SystemExit(1)

    library_items = [item for _, item in entries]
    LIB_OUT.write_text(json.dumps(make_lib(library_items)))

    # per-family sub-packs next to the full library
    packs = pack_items(entries)
    pack_files = {OUT_ROOT / f"{family}-icons.excalidrawlib": items
                  for family, items in packs.items()}
    for path, items in pack_files.items():
        path.write_text(json.dumps(make_lib(items)))
    for p in OUT_ROOT.glob("*-icons.excalidrawlib"):
        if p != LIB_OUT and p not in pack_files:
            p.unlink()  # family removed from the sources

    valid = {ICONS_OUT / svg.relative_to(SRC_ROOT).with_suffix(".excalidraw") for svg in svgs}
    pruned = 0
    for p in ICONS_OUT.rglob("*.excalidraw"):
        if p not in valid:
            try:
                p.unlink()
                pruned += 1
            except OSError:
                pass  # sandbox-blocked path (e.g. *credentials*); leave it
    def _is_dir(d):
        try:
            return d.is_dir()
        except OSError:
            return False
    for d in sorted((d for d in ICONS_OUT.rglob("*") if _is_dir(d)), reverse=True):
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    print(f"icons written: {len(library_items)}")
    print(f"sub-packs: {', '.join(sorted(f'{f} ({len(v)})' for f, v in packs.items()))}")
    print(f"stale outputs pruned: {pruned}")
    print(f"total elements: {total_elements}")
    print(f"blocked (sandbox, fell back to existing): {len(blocked)}")
    for k, _ in blocked:
        print(f"  {k}")


def _write_icon(rel, elements):
    out = ICONS_OUT / rel.with_suffix(".excalidraw")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(make_icon_json(elements)))


def _label_element(rel_key, text, icon_elements):
    """A text element with the icon's name, centered below its bounding box,
    sharing the icon's group so it drags along with it."""
    minx = min(e["x"] for e in icon_elements)
    maxx = max(e["x"] + e["width"] for e in icon_elements)
    maxy = max(e["y"] + e["height"] for e in icon_elements)
    w = len(text) * LABEL_FONT_SIZE * LABEL_CHAR_WIDTH
    h = LABEL_FONT_SIZE * LABEL_LINE_HEIGHT
    el = _common_fields(rel_key, "label")
    el.update({
        "type": "text",
        "x": (minx + maxx) / 2.0 - w / 2.0,
        "y": maxy + LABEL_GAP,
        "width": w,
        "height": h,
        "strokeColor": OUTLINE_COLOR,
        "backgroundColor": "transparent",
        "opacity": 100,
        "groupIds": [_det_uuid(rel_key + "|group")],
        "text": text,
        "originalText": text,
        "fontSize": LABEL_FONT_SIZE,
        "fontFamily": 1,
        "textAlign": "center",
        "verticalAlign": "top",
        "containerId": None,
        "lineHeight": LABEL_LINE_HEIGHT,
        "baseline": int(LABEL_FONT_SIZE * LABEL_LINE_HEIGHT * 0.8),
        "autoResize": True,
    })
    return el


def _lib_item(rel, name, elements):
    # Deterministic, position-independent identity: stable across runs even as
    # icons are added or removed around this one.
    # Label with the base humanized name (no disambiguation qualifiers) so the
    # canvas text stays clean; strip any prior label first (the sandbox-fallback
    # path feeds already-labeled library elements back through here).
    icon_els = [e for e in elements if e.get("type") != "text"]
    label = [_label_element(str(rel), humanized_name(rel.stem), icon_els)] if icon_els else []
    return {
        "id": _det_uuid(str(rel) + "|item"),
        "status": "unpublished",
        "name": name,
        "created": FIXED_TS,
        "updated": FIXED_TS,
        "elements": icon_els + label,
    }


if __name__ == "__main__":
    main()
