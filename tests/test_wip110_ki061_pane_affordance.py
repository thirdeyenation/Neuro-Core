"""WI-P110-KI061-PANE-AFFORDANCE-DESIGN: tests pinning the in-panel toggle
affordance for the graph-panel middle pane (ARC conditions C1, C2, C4, C6).

Covers the implementation in webui/right-canvas-panels/graph-panel.html:

* C1 — the header toggle tooltip must disambiguate the two resize layers:
  the toggle restores the middle pane; canvas resizing is the HOST canvas
  edge handle's job (available only while the surface is open). The tooltip
  must NOT imply the pane/toggle is draggable.
* C2 — the toggle lives in the header area (outside the collapsing .nc-body
  flex row); restore yields width from Details first, then Search; collapse
  is CSS/state-based (display toggle, no DOM removal) so side-pane state
  survives collapse/restore.
* C4 — detection is ResizeObserver-based rendered-width detection at the
  measured 520px threshold constant (threshold-measurement.yaml rev 1);
  observer cleanup is required; no polling.
* C6 — strictly additive: the existing 3-pane flex layout, host resize
  behavior, and all prior affordances remain present.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL_CONTENT = PLUGIN_ROOT / "webui/right-canvas-panels/graph-panel.html"


@pytest.fixture(scope="module")
def src() -> str:
    return PANEL_CONTENT.read_text(encoding="utf-8")


# ------------------------------------------------------------------ C1 pins


def test_c1_tooltip_disambiguates_resize_layers(src):
    """The tooltip must name both resize layers and must not imply dragging."""
    m = re.search(r'nc-header__btn--pane-toggle"[^>]*title="([^"]+)"', src)
    assert m, "pane-toggle button with title attribute not found"
    tip = m.group(1)
    assert "restore the middle pane" in tip
    assert "HOST canvas edge handle" in tip
    assert "not draggable" in tip
    # aside from naming the HOST drag handle, the tooltip must not suggest dragging
    assert not re.search(r"\bdrag\b", tip.replace("drag the HOST canvas edge handle", ""))


def test_c1_toggle_hidden_by_default(src):
    """The toggle must be x-show gated on paneToggleVisible (shown only while
    the middle pane is collapsed), with x-cloak to prevent pre-init flash."""
    assert '<button x-show="paneToggleVisible" x-cloak' in src


# ------------------------------------------------------------------ C2 pins


def test_c2_toggle_in_header_outside_collapsing_body(src):
    """The toggle must be inside .nc-header__actions, which sits before the
    .nc-body flex row in document order (header never collapses)."""
    actions_idx = src.find('class="nc-header__actions"')
    toggle_idx = src.find("nc-header__btn--pane-toggle\"")
    body_idx = src.find('<div class="nc-body">')
    assert -1 < actions_idx < toggle_idx < body_idx


def test_c2_collapse_is_css_state_based_no_dom_removal(src):
    """Collapse must be a display-toggle class; both side panes must remain
    present in the markup (no x-if/x-show DOM removal of the panes)."""
    assert (
        ".nc-details-panel.nc-pane--yielded, .nc-search-panel.nc-pane--yielded { display: none; }"
        in src
    )
    assert "'nc-pane--yielded': detailsYielded" in src
    assert "'nc-pane--yielded': searchYielded" in src
    for pane in ('class="nc-search-panel"', 'class="nc-details-panel"'):
        idx = src.find(pane)
        assert idx != -1
        tag_end = src.find(">", idx)
        assert "x-if" not in src[idx:tag_end]


def test_c2_restore_yields_details_first_then_search(src):
    """restoreMiddlePane must yield Details first, then Search (C2 order)."""
    m = re.search(r"restoreMiddlePane\(\) \{(.*?)\},", src, re.DOTALL)
    assert m, "restoreMiddlePane not found"
    body = m.group(1)
    details_pos = body.find("detailsYielded = true")
    search_pos = body.find("searchYielded = true")
    assert -1 < details_pos < search_pos, "Details must be yielded before Search"


def test_c2_symmetric_restore_when_space_returns(src):
    """updatePaneAffordance must clear BOTH yields symmetrically once the
    body is wide enough again (>= threshold)."""
    m = re.search(r"updatePaneAffordance\(\) \{(.{0,900}?)\},", src, re.DOTALL)
    assert m, "updatePaneAffordance not found"
    body = m.group(1)
    assert "searchYielded = false" in body and "detailsYielded = false" in body
    assert "PANE_COLLAPSE_THRESHOLD_PX" in body


# ------------------------------------------------------------------ C4 pins


def test_c4_resizeobserver_detection_no_polling(src):
    """Detection must use ResizeObserver on the canvas element; no setInterval
    polling may be introduced by this work item."""
    m = re.search(r"initPaneAffordance\(\) \{(.*?)\},", src, re.DOTALL)
    assert m, "initPaneAffordance not found"
    assert "new ResizeObserver" in m.group(1)
    assert "setInterval" not in m.group(1)


def test_c4_threshold_constant_is_measured_520(src):
    """The detection constant must be the empirically measured 520px."""
    assert "PANE_COLLAPSE_THRESHOLD_PX: 520" in src


def test_c4_observer_cleanup_required(src):
    """A destroy/disconnect path must exist and init must guard against
    duplicate observers (guard-disconnect pattern, same as cyResizeObserver)."""
    m = re.search(r"initPaneAffordance\(\) \{(.*?)\},", src, re.DOTALL)
    assert m and "destroyPaneAffordance()" in m.group(1)
    d = re.search(r"destroyPaneAffordance\(\) \{(.*?)\},", src, re.DOTALL)
    assert d and ".disconnect()" in d.group(1)


# ------------------------------------------------------------------ C6 pins


def test_c6_existing_3pane_layout_preserved(src):
    """The pre-existing flex layout rules must remain intact (strictly
    additive change)."""
    assert ".nc-body { display: flex; flex: 1; overflow: hidden; min-width: 0; min-height: 0; }" in src
    assert ".nc-search-panel { width: 240px; flex-shrink: 0;" in src
    assert ".nc-details-panel { width: 280px; flex-shrink: 0;" in src
    assert ".nc-graph-canvas { flex: 1;" in src


def test_c6_no_media_query_added(src):
    """C4 approved ResizeObserver detection over a pure-CSS breakpoint; no
    @media rule may be introduced."""
    assert "@media" not in src


# ------------------------------------------------- parse-level DOM probe


class _PaneDOMProbe(HTMLParser):
    """Records that both side panes and the graph canvas survive HTML parsing
    with their class bindings intact (regression-class catcher)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = d.get("class", "")
        if "nc-search-panel" in cls:
            self.found["search"] = d.get(":class", "")
        if "nc-details-panel" in cls:
            self.found["details"] = d.get(":class", "")
        if "nc-graph-canvas" in cls:
            self.found["canvas"] = cls


def test_parse_level_panes_survive_with_bindings(src):
    probe = _PaneDOMProbe()
    probe.feed(src)
    assert "nc-pane--yielded" in probe.found.get("search", "")
    assert "nc-pane--yielded" in probe.found.get("details", "")
    assert "nc-graph-canvas" in probe.found.get("canvas", "")


# ------------------------------------------------- headless logic execution


def _extract_method(src: str, name: str) -> str:
    m = re.search(name + r"\(\) \{(.*?)\},", src, re.DOTALL)
    assert m, f"{name} not found"
    return m.group(1)


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available for headless JS execution")
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return result.stdout.strip().splitlines()[-1]


def test_logic_restore_order_headless(src):
    """Execute restoreMiddlePane headlessly: two clicks must yield Details
    then Search; a third click is a no-op."""
    fn = _extract_method(src, "restoreMiddlePane")
    script = (
        "const log = [];\n"
        "const self = { detailsYielded: false, searchYielded: false };\n"
        "function restoreMiddlePane() { "
        + fn.replace("this.", "self.")
        + " }\n"
        "restoreMiddlePane(); log.push([self.detailsYielded, self.searchYielded]);\n"
        "restoreMiddlePane(); log.push([self.detailsYielded, self.searchYielded]);\n"
        "restoreMiddlePane(); log.push([self.detailsYielded, self.searchYielded]);\n"
        "console.log(JSON.stringify(log));"
    )
    log = json.loads(_run_node(script))
    assert log == [[True, False], [True, True], [True, True]]


def test_logic_threshold_headless(src):
    """Execute updatePaneAffordance headlessly: zero canvas width shows the
    toggle; width restored at body >= 520 clears both yields symmetrically."""
    fn = _extract_method(src, "updatePaneAffordance")

    def harness_for(canvas_w, body_w, details, search):
        return (
            "const __canvasEl = { getBoundingClientRect: () => ({ width: "
            + str(canvas_w)
            + " }), closest: () => ({ getBoundingClientRect: () => ({ width: "
            + str(body_w)
            + " }) }) };\n"
            "const self = { detailsYielded: "
            + json.dumps(details)
            + ", searchYielded: "
            + json.dumps(search)
            + ", paneToggleVisible: false, PANE_COLLAPSE_THRESHOLD_PX: 520 };\n"
            "function updatePaneAffordance() { "
            + fn.replace("this.", "self.")
            .replace("document.querySelector('.nc-graph-canvas')", "__canvasEl")
            + " }\n"
            "updatePaneAffordance();\n"
            "console.log(JSON.stringify([self.paneToggleVisible, self.detailsYielded, self.searchYielded]));"
        )

    # collapsed: canvas rendered width 0 -> toggle visible, yields untouched
    out = json.loads(_run_node(harness_for(0, 500, True, True)))
    assert out == [True, True, True]
    # wide again: body 800 >= 520 -> toggle hidden, both yields cleared
    out = json.loads(_run_node(harness_for(280, 800, True, True)))
    assert out == [False, False, False]
    # wide but nothing yielded -> toggle hidden, no state change
    out = json.loads(_run_node(harness_for(280, 800, False, False)))
    assert out == [False, False, False]
