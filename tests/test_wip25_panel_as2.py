"""WI-P25-AS-SLICE2 — panel-side audit remediation (V-7, V-5, V-8).

Pins the bounded panel-side changes applied to
webui/right-canvas-panels/graph-panel.html:

* V-7 — an in-canvas legend maps the 8 relationship-type edge colors by
  referencing the SAME CSS variables as the edge palette (single source via
  REL_TYPE_COLORS / relTypeColor(); no duplicated hex), plus node badge
  meaning (size = importance, badge = memory_type); zoom controls
  (zoom in / out / fit) clamped to the existing minZoom 0.2 / maxZoom 3.
* V-5 — bounded density mitigation: a pure decision function densityLevel()
  selects a style level (full / simplified / minimal); degradation is
  style-only (label drops), never data culling, never API/helper/data-model
  changes; full level preserves the pre-P25 style output exactly; theme
  re-styling preserves the active level.
* V-8 — a subtle, theme-aware dot-lattice background derived from
  var(--color-primary) via color-mix; legibility of nodes/edges/labels
  unaffected in either theme (no literal theme-specific colors).

Guard pins: no functionality removed, no new endpoints, no material-symbols
ligature additions (WI-P24 C5 rule), graph-panel.css untouched.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"
CSS = PLUGIN_ROOT / "webui" / "graph-panel.css"

REL_TYPES = [
    "supports", "contradicts", "depends_on", "derived_from",
    "related_to", "precedes", "follows", "part_of",
]


def _source() -> str:
    return PANEL.read_text(encoding="utf-8")


def _xdata_js() -> str:
    src = _source()
    m = re.search(r'x-data="\{([\s\S]*)\}" x-init=', src)
    assert m, "x-data block not found"
    return m.group(1)


def _legend_block() -> str:
    src = _source()
    start = src.index('class="nc-cy-legend"')
    end = src.index('class="nc-cy-zoom"', start)
    return src[start:end]


def _node_eval(script: str) -> str:
    return subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    ).stdout.strip()


# ------------------------------------------------------- V-7 legend

def test_v7_legend_exists_and_lists_all_8_rel_types():
    """Legend renders via x-for over the exact 8-type mapping (single source:
    the legend derives its rows from REL_TYPE_COLORS itself, so all 8 types
    are covered without duplicating the enumeration)."""
    legend = _legend_block()
    assert "Object.keys(REL_TYPE_COLORS)" in legend, (
        "legend rows must iterate the REL_TYPE_COLORS mapping (single source)"
    )
    assert "x-for" in legend
    js = _xdata_js()
    m = re.search(r"REL_TYPE_COLORS\s*:\s*\{([\s\S]*?)\}", js)
    assert m
    types = set(re.findall(r"([a-z_]+)\s*:\s*\[", m.group(1)))
    assert types == set(REL_TYPES), "mapping must remain exactly the 8 types"


def test_v7_legend_swatch_uses_reltypecolor_css_vars():
    """Swatch colors resolve through relTypeColor() — the same CSS-variable
    lookup the edges use — so no hex values are duplicated in the legend."""
    legend = _legend_block()
    assert ":style=" in legend and "relTypeColor(rt)" in legend, (
        "legend swatches must reference relTypeColor(rt)"
    )
    hex_hits = re.findall(r"#[0-9a-fA-F]{3,8}", legend)
    assert not hex_hits, f"legend must not duplicate hex values: {hex_hits}"


def test_v7_legend_node_badge_meanings_present():
    legend = _legend_block()
    assert "importance" in legend, "legend must explain node size = importance"
    assert "memory_type" in legend, "legend must explain badge = memory_type"


def test_v7_legend_markup_present_in_canvas():
    src = _source()
    assert 'class="nc-cy-legend"' in src
    # hidden when there is nothing to show
    assert re.search(r'x-show="nodes\.length\s*>\s*0"', _legend_block())


# ------------------------------------------------------- V-7 zoom controls

def test_v7_zoom_methods_exist_and_are_clamped():
    js = _xdata_js()
    for meth in ("zoomIn()", "zoomOut()", "fitToView()"):
        assert meth in js, f"{meth} must exist"
    # clamped to the existing minZoom/maxZoom config (0.2 / 3 declared below)
    zi = re.search(r"zoomIn\(\)\s*\{[^}]*\}", js).group(0)
    zo = re.search(r"zoomOut\(\)\s*\{[^}]*\}", js).group(0)
    assert "Math.min(" in zi and "cy.maxZoom()" in zi, (
        "zoomIn must clamp against cy.maxZoom()"
    )
    assert "Math.max(" in zo and "cy.minZoom()" in zo, (
        "zoomOut must clamp against cy.minZoom()"
    )
    assert "minZoom: 0.2" in js and "maxZoom: 3" in js, (
        "existing zoom bounds must remain unchanged"
    )
    assert re.search(r"fitToView\(\)\s*\{[^}]*cy\.fit\(", js, re.S), (
        "fitToView must call cy.fit()"
    )


def test_v7_zoom_buttons_wired():
    src = _source()
    start = src.index('class="nc-cy-zoom"')
    zoom = src[start:start + 700]
    assert '@click="zoomIn()"' in zoom
    assert '@click="zoomOut()"' in zoom
    assert '@click="fitToView()"' in zoom


def test_v7_zoom_no_cy_guard():
    js = _xdata_js()
    for meth in ("zoomIn", "zoomOut", "fitToView"):
        m = re.search(meth + r"\(\)\s*\{[^}]*\}", js)
        assert m and "if (!this.cy) return;" in m.group(0), (
            f"{meth} must guard against missing cy instance"
        )


# ------------------------------------------------------- V-5 density mitigation

def _extract_fn(name: str) -> str:
    js = _xdata_js()
    m = re.search(name + r"\([^)]*\)\s*\{", js)
    assert m, f"{name} not found"
    start = m.start()
    i = js.index("{", start)
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[start:j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _density_levels() -> str:
    """Execute the real densityLevel() from the panel source in node."""
    fn = _extract_fn("densityLevel")
    script = (
        "const o = { " + fn + " };\n"
        "const cases = [[0,0,'full'],[200,400,'full'],[201,400,'simplified'],"
        "[200,401,'simplified'],[500,1000,'simplified'],[501,1000,'minimal'],"
        "[500,1001,'minimal'],[150,50,'full'],[10,900,'simplified']];\n"
        "let out = [];\n"
        "for (const [n,e,want] of cases) {\n"
        "  const got = o.densityLevel(n, e);\n"
        "  if (got !== want) { console.error('FAIL', n, e, got, '!=', want); process.exit(1); }\n"
        "  out.push(got);\n"
        "}\n"
        "console.log('ok: ' + out.join(','));"
    )
    return _node_eval(script)


def test_v5_density_level_thresholds_executed():
    """Unit-test the threshold DECISION LOGIC (not raw rendering): the real
    densityLevel() function must classify full/simplified/minimal exactly at
    the documented bounded thresholds."""
    assert _density_levels().startswith("ok: ")


def test_v5_density_level_pure_and_documented():
    fn = _extract_fn("densityLevel")
    # purity: only reads its two arguments, no this/state access
    assert "this." not in fn, "densityLevel must be a pure function of (nc, ec)"
    # discoverable/documented in code comments (bounded mitigation)
    js = _xdata_js()
    assert "WI-P25 V-5" in js, "density mitigation must be documented in a comment"
    ok_comment = (
        re.search(r"WI-P25 V-5[\s\S]{0,600}densityLevel", js)
        or re.search(r"densityLevel[\s\S]{0,600}WI-P25 V-5", js)
        or re.search(r"V-5[\s\S]{0,400}(thresholds?|bounded|simplified|minimal)", js)
    )
    assert ok_comment, "comment must describe the bounded thresholds and behavior"


def test_v5_levels_style_only_no_culling():
    """Degradation must be style-only: the density path applies a style level
    via cy.style(); no element culling and no API/helper/data-model contact."""
    render = _body_between("renderGraph() {", "watchTheme()")
    assert "densityLevel(nCount, eCount)" in render
    assert "cy.style().fromJson(this.cyStyle(level))" in render, (
        "renderGraph must apply the level via style only"
    )
    # the only removal is the pre-existing render reset, allowed
    stripped = render.replace("this.cy.elements().remove();", "")
    for banned in (".remove()", "cy.remove(", "cy.batch("):
        assert banned not in stripped
    # no fetch/API calls in the density path
    assert "fetch(" not in render


def _body_between(anchor: str, next_anchor: str) -> str:
    src = _source()
    start = src.index(anchor)
    end = src.index(next_anchor, start)
    return src[start:end]


def test_v5_full_level_preserves_pre_p25_style_output():
    """The default/'full' level must produce the exact pre-P25 style sets:
    node labels + edge labels + text-rotation all present."""
    js = _xdata_js()
    assert "cyStyle(level)" in js and "level = level || 'full'" in js
    assert "'label': 'data(label)'" in js
    assert "'label': 'data(rel_type)'" in js  # WI-P24 C2 rule retained at full
    assert "'text-rotation': 'autorotate'" in js


def test_v5_simplified_and_minimal_drop_labels_only():
    """simplified drops edge labels; minimal additionally drops node labels;
    size/weight encodings (mapData) are preserved at every level."""
    fn = _extract_fn("cyStyle")
    script = (
        "global.getComputedStyle = () => ({ getPropertyValue: (p) => '' });\n"
        "global.document = { documentElement: {} };\n"
        "const o = { " + fn + " };\n"
        "const st = (s, sel) => s.find(x => x.selector === sel).style;\n"
        "const full = o.cyStyle('full'), simp = o.cyStyle('simplified'), mini = o.cyStyle('minimal');\n"
        "const checks = [\n"
        "  ['label' in st(full,'node'), true], ['label' in st(full,'edge'), true],\n"
        "  ['label' in st(simp,'node'), true], ['label' in st(simp,'edge'), false],\n"
        "  ['label' in st(mini,'node'), false], ['label' in st(mini,'edge'), false],\n"
        "  [st(mini,'node')['width'], 'mapData(importance, 0, 1, 12, 48)'],\n"
        "  [st(mini,'edge')['width'], 'mapData(weight, 0, 1, 1, 6)'],\n"
        "  [st(o.cyStyle(),'edge')['label'], 'data(rel_type)']\n"
        "];\n"
        "for (const [got, want] of checks) {\n"
        "  if (got !== want) { console.error('FAIL', JSON.stringify(got), '!=', JSON.stringify(want)); process.exit(1); }\n"
        "}\n"
        "console.log('ok levels');"
    )
    assert _node_eval(script) == "ok levels"


def test_v5_theme_restyle_preserves_active_level():
    js = _xdata_js()
    wt = _body_between("watchTheme() {", "get lastUpdated")
    assert "this._densityLevel || 'full'" in wt, (
        "theme mutation re-style must preserve the active density level"
    )
    assert "_densityLevel = level" in _body_between("renderGraph() {", "watchTheme()")


# ------------------------------------------------------- V-8 background polish

def test_v8_background_lattice_theme_aware():
    """Subtle dot-lattice background derived from var(--color-primary) via
    color-mix — resolves automatically in both dark and light themes; no
    literal theme-specific hex in the canvas rule."""
    src = _source()
    rule = _body_between(".nc-graph-canvas {", "background-size: 18px 18px; }")
    assert "background-image" in rule and "radial-gradient" in rule
    assert "color-mix(in srgb, var(--color-primary)" in rule, (
        "lattice color must derive from the theme primary variable"
    )
    assert "background-size: 18px 18px;" in src
    # no theme-specific hex in the canvas background rule
    assert not re.search(r"#[0-9a-fA-F]{3,8}", rule), (
        "canvas background must not hardcode theme colors"
    )
    assert "WI-P25 V-8" in src, "background change must be documented"


def test_v8_dark_and_light_reachability():
    """The var-driven lattice applies in either theme: the canvas rule uses
    only theme variables and the panel never redefines --color-primary."""
    rule = _body_between(".nc-graph-canvas {", "background-size: 18px 18px; }")
    assert "var(--color-primary)" in rule
    # panel itself must not override --color-primary per theme (would break
    # the single-source theme derivation)
    assert not re.search(r"--color-primary\s*:", _source()), (
        "panel must not redefine --color-primary"
    )


# ------------------------------------------------------- Guards

def test_guard_no_new_endpoints():
    src = _source()
    endpoints = set(re.findall(r"/api/plugins/neuro_core/[a-z_]+", src))
    assert endpoints <= {
        "/api/plugins/neuro_core/context_graph",
        "/api/plugins/neuro_core/relationships",
        "/api/plugins/neuro_core/memory_subdirs",
        "/api/plugins/neuro_core/projects",
        "/api/plugins/neuro_core/advanced_filters",
    }, f"unexpected API endpoint usage: {endpoints}"


def test_guard_no_ligature_additions():
    head = subprocess.run(
        ["git", "-C", str(PLUGIN_ROOT), "show", "HEAD:webui/right-canvas-panels/graph-panel.html"],
        capture_output=True, text=True, check=True).stdout
    assert _source().count("material-symbols-outlined") == head.count("material-symbols-outlined")


def test_guard_functionality_intact():
    src = _source()
    for anchor in ("deleteEdge", "addEdge", "openAddForm", "relLabel", "search()",
                   "applyAdvancedFilters", "watchTheme", "cyResizeObserver",
                   "saveSubdir", "removeSubdir", "loadSubdirOptions"):
        assert anchor in src, f"existing functionality anchor {anchor} must remain"


def test_guard_css_file_untouched():
    diff = subprocess.run(
        ["git", "-C", str(PLUGIN_ROOT), "diff", "HEAD", "--", "webui/graph-panel.css"],
        capture_output=True, text=True, check=True).stdout
    assert diff == "", "graph-panel.css must remain untouched"


def test_guard_manual_editing_flows_untouched():
    """Manual assignment/editing in the panel stays intact (add-edge form and
    delete flow are still reachable and unmodified in their contracts)."""
    js = _xdata_js()
    add = _body_between("async addEdge() {", "relLabel(rel)")
    assert "to_id is required" in add
    assert "method: 'POST'" in js
    assert "/api/plugins/neuro_core/relationships" in js
    delh = _body_between("async deleteEdge(rel) {", "openAddForm")
    assert "method: 'DELETE'" in delh
