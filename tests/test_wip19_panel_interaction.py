"""WI-P19 — panel interaction batch (KI-018-BB/BC/BD/BA) pin tests.

Pins the four panel-side fixes applied to the content copy
(webui/right-canvas-panels/graph-panel.html):

* BB — direction-aware relationship labeling in the inspector Relationships
  list (rendering-side only; stored rel_type data is never rewritten, and
  deleteEdge keeps consuming the stored rel_type verbatim).
* BC — after a successful addEdge, the current search is re-run and the
  inspected node is re-focused/re-selected; the user's query is no longer
  overwritten, so nodes must not disappear and selection must not freeze.
* BD — the Add-edge form (to/rel/weight + messages) is reset when the
  inspected node changes.
* BA — the subdir dropdown composes 'projects/<name>' entries from the
  existing framework projects source (POST /api/projects action=list_options)
  alongside memory_subdirs results; graceful on projects-source failure.

The extensions shell copy stays the small pointer copy (WI-P5B) — untouched.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"
SHELL = PLUGIN_ROOT / "extensions" / "webui" / "right-canvas-panels" / "graph-panel.html"


def _body_between(anchor: str, next_anchor: str) -> str:
    src = PANEL.read_text(encoding="utf-8")
    start = src.index(anchor)
    end = src.index(next_anchor, start)
    return src[start:end]


def _xdata_js() -> str:
    src = PANEL.read_text(encoding="utf-8")
    m = re.search(r'x-data="\{([\s\S]*)\}" x-init=', src)
    assert m, "x-data block not found"
    return m.group(1)


# --------------------------------------------------------------------- BB

def test_bb_relationships_row_uses_direction_aware_label():
    """The inspector Relationships row label is direction-aware via
    relLabel(rel); the raw rel_type span no longer renders verbatim."""
    src = PANEL.read_text(encoding="utf-8")
    assert 'x-text="relLabel(rel)"' in src
    assert 'x-text="rel.relType"' not in src


def test_bb_rellabel_inverts_incoming_precedes():
    """relLabel() inverts incoming edges so a stored edge
    i5lKFdKfd2 --[precedes]--> jzKMhJuprm renders on the jzKMhJuprm side as
    'follows i5lKFdKfd2' semantics; outbound labels stay verbatim."""
    body = _body_between("relLabel(rel) {", "get relatedNodes() {")
    assert "direction === 'in'" in body
    assert "precedes: 'follows'" in body
    assert "follows: 'precedes'" in body
    assert "return rel.relType;" in body, "outbound labels must stay verbatim"


def test_bb_deleteedge_keeps_stored_rel_type_verbatim():
    """Rendering-side only: deleteEdge still sends the stored rel_type
    verbatim to the DELETE API — stored rel_type data is never rewritten."""
    body = _body_between("async deleteEdge(", "async openAddForm() {")
    assert "rel.relType" in body


# --------------------------------------------------------------------- BC

def test_bc_addedge_no_longer_overwrites_user_query():
    """The WI-P19 defect: addEdge success overwrote this.q with the inspected
    node id, collapsing the search result set (nodes visually disappeared).
    The overwrite must be gone from the addEdge span."""
    body = _body_between("async addEdge() {", "get relatedNodes() {")
    assert "this.q = this.inspectNode.id" not in body


def test_bc_addedge_reruns_current_view_and_refocuses():
    """After a successful create, addEdge re-runs the current search (or the
    advanced-filter view when the graph was loaded filter-first) and then
    re-focuses/re-selects the inspected node via refocusInspectNode()."""
    body = _body_between("async addEdge() {", "get relatedNodes() {")
    assert "if (this.q.trim()) { await this.search(); }" in body
    assert "await this.applyAdvancedFilters();" in body
    assert "this.refocusInspectNode()" in body


def test_bc_refocus_reselects_inspected_node():
    """refocusInspectNode() re-selects the inspected node in cytoscape after
    the rebuild so the selection does not freeze on an orphaned element."""
    body = _body_between("async refocusInspectNode() {", "get relatedNodes() {")
    assert "cy.getElementById(id)" in body
    assert "el.select()" in body


def test_bc_deleteedge_unchanged_out_of_scope():
    """deleteEdge keeps its existing behavior (outside this work item's
    scope); its q-refresh pattern is untouched pending a follow-up item."""
    body = _body_between("async deleteEdge(", "async openAddForm() {")
    assert "this.q = this.inspectNode.id" in body


# --------------------------------------------------------------------- BD

def test_bd_tap_handler_resets_add_form_on_node_change():
    """Selecting a new node resets the Add-edge form (to/rel/weight), the
    success message, and the form-open state — stale form state from the
    previous node must not leak into the next inspection."""
    body = _body_between("this.cy.on('tap', 'node'", "this.cy.on('tap', function")
    assert "self.addForm = { to_id: '', rel_type: 'related_to', weight: 1.0 };" in body
    assert "self.lastCreated = null;" in body
    assert "self.addFormOpen = false;" in body


def test_bd_openaddform_still_resets():
    """The explicit Add-edge open path keeps its own reset behavior."""
    body = _body_between("async openAddForm() {", "closeAddForm() {")
    assert "addForm = { to_id: '', rel_type: 'related_to', weight: 1.0 };" in body
    assert "addFormOpen = true" in body
    assert "lastCreated = null;" in body


# --------------------------------------------------------------------- BA

def test_ba_loadsubdiroptions_composes_projects_entries():
    """BA disposition: project subdirs are servable by the existing APIs
    (context_graph and relationships both accept 'projects/<name>' — verified
    live), so the dropdown composes 'projects/<name>' entries from the
    existing framework projects source (POST /api/projects,
    action=list_options, response {ok, data:[{key,label}]})."""
    body = _body_between("async loadSubdirOptions() {", "async search() {")
    assert "fetch('/api/projects'" in body
    assert "method: 'POST'" in body
    assert "action: 'list_options'" in body
    assert "'projects/' + p.key" in body
    assert "new Set(" in body, "projects entries must be de-duplicated"


def test_ba_wip18_mapping_literal_preserved():
    """WI-P18's pinned mapping expression (memory_subdirs project rows are
    already emitted in 'projects/<name>' format) stays intact."""
    src = PANEL.read_text(encoding="utf-8")
    assert "s.type === 'project' ? ('projects/' + s.name) : s.name" in src


def test_ba_projects_source_failure_is_graceful():
    """A projects-source failure must not wipe the memory_subdirs results:
    the composition is wrapped in its own try/catch after the API load."""
    body = _body_between("async loadSubdirOptions() {", "async search() {")
    assert body.count("catch (e") >= 2


# ------------------------------------------------------------ shell + syntax

def test_shell_copy_untouched_pointer():
    """The extensions shell copy stays the small pointer copy (WI-P5B); all
    WI-P19 edits landed only in the content copy."""
    shell = SHELL.read_text(encoding="utf-8")
    assert len(shell) < 1024
    assert "x-component" in shell
    assert "relLabel" not in shell
    assert "refocusInspectNode" not in shell


def test_xdata_javascript_is_syntactically_valid():
    """The full x-data object must remain parseable JavaScript after the
    WI-P19 edits (guards against brace/quote regressions)."""
    with tempfile.TemporaryDirectory() as td:
        js = Path(td) / "xdata.js"
        js.write_text("async function __wrap(){ return ({" + _xdata_js() + "}); }", encoding="utf-8")
        proc = subprocess.run(
            ["node", "--check", str(js)], capture_output=True, text=True, timeout=30
        )
    assert proc.returncode == 0, "x-data JS syntax invalid: " + proc.stderr
