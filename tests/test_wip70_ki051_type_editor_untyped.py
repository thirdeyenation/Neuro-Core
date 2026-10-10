'''Tests for WI-P70-KI051-TYPE-EDITOR-UNTYPED — inspector Type(primary)
editor honesty for untyped memories and same-value selection registration
(KI-051, HITL live pass on memory Wm9h3grYO6, badge "type unknown").

Two defects:
  (a) For a memory with NO established type, the dropdown silently displayed
      the first enum value ("fact") — misrepresenting stored state.
      Fix: a display-only untyped placeholder option (value "") rendered only
      while the authoritative GET reports no established primary
      (typeLoadedTypes null). No new writable type value: the placeholder's
      value is the blank-draft no-op semantics (empty primary never produces
      a types write), so ADR-NC1-004's enum lock and the type-authority
      write path are untouched. The KI-045 display-only current-primary
      option (out-of-enum legacy scalar) is unchanged.
  (b) Selecting the already-displayed value (fact -> fact) did not register:
      the types payload was attached only when the draft DIFFERED from the
      loaded set, so a same-value explicit selection was silently dropped.
      Fix: an explicit-selection marker (typeTouched, set by @change on the
      primary select, reset on open/cancel) ORs into the dirty check in BOTH
      the save path and the unsaved-draft notice (editDraftDirty), so an
      explicit same-value selection produces the full-set-replace types
      payload through the established type-authority path (POST /memory_edit
      `types`), while an untouched dropdown remains a no-op.

Pinned groups:
  (a) structural — placeholder option gated on !typeLoadedTypes; typeTouched
      present in saveEdits and editDraftDirty; reset in openEdit/cancelEdit;
      panel JS syntax still valid.
  (b) behavioral (Node harness over the panel's REAL x-data object with a
      stubbed fetch, per the test_wip56_ki042_resultsource.py convention) —
      untyped memory: untouched dropdown stays a no-op (no types write);
      explicit fact selection on an untyped memory POSTs the types payload;
      typed memory: same-value re-selection also registers.

These pins prove the panel's client-side dispatch logic. They do NOT prove
live WebUI rendering or host API behavior — VAL/host scope.
'''

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

PLUGIN = Path("/a0/usr/plugins/neuro_core")
PANEL = PLUGIN / "webui" / "right-canvas-panels" / "graph-panel.html"


# ---------------------------------------------------------------------------
# Node harness: execute the panel's real x-data object with stubbed fetch
# (test_wip56_ki042_resultsource.py convention)
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
  let postBodies = [];
  let tokenCounter = 0;
  const tokenResp = () => { tokenCounter += 1; return makeResponse({ body: { ok: true, token: 'tok-' + tokenCounter } }); };

  // Reproduces the REAL authoritative GET payload shape from
  // api/memory_edit.py: types = NormalizedMemoryTypes.to_payload() —
  // {memory_type, memory_types, additional, inconsistent} — NOT {primary,
  // additional}. Rev1's stubs used {primary, additional}, which never matched
  // the live payload (test-fidelity gap behind the live check 3/4 failures).
  // Untyped memory: normalize_memory_types({}) -> primary None.
  // Typed memory: primary 'fact', no additional types.
  const storedTypes = (scenario.indexOf('typed') === 0)
    ? { memory_type: 'fact', memory_types: ['fact'], additional: [], inconsistent: false }
    : { memory_type: null, memory_types: [], additional: [], inconsistent: false };

  const responders = (url, opts) => {
    const method = (opts && opts.method) || 'GET';
    calls.push({ url: url.split('?')[0], method: method });
    if (url === '/api/csrf_token') return tokenResp();
    if (url.startsWith('/api/plugins/neuro_core/memory_edit')) {
      if (method === 'POST') {
        postBodies.push(JSON.parse(opts.body));
        return makeResponse({ body: { success: true, types_changed: true } });
      }
      return makeResponse({ body: { success: true, content: 'stored content', scores: null, types: storedTypes, validation_status: { unknown: true } } });
    }
    if (url.startsWith('/api/plugins/neuro_core/context_graph'))
      return makeResponse({ body: { success: true, context_graph: { nodes: [{ doc_id: 'u1', content: 'stored content', score: 0.9, metadata: {} }], edges: [] } } });
    if (url.startsWith('/api/plugins/neuro_core/advanced_filters'))
      return makeResponse({ body: { success: true, nodes: [{ doc_id: 'u1', content: 'stored content', score: 0.9, metadata: {} }], edges: [] } });
    return makeResponse({ ok: false, status: 404, body: { error: 'unexpected url ' + url } });
  };
  global.fetch = responders;
  global.window = { confirm: () => true };
  panel.sub = 'projects/neuro_core';

  const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

  if (scenario === 'untyped-same-value-save' || scenario === 'typed-same-value-save') {
    panel.inspectNode = { id: 'u1', content: 'stored content', scores: {}, metadata: {} };
    panel.openEdit();
    await tick(); await tick(); // let the GET prefill settle
    const prefill = { loadedNull: panel.typeLoadedTypes === null, draftPrimary: panel.typeDraft.primary };
    // Simulate the user selecting 'fact' in the dropdown: x-model write plus
    // the @change handler firing (the explicit-selection marker).
    panel.typeDraft.primary = 'fact';
    panel.typeTouched = true;
    panel.editConfirm = true; // two-step safe mode: confirm already given
    await panel.saveEdits();
    return { ok: true, prefill: prefill, error: panel.editError,
             postCount: postBodies.length,
             typesPayload: postBodies.length ? (postBodies[0].types === undefined ? null : postBodies[0].types) : null };
  }

  if (scenario === 'typed-untouched-addtype-save') {
    // KI-051 rev2 acceptance (live checks 3-5): a typed memory opens with the
    // dropdown showing the CURRENT primary; the untouched dropdown plus a
    // custom-type addition must persist the custom types (the types write
    // must NOT be suppressed when the primary is legitimately unchanged and
    // set).
    panel.inspectNode = { id: 'u1', content: 'stored content', scores: {}, metadata: {} };
    panel.openEdit();
    await tick(); await tick();
    const prefill = { loadedNull: panel.typeLoadedTypes === null, draftPrimary: panel.typeDraft.primary };
    panel.typeAddValue = 'my-custom-tag';
    panel.addType();
    panel.editConfirm = true;
    await panel.saveEdits();
    return { ok: true, prefill: prefill, error: panel.editError,
             postCount: postBodies.length,
             typesPayload: postBodies.length ? (postBodies[0].types === undefined ? null : postBodies[0].types) : null };
  }

  if (scenario === 'typed-untouched-noop') {
    // A typed memory with a fully untouched draft (no selection, no custom
    // additions) remains a no-op — no accidental types write.
    panel.inspectNode = { id: 'u1', content: 'stored content', scores: {}, metadata: {} };
    panel.openEdit();
    await tick(); await tick();
    const prefill = { loadedNull: panel.typeLoadedTypes === null, draftPrimary: panel.typeDraft.primary };
    panel.editConfirm = true;
    await panel.saveEdits();
    return { ok: true, prefill: prefill, error: panel.editError,
             postCount: postBodies.length,
             typesPayload: postBodies.length ? (postBodies[0].types === undefined ? null : postBodies[0].types) : null };
  }

  if (scenario === 'untyped-untouched-noop') {
    panel.inspectNode = { id: 'u1', content: 'stored content', scores: {}, metadata: {} };
    panel.openEdit();
    await tick(); await tick();
    const prefill = { loadedNull: panel.typeLoadedTypes === null, draftPrimary: panel.typeDraft.primary };
    panel.editConfirm = true;
    await panel.saveEdits();
    return { ok: true, prefill: prefill, error: panel.editError,
             postCount: postBodies.length,
             typesPayload: postBodies.length ? (postBodies[0].types === undefined ? null : postBodies[0].types) : null };
  }

  if (scenario === 'untyped-placeholder-reenabled-noop') {
    // Selecting the placeholder itself (value '') must stay a no-op even when
    // the change event fired — empty primary never produces a types write.
    panel.inspectNode = { id: 'u1', content: 'stored content', scores: {}, metadata: {} };
    panel.openEdit();
    await tick(); await tick();
    panel.typeDraft.primary = '';
    panel.typeTouched = true;
    panel.editConfirm = true;
    await panel.saveEdits();
    return { ok: true, error: panel.editError, postCount: postBodies.length,
             typesPayload: postBodies.length ? (postBodies[0].types === undefined ? null : postBodies[0].types) : null };
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

_HARNESS_PATH = Path("/tmp/wip70_ki051_harness.js")
_GUARD_PATH = Path("/tmp/wip70_ki051_syntax_guard.js")


def _run_scenario(scenario: str) -> dict:
    _HARNESS_PATH.write_text(NODE_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(_HARNESS_PATH), str(PANEL), scenario],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr}"
    out = proc.stdout.strip().splitlines()[-1]
    return json.loads(out)


def _panel_src() -> str:
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


def test_untyped_placeholder_option_present_and_gated():
    """KI-051 (a): a display-only untyped placeholder exists and is rendered
    only when the authoritative GET reports no established primary."""
    src = _panel_src()
    assert "type unknown (no primary set)" in src
    m = re.search(r'<template x-if="!typeLoadedTypes">', src)
    assert m, "placeholder must be gated on !typeLoadedTypes (untyped state only)"
    # The placeholder's value is the blank-draft no-op value, not a new
    # writable type token.
    assert '<option value="" selected>type unknown (no primary set)</option>' in src


def test_no_new_writable_type_token_introduced():
    """ADR-NC1-004 enum lock untouched: the 8 enum values remain the only
    writable option values; 'type unknown' appears only as display text."""
    src = _panel_src()
    assert src.count("'fact','concept','task','event','decision','skill','preference','note'") >= 1
    # The placeholder option carries value="", never a 'type unknown' value.
    assert 'value="type unknown"' not in src


def test_type_touched_marker_wired_everywhere():
    """KI-051 (b): the explicit-selection marker is set by @change on the
    primary select, ORs into the dirty check in BOTH the save path and the
    unsaved-draft notice, and is reset on open and cancel."""
    src = _panel_src()
    assert 'x-model="typeDraft.primary" @change="typeTouched = true"' in src
    # saveEdits dirty check includes the marker (the save-path occurrence
    # carries the WI tag comment).
    assert src.count("this.typeTouched || !lt || dPrimary !== lt.primary") == 2
    # Reset on open and cancel.
    assert src.count("this.typeTouched = false;") == 2


def test_ki045_current_primary_option_unchanged():
    """The WI-P61-KI045 display-only current-primary option survives intact."""
    src = _panel_src()
    assert "(current)" in src
    assert "indexOf(typeLoadedTypes.primary) === -1" in src


def test_empty_primary_still_never_writes():
    """Type-unknown preservation: an empty primary draft never produces a
    types payload regardless of the touched marker."""
    src = _panel_src()
    assert "const typesDirty = !!dPrimary && (this.typeTouched || !lt" in src


# ---------------------------------------------------------------------------
# (b) Behavioral pins (acceptance criteria)
# ---------------------------------------------------------------------------


def test_untyped_untouched_dropdown_is_noop():
    """An untouched dropdown on an untyped memory performs no types write
    (prefill shows the honest placeholder state, no POST issued)."""
    r = _run_scenario("untyped-untouched-noop")
    assert r["ok"], r.get("error")
    assert r["prefill"] == {"loadedNull": True, "draftPrimary": ""}
    assert r["postCount"] == 0  # nothing to save -> no POST at all
    assert r["typesPayload"] is None


def test_untyped_same_value_selection_registers():
    """KI-051 (b) acceptance: selecting 'fact' on an untyped memory (whose
    dropdown honestly showed the untyped placeholder) registers on save —
    the full-set-replace types payload goes through the established
    type-authority POST path."""
    r = _run_scenario("untyped-same-value-save")
    assert r["ok"], r.get("error")
    assert r["prefill"] == {"loadedNull": True, "draftPrimary": ""}
    assert r["error"] in (None, ""), r.get("error")
    assert r["postCount"] == 1
    assert r["typesPayload"] == {"primary": "fact", "additional": []}


def test_typed_same_value_selection_registers():
    """Same-value re-selection on an already-typed memory (fact -> fact) also
    registers instead of being silently dropped."""
    r = _run_scenario("typed-same-value-save")
    assert r["ok"], r.get("error")
    assert r["prefill"] == {"loadedNull": False, "draftPrimary": "fact"}
    assert r["error"] in (None, ""), r.get("error")
    assert r["postCount"] == 1
    assert r["typesPayload"] == {"primary": "fact", "additional": []}


def test_placeholder_reselection_stays_noop():
    """Re-selecting the placeholder itself (value '') never writes a type —
    the type-unknown state is preserved even when the change event fired."""
    r = _run_scenario("untyped-placeholder-reenabled-noop")
    assert r["ok"], r.get("error")
    assert r["postCount"] == 0
    assert r["typesPayload"] is None


# ---------------------------------------------------------------------------
# (c) Rev2 pins — live check 3/4 regression (test-fidelity gap fix)
# ---------------------------------------------------------------------------


def test_typed_memory_opens_with_current_primary():
    """Live check 3 (rev2 acceptance): a TYPED memory opens the editor with
    the dropdown showing the CURRENT primary ('fact'), not the untyped
    placeholder — typeLoadedTypes is populated from the real GET payload
    shape (memory_type), and the untouched save is a no-op."""
    r = _run_scenario("typed-untouched-noop")
    assert r["ok"], r.get("error")
    assert r["prefill"] == {"loadedNull": False, "draftPrimary": "fact"}
    assert r["postCount"] == 0  # untouched draft -> no POST at all
    assert r["typesPayload"] is None


def test_typed_untouched_dropdown_with_custom_addition_persists():
    """Live check 4 (rev2 acceptance): a typed memory with an UNTOUCHED
    dropdown plus a custom-type addition persists the custom types — the
    types write is NOT suppressed when the primary is legitimately unchanged
    and set (full-set-replace: primary + additional together)."""
    r = _run_scenario("typed-untouched-addtype-save")
    assert r["ok"], r.get("error")
    assert r["prefill"] == {"loadedNull": False, "draftPrimary": "fact"}
    assert r["error"] in (None, ""), r.get("error")
    assert r["postCount"] == 1
    assert r["typesPayload"] == {"primary": "fact", "additional": ["my-custom-tag"]}


def test_harness_uses_real_get_payload_shape():
    """Test-fidelity lesson: the harness stub must reproduce the REAL
    authoritative GET payload (NormalizedMemoryTypes.to_payload: memory_type
    key), not the invented {primary, additional} shape rev1 used."""
    harness = NODE_HARNESS
    assert "memory_type: 'fact'" in harness
    assert "memory_type: null" in harness
    assert "{ primary: 'fact', additional: [] }" not in harness
