"""WI-P28-AS-FINAL-SLICES — final KI-018-AS visual slices (R2).

Pins the panel-side additions applied to webui/right-canvas-panels/graph-panel.html:

* V-1 (node shape differentiation) — bounded NODE_TYPE_SHAPES mapping over the
  exact 8-type memory_type enumeration (concept, episode, task, solution,
  reflection, fragment, observation, summary) with unknown/missing fallback to
  ellipse; bound via 'shape': 'data(node_shape)' inside the always-applied node
  style block of cyStyle(level), so the encoding survives all V-5 density
  levels including minimal (which only drops labels).
* V-4 (cluster coloring) — connected-component clustering computed purely
  client-side over the nodes/edges arrays the panel already received (no API
  change); deterministic component ordering by minimum node index; palette
  (CLUSTER_COLORS, green/teal/orange/brown hue families) disjoint from the 8
  rel-type edge fallback colors (purple/red/yellow/blue/pink/gray families);
  single-component graphs stay calm (empty map, base node color); cluster
  legend rows additive to the WI-P25 V-7 legend, shown only for 2+ components.

Style mechanism (verified): nodes are styled by the Cytoscape style array
returned by cyStyle(level) — not CSS classes. Shape strings are verified both
against the accepted vocabulary and against the vendored Cytoscape 3.30.2
bundle source. Functional tests execute the extracted x-data object literal
under Node; they skip explicitly (never silently pass) when node is absent.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"
SHELL_REL = "extensions" / Path("webui") / "right-canvas-panels" / "graph-panel.html"
SHELL = PLUGIN_ROOT / SHELL_REL

VOCAB = {
    "ellipse", "rectangle", "round-rectangle", "rhomboid", "diamond",
    "pentagon", "hexagon", "octagon", "star",
}

NODE_TYPE_SHAPES = {
    "concept": "ellipse",
    "episode": "round-rectangle",
    "task": "diamond",
    "solution": "hexagon",
    "reflection": "pentagon",
    "fragment": "rectangle",
    "observation": "octagon",
    "summary": "star",
}

CLUSTER_COLORS = [
    "#e67e22", "#27ae60", "#16a085", "#d35400", "#1abc9c",
    "#8bc34a", "#a0522d", "#2e7d32", "#b5651d", "#00695c",
]

# Fallback hexes of the 8 REL_TYPE_COLORS entries (edge color authority)
EDGE_FALLBACK_HEXES = {
    "#9b59b6", "#f87171", "#facc15", "#5b9bd5",
    "#f472b6", "#7d3c98", "#c39bd3", "#94a3b8",
}


def _source() -> str:
    return PANEL.read_text(encoding="utf-8")


def _xdata_js() -> str:
    src = _source()
    m = re.search(r'x-data="\{([\s\S]*)\}" x-init=', src)
    assert m, "x-data block not found"
    return m.group(1)


# ------------------------------------------------------------------ V-1 pins

def test_v1_node_type_shapes_mapping_pinned_exactly():
    src = _source()
    m = re.search(r"NODE_TYPE_SHAPES:\s*\{([^}]*)\}", src)
    assert m, "NODE_TYPE_SHAPES map not found"
    entries = re.findall(r"(\w+):\s*'([a-z-]+)'", m.group(1))
    assert dict(entries) == NODE_TYPE_SHAPES
    assert len(entries) == 8


def test_v1_unknown_and_missing_type_fall_back_to_ellipse():
    js = _xdata_js()
    m = re.search(r"nodeTypeShape\(mt\)\s*\{[^}]*\}", js)
    assert m, "nodeTypeShape fallback helper not found"
    assert "|| 'ellipse'" in m.group(0)


def test_v1_cystyle_node_block_binds_shape_to_node_shape_data():
    js = _xdata_js()
    m = re.search(
        r"selector:\s*'node',\s*style:\s*Object\.assign\(\{([\s\S]*?),\s*level !== 'minimal'",
        js,
    )
    assert m, "always-applied node style block not located"
    block = m.group(1)
    assert "'shape': 'data(node_shape)'" in block
    # size encoding stays in the same always-applied block
    assert "mapData(importance, 0, 1, 12, 48)" in block


def test_v1_shape_survives_minimal_density_level():
    js = _xdata_js()
    cy = re.search(r"cyStyle\(level\)\s*\{([\s\S]*?)\},\s*renderGraph", js)
    assert cy, "cyStyle body not found"
    body = cy.group(1)
    shape_pos = body.find("'shape': 'data(node_shape)'")
    minimal_pos = body.find("level !== 'minimal'")
    assert shape_pos != -1 and minimal_pos != -1
    assert shape_pos < minimal_pos, "shape binding must precede the minimal-label conditional"


def test_v1_mapped_shapes_within_accepted_vocabulary():
    for shape in NODE_TYPE_SHAPES.values():
        assert shape in VOCAB, shape


def test_v1_shape_strings_present_in_vendored_cytoscape_bundle():
    src = _source()
    m = re.search(r"vendor[\\/]cytoscape-([\d.]+)\.min\.js", src)
    assert m, "vendored cytoscape reference not found in panel"
    bundle = PLUGIN_ROOT / "webui" / "vendor" / ("cytoscape-%s.min.js" % m.group(1))
    assert bundle.exists(), bundle
    text = bundle.read_text(encoding="utf-8", errors="ignore")
    for shape in NODE_TYPE_SHAPES.values():
        assert shape in text, shape


def test_v1_loader_shell_remains_untouched_vs_git_head():
    assert SHELL.exists(), SHELL
    r = subprocess.run(
        ["git", "status", "--porcelain", "--", str(SHELL_REL)],
        cwd=PLUGIN_ROOT, capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip("git unavailable for loader-shell identity check")
    assert r.stdout.strip() == "", "extensions loader shell was modified"


# ------------------------------------------------------------------ V-4 pins

def test_v4_cluster_map_computed_from_already_received_data():
    js = _xdata_js()
    assert "this._clusterMap = this.computeClusters();" in js
    m = re.search(r"computeClusters\(\)\s*\{([\s\S]*?)\},\s*get clusterLegend", js)
    assert m, "computeClusters body not found"
    body = m.group(1)
    assert "this.nodes" in body and "this.edges" in body
    assert "fetch(" not in body and "/api/" not in body, "clustering must stay client-side"


def test_v4_deterministic_component_ordering_by_min_node_index():
    js = _xdata_js()
    m = re.search(r"\.sort\(\(x, y\) => Math\.min\.apply\(null, members\[x\]\) - Math\.min\.apply\(null, members\[y\]\)\)", js)
    assert m, "deterministic min-index component sort not found"
    assert "CLUSTER_COLORS[ci % this.CLUSTER_COLORS.length]" in js


def test_v4_single_component_graphs_stay_calm():
    js = _xdata_js()
    assert "if (roots.length <= 1) return {};" in js
    m = re.search(r"selector:\s*'node\[cluster_active\]',\s*style:\s*\{([^}]*)\}", js)
    assert m, "cluster override selector not found"
    assert "'background-color': 'data(cluster_color)'" in m.group(1)


def test_v4_palette_disjoint_from_edge_fallback_colors():
    js = _xdata_js()
    m = re.search(r"CLUSTER_COLORS:\s*\[([^\]]*)\]", js)
    assert m, "CLUSTER_COLORS palette not found"
    panel_colors = re.findall(r"'(#[0-9a-fA-F]{6})'", m.group(1))
    assert panel_colors == CLUSTER_COLORS
    assert set(panel_colors).isdisjoint(EDGE_FALLBACK_HEXES)


def test_v4_legend_cluster_rows_additive_and_gated():
    src = _source()
    assert 'x-if="clusterLegend.length > 1"' in src, "cluster legend gating missing"
    assert "Clusters" in src and "component " in src and "c.size" in src
    # additive: pre-existing V-7 legend rows remain present
    assert "Edge types" in src
    assert "node size = importance" in src
    assert "badge = memory_type" in src


# ------------------------------------------------- functional (Node runtime)

def _node_eval(expr: str, setup: str = ""):
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        pytest.skip("node runtime unavailable; functional pins require it")
    js = _xdata_js()
    script = (
        "const scope = eval('(' + process.env.OBJ + ')');\n"
        + (setup + "\n" if setup else "")
        + "console.log(JSON.stringify(eval(process.env.EXPR)));"
    )
    env = dict(os.environ, OBJ="{" + js + "}", EXPR=expr, SETUP=setup)
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    assert lines, "no functional output"
    return json.loads(lines[-1])


_SETUP_ISOLATED = "scope.nodes = [{doc_id: 'a'}, {doc_id: 'b'}, {doc_id: 'c'}]; scope.edges = []; scope._clusterMap = scope.computeClusters.call(scope);"


def test_fn_v1_shape_mapping_and_fallback():
    expr = ("[scope.nodeTypeShape('task'), scope.nodeTypeShape('episode'), "
            "scope.nodeTypeShape('concept'), scope.nodeTypeShape('summary'), "
            "scope.nodeTypeShape('definitely_not_a_type'), scope.nodeTypeShape('')]")
    out = _node_eval(expr, _SETUP_ISOLATED)
    assert out == ["diamond", "round-rectangle", "ellipse", "star", "ellipse", "ellipse"]


def test_fn_v4_two_components_deterministic_colors_and_sizes():
    setup = (
        "scope.nodes = [{doc_id: 'a'}, {doc_id: 'b'}, {doc_id: 'x'}, {doc_id: 'y'}];"
        "scope.edges = [{from_id: 'a', to_id: 'b'}, {from_id: 'x', to_id: 'y'}];"
        "scope._clusterMap = scope.computeClusters.call(scope);"
    )
    legend = _node_eval("scope.clusterLegend", setup)
    assert legend == [
        {"color": CLUSTER_COLORS[0], "size": 2},
        {"color": CLUSTER_COLORS[1], "size": 2},
    ]
    ids = _node_eval(
        "[scope._clusterMap['a'].cluster_id, scope._clusterMap['y'].cluster_id, "
        "scope._clusterMap['a'].cluster_color, scope._clusterMap['y'].cluster_color]",
        setup,
    )
    assert ids == [0, 1, CLUSTER_COLORS[0], CLUSTER_COLORS[1]]


def test_fn_v4_single_component_returns_empty_map():
    setup = (
        "scope.nodes = [{doc_id: 'a'}, {doc_id: 'b'}, {doc_id: 'c'}];"
        "scope.edges = [{from_id: 'a', to_id: 'b'}, {from_id: 'b', to_id: 'c'}];"
    )
    out = _node_eval("scope.computeClusters.call(scope)", setup)
    assert out == {}
    legend = _node_eval("scope.clusterLegend", setup + " scope._clusterMap = scope.computeClusters.call(scope);")
    assert legend == []


def test_fn_v4_palette_cycles_deterministically_beyond_ten_components():
    nodes = ", ".join("{doc_id: 'n%d'}" % i for i in range(12))
    setup = "scope.nodes = [%s]; scope.edges = []; scope._clusterMap = scope.computeClusters.call(scope);" % nodes
    out = _node_eval("[scope._clusterMap['n10'].cluster_color, scope.clusterLegend.length]", setup)
    assert out == [CLUSTER_COLORS[0], 12]  # 11th component cycles back to first palette color


def test_fn_v4_disconnected_and_filtered_edges_ignored():
    # edges referencing nodes outside this.nodes must not corrupt clustering
    setup = (
        "scope.nodes = [{doc_id: 'a'}, {doc_id: 'b'}];"
        "scope.edges = [{from_id: 'a', to_id: 'ghost'}, {from_id: 'b', to_id: 'a'}];"
        "scope._clusterMap = scope.computeClusters.call(scope);"
    )
    out = _node_eval("[Object.keys(scope._clusterMap).length, scope.clusterLegend.length]", setup)
    assert out == [0, 0]  # single component after ignoring the ghost edge


def test_fn_cystyle_shape_present_at_all_density_levels():
    for level in ("full", "simplified", "minimal"):
        expr = ("(scope.cyStyle.call(scope, '%s').find(function (s) "
                "{ return s.selector === 'node'; }).style).shape" % level)
        out = _node_eval(expr)
        assert out == "data(node_shape)", level
