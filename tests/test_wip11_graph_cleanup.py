"""WI-P11-GRAPH-CLEANUP tests (KI-018-AG/AH/AI/AJ).

Covers:
- KI-018-AI: the advanced-filter relationship-type dropdown lists the FULL
  RelationshipType vocabulary from helpers/graph_store.py (8 values incl.
  part_of) — dropdown options == store enum, no more, no less.
- KI-018-AJ: cytoscape is vendored locally under webui/vendor/ and referenced
  via the plugin-asset route; the panel contains zero external script refs.
- KI-018-AG: the minScore threshold slider is consumed by renderGraph —
  behavioral harness executes the panel's x-data scope with a stubbed cy
  container and verifies nodes below the threshold are excluded and edges
  are kept only when both endpoints survive.
- KI-018-AH: webui/graph-store.js is removed (dormant divergent code) and
  no doc surface still describes it as the panel's Alpine store.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL = PLUGIN_ROOT / "webui/right-canvas-panels/graph-panel.html"
VENDOR = PLUGIN_ROOT / "webui/vendor/cytoscape-3.30.2.min.js"
STORE_JS = PLUGIN_ROOT / "webui/graph-store.js"
README = PLUGIN_ROOT / "README.md"
ARCH = PLUGIN_ROOT / "docs/architecture.md"


def _panel_text() -> str:
    return PANEL.read_text(encoding="utf-8")


def _xdata_js() -> str:
    m = re.search(r'x-data="([^"]+)"', _panel_text())
    assert m, "panel x-data scope not found"
    return m.group(1).replace("&quot;", '"').replace("&amp;", "&")


# ---------------------------------------------------------------------------
# KI-018-AI — dropdown vocabulary completeness
# ---------------------------------------------------------------------------

def test_dropdown_lists_full_store_vocabulary():
    from usr.plugins.neuro_core.helpers.graph_store import RelationshipType

    expected = sorted(v.value for v in RelationshipType)
    assert "part_of" in expected

    text = _panel_text()
    m = re.search(r'x-for="r in \[([^\]]+)\]"', text)
    assert m, "relationship-type dropdown template not found"
    listed = sorted(x.strip("'") for x in m.group(1).split(","))
    assert listed == expected, f"dropdown {listed} != store enum {expected}"


def test_part_of_present_in_dropdown():
    assert "'part_of'" in _panel_text()


# ---------------------------------------------------------------------------
# KI-018-AJ — local vendored cytoscape, zero external references
# ---------------------------------------------------------------------------

def test_vendor_file_exists_and_is_cytoscape():
    assert VENDOR.is_file(), "vendored cytoscape missing"
    head = VENDOR.read_text(encoding="utf-8", errors="replace")[:400]
    assert "cytoscape" in head.lower()
    assert VENDOR.stat().st_size > 100_000


def test_panel_references_local_vendor_route_not_cdn():
    text = _panel_text()
    assert "/usr/plugins/neuro_core/webui/vendor/cytoscape-3.30.2.min.js" in text
    assert "cdn.jsdelivr" not in text
    assert not re.search(r'<script[^>]+src="https?://', text), (
        "panel still loads a script from an external origin"
    )


# ---------------------------------------------------------------------------
# KI-018-AG — minScore consumed by renderGraph (node-backed harness)
# ---------------------------------------------------------------------------

def _run_render_graph(nodes, edges, min_score):
    js = _xdata_js()
    harness = (
        "const document = { getElementById: () => null };\n"
        "const requestAnimationFrame = (fn) => fn();\n"
        "const localStorage = { getItem: () => null, setItem: () => {} };\n"
        "const scope = (" + js + ");\n"
        "scope.cy = { elements: () => ({ remove: () => {} }), "
        "add: (els) => { scope.__added = els; }, "
        "layout: () => ({ run: () => {} }), resize: () => {} };\n"
        "scope.minScore = " + repr(float(min_score)) + ";\n"
        "scope.nodes = " + json.dumps(nodes) + ";\n"
        "scope.edges = " + json.dumps(edges) + ";\n"
        "scope.renderGraph();\n"
        "const els = scope.__added;\n"
        "const nodes2 = els.filter(e => e.group === 'nodes')"
        ".map(e => e.data.id).sort();\n"
        "const edges2 = els.filter(e => e.group === 'edges')"
        ".map(e => [e.data.source, e.data.target]).sort();\n"
        "console.log(JSON.stringify({ nodes: nodes2, edges: edges2 }));\n"
    )
    r = subprocess.run(
        ["node", "-e", harness],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, f"harness failed: {r.stderr[-400:]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_minscore_filters_low_score_nodes_and_dangling_edges():
    nodes = [
        {"doc_id": "a", "content": "A", "score": 0.9},
        {"doc_id": "b", "content": "B", "score": 0.2},
        {"doc_id": "c", "content": "C", "score": 0.7},
    ]
    edges = [
        {"from_id": "a", "to_id": "c", "rel_type": "supports"},
        {"from_id": "a", "to_id": "b", "rel_type": "related_to"},
        {"from_id": "b", "to_id": "c", "rel_type": "precedes"},
    ]
    out = _run_render_graph(nodes, edges, 0.5)
    assert out["nodes"] == ["a", "c"], "low-score nodes must be excluded"
    assert out["edges"] == [["a", "c"]], (
        "edges touching filtered nodes must be excluded"
    )


def test_minscore_zero_keeps_everything():
    nodes = [
        {"doc_id": "a", "content": "A", "score": 0.1},
        {"doc_id": "b", "content": "B", "score": 0.05},
    ]
    edges = [{"from_id": "a", "to_id": "b", "rel_type": "related_to"}]
    out = _run_render_graph(nodes, edges, 0.0)
    assert out["nodes"] == ["a", "b"]
    assert out["edges"] == [["a", "b"]]


def test_minscore_slider_bound_and_consumed():
    text = _panel_text()
    assert 'x-model.number="minScore"' in text
    assert "nsc < this.minScore" in text


# ---------------------------------------------------------------------------
# KI-018-AH — graph-store.js removed, docs corrected
# ---------------------------------------------------------------------------

def test_graph_store_js_removed():
    assert not STORE_JS.exists(), "graph-store.js must be removed"


def test_docs_no_longer_describe_graph_store_as_panel_store():
    readme = README.read_text(encoding="utf-8")
    arch = ARCH.read_text(encoding="utf-8")
    assert "graph-store.js" not in readme, (
        "README still presents graph-store.js as a live WebUI asset"
    )
    for m in re.finditer(r"graph-store\.js", arch):
        ctx = arch[max(0, m.start() - 200): m.end() + 200]
        assert "removed" in ctx or "historical" in ctx, (
            "architecture.md graph-store.js mention is not framed as historical"
        )
