"""WI-P113-KI061AC-PANE-CONTROL-REDESIGN (revision 2): tests pinning the pane
state machine after the HITL-ratified revision (popover removed; default-open
fix via a plugin-side minimum HOST canvas width assertion).

Every changed/removed pin is enumerated in the WI-P113 implementation report
revision 2 - no silent removals.

Covers the implementation in webui/right-canvas-panels/graph-panel.html:

* Popover removed (HITL decision, rev 2) - the popover element, its CSS, and
  the panePopoverOpen state are gone entirely and must never return.
* Pane toggles re-instated (WI-P114-KI061AE, HITL directive) - three direct,
  always-visible per-pane header buttons (Search / Graph / Inspector) drive the
  retained togglePane state machine; no popover. The last user-enabled pane's
  button is disabled as guardrail feedback (never hidden - the KI-061-AC
  no-disappearing-control lesson). Width-based collapse/auto-restore and
  node-click reveal remain the primary interactions alongside the toggles.
* Guardrail - at least one pane must remain visible; the state machine refuses
  to disable the last user-enabled pane (togglePane is retained as the
  guardrail carrier even though no UI currently invokes it).
* Default layout - all 3 panes visible at equal thirds when nothing is
  toggled off or width-collapsed; the per-pane minimum widths from the
  original layout are preserved.
* Default-open fix (KI-061-AD, rev 2) - on panel init the panel asserts a
  plugin-side minimum HOST canvas width (560px) via the host Alpine store's
  setWidth(px, { persist: false }): the persisted user width is never
  overwritten, and a missing host store is a safe no-op.
* Persistence - the user's pane choice round-trips through the localStorage
  key 'nc_graph_pane_layout' (the panel's existing localStorage preference
  surface, same mechanism as 'nc_memory_subdirs').
* Explicit choice beats width - width collapse is a floor, not a controller:
  only the flexible graph pane can be width-collapsed, it auto-restores when
  space allows, and an explicitly toggled-off pane stays off regardless of
  width.
* Node-click reveal - revealInspector respects an explicitly toggled-off
  Inspector (harmonized; no yield fighting).
* Detection - ResizeObserver on .nc-body (body width is invariant to pane
  toggles, so explicit toggles can never retrigger width collapse) at the
  measured 520px threshold constant (threshold-measurement.yaml rev 1);
  observer cleanup required; no polling.
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


# --------------------------------------- popover removal pins (rev 2)


def test_popover_fully_removed(src):
    """HITL decision (rev 2): the popover UI is removed entirely - no popover
    element, no panePopoverOpen state, no header pane-toggle button, and no
    leftover popover/pane-control CSS. The only permitted mention is the
    explanatory comment documenting the removal."""
    assert "nc-pane-popover" not in src
    assert "nc-header__btn--pane-toggle" not in src
    assert "nc-pane-control" not in src
    # panePopoverOpen may appear only inside the removal-explanation comment
    without_comment = src.replace(
        "The WI-P113 rev2 popover (panePopoverOpen) was removed by HITL decision",
        "",
    )
    assert "panePopoverOpen" not in without_comment


def test_pane_toggle_buttons_present(src):
    """WI-P114-KI061AE: three direct per-pane toggle buttons (Search / Graph /
    Inspector) exist in the header actions and call the retained togglePane
    state machine - a direct affordance, not a popover."""
    for pane in ("search", "graph", "inspector"):
        needle = f'@click="togglePane(\'{pane}\')"'
        assert needle in src, f"missing toggle button for {pane}"
    # all three sit inside the header actions block, before Refresh
    actions = src.find('nc-header__actions')
    refresh = src.find('@click="refresh()"')
    assert -1 < actions < refresh
    first_toggle = src.find('@click="togglePane(')
    assert -1 < actions < first_toggle < refresh


def test_pane_toggle_no_popover(src):
    """The re-instated toggles are direct header buttons: no popover element,
    state, or CSS may exist (HITL rev-2 removal stands)."""
    assert "nc-pane-popover" not in src
    assert "nc-pane-control" not in src
    without_comment = src.replace(
        "The WI-P113 rev2 popover (panePopoverOpen) was removed by HITL decision",
        "",
    )
    assert "panePopoverOpen" not in without_comment


def test_pane_toggle_always_visible_guardrail_disabled(src):
    """KI-061-AC lesson: controls never disappear. The toggle buttons carry no
    x-show/x-if conditional; the at-least-one-pane guardrail is expressed as a
    :disabled binding (visible but inert), never by hiding the control."""
    for pane in ("search", "graph", "inspector"):
        marker = '<button @click="togglePane(' + chr(39) + pane + chr(39) + ')"'
        idx = src.find(marker)
        assert idx != -1, f"toggle button for {pane} not found"
        attrs = src[idx : idx + 500]
        assert "x-show" not in attrs and "x-if" not in attrs
        assert ':disabled="paneUser.' + pane in attrs
        assert "userEnabledCount() <= 1" in attrs


def test_pane_toggle_glyph_inline_svg_currentcolor(src):
    """The toggle icons reuse the approved panel-layout glyph style: inline
    SVG stroked with currentColor (no material-symbols ligature spans)."""
    for m in re.finditer(r'@click="togglePane\(.*?</button>', src, re.DOTALL):
        inner = m.group(0)
        assert "<svg" in inner
        assert "material-symbols" not in inner
        assert "currentColor" in inner
    assert src.count('class="nc-pane-glyph__pane"') == 3


def test_min_width_assertion_present(src):
    """KI-061-AD default-open fix: the panel must assert a plugin-side minimum
    HOST canvas width on init via the host store's setWidth with persist
    disabled (the user's stored width preference is never overwritten)."""
    assert "assertMinCanvasWidth()" in src
    m = re.search(r"assertMinCanvasWidth\(\) \{(.*?)\},", src, re.DOTALL)
    assert m, "assertMinCanvasWidth method not found"
    body = m.group(1)
    assert "Alpine.store('rightCanvas')" in body
    assert "setWidth(560" in body
    assert "persist: false" in body


def test_min_width_assertion_called_from_init(src):
    """The assertion must run during pane-affordance init (i.e., on every
    panel open), after layout load and before the width floor evaluation."""
    m = re.search(r"initPaneAffordance\(\) \{(.*?)\},", src, re.DOTALL)
    assert m, "initPaneAffordance not found"
    body = m.group(1)
    init_pos = body.find("this.loadPaneLayout()")
    assert_pos = body.find("this.assertMinCanvasWidth()")
    floor_pos = body.find("this.updateWidthFloor()")
    assert -1 < init_pos < assert_pos, "assertMinCanvasWidth must follow loadPaneLayout"
    assert -1 < assert_pos < floor_pos, "assertMinCanvasWidth must precede updateWidthFloor"


def test_min_width_assertion_headless(src):
    """Headless: a narrow host canvas is widened to 560 with persist disabled;
    a wide-enough canvas is left alone; a missing host store is a no-op."""
    m = re.search(r"assertMinCanvasWidth\(\) \{(.*?)\},", src, re.DOTALL)
    assert m, "assertMinCanvasWidth not found"
    fn = m.group(1).replace("this.", "self.")

    def harness(host_width):
        if host_width is None:
            host = "null"
        else:
            host = (
                "{ width: %d, setWidth: (px, opts) => "
                "calls.push([px, JSON.stringify(opts)]) }" % host_width
            )
        return (
            "const calls = [];\n"
            "const host = " + host + ";\n"
            "globalThis.Alpine = { store: (name) => "
            "name === 'rightCanvas' ? host : undefined };\n"
            "function assertMinCanvasWidth() { " + fn + " }\n"
            "assertMinCanvasWidth();\n"
            "console.log(JSON.stringify(calls));"
        )

    # narrow host canvas: widened to 560, persist disabled
    log = json.loads(_run_node(harness(400)))
    assert log == [[560, '{"persist":false}']]
    # already wide enough: untouched
    log = json.loads(_run_node(harness(800)))
    assert log == []
    # no host store (test harness): safe no-op
    log = json.loads(_run_node(harness(None)))
    assert log == []


# --------------------------------------- guardrail pins


def test_guardrail_last_pane_cannot_be_toggled_off_headless(src):
    """Headless: with only one pane user-enabled, togglePane must refuse to
    disable it (at least one pane always remains visible)."""
    toggle = _extract_method(src, "togglePane")
    count = _extract_method(src, "userEnabledCount")
    script = (
        "const self = { paneUser: { search: true, graph: true, inspector: true }, "
        "paneWidthOff: { search: false, graph: false, inspector: false } };\n"
        "self.savePaneLayout = () => {};\n"
        "self.applyPaneLayout = () => {};\n"
        "self.updateWidthFloor = () => {};\n"
        "function userEnabledCount() { " + count.replace("this.", "self.") + " }\n"
        "function togglePane(k) { " + toggle.replace("this.", "self.") + " }\n"
        "self.userEnabledCount = userEnabledCount;\n"
        "const log = [];\n"
        "togglePane('search'); log.push(JSON.parse(JSON.stringify(self.paneUser)));\n"
        "togglePane('inspector'); log.push(JSON.parse(JSON.stringify(self.paneUser)));\n"
        "// guardrail: last enabled pane must be refused\n"
        "togglePane('graph'); log.push(JSON.parse(JSON.stringify(self.paneUser)));\n"
        "console.log(JSON.stringify(log));"
    )
    log = json.loads(_run_node(script))
    assert log[0] == {"search": False, "graph": True, "inspector": True}
    assert log[1] == {"search": False, "graph": True, "inspector": False}
    assert log[2] == {"search": False, "graph": True, "inspector": False}, (
        "guardrail violated: the last visible pane was toggled off"
    )


# --------------------------------------- default layout pins


def test_default_layout_equal_thirds_rule(src):
    """An equal-thirds layout rule must exist and apply to both side panes
    when all three panes are effectively visible."""
    assert ".nc-body--thirds .nc-search-panel, .nc-body--thirds .nc-details-panel { width: 33.333%; }" in src


def test_default_layout_thirds_class_applied_only_when_all_visible_headless(src):
    """Headless: applyPaneLayout must toggle the thirds class on exactly when
    all three panes are user-enabled and none is width-collapsed."""
    apply_fn = _extract_method(src, "applyPaneLayout")
    script = (
        "const classes = {};\n"
        "const __bodyEl = { classList: { toggle: (c, on) => { classes[c] = !!on; } } };\n"
        "const document = { querySelector: (s) => s === '.nc-body' ? __bodyEl : null };\n"
        "const self = { paneUser: { search: true, graph: true, inspector: true }, "
        "paneWidthOff: { search: false, graph: false, inspector: false } };\n"
        "function applyPaneLayout() { " + apply_fn.replace("this.", "self.") + " }\n"
        "const log = [];\n"
        "applyPaneLayout(); log.push(classes['nc-body--thirds']);\n"
        "self.paneUser.inspector = false; applyPaneLayout(); log.push(classes['nc-body--thirds']);\n"
        "self.paneUser.inspector = true; self.paneWidthOff.graph = true;\n"
        "applyPaneLayout(); log.push(classes['nc-body--thirds']);\n"
        "self.paneWidthOff.graph = false; applyPaneLayout(); log.push(classes['nc-body--thirds']);\n"
        "console.log(JSON.stringify(log));"
    )
    log = json.loads(_run_node(script))
    assert log == [True, False, False, True]


def test_c6_existing_3pane_layout_preserved(src):
    """The pre-existing flex layout rules and per-pane minimum widths must
    remain intact (the thirds rule is additive and the base widths stay)."""
    assert ".nc-body { display: flex; flex: 1; overflow: hidden; min-width: 0; min-height: 0; }" in src
    assert ".nc-search-panel { width: 240px; flex-shrink: 0;" in src
    assert ".nc-details-panel { width: 280px; flex-shrink: 0;" in src
    assert ".nc-graph-canvas { flex: 1;" in src


def test_c6_no_media_query_added(src):
    """ResizeObserver detection stays approved over a pure-CSS breakpoint; no
    @media rule may be introduced."""
    assert "@media" not in src


# --------------------------------------- persistence pins


def test_persistence_uses_panel_localstorage_surface(src):
    """Persistence must use the panel's existing localStorage preference
    surface under a dedicated key (same mechanism as 'nc_memory_subdirs')."""
    assert "localStorage.getItem('nc_graph_pane_layout')" in src
    assert "localStorage.setItem('nc_graph_pane_layout'" in src


def test_persistence_round_trip_headless(src):
    """Headless: savePaneLayout must persist the pane choice and
    loadPaneLayout must restore it; corrupt storage keeps the default."""
    save = _extract_method(src, "savePaneLayout")
    load = _extract_method(src, "loadPaneLayout")
    script = (
        "const store = {};\n"
        "const localStorage = { getItem: (k) => (k in store ? store[k] : null), "
        "setItem: (k, v) => { store[k] = v; } };\n"
        "const self = { paneUser: { search: true, graph: true, inspector: true } };\n"
        "function savePaneLayout() { " + save.replace("this.", "self.") + " }\n"
        "function loadPaneLayout() { " + load.replace("this.", "self.") + " }\n"
        "const log = [];\n"
        "self.paneUser = { search: false, graph: true, inspector: true };\n"
        "savePaneLayout();\n"
        "self.paneUser = { search: true, graph: true, inspector: true };\n"
        "loadPaneLayout(); log.push(JSON.parse(JSON.stringify(self.paneUser)));\n"
        "// corrupt payload must be ignored (defaults kept)\n"
        "store['nc_graph_pane_layout'] = 'not-json';\n"
        "loadPaneLayout(); log.push(JSON.parse(JSON.stringify(self.paneUser)));\n"
        "// wrong-shape payload must be ignored too\n"
        "store['nc_graph_pane_layout'] = JSON.stringify({ search: 'yes' });\n"
        "loadPaneLayout(); log.push(JSON.parse(JSON.stringify(self.paneUser)));\n"
        "console.log(JSON.stringify(log));"
    )
    log = json.loads(_run_node(script))
    assert log[0] == {"search": False, "graph": True, "inspector": True}
    assert log[1] == {"search": False, "graph": True, "inspector": True}
    assert log[2] == {"search": False, "graph": True, "inspector": True}


# --------------------------------- explicit choice beats width


def test_width_floor_auto_restore_headless(src):
    """Headless: the flexible graph pane is width-collapsed when the body is
    too narrow for the enabled side panes and auto-restores when space
    allows (floor, not controller)."""
    floor = _extract_method(src, "updateWidthFloor")
    apply_fn = _extract_method(src, "applyPaneLayout")

    def harness(body_w):
        return (
            "const classes = {};\n"
            "const __bodyEl = { getBoundingClientRect: () => ({ width: "
            + str(body_w)
            + " }), classList: { toggle: (c, on) => { classes[c] = !!on; } } };\n"
            "const document = { querySelector: (s) => s === '.nc-body' ? __bodyEl : null };\n"
            "const self = { paneUser: { search: true, graph: true, inspector: true }, "
            "paneWidthOff: { search: false, graph: false, inspector: false } };\n"
            "function applyPaneLayout() { " + apply_fn.replace("this.", "self.") + " }\n"
            "function updateWidthFloor() { " + floor.replace("this.", "self.") + " }\n"
            "self.applyPaneLayout = applyPaneLayout;\n"
            "updateWidthFloor();\n"
            "console.log(JSON.stringify(self.paneWidthOff));"
        )

    # narrow: body 500 <= 240 + 280 -> graph width-collapsed
    assert json.loads(_run_node(harness(500))) == {
        "search": False, "graph": True, "inspector": False
    }
    # wide again: graph auto-restores
    assert json.loads(_run_node(harness(800))) == {
        "search": False, "graph": False, "inspector": False
    }


def test_explicit_off_survives_width_headless(src):
    """Headless: an explicitly toggled-off pane stays off regardless of width,
    and an explicitly disabled graph pane is never width-restored."""
    floor = _extract_method(src, "updateWidthFloor")
    apply_fn = _extract_method(src, "applyPaneLayout")
    script = (
        "const classes = {};\n"
        "const __bodyEl = { getBoundingClientRect: () => ({ width: 250 }), "
        "classList: { toggle: (c, on) => { classes[c] = !!on; } } };\n"
        "const document = { querySelector: (s) => s === '.nc-body' ? __bodyEl : null };\n"
        "const self = { paneUser: { search: false, graph: true, inspector: true }, "
        "paneWidthOff: { search: false, graph: false, inspector: false } };\n"
        "function applyPaneLayout() { " + apply_fn.replace("this.", "self.") + " }\n"
        "function updateWidthFloor() { " + floor.replace("this.", "self.") + " }\n"
        "self.applyPaneLayout = applyPaneLayout;\n"
        "const log = [];\n"
        "// search explicitly off: width must not resurrect it\n"
        "updateWidthFloor(); log.push(JSON.parse(JSON.stringify(self.paneUser)));\n"
        "log.push(JSON.parse(JSON.stringify(self.paneWidthOff)));\n"
        "// graph explicitly off: width must not force it back on\n"
        "self.paneUser.graph = false; updateWidthFloor();\n"
        "log.push(JSON.parse(JSON.stringify(self.paneUser)));\n"
        "log.push(JSON.parse(JSON.stringify(self.paneWidthOff)));\n"
        "console.log(JSON.stringify(log));"
    )
    log = json.loads(_run_node(script))
    # explicit user choice unchanged...
    assert log[0] == {"search": False, "graph": True, "inspector": True}
    assert log[2] == {"search": False, "graph": False, "inspector": True}
    # ...while the width floor only ever touches the user-enabled graph pane
    assert log[1] == {"search": False, "graph": True, "inspector": False}
    assert log[3] == {"search": False, "graph": False, "inspector": False}


# --------------------------------------- node-click reveal pins


def test_node_click_reveal_respects_explicit_off(src):
    """revealInspector must be a no-op when the user explicitly toggled the
    Inspector off; a user-enabled Inspector is always visible."""
    m = re.search(r"revealInspector\(\) \{(.*?)\},", src, re.DOTALL)
    assert m, "revealInspector not found"
    assert "if (!this.paneUser.inspector) return;" in m.group(1)


def test_node_click_reveal_respects_explicit_off_headless(src):
    """Headless: with Inspector explicitly off, node selection must not
    re-enable it; with Inspector enabled, selection is a safe no-op."""
    reveal = _extract_method(src, "revealInspector")
    script = (
        "const log = [];\n"
        "const self = { paneUser: { search: true, graph: true, inspector: false }, "
        "paneWidthOff: { search: false, graph: false, inspector: false } };\n"
        "function revealInspector() { " + reveal.replace("this.", "self.") + " }\n"
        "revealInspector();\n"
        "log.push(JSON.parse(JSON.stringify(self.paneUser)));\n"
        "self.paneUser.inspector = true;\n"
        "revealInspector();\n"
        "log.push(JSON.parse(JSON.stringify(self.paneUser)));\n"
        "console.log(JSON.stringify(log));"
    )
    log = json.loads(_run_node(script))
    assert log[0]["inspector"] is False, "explicit-off Inspector was overridden"
    assert log[1]["inspector"] is True


# --------------------------------------- detection pins


def test_c4_resizeobserver_detection_no_polling(src):
    """Detection must use ResizeObserver on .nc-body (invariant to pane
    toggles); no setInterval polling may be introduced."""
    m = re.search(r"initPaneAffordance\(\) \{(.*?)\},", src, re.DOTALL)
    assert m, "initPaneAffordance not found"
    assert "document.querySelector('.nc-body')" in m.group(1)
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


# --------------------------------------- parse-level DOM probe


class _PaneDOMProbe(HTMLParser):
    """Records that all three panes survive HTML parsing with their class
    bindings intact (regression-class catcher)."""

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
            self.found["canvas"] = d.get(":class", "")


def test_parse_level_panes_survive_with_bindings(src):
    probe = _PaneDOMProbe()
    probe.feed(src)
    assert "nc-pane--hidden" in probe.found.get("search", "")
    assert "nc-pane--hidden" in probe.found.get("details", "")
    assert "nc-pane--hidden" in probe.found.get("canvas", "")


# --------------------------------------- headless helpers


def _extract_method(src: str, name: str) -> str:
    m = re.search(re.escape(name) + r"\([^)]*\) \{(.*?)\},", src, re.DOTALL)
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
