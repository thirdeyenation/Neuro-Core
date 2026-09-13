"""WI-P13-ADD-EDGE-UI: tests for the Context Graph panel add-edge affordance.

Covers the panel consuming the ALREADY-EXISTING POST /relationships route per
the approved WI-P13 design (S1) and ARC binding conditions C1-C7:

* happy path via REAL GraphStore + REAL handler plumbing, no mocks on success
  paths (WI-P8/F1, WI-P9 precedent),
* JSON-body contract pinned (ARC C3 — POST with a JSON body, never query
  params; the POST handler is input-only per KI-018-AL),
* error-shape battery matching the documented POST error contract
  (docs/api.md:178-180) — every failure path structured, never silent,
* D25 dedup behavior verified via fresh-store re-read (same triple updates in
  place; different (to_id, type) kept as separate edges),
* auth/CSRF contract pins (requires_auth True; CSRF enforcement rides the
  framework wrap at helpers/api.py:263-265) — live browser CSRF/serving rides
  the standing host/browser limitation (ARC C7),
* panel affordance source pins per the established
  tests/test_graphpanel_copies.py convention (ARC C4/C5/C6):
  inspector-only placement, explicit labeled submit, rel_type list equal to
  the store's 8-type enum, visible created-triple echo, two-copy pinning with
  the extensions shell byte-identical.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import types
from pathlib import Path

import pytest

from usr.plugins.neuro_core.api import relationships as api_mod
from usr.plugins.neuro_core.helpers.graph_store import (
    VALID_RELATIONSHIP_TYPES,
    GraphEdge,
    GraphStore,
)


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL_CONTENT = PLUGIN_ROOT / "webui/right-canvas-panels/graph-panel.html"
PANEL_SHELL = PLUGIN_ROOT / "extensions/webui/right-canvas-panels/graph-panel.html"
FRAMEWORK_API = Path("/a0/helpers/api.py")

# ARC C6: the extensions shell copy must stay byte-identical to its
# pre-WI-P13 state (sha256 recorded before implementation this turn).
SHELL_PRE_P13_SHA256 = (
    "1b4caa36a8f1b7e69d59b76446c8afa7d4732492d37b127891f370fa816fc7ac"
)

# ARC C5: the 8-type store enum (order as pinned in the panel templates).
STORE_ENUM = [
    "supports",
    "contradicts",
    "depends_on",
    "derived_from",
    "related_to",
    "precedes",
    "follows",
    "part_of",
]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _req(method: str = "POST", args: dict | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        path="/api/plugins/neuro_core/relationships",
        method=method,
        args=args or {},
    )


def _post(memory_subdir: str, from_id: str, to_id: str, rel_type: str,
          weight=None) -> dict:
    body = {
        "memory_subdir": memory_subdir,
        "from_id": from_id,
        "to_id": to_id,
        "rel_type": rel_type,
    }
    if weight is not None:
        body["weight"] = weight
    return _run(api_mod.RelationshipsApi().process(
        input=body,
        request=_req(method="POST"),
    ))


def _edge(fid: str, tid: str, rel: str = "related_to") -> GraphEdge:
    return GraphEdge(
        from_id=fid,
        to_id=tid,
        type=rel,
        weight=0.8,
        confidence=0.9,
        source="test",
        created_at="2026-09-12T00:00:00+00:00",
    )


def _store_hash(memory_subdir: str) -> str:
    from usr.plugins.neuro_core.helpers import graph_store as gs_mod
    path = gs_mod._relationships_path(memory_subdir)
    if not Path(path).exists():
        return "<missing>"
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fresh_triples(memory_subdir: str) -> set[tuple[str, str, str]]:
    store = GraphStore(memory_subdir)
    adjacency = store.get_edges()
    return {
        (e.from_id, e.to_id, getattr(e.type, "value", e.type))
        for edges in adjacency.values()
        for e in edges
    }


def _fresh_edge_weights(memory_subdir: str) -> dict[tuple[str, str, str], float]:
    store = GraphStore(memory_subdir)
    adjacency = store.get_edges()
    return {
        (e.from_id, e.to_id, getattr(e.type, "value", e.type)): float(e.weight)
        for edges in adjacency.values()
        for e in edges
    }


# ---------------------------------------------------------------------------
# Happy path — real GraphStore, real handler plumbing, no mocks (WI-P8/P9)
# ---------------------------------------------------------------------------


def test_post_happy_path_creates_edge_via_real_store(memory_subdir):
    result = _post(memory_subdir, "doc-a", "doc-b", "related_to", weight=0.7)

    assert result["success"] is True, f"POST must succeed: {result}"
    assert result["status"] == "ok"
    assert result["from_id"] == "doc-a"
    assert result["to_id"] == "doc-b"
    assert result["rel_type"] == "related_to"
    assert result["weight"] == 0.7

    # Fresh-store re-read: the edge exists exactly once, written by the API.
    store = GraphStore(memory_subdir)
    edges = [e for bucket in store.get_edges().values() for e in bucket]
    assert [(e.from_id, e.to_id, getattr(e.type, "value", e.type)) for e in edges] == [
        ("doc-a", "doc-b", "related_to")
    ]
    assert edges[0].source == "api"  # ARC/design: source="api" write contract
    assert edges[0].weight == 0.7


def test_post_dispatch_via_process_uses_post_branch(memory_subdir):
    """The POST method must route through the POST branch of process()."""
    handler = api_mod.RelationshipsApi()
    result = _run(handler.process(
        input={
            "memory_subdir": memory_subdir,
            "from_id": "doc-a",
            "to_id": "doc-b",
            "rel_type": "supports",
        },
        request=_req(method="POST"),
    ))
    assert result["success"] is True
    assert _fresh_triples(memory_subdir) == {("doc-a", "doc-b", "supports")}


def test_post_weight_clamped_to_unit_range(memory_subdir):
    result_hi = _post(memory_subdir, "doc-a", "doc-b", "supports", weight=2.0)
    result_lo = _post(memory_subdir, "doc-c", "doc-d", "supports", weight=-0.5)
    assert result_hi["success"] is True and result_hi["weight"] == 1.0
    assert result_lo["success"] is True and result_lo["weight"] == 0.0
    weights = _fresh_edge_weights(memory_subdir)
    assert weights[("doc-a", "doc-b", "supports")] == 1.0
    assert weights[("doc-c", "doc-d", "supports")] == 0.0


def test_post_non_numeric_weight_falls_back_to_one(memory_subdir):
    result = _post(memory_subdir, "doc-a", "doc-b", "supports", weight="not-a-number")
    assert result["success"] is True
    assert result["weight"] == 1.0  # documented fallback (docs/api.md weight row)


# ---------------------------------------------------------------------------
# Error-shape battery — structured failures, never silent (ARC C2)
# ---------------------------------------------------------------------------


def test_post_missing_memory_subdir_structured_error():
    handler = api_mod.RelationshipsApi()
    result = _run(handler.process(
        input={"from_id": "a", "to_id": "b", "rel_type": "supports"},
        request=_req(method="POST"),
    ))
    assert result["success"] is False
    assert "memory_subdir" in result["error"]


def test_post_missing_from_or_to_structured_error(memory_subdir):
    result = _post(memory_subdir, "", "doc-b", "supports")
    assert result["success"] is False
    assert "from_id" in result["error"] and "to_id" in result["error"]


def test_post_self_edge_rejected(memory_subdir):
    before = _store_hash(memory_subdir)
    result = _post(memory_subdir, "doc-a", "doc-a", "supports")
    assert result["success"] is False
    assert "self-referential" in result["error"]
    assert _store_hash(memory_subdir) == before  # no store mutation on failure


def test_post_invalid_rel_type_structured_error(memory_subdir):
    before = _store_hash(memory_subdir)
    result = _post(memory_subdir, "doc-a", "doc-b", "not_a_type")
    assert result["success"] is False
    assert "rel_type" in result["error"] and "not_a_type" in result["error"]
    assert _store_hash(memory_subdir) == before


# ---------------------------------------------------------------------------
# D25 dedup — fresh-store re-read (ARC C7 validation pins for VAL)
# ---------------------------------------------------------------------------


def test_post_d25_same_triple_updates_in_place(memory_subdir):
    first = _post(memory_subdir, "doc-a", "doc-b", "related_to", weight=0.5)
    second = _post(memory_subdir, "doc-a", "doc-b", "related_to", weight=0.9)
    assert first["success"] is True and second["success"] is True
    triples = _fresh_triples(memory_subdir)
    assert ("doc-a", "doc-b", "related_to") in triples
    # Same from/to/type triple must not duplicate across buckets.
    edges = [e for bucket in GraphStore(memory_subdir).get_edges().values() for e in bucket]
    same = [e for e in edges if (e.from_id, e.to_id,
            getattr(e.type, "value", e.type)) == ("doc-a", "doc-b", "related_to")]
    assert len(same) == 1
    assert same[0].weight == 0.9  # second POST overwrote in place


def test_post_d25_same_triple_different_type_kept_separate(memory_subdir):
    _post(memory_subdir, "doc-a", "doc-b", "related_to", weight=0.5)
    _post(memory_subdir, "doc-a", "doc-b", "supports", weight=0.8)
    triples = _fresh_triples(memory_subdir)
    assert ("doc-a", "doc-b", "related_to") in triples
    assert ("doc-a", "doc-b", "supports") in triples


# ---------------------------------------------------------------------------
# Forced-failure injection — store exception surfaced, not swallowed (C2)
# ---------------------------------------------------------------------------


def test_post_store_failure_surfaced_not_swallowed(memory_subdir, monkeypatch):
    def boom(self, edge):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(GraphStore, "add_edge", boom)
    result = _post(memory_subdir, "doc-a", "doc-b", "supports")
    assert result["success"] is False
    assert "store exploded" in result["error"]


# ---------------------------------------------------------------------------
# Auth / CSRF contract pins (ARC C7) — live browser CSRF/serving rides the
# standing host/browser limitation; here we pin the handler-level contract.
# ---------------------------------------------------------------------------


def test_relationships_api_declares_auth_required_methods():
    """Unauthenticated-rejection contract: the handler requires auth and its
    method set covers POST (the add-edge surface)."""
    assert "POST" in api_mod.RelationshipsApi.get_methods()
    assert api_mod.RelationshipsApi.requires_auth() is True


def test_framework_wrap_enforces_csrf_decorator():
    """CSRF enforcement rides the framework wrap (helpers/api.py) — verify the
    csrf-protected decorator is still applied at the framework layer."""
    text = FRAMEWORK_API.read_text(encoding="utf-8")
    assert "def csrf_protect(f):" in text
    assert "requires_csrf" in text
    assert "csrf_protect(handler_fn)" in text


# ---------------------------------------------------------------------------
# Panel affordance source pins (test_graphpanel_copies.py convention, C4-C6)
# ---------------------------------------------------------------------------


def _panel_text() -> str:
    return PANEL_CONTENT.read_text(encoding="utf-8")


def test_panel_add_edge_entry_in_inspector_with_explicit_open():
    """C4: entry affordance in the inspector, explicitly opened."""
    text = _panel_text()
    assert "openAddForm()" in text
    assert "Add edge" in text


def test_panel_add_form_fields_and_explicit_labeled_submit():
    """C4: from_id pre-filled from the inspected node, to_id input, weight
    input, and an explicit labeled submit button (Create edge)."""
    text = _panel_text()
    assert "from_id: <span x-text=\"inspectNode ? inspectNode.id : ''\">" in text
    assert "x-model=\"addForm.to_id\"" in text
    assert "x-model=\"addForm.rel_type\"" in text
    assert "x-model.number=\"addForm.weight\"" in text
    assert ">Create edge</button>" in text
    assert "@click=\"addEdge()\"" in text


def test_panel_add_edge_uses_json_body_post_never_query_params():
    """C3: addEdge POSTs a JSON body to the relationships route and never
    builds query params."""
    text = _panel_text()
    start = text.index("async addEdge() {")
    end = text.index("get relatedNodes()", start)
    add_edge_src = text[start:end]
    assert "fetch('/api/plugins/neuro_core/relationships', { method: 'POST'" in add_edge_src
    assert "JSON.stringify(body)" in add_edge_src
    assert "URLSearchParams" not in add_edge_src
    # The body must include all four POST fields + weight.
    assert "memory_subdir:" in text
    assert "from_id: fromId" in text
    assert "to_id: toId" in text
    assert "rel_type: this.addForm.rel_type" in text
    assert "weight:" in text


def test_panel_add_edge_errors_surface_in_visible_error_state():
    """C2: every addEdge failure path assigns to the panel's visible
    `error` state; required-field guard and thrown fetch/HTTP errors."""
    text = _panel_text()
    assert "this.error = 'Add edge failed: to_id is required'" in text
    assert "this.error = 'Add edge failed: ' + e.message" in text
    assert "throw new Error(data.error" in text  # structured API error surfaced


def test_panel_add_form_rel_type_enum_equals_store_enum():
    """C5: the add-form rel_type list equals the 8-type store enum exactly —
    same pinning convention as the advanced-filter dropdown (KI-018-AI)."""
    text = _panel_text()
    # Isolate the add-form select (x-model="addForm.rel_type") and the
    # template x-for list inside it.
    anchor = text.index('x-model="addForm.rel_type"')
    tail = text[anchor:]
    idx = tail.index("['supports'")
    # The list runs between the opening quote after "in " and the closing "]".
    open_bracket = tail.index("[", idx)
    close_bracket = tail.index("]", open_bracket)
    items = [s.strip().strip("'").strip('"') for s in tail[open_bracket + 1:close_bracket].split(",")]
    assert items == STORE_ENUM
    # And the pinned list matches the store's actual enum.
    assert set(items) == set(VALID_RELATIONSHIP_TYPES)
    assert len(VALID_RELATIONSHIP_TYPES) == 8


def test_panel_created_triple_echo_visible():
    """C4: visible created-triple echo so a mistyped to_id is recognizable."""
    text = _panel_text()
    assert "lastCreated" in text
    assert "Created edge:" in text
    assert "lastCreated.from_id + ' --[' + lastCreated.rel_type" in text
    assert "lastCreated.weight + ')'" in text


def test_panel_add_edge_success_requeries_graph():
    """C4: after a successful add the panel re-queries via search()."""
    text = _panel_text()
    assert "await this.search()" in text


def test_panel_add_edge_cancel_closes_form():
    """C4: explicit cancel affordance closes the form."""
    text = _panel_text()
    assert ">Cancel</button>" in text
    assert "@click=\"closeAddForm()\"" in text


def test_extensions_shell_copy_byte_identical_to_pre_p13():
    """C6: the extensions shell copy is unchanged (byte-identical to its
    pre-WI-P13 sha256)."""
    data = PANEL_SHELL.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    assert digest == SHELL_PRE_P13_SHA256, (
        "extensions shell copy must remain byte-identical; "
        f"observed {digest}, expected {SHELL_PRE_P13_SHA256}"
    )


def test_shell_does_not_gain_add_form_markers():
    """C6: no add-form markers leaked into the extensions shell."""
    text = PANEL_SHELL.read_text(encoding="utf-8")
    for marker in ("addFormOpen", "addEdge()", "lastCreated", "openAddForm"):
        assert marker not in text


def test_content_copy_carries_add_form_markers():
    """Two-copy pinning: the content copy is the changed surface."""
    text = _panel_text()
    for marker in ("addFormOpen", "addEdge()", "lastCreated", "openAddForm"):
        assert marker in text
