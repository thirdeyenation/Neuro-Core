'''Tests for WI-P56-KI042-RESULTSOURCE — result-source mode preservation in
the graph panel across mutations (KI-042).

Defect (HITL live-confirmed 2026-10-03): saveEdits(), addEdge(), deleteEdge()
(and the sibling sites saveMemoryName() and refresh()) dispatched the refresh
as `if (this.q.trim()) await this.search(); else await this.applyAdvancedFilters();`
— when search text was populated while the user was viewing an
Advanced-Filters result set, every mutation silently re-ran the narrower
text search and the filter view was discarded (nodes vanished after
confirm-save and after each Create-edge; re-applying the filter restored
them).

Fix: explicit result-source mode field `_resultSource` ('search' | 'filters'
| 'none') set by whichever fetch last populated the view, plus one shared
refreshAfterMutation() helper called by all four mutation sites; refresh()
is source-aware while keeping its literal this.search() branch (WI-P29 pin).

Pinned groups:
  (a) structural — the four mutation sites call refreshAfterMutation(); the
      legacy q-only dispatch pattern is gone; both fetch paths record the
      source; refresh() keeps an explicit this.search() branch (WI-P29
      structure pin intact).
  (b) behavioral (Node harness over the panel's REAL x-data object with a
      stubbed fetch, per the test_wip29_403_recovery.py convention) —
      after applyAdvancedFilters() populates the view, saveEdits / addEdge /
      deleteEdge re-run advanced_filters (filter result set remains
displayed), NOT context_graph; after search() populates the view, mutations
      still re-run the search (search-mode behavior unchanged); refresh()
      re-runs advanced_filters when the filter view is active.

These pins prove the panel's client-side dispatch logic. They do NOT prove
live WebUI rendering or host API behavior — VAL/host scope.
'''

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PLUGIN = Path("/a0/usr/plugins/neuro_core")
PANEL = PLUGIN / "webui" / "right-canvas-panels" / "graph-panel.html"

LEGACY_PATTERN = (
    "if (this.q.trim()) { await this.search(); } "
    "else { await this.applyAdvancedFilters(); }"
)


# ---------------------------------------------------------------------------
# Node harness: execute the panel's real x-data object with stubbed fetch
# (test_wip29_403_recovery.py convention)
# ---------------------------------------------------------------------------

NODE_HARNESS = r"""
const fs = require('fs');
const panelPath = process.argv[2];
const scenario = process.argv[3];
const src = fs.readFileSync(panelPath, 'utf8');
const m = src.match(/x-data="\{([\s\S]*)\}" x-init=/);
if (!m) { console.log(JSON.stringify({ ok: false, error: 'x-data not found' })); process.exit(0); }
const factory = new Function('return ({' + m[1] + '});');

function makeResponse(spec) {
  const s = spec || {};
  return {
    ok: (s.ok === undefined) ? true : s.ok,
    status: s.status || 200,
    headers: { get: (name) => (name.toLowerCase() === 'content-type' ? (s.contentType || 'application/json') : null) },
    json: async () => s.body,
  };
}

async function run() {
  const panel = factory();
  panel.$nextTick = (fn) => fn();
  const calls = [];
  let tokenCounter = 0;
  const tokenResp = () => { tokenCounter += 1; return makeResponse({ body: { ok: true, token: 'tok-' + tokenCounter } }); };

  // Shared responder: records every call; serves csrf, context_graph,
  // advanced_filters, memory_edit, relationships endpoints.
  const filterNodes = [
    { doc_id: 'f1', content: 'filter node 1', score: 0.8, metadata: {} },
    { doc_id: 'f2', content: 'filter node 2', score: 0.7, metadata: {} },
  ];
  const searchNodes = [{ doc_id: 's1', content: 'search node', score: 0.9, metadata: {} }];
  const responders = (url, opts) => {
    calls.push({ url: url.split('?')[0], method: (opts && opts.method) || 'GET' });
    if (url === '/api/csrf_token') return tokenResp();
    if (url.startsWith('/api/plugins/neuro_core/context_graph'))
      return makeResponse({ body: { success: true, context_graph: { nodes: searchNodes, edges: [] } } });
    if (url.startsWith('/api/plugins/neuro_core/advanced_filters'))
      return makeResponse({ body: { success: true, nodes: filterNodes, edges: [] } });
    if (url.startsWith('/api/plugins/neuro_core/memory_edit'))
      return makeResponse({ body: { success: true } });
    if (url.startsWith('/api/plugins/neuro_core/relationships'))
      return makeResponse({ body: { success: true, from_id: 'f1', to_id: 'f2', rel_type: 'related_to', weight: 1.0 } });
    return makeResponse({ ok: false, status: 404, body: { error: 'unexpected url ' + url } });
  };
  global.fetch = responders;
  global.window = { confirm: () => true };
  panel.sub = 'projects/neuro_core';

  const countUrls = (u) => calls.filter((c) => c.url === u).length;

  if (scenario === 'saveEdits-preserves-filters') {
    await panel.applyAdvancedFilters();
    panel.q = 'recent'; // search text populated while filter view is active (defect scenario)
    panel.inspectNode = { id: 'f1', content: 'old content', scores: {} };
    panel.editConfirm = true;
    panel.editForm = { content: 'new content', importance: '', confidence: '', stability: '' };
    await panel.saveEdits();
    return { ok: true, error: panel.editError, source: panel._resultSource,
             nodes: panel.nodes.map((n) => n.doc_id),
             filtersCalls: countUrls('/api/plugins/neuro_core/advanced_filters'),
             searchCalls: countUrls('/api/plugins/neuro_core/context_graph') };
  }

  if (scenario === 'addEdge-preserves-filters') {
    await panel.applyAdvancedFilters();
    panel.q = 'recent';
    panel.inspectNode = { id: 'f1' };
    panel.addForm = { to_id: 'f2', rel_type: 'related_to', weight: 1.0 };
    await panel.addEdge();
    return { ok: true, error: panel.error, source: panel._resultSource,
             nodes: panel.nodes.map((n) => n.doc_id),
             filtersCalls: countUrls('/api/plugins/neuro_core/advanced_filters'),
             searchCalls: countUrls('/api/plugins/neuro_core/context_graph') };
  }

  if (scenario === 'deleteEdge-preserves-filters') {
    await panel.applyAdvancedFilters();
    panel.q = 'recent';
    panel.inspectNode = { id: 'f1' };
    await panel.deleteEdge({ relType: 'related_to', otherId: 'f2', direction: 'out' });
    return { ok: true, error: panel.error, source: panel._resultSource,
             nodes: panel.nodes.map((n) => n.doc_id),
             filtersCalls: countUrls('/api/plugins/neuro_core/advanced_filters'),
             searchCalls: countUrls('/api/plugins/neuro_core/context_graph') };
  }

  if (scenario === 'mutation-keeps-search-mode') {
    panel.q = 'recent';
    await panel.search(); // genuine search view (q populated)
    panel.inspectNode = { id: 's1', content: 'search node', scores: {} };
    panel.editConfirm = true;
    panel.editForm = { content: 'edited', importance: '', confidence: '', stability: '' };
    await panel.saveEdits();
    return { ok: true, error: panel.editError, source: panel._resultSource,
             nodes: panel.nodes.map((n) => n.doc_id),
             filtersCalls: countUrls('/api/plugins/neuro_core/advanced_filters'),
             searchCalls: countUrls('/api/plugins/neuro_core/context_graph') };
  }

  if (scenario === 'refresh-preserves-filters') {
    await panel.applyAdvancedFilters();
    panel.q = 'recent';
    panel.refresh();
    await new Promise((resolve) => setTimeout(resolve, 0));
    return { ok: true, source: panel._resultSource,
             nodes: panel.nodes.map((n) => n.doc_id),
             filtersCalls: countUrls('/api/plugins/neuro_core/advanced_filters'),
             searchCalls: countUrls('/api/plugins/neuro_core/context_graph') };
  }

  if (scenario === 'initial-none-state-legacy-fallback') {
    // Pre-query state (_resultSource 'none'): empty q -> filter path;
    // populated q -> search path (legacy behavior preserved exactly).
    panel.q = '';
    await panel.refreshAfterMutation();
    const emptyQFilters = countUrls('/api/plugins/neuro_core/advanced_filters');
    // Second leg on a FRESH panel instance so _resultSource is still 'none'
    // (the first leg legitimately set it to 'filters' on success).
    const panel2 = factory();
    panel2.$nextTick = (fn) => fn();
    const calls2 = [];
    const responders2 = (url, opts) => {
      calls2.push({ url: url.split('?')[0] });
      if (url === '/api/csrf_token') return tokenResp();
      if (url.startsWith('/api/plugins/neuro_core/context_graph'))
        return makeResponse({ body: { success: true, context_graph: { nodes: searchNodes, edges: [] } } });
      if (url.startsWith('/api/plugins/neuro_core/advanced_filters'))
        return makeResponse({ body: { success: true, nodes: filterNodes, edges: [] } });
      return makeResponse({ ok: false, status: 404, body: {} });
    };
    global.fetch = responders2;
    panel2.sub = 'projects/neuro_core';
    panel2.q = 'recent';
    await panel2.refreshAfterMutation();
    global.fetch = responders;
    return { ok: true, emptyQFilters: emptyQFilters,
             searchCalls: calls2.filter((c) => c.url === '/api/plugins/neuro_core/context_graph').length,
             filtersCalls2: calls2.filter((c) => c.url === '/api/plugins/neuro_core/advanced_filters').length };
  }

  return { ok: false, error: 'unknown scenario: ' + scenario };
}

run().then((r) => console.log(JSON.stringify(r))).catch((e) => console.log(JSON.stringify({ ok: false, error: String(e && e.message || e) })));
"""

SYNTAX_GUARD = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const m = src.match(/x-data="\{([\s\S]*)\}" x-init=/);
if (!m) { console.error('x-data not found'); process.exit(1); }
new Function('return ({' + m[1] + '});');
console.log('JS syntax OK');
"""

_HARNESS_PATH = Path("/tmp/wip56_resultsource_harness.js")
_GUARD_PATH = Path("/tmp/wip56_syntax_guard.js")


def _run_scenario(scenario: str) -> dict:
    _HARNESS_PATH.write_text(NODE_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(_HARNESS_PATH), str(PANEL), scenario],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr}"
    out = proc.stdout.strip().splitlines()[-1]
    return json.loads(out)


def _panel_text() -> str:
    return PANEL.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (a) Structural pins
# ---------------------------------------------------------------------------


def test_panel_js_syntax_still_valid():
    _GUARD_PATH.write_text(SYNTAX_GUARD, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(_GUARD_PATH), str(PANEL)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "JS syntax OK" in proc.stdout


def test_all_four_mutation_sites_call_refreshAfterMutation():
    text = _panel_text()
    assert text.count("await this.refreshAfterMutation();") == 4


def test_legacy_q_only_dispatch_pattern_is_gone():
    text = _panel_text()
    assert LEGACY_PATTERN not in text


def test_both_fetch_paths_record_result_source():
    text = _panel_text()
    # applyAdvancedFilters success records 'filters'
    assert "this._resultSource = 'filters';" in text
    # search success records 'search'
    assert "this._resultSource = 'search';" in text


def test_refresh_keeps_literal_search_branch_wip29_pin_intact():
    """test_wip29_403_recovery.py slices refresh()..initCytoscape() and asserts
    'this.search()' is present; that structure must survive this change."""
    text = _panel_text()
    body = text[text.index("refresh() {") : text.index("initCytoscape()")]
    assert "this.search()" in body


# ---------------------------------------------------------------------------
# (b) Behavioral pins (acceptance criteria)
# ---------------------------------------------------------------------------


def test_saveEdits_preserves_advanced_filter_view():
    r = _run_scenario("saveEdits-preserves-filters")
    assert r["ok"], r
    assert not r.get("error"), r
    # The filter result set remains displayed (the 2 filter nodes, not the search node)
    assert r["nodes"] == ["f1", "f2"], r
    # The refresh re-ran advanced_filters, not the narrower text search
    assert r["filtersCalls"] == 2, r  # initial apply + mutation refresh
    assert r["searchCalls"] == 0, r
    assert r["source"] == "filters", r


def test_addEdge_preserves_advanced_filter_view():
    r = _run_scenario("addEdge-preserves-filters")
    assert r["ok"], r
    assert not r.get("error"), r
    assert r["nodes"] == ["f1", "f2"], r
    assert r["filtersCalls"] == 2, r
    assert r["searchCalls"] == 0, r


def test_deleteEdge_preserves_advanced_filter_view():
    r = _run_scenario("deleteEdge-preserves-filters")
    assert r["ok"], r
    assert not r.get("error"), r
    assert r["nodes"] == ["f1", "f2"], r
    assert r["filtersCalls"] == 2, r
    assert r["searchCalls"] == 0, r


def test_mutation_keeps_search_mode_for_genuine_search_views():
    r = _run_scenario("mutation-keeps-search-mode")
    assert r["ok"], r
    assert not r.get("error"), r
    # Search-mode behavior unchanged: the search result set stays displayed
    assert r["nodes"] == ["s1"], r
    assert r["searchCalls"] == 2, r  # initial search + mutation refresh
    assert r["filtersCalls"] == 0, r


def test_refresh_reuns_active_filter_view():
    r = _run_scenario("refresh-preserves-filters")
    assert r["ok"], r
    assert r["nodes"] == ["f1", "f2"], r
    assert r["filtersCalls"] == 2, r
    assert r["searchCalls"] == 0, r


def test_initial_none_state_keeps_legacy_fallback():
    """Before any query has run ('none' source), empty q -> filters and
    populated q -> search, exactly as before this change."""
    r = _run_scenario("initial-none-state-legacy-fallback")
    assert r["ok"], r
    assert r["emptyQFilters"] == 1, r
    assert r["searchCalls"] == 1, r
    assert r.get("filtersCalls2", 0) == 0, r
