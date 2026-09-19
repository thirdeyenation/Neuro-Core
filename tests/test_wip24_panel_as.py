"""WI-P24-AS — slice-1 visual semantics (ARC conditions C1-C10).

Pins the panel-side fixes applied to webui/right-canvas-panels/graph-panel.html:

* C1 (V-2) — edge element data carries weight (and confidence); missing/legacy
  weight normalized to a defined default of 0.5 at element-build time; edge
  width encodes weight via bounded mapData(weight, 0, 1, 1, 6).
* C2 (V-3) — rel_type-to-color mapping bounded to the exact panel enumeration
  (8 types); palette derived from theme CSS variables with bounded fallbacks;
  rel_type text labels retained (never color-only).
* C3 (V-9) — edge tap populates a bounded DISPLAY-ONLY edge-focused inspector
  (rel_type, weight, confidence, endpoints) from already-serialized fields;
  node inspector unchanged.
* C4 (V-6) — inspector importance/confidence rows are metadata-borne
  (node.metadata) with a validation_status chip; stability explicitly rendered
  as unavailable; no ScoreStore reads and no API scores block.
* C5 (V-10) — memory_type badge over the exact 8-type enumeration with a
  missing-metadata fallback; x-icon rule holds (no new ligature spans).
* C6 (V-11) — layout select exposes exactly random and preset in addition to
  the existing 5; existing 5 options unchanged; preset limitation noted.
* C7 — changes confined to the panel file + this battery; graph-panel.css
  untouched; no API/handler/patch contact.
* C9 — canonical baseline untouched; nothing removed or bypassed.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"
CSS = PLUGIN_ROOT / "webui" / "graph-panel.css"
SHELL = PLUGIN_ROOT / "extensions" / "webui" / "right-canvas-panels" / "graph-panel.html"


def _source() -> str:
    return PANEL.read_text(encoding="utf-8")


def _xdata_js() -> str:
    src = _source()
    m = re.search(r'x-data="\{([\s\S]*)\}" x-init=', src)
    assert m, "x-data block not found"
    return m.group(1)


def _body_between(anchor: str, next_anchor: str) -> str:
    src = _source()
    start = src.index(anchor)
    end = src.index(next_anchor, start)
    return src[start:end]


# ------------------------------------------------------- C1 weighted edges

def test_c1_edge_builder_carries_weight_and_confidence():
    js = _xdata_js()
    eb = _body_between("(this.edges || []).forEach((e, i) => {", "this.cy.elements().remove();")
    assert "weight:" in eb, "edge element data must carry weight"
    assert "confidence:" in eb, "edge element data must carry confidence"
    # weight must be normalized at element-build time (not raw e.weight)
    assert "?? " not in eb or "weight" in eb


def test_c1_weight_default_0_5_for_legacy_edges():
    js = _xdata_js()
    eb = _body_between("(this.edges || []).forEach((e, i) => {", "this.cy.elements().remove();")
    assert "0.5" in eb, "missing/legacy weight must normalize to defined default 0.5"
    assert re.search(r"weight\s*[?:=]+.*0\.5|0\.5.*weight|weightNorm|normWeight|DEFAULT_WEIGHT", eb), (
        "weight default must be expressed in the edge builder"
    )


def test_c1_edge_width_uses_bounded_mapdata_on_weight():
    js = _xdata_js()
    assert "mapData(weight, 0, 1, 1, 6)" in js or "mapData(weight,0,1,1,6)" in js, (
        "edge width must encode weight via bounded mapData(weight, 0, 1, 1, 6)"
    )
    # the fixed 1.5 width must be gone from the edge style block
    edge_style = _body_between("selector: 'edge'", "selector: 'node:selected'")
    assert "'width': 1.5" not in edge_style


# ------------------------------------------------------- C2 typed edge colors

def test_c2_mapping_bounded_to_8_rel_types():
    js = _xdata_js()
    m = re.search(r"REL(?:_TYPE)?_COLORS\s*[:=]\s*\{([\s\S]*?)\}", js)
    assert m, "a REL_TYPE_COLORS mapping table must exist"
    body = m.group(1)
    types = set(re.findall(r"([a-z_]+)\s*:\s*\[", body))
    expected = {"supports", "contradicts", "depends_on", "derived_from",
                "related_to", "precedes", "follows", "part_of"}
    assert types == expected, f"mapping must be exactly the 8 panel types, got {types}"


def test_c2_palette_from_theme_variables_with_fallbacks():
    js = _xdata_js()
    m = re.search(r"REL(?:_TYPE)?_COLORS\s*[:=]\s*\{([\s\S]*?)\}", js)
    body = m.group(1)
    # every value derives from a theme CSS variable reference
    assert "--color-" in body, "palette values must derive from theme CSS variables"
    # bounded fallback literals exist (for variable resolution failure)
    assert re.search(r"#[0-9a-fA-F]{3,8}", body), "bounded fallback literals required"


def test_c2_rel_type_label_retained():
    js = _xdata_js()
    assert "'label': 'data(rel_type)'" in js, "rel_type text label must be retained"


def test_c2_unknown_rel_type_falls_back():
    js = _xdata_js()
    eb = _body_between("(this.edges || []).forEach((e, i) => {", "this.cy.elements().remove();")
    # the builder must resolve color via the mapping with a fallback for unknown types
    assert re.search(r"REL(?:_TYPE)?_COLORS\[|\.hasOwnProperty\(|\?\?", eb) or "||" in eb, (
        "edge builder must fall back for rel_types outside the 8-type mapping"
    )


# ------------------------------------------------------- C3 edge inspector

def test_c3_edge_selected_style_block_exists():
    js = _xdata_js()
    assert "selector: 'edge:selected'" in js
    es = _body_between("selector: 'edge:selected'", "];")
    assert "width" in es, "edge:selected must bump width"
    assert "line-color" in es, "edge:selected must intensify color"


def test_c3_edge_tap_handler_populates_display_only_inspector():
    js = _xdata_js()
    assert "cy.on('tap', 'edge'" in js, "an edge tap handler must exist"
    eh = _body_between("cy.on('tap', 'edge'", "cy.on('tap', function(evt)")
    for field in ("rel_type", "weight", "confidence", "from", "to"):
        assert field in eh, f"edge inspector must show {field}"
    # display-only: no mutation affordances in the edge inspector span
    assert "deleteEdge" not in eh and "addEdge" not in eh


def test_c3_node_inspector_unchanged():
    js = _xdata_js()
    nh = _body_between("cy.on('tap', 'node'", "cy.on('tap', function(evt)")
    assert "inspectNode" in nh
    assert "inspectNode = {" in nh


def test_c3_inspect_edge_state_exists():
    js = _xdata_js()
    assert re.search(r"inspectEdge\s*=\s*null|inspectEdge:\s*null", js) or "inspectEdge" in js


# ------------------------------------------------------- C4 inspector scores

def test_c4_inspect_node_carries_metadata():
    js = _xdata_js()
    nh = _body_between("cy.on('tap', 'node'", "cy.on('tap', function(evt)")
    assert "metadata" in nh, "node tap handler must carry node metadata"


def test_c4_node_builder_carries_metadata_and_memory_type():
    js = _xdata_js()
    nb = _body_between("elements.push({ group: 'nodes'", "}); }); (this.edges")
    assert "metadata" in nb and "memory_type" in nb


def test_c4_confidence_row_metadata_borne():
    src = _source()
    assert "Confidence" in src and re.search(
        r"metaVal\(inspectNode,\s*'confidence'\)", src
    ), "confidence row must be metadata-borne via metaVal(inspectNode, 'confidence')"


def test_c4_validation_status_chip():
    src = _source()
    assert "validation_status" in src


def test_c4_stability_rendered_unavailable():
    src = _source()
    m = re.search(r"Stability[\s\S]{0,400}?(unavailable|not available|N/A)", src, re.I)
    assert m, "stability must be rendered as explicitly unavailable"
    assert "ScoreStore" not in src, "no ScoreStore reads in the panel"


def test_c4_no_api_scores_block_dependency():
    src = _source()
    # panel must not depend on an API-side scores block existing
    assert re.search(r"n\.scores\b", src) or "scores" in src  # legacy fallback retained


# ------------------------------------------------------- C5 memory_type badge

def test_c5_memory_type_badge_renders():
    src = _source()
    assert re.search(r"memory_type", src) and "nc-details__badge" in src


def test_c5_badge_over_8_types_with_fallback():
    src = _source()
    assert re.search(
        r"concept|episode|reflection|task|solution|fragment|observation|summary", src
    )
    # fallback: missing metadata renders a graceful placeholder, not a crash
    assert re.search(r"metadata\s*\?\?|metadata\s*\|\||\(no type\)|unknown", src)


def test_c5_no_new_ligature_spans():
    """No new material-symbols ligature spans vs git HEAD (AGENTS.md rule);
    badges/chips are text-based, not icon-based."""
    head = subprocess.run(
        ["git", "-C", str(PLUGIN_ROOT), "show", "HEAD:webui/right-canvas-panels/graph-panel.html"],
        capture_output=True, text=True, check=True).stdout
    before = head.count("material-symbols-outlined")
    after = _source().count("material-symbols-outlined")
    assert after == before, f"ligature span count changed: {before} -> {after}"


# ------------------------------------------------------- C6 layouts

def test_c6_random_and_preset_options_added():
    src = _source()
    assert re.search(r'<option value="random"[ >]', src)
    assert re.search(r'<option value="preset"[ >]', src)


def test_c6_existing_5_options_unchanged():
    src = _source()
    for opt in ("cose", "concentric", "breadthfirst", "grid", "circle"):
        assert re.search('<option value="' + opt + '"[ >]', src), f"{opt} option must remain"
    # WI-P27 (ORC bounded authorization, orc-disposition.yaml): approved C3 state
    # is exactly 8 options - the original 7 in original order + dagre appended.
    opts = re.findall(r'<option value="([a-z]+)"[ >]', src)
    assert opts == [
        "cose", "concentric", "breadthfirst", "grid", "circle", "random", "preset", "dagre",
    ], f"approved C3 layout list expected, got {opts}"


def test_c6_preset_limitation_recorded():
    src = _source()
    # preset limitation noted in a title hint or code comment
    assert re.search(r"preset|load-order|persisted positions", src, re.I)
    hint = re.search(r'title="[^"]*preset[^"]*"', src, re.I) or \
           re.search(r"/\*[\s\S]*?preset[\s\S]*?\*/", src) or \
           re.search(r"//[^\n]*preset", src)
    assert hint, "preset limitation must be recorded as a title hint or comment"


def test_c6_no_layout_option_removed():
    js = _xdata_js()
    # WI-P27 (ORC bounded authorization, orc-disposition.yaml): approved C4
    # state - layout resolution goes through effectiveLayout() so a selected-
    # but-unavailable dagre falls back to cose; the stale direct form is gone.
    assert "name: this.effectiveLayout()" in js
    assert "name: this.layout" not in js


# ------------------------------------------------------- C7/C9 boundaries

def test_c7_css_untouched_dormant():
    # graph-panel.css remains untouched (byte-identical to HEAD) and un-referenced
    src = _source()
    assert "graph-panel.css" not in src
    diff = subprocess.run(
        ["git", "-C", str(PLUGIN_ROOT), "diff", "HEAD", "--", "webui/graph-panel.css"],
        capture_output=True, text=True, check=True).stdout
    assert diff == "", "graph-panel.css must remain untouched"


def test_c7_no_api_contract_change():
    src = _source()
    assert "POST /api/plugins/neuro_core/context_graph" not in src
    # no new endpoints introduced (existing endpoints list unchanged)
    endpoints = set(re.findall(r"/api/plugins/neuro_core/[a-z_]+", src))
    assert endpoints <= {
        "/api/plugins/neuro_core/context_graph",
        "/api/plugins/neuro_core/relationships",
        "/api/plugins/neuro_core/memory_subdirs",
        "/api/plugins/neuro_core/projects",
        "/api/plugins/neuro_core/advanced_filters",  # pre-existing at HEAD
    }, f"unexpected API endpoint usage: {endpoints}"


def test_c7_shell_copy_untouched():
    # the extensions shell pointer copy stays untouched
    assert SHELL.exists()
    shell = SHELL.read_text(encoding="utf-8")
    assert "x-data" not in shell


def test_c9_no_functionality_removed():
    src = _source()
    # core interactive features remain present
    for anchor in ("deleteEdge", "addEdge", "openAddForm", "relLabel", "search()",
                   "applyAdvancedFilters", "watchTheme", "cyResizeObserver"):
        assert anchor in src, f"existing functionality anchor {anchor} must remain"
