"""WI-P9-EDGE-DELETE: tests for targeted graph-edge deletion.

Covers the DELETE /relationships route and the new public
GraphStore.remove_edge(from_id, to_id, rel_type) contract per the approved
WI-P9 design (S1) and ARC conditions C1-C6:

* happy path via REAL GraphStore, no mocks on success paths (WI-P8/F1
  precedent),
* structured not-found on 0-removed with store-file content-hash invariance
  (C3, idempotent re-DELETE),
* KI-003 regression signature: incoming edges to from_id survive a targeted
  removal (fresh-store re-read),
* KI-013 non-bulk invariant: unrelated edges in the same from_id bucket
  survive,
* D24-mirror symmetric reverse-removal for related_to (happy path + forced
  failure surfaced without failing the request),
* structured errors on every failure path (C2),
* panel affordance source pins per the established
  tests/test_graphpanel_copies.py convention (C4).
"""

from __future__ import annotations

import asyncio
import hashlib
import types
from pathlib import Path

import pytest

from usr.plugins.neuro_core.api import relationships as api_mod
from usr.plugins.neuro_core.helpers.graph_store import GraphEdge, GraphStore


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL_CONTENT = PLUGIN_ROOT / "webui/right-canvas-panels/graph-panel.html"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _req(method: str = "DELETE", args: dict | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        path="/api/plugins/neuro_core/relationships",
        method=method,
        args=args or {},
    )


def _edge(fid: str, tid: str, rel: str = "related_to") -> GraphEdge:
    return GraphEdge(
        from_id=fid,
        to_id=tid,
        type=rel,
        weight=0.8,
        confidence=0.9,
        source="test",
        created_at="2026-09-11T00:00:00+00:00",
    )


def _delete(memory_subdir: str, from_id: str, to_id: str, rel_type: str) -> dict:
    return _run(api_mod.RelationshipsApi().process(
        input={
            "memory_subdir": memory_subdir,
            "from_id": from_id,
            "to_id": to_id,
            "rel_type": rel_type,
        },
        request=_req(method="DELETE"),
    ))


def _store_hash(memory_subdir: str) -> str:
    from usr.plugins.neuro_core.helpers import graph_store as gs_mod
    path = gs_mod._relationships_path(memory_subdir)
    if not Path(path).exists():
        return "<missing>"
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fresh_pairs(memory_subdir: str) -> set[tuple[str, str, str]]:
    store = GraphStore(memory_subdir)
    adjacency = store.get_edges()
    return {
        (e.from_id, e.to_id, getattr(e.type, "value", e.type))
        for edges in adjacency.values()
        for e in edges
    }


# ---------------------------------------------------------------------------
# Happy path — real GraphStore, no mocks (WI-P8/F1 precedent)
# ---------------------------------------------------------------------------


def test_delete_happy_path_removes_targeted_edge(memory_subdir):
    store = GraphStore(memory_subdir)
    store.add_edge(_edge("doc-a", "doc-b", "related_to"))
    store.add_edge(_edge("doc-a", "doc-c", "supports"))

    result = _delete(memory_subdir, "doc-a", "doc-b", "related_to")

    assert result["success"] is True, f"DELETE must succeed: {result}"
    assert result["removed"] == 1
    assert result["from_id"] == "doc-a"
    assert result["to_id"] == "doc-b"
    assert result["rel_type"] == "related_to"
    # Fresh-store re-read: only the targeted triple is gone.
    assert _fresh_pairs(memory_subdir) == {("doc-a", "doc-c", "supports")}


def test_delete_dispatch_via_process_uses_delete_branch(memory_subdir):
    """The DELETE method must route through the DELETE branch of process()."""
    store = GraphStore(memory_subdir)
    store.add_edge(_edge("doc-a", "doc-b", "supports"))
    handler = api_mod.RelationshipsApi()
    result = _run(handler.process(
        input={
            "memory_subdir": memory_subdir,
            "from_id": "doc-a",
            "to_id": "doc-b",
            "rel_type": "supports",
        },
        request=_req(method="DELETE"),
    ))
    assert result["success"] is True
    assert result["removed"] == 1
    assert _fresh_pairs(memory_subdir) == set()


def test_get_methods_includes_delete_and_auth_required():
    """Route registration contract: DELETE is a registered method and the
    handler still requires authentication (framework gates apply)."""
    assert api_mod.RelationshipsApi.get_methods() == ["GET", "POST", "DELETE"]
    assert api_mod.RelationshipsApi.requires_auth() is True


# ---------------------------------------------------------------------------
# C5 — KI-003 signature and KI-013 non-bulk invariant
# ---------------------------------------------------------------------------


def test_delete_ki003_signature_incoming_edges_survive(memory_subdir):
    """KI-003 pin: deleting edge a->b must NOT remove the unrelated incoming
    edge c->a. Fresh-store re-read after the deletion."""
    store = GraphStore(memory_subdir)
    store.add_edge(_edge("doc-a", "doc-b", "related_to"))
    store.add_edge(_edge("doc-c", "doc-a", "supports"))
    store.add_edge(_edge("doc-a", "doc-d", "supports"))

    result = _delete(memory_subdir, "doc-a", "doc-b", "related_to")
    assert result["success"] is True

    pairs = _fresh_pairs(memory_subdir)
    assert ("doc-c", "doc-a", "supports") in pairs, (
        "KI-003 signature: incoming edge to from_id must survive"
    )
    assert ("doc-a", "doc-d", "supports") in pairs
    assert ("doc-a", "doc-b", "related_to") not in pairs


def test_delete_ki013_invariant_unrelated_edges_in_bucket_survive(memory_subdir):
    """KI-013 pin: a single-edge delete must never bulk-remove unrelated
    edges sharing the same from_id bucket (no wipe-and-rewrite)."""
    store = GraphStore(memory_subdir)
    store.add_edge(_edge("doc-a", "doc-b", "related_to"))
    store.add_edge(_edge("doc-a", "doc-c", "supports"))
    store.add_edge(_edge("doc-a", "doc-d", "contradicts"))
    store.add_edge(_edge("doc-e", "doc-f", "related_to"))

    result = _delete(memory_subdir, "doc-a", "doc-b", "related_to")
    assert result["success"] is True

    pairs = _fresh_pairs(memory_subdir)
    assert pairs == {
        ("doc-a", "doc-c", "supports"),
        ("doc-a", "doc-d", "contradicts"),
        ("doc-e", "doc-f", "related_to"),
    }


# ---------------------------------------------------------------------------
# Symmetric related_to reverse pass (D24 mirror)
# ---------------------------------------------------------------------------


def test_delete_related_to_symmetric_reverse_removed(memory_subdir):
    store = GraphStore(memory_subdir)
    store.add_edge(_edge("doc-a", "doc-b", "related_to"))
    store.add_edge(_edge("doc-b", "doc-a", "related_to"))

    result = _delete(memory_subdir, "doc-a", "doc-b", "related_to")
    assert result["success"] is True
    assert result["removed"] == 1
    assert result["reverse_removed"] == 1
    assert _fresh_pairs(memory_subdir) == set()


def test_delete_related_to_reverse_absent_is_surfaced_not_failed(memory_subdir):
    """D24 mirror: absent reverse edge is reported (reverse_removed: 0),
    never an error."""
    store = GraphStore(memory_subdir)
    store.add_edge(_edge("doc-a", "doc-b", "related_to"))

    result = _delete(memory_subdir, "doc-a", "doc-b", "related_to")
    assert result["success"] is True
    assert result["reverse_removed"] == 0
    assert "reverse_error" not in result


def test_delete_symmetric_reverse_failure_surfaced_request_succeeds(
    memory_subdir, monkeypatch
):
    """D24 mirror + C2: a forced reverse-pass failure is surfaced in the
    response (reverse_error) and the request still succeeds."""
    store = GraphStore(memory_subdir)
    store.add_edge(_edge("doc-a", "doc-b", "related_to"))
    store.add_edge(_edge("doc-b", "doc-a", "related_to"))
    store.add_edge(_edge("doc-a", "doc-c", "supports"))

    real_remove = GraphStore.remove_edge
    calls = {"n": 0}

    def flaky_remove(self, from_id, to_id, rel_type):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated reverse-pass failure")
        return real_remove(self, from_id, to_id, rel_type)

    monkeypatch.setattr(GraphStore, "remove_edge", flaky_remove)

    result = _delete(memory_subdir, "doc-a", "doc-b", "related_to")
    assert result["success"] is True
    assert result["removed"] == 1
    assert result["reverse_removed"] is None
    assert "simulated reverse-pass failure" in (result.get("reverse_error") or "")

    # Reverse pass failed, so the reverse edge remains; the forward target
    # and the unrelated edge are intact.
    pairs = _fresh_pairs(memory_subdir)
    assert ("doc-b", "doc-a", "related_to") in pairs
    assert ("doc-a", "doc-c", "supports") in pairs
    assert ("doc-a", "doc-b", "related_to") not in pairs


# ---------------------------------------------------------------------------
# C3 — structured not-found, no write, idempotency
# ---------------------------------------------------------------------------


def test_delete_not_found_structured_and_no_write(memory_subdir):
    store = GraphStore(memory_subdir)
    store.add_edge(_edge("doc-a", "doc-b", "related_to"))
    before = _store_hash(memory_subdir)

    result = _delete(memory_subdir, "doc-x", "doc-y", "supports")

    assert result["success"] is False
    assert "edge not found" in result["error"]
    assert result["removed"] == 0
    assert _store_hash(memory_subdir) == before, (
        "C3: a 0-removal must not write the store file"
    )


def test_delete_idempotent_redelete_structured_not_found(memory_subdir):
    GraphStore(memory_subdir).add_edge(_edge("doc-a", "doc-b", "supports"))
    first = _delete(memory_subdir, "doc-a", "doc-b", "supports")
    assert first["success"] is True
    after_first = _store_hash(memory_subdir)

    second = _delete(memory_subdir, "doc-a", "doc-b", "supports")
    assert second["success"] is False
    assert "edge not found" in second["error"]
    assert _store_hash(memory_subdir) == after_first


# ---------------------------------------------------------------------------
# C2 — structured validation errors, never silent
# ---------------------------------------------------------------------------


def test_delete_invalid_rel_type_structured_error(memory_subdir):
    before = _store_hash(memory_subdir)
    result = _delete(memory_subdir, "doc-a", "doc-b", "not_a_type")
    assert result["success"] is False
    assert "rel_type" in result["error"]
    assert _store_hash(memory_subdir) == before


def test_delete_self_edge_rejected(memory_subdir):
    result = _delete(memory_subdir, "doc-a", "doc-a", "supports")
    assert result["success"] is False
    assert "self-referential" in result["error"]


def test_delete_missing_memory_subdir_structured_error():
    handler = api_mod.RelationshipsApi()
    result = _run(handler.process(
        input={"from_id": "a", "to_id": "b", "rel_type": "supports"},
        request=_req(method="DELETE"),
    ))
    assert result["success"] is False
    assert "memory_subdir" in result["error"]


def test_delete_missing_from_or_to_structured_error(memory_subdir):
    handler = api_mod.RelationshipsApi()
    result = _run(handler.process(
        input={"memory_subdir": memory_subdir, "from_id": "a",
               "rel_type": "supports"},
        request=_req(method="DELETE"),
    ))
    assert result["success"] is False
    assert "from_id" in result["error"] and "to_id" in result["error"]


def test_delete_store_failure_surfaced_not_swallowed(memory_subdir, monkeypatch):
    """C2: store exceptions surface as structured errors."""
    GraphStore(memory_subdir).add_edge(_edge("doc-a", "doc-b", "supports"))

    def boom(self, from_id, to_id, rel_type):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(GraphStore, "remove_edge", boom)
    result = _delete(memory_subdir, "doc-a", "doc-b", "supports")
    assert result["success"] is False
    assert "store removal failed" in result["error"]
    assert "store exploded" in result["error"]


# ---------------------------------------------------------------------------
# C4 — panel affordance source pins (test_graphpanel_copies.py convention)
# ---------------------------------------------------------------------------

PANEL_CONTENT = PLUGIN_ROOT / "webui/right-canvas-panels/graph-panel.html"


def test_panel_delete_button_in_inspector_with_confirm():
    """C4: delete affordance lives in the inspector relatedNodes loop, uses a
    confirm step, and does not trigger the row's navigate click."""
    content = PANEL_CONTENT.read_text()
    assert "nc-details__rel-delete" in content
    assert "@click.stop=\"deleteEdge(rel)\"" in content
    assert "window.confirm" in content
    assert "method: 'DELETE'" in content


def test_panel_delete_errors_surface_in_visible_error_state(memory_subdir):
    """C4/KI-018-AE pattern: deleteEdge assigns failures to the panel's
    visible `error` state rather than swallowing them."""
    content = PANEL_CONTENT.read_text()
    assert "this.error = 'Delete failed: ' + e.message" in content
    assert "if (data.success === false) throw new Error(data.error" in content


def test_panel_delete_success_requeries_graph(memory_subdir):
    """C4: after a successful delete the panel re-queries via search() so the
    edge disappears from the rendered graph."""
    content = PANEL_CONTENT.read_text()
    assert "await this.search()" in content


def _delete_handler_delete(api, memory_subdir, from_id, to_id, rel_type):
    raise AssertionError("unused helper — kept out")
