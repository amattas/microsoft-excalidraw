"""Tests for the excalidraw -> SVG preview renderer / README generator."""
import pytest

import render_readme as r


LINE = {
    "type": "line", "x": 10.0, "y": 20.0, "width": 4.0, "height": 4.0,
    "points": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 0.0]],
    "backgroundColor": "#ff0000", "strokeColor": "#1e1e1e", "opacity": 100,
}
RECT = {
    "type": "rectangle", "x": 0.0, "y": 0.0, "width": 8.0, "height": 6.0,
    "backgroundColor": "#00ff00", "strokeColor": "#00ff00", "opacity": 50,
}
ELLIPSE = {
    "type": "ellipse", "x": 2.0, "y": 2.0, "width": 10.0, "height": 4.0,
    "backgroundColor": "#0000ff", "strokeColor": "#1e1e1e", "opacity": 100,
}


@pytest.mark.parametrize("el,expect", [
    (LINE, ['<path', 'M10.00,20.00', 'Z"', 'fill="#ff0000"', 'stroke="#1e1e1e"']),
    (RECT, ['<rect', 'width="8.00"', 'fill="#00ff00"', 'opacity="0.5"']),
    (ELLIPSE, ['<ellipse', 'cx="7.00"', 'cy="4.00"', 'rx="5.00"', 'fill="#0000ff"']),
])
def test_element_svg(el, expect):
    frag = r.element_svg(el)
    for token in expect:
        assert token in frag, (token, frag)


def test_icon_svg_viewbox_covers_bbox():
    svg = r.icon_svg([LINE, RECT], pad=2.0)
    assert svg.startswith("<svg")
    # bbox is (0,0)-(14,24) across both elements, padded by 2
    assert 'viewBox="-2.00 -2.00 18.00 28.00"' in svg
    assert svg.count("<path") == 1 and svg.count("<rect") == 1


def test_readme_cell_escapes_and_quotes():
    cell = r.readme_cell("azure/web/Web App + Database.excalidraw", "Web App + Database")
    assert 'previews/azure/web/Web%20App%20%2B%20Database.svg' in cell
    assert "Web App + Database" in cell
