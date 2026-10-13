"""WI-P31-AS-LIVE-DEFECT-BATCH — regression tests for the six live-verified
defects found by HITL live verification 2026-09-23 (post commit 75043ff).

Covers the panel-side fixes applied to
webui/right-canvas-panels/graph-panel.html (content copy only; the
extensions/ shell copy must remain byte-identical):

* KI-018-BJ (HIGH) — V-1 node shapes were invisible live because the WI-P28
  NODE_TYPE_SHAPES map covers the spec-draft vocabulary only, while the
  implemented backend enum is MemoryType (helpers/metadata.py): fact, concept,
  task, event, decision, skill, preference, note. The six live types absent
  from the P28 map fell back to ellipse, visually indistinguishable from a
  circle at width==height. Fix: supplemental NODE_TYPE_SHAPES_LIVE map keyed
  by the six implemented-enum values (verified present in the vendored
  cytoscape bundle, disjoint from all 8 P28 shapes), checked first in
  nodeTypeShape(); P28-pinned outputs and the || 'ellipse' fallback unchanged.
* KI-018-BK (MEDIUM) — a single node is not a cluster (HITL direction):
  singleton components are excluded from legend rows AND from cluster
  coloring (they keep the calm base color) via realClusters (2+ node
  components only); the legend is collapsible and row-capped at 6 rows with
  a '+k more clusters…' overflow row so it can no longer cover the pane.
* KI-018-BL (LOW) — the Edge Inspector plate dismisses on any click outside
  the plate (graph background, canvas chrome, elsewhere in the document), not
  only when another node is tapped; re-click of the same edge also dismisses.
* KI-018-BM (LOW) — dormant display hooks for custom Memory Names (KI-023
  dependency, not yet implemented): from/to name rows render only when a
  name field is present in the data; today they stay hidden.
* KI-018-BN (LOW) — the plate button is dismiss-only (verified from source:
  @click="inspectEdge = null"; edge deletion is the separate deleteEdge()
  flow in the node inspector), so per HITL rule it uses non-destructive
  Exit/✕ styling, explicitly NOT red/STOP-style, with wording that makes
  clear the edge is not deleted.
* KI-018-BO (HIGH) — NOT fixed panel-side: the root cause is the backend
  read path (GraphStore.neighbors() BFS drops parallel edges to
  already-visited targets), outside this WI's R2 write boundary; recorded in
  impact-discovery.yaml. NO panel half-fix is implemented (it would not
  survive the next search() refresh and would mask the backend defect).
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
SHELL = PLUGIN_ROOT / "extensions" / "webui" / "right-canvas-panels" / "graph-panel.html"

# Implemented backend enum values absent from the WI-P28 map (diagnosis:
# helpers/metadata.py MemoryType; only concept and task overlap).
LIVE_ENUM_SHAPES = {
    "fact": "round-tag",
    "event": "round-diamond",
    "decision": "cut-rectangle",
    "skill": "round-hexagon",
    "preference": "rhomboid",
    "note": "barrel",
}

# WI-P28 map, restated here for disjointness/priority checks.
P28_SHAPES = {
    "concept": "ellipse",
    "episode": "round-rectangle",
    "task": "diamond",
    "solution": "hexagon",
    "reflection": "pentagon",
    "fragment": "rectangle",
    "observation": "octagon",
    "summary": "star",
}

BK_VISIBLE_ROWS = 6


def _source() -> str:
    return PANEL.read_text(encoding="utf-8")


def _xdata_js() -> str:
    src = _source()
    m = re.search(r'x-data="\{([\s\S]*)\}" x-init=', src)
    assert m, "x-data block not found"
    return m.group(1)


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


# ---------------------------------------------------------------- KI-018-BJ


def test_bj_live_map_pinned_exactly():
    src = _source()
    m = re.search(r"NODE_TYPE_SHAPES_LIVE:\s*\{([^}]*)\}", src)
    assert m, "NODE_TYPE_SHAPES_LIVE map not found"
    entries = re.findall(r"(\w+):\s*'([a-z-]+)'", m.group(1))
    assert dict(entries) == LIVE_ENUM_SHAPES
    assert len(entries) == 6


def test_bj_live_shapes_disjoint_from_p28_shapes_and_each_other():
    assert set(LIVE_ENUM_SHAPES.values()).isdisjoint(set(P28_SHAPES.values()))
    assert len(set(LIVE_ENUM_SHAPES.values())) == 6


def test_bj_live_shapes_present_in_vendored_cytoscape_bundle():
    src = _source()
    m = re.search(r"vendor[\\/]cytoscape-([\d.]+)\.min\.js", src)
    assert m, "vendored cytoscape reference not found in panel"
    bundle = PLUGIN_ROOT / "webui" / "vendor" / ("cytoscape-%s.min.js" % m.group(1))
    assert bundle.exists(), bundle
    text = bundle.read_text(encoding="utf-8", errors="ignore")
    for shape in LIVE_ENUM_SHAPES.values():
        assert shape in text, shape


def test_bj_nodetypeshape_checks_live_map_first_and_preserves_fallback():
    js = _xdata_js()
    m = re.search(r"nodeTypeShape\(mt\)\s*\{[^}]*\}", js)
    assert m, "nodeTypeShape helper not found"
    body = m.group(0)
    live_pos = body.find("NODE_TYPE_SHAPES_LIVE")
    p28_pos = body.find("NODE_TYPE_SHAPES[")
    assert live_pos != -1 and p28_pos != -1
    assert live_pos < p28_pos, "live map must be checked first"
    assert "|| 'ellipse'" in body, "P28 fallback must be preserved"


def test_bj_fn_live_enum_shapes_take_priority_and_p28_outputs_unchanged():
    out = _node_eval(
        "["
        "scope.nodeTypeShape('fact'), scope.nodeTypeShape('event'), "
        "scope.nodeTypeShape('decision'), scope.nodeTypeShape('skill'), "
        "scope.nodeTypeShape('preference'), scope.nodeTypeShape('note')]",
    )
    assert out == list(LIVE_ENUM_SHAPES.values())
    out2 = _node_eval(
        "[scope.nodeTypeShape('task'), scope.nodeTypeShape('episode'), "
        "scope.nodeTypeShape('concept'), scope.nodeTypeShape('summary'), "
        "scope.nodeTypeShape('definitely_not_a_type'), scope.nodeTypeShape('')]",
    )
    assert out2 == ["diamond", "round-rectangle", "ellipse", "star", "ellipse", "ellipse"]


# ---------------------------------------------------------------- KI-018-BK


def test_bk_realclusters_excludes_single_node_components():
    setup = (
        "scope.nodes = [{doc_id: 'a'}, {doc_id: 'b'}, {doc_id: 'x'}];"
        "scope.edges = [{from_id: 'a', to_id: 'b'}];"
        "scope._clusterMap = scope.computeClusters.call(scope);"
    )
    out = _node_eval("scope.realClusters", setup)
    assert len(out) == 1, "only the 2-node component is a real cluster"
    assert out[0]["size"] == 2


def test_bk_fn_all_singleton_components_excluded_from_realclusters():
    setup = "scope.nodes = [{doc_id: 'a'}, {doc_id: 'b'}, {doc_id: 'c'}]; scope.edges = []; scope._clusterMap = scope.computeClusters.call(scope);"
    out = _node_eval("scope.realClusters", setup)
    assert out == [], "singleton components are not clusters"


def test_bk_single_node_component_keeps_base_color_in_render():
    js = _xdata_js()
    m = re.search(r"renderGraph\(\)\s*\{", js)
    assert m, "renderGraph not found"
    tail = js[m.end():]
    cl_gate = re.search(r"cluster_size >= 2\) \? rawCl : null", tail)
    assert cl_gate, "singleton components must not get cluster_color in renderGraph"


def test_bk_legend_rows_driven_by_realclusters_and_capped():
    src = _source()
    m = re.search(r"realClusters\.slice\(0, %d\)" % BK_VISIBLE_ROWS, src)
    assert m, "legend rows must be capped via realClusters.slice(0, 6)"
    overflow = re.search(r"'\+' \+ \(realClusters\.length - %d\) \+ ' more clusters" % BK_VISIBLE_ROWS, src)
    assert overflow, "'+k more clusters' overflow row missing"
    assert "x-show=\"realClusters.length > 0\"" in src, "legend hidden when no real clusters"


def test_bk_fn_legend_overflow_counts_twelve_pair_components():
    # 12 real (2-node) components => 12 candidate rows; visible cap is 6, overflow 6.
    nodes, edges = [], []
    for i in range(12):
        a, b = "a%02d" % i, "b%02d" % i
        nodes.append("{doc_id: '%s'}" % a)
        nodes.append("{doc_id: '%s'}" % b)
        edges.append("{from_id: '%s', to_id: '%s'}" % (a, b))
    setup = "scope.nodes = [%s]; scope.edges = [%s]; scope._clusterMap = scope.computeClusters.call(scope);" % (
        ", ".join(nodes), ", ".join(edges))
    out = _node_eval(
        "[scope.realClusters.length, scope.realClusters.slice(0, 6).length, scope.realClusters.length - 6]",
        setup,
    )
    assert out == [12, BK_VISIBLE_ROWS, 6]


# ---------------------------------------------------------------- KI-018-BL


def test_bl_plate_has_stable_id_and_outside_click_dismiss_handler():
    src = _source()
    assert 'id="nc-edge-plate"' in src
    js = _xdata_js()
    assert "_edgePlateOutside" in js, "outside-click dismiss handler missing"
    m = re.search(r"document\.addEventListener\('mousedown', self\._edgePlateOutside\)", js)
    assert m, "outside mousedown listener not registered"
    assert "plate.contains(ev.target)" in js, "clicks inside the plate must not dismiss"


def test_bl_reclick_same_edge_dismisses_plate():
    js = _xdata_js()
    m = re.search(r"this\.cy\.on\('tap', 'edge'[\s\S]*?\}\);", js)
    assert m, "edge tap handler not found"
    body = m.group(0)
    assert "const same = self.inspectEdge &&" in body
    assert "self.inspectEdge = same ? null : {" in body


def test_bl_background_tap_dismisses_edge_plate():
    js = _xdata_js()
    m = re.search(r"this\.cy\.on\('tap', function\(evt\) \{[\s\S]*?\}\);", js)
    assert m, "background tap handler not found"
    assert "self.inspectEdge = null" in m.group(0)


# ---------------------------------------------------------------- KI-018-BM


def test_bm_dormant_name_rows_render_only_when_names_present():
    src = _source()
    assert 'x-show="inspectEdge.from_name"' in src
    assert 'x-show="inspectEdge.to_name"' in src
    js = _xdata_js()
    m = re.search(r"self\.inspectEdge = same \? null : \{([\s\S]*?)\};", js)
    assert m, "edge inspect payload not found"
    body = m.group(1)
    assert "from_name: ed.data('from_name') || ''" in body
    assert "to_name: ed.data('to_name') || ''" in body


def test_bm_edge_data_carry_from_to_names_when_available():
    js = _xdata_js()
    m = re.search(r"group: 'edges', data: \{([\s\S]*?)\} \}\);", js)
    assert m, "edge element builder not found"
    body = m.group(1)
    assert "from_name" in body and "to_name" in body


# ---------------------------------------------------------------- KI-018-BN


def test_bn_plate_button_is_dismiss_only_and_not_destructive():
    src = _source()
    plate = re.search(r'id="nc-edge-plate"[\s\S]*?\n\s*</template>\n\s*</div>', src)
    assert plate, "edge plate region not found"
    body = plate.group(0)
    assert "the edge is NOT deleted" in body, "dismissing wording must make non-deletion explicit"
    assert "&times;" in body, "Exit/✕ dismiss button missing"
    assert "Close edge" not in body, "ambiguous 'Close edge' wording must be gone"
    assert "Delete edge" not in body, "destructive wording must not appear on the dismiss button"
    # Non-destructive: the dismiss button must not use the error/STOP color scheme.
    btn = re.search(r"<button @click=\"inspectEdge = null\"[\s\S]*?</button>", body)
    assert btn, "dismiss button not found"
    assert "color-error" not in btn.group(0), "dismiss button must not use red/STOP styling"
    assert "Delete" not in btn.group(0), "dismiss button must not carry delete wording"


# ------------------------------------------------------------ shared pins


def test_shell_copy_remains_byte_identical_to_git_head():
    assert SHELL.exists(), SHELL
    r = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", str(SHELL.relative_to(PLUGIN_ROOT))],
        cwd=PLUGIN_ROOT, capture_output=True, text=True,
    )
    assert r.returncode == 0, "extensions/ shell copy must remain byte-identical"


def test_panel_xdata_parses_under_node():
    js = _xdata_js()
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        pytest.skip("node runtime unavailable; parse check requires it")
    p = Path("/tmp/p31_test_xdata.js")
    p.write_text("({" + js + "})", encoding="utf-8")
    r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
