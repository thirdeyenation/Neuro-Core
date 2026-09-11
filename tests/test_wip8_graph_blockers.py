"""WI-P8-GRAPH-BLOCKERS: remediation tests for the three MVP-blocking
Context Graph defects confirmed by the WI-P7 audit (KI-018-AC / -AD / -AE).

Method constraint (F1 lesson): the list-all success path MUST be exercised
against a REAL GraphStore instance backed by the conftest ``memory_subdir``
temp fixture. A mocked-store validation previously hid the ``store._data``
AttributeError through two gates; these tests execute the real adjacency
contract end-to-end at the handler level.

Panel tests are source-level pinning tests, consistent with the established
tests/test_graphpanel_copies.py convention for WebUI panel surfaces.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

from usr.plugins.neuro_core.api import relationships as api_mod
from usr.plugins.neuro_core.helpers.graph_store import GraphEdge, GraphStore


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL_CONTENT = PLUGIN_ROOT / "webui/right-canvas-panels/graph-panel.html"
RELATIONSHIPS_API = PLUGIN_ROOT / "api/relationships.py"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _req(args: dict | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        path="/api/plugins/neuro_core/relationships",
        method="GET",
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
        created_at="2026-09-10T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# KI-018-AC — REAL GraphStore list-all (integration, no mocks)
# ---------------------------------------------------------------------------


def test_list_all_real_graph_store_returns_edges(memory_subdir):
    """GET /relationships list-all against a REAL GraphStore must return the
    serialized adjacency without AttributeError (F1/KI-018-AC fix). Before the
    fix, store._data.values() raised AttributeError and the broad except
    converted it to {success: false} at HTTP 200."""
    store = GraphStore(memory_subdir)
    store.add_edge(_edge("doc-a", "doc-b", "related_to"))
    store.add_edge(_edge("doc-c", "doc-d", "supports"))

    handler = api_mod.RelationshipsApi()
    result = _run(handler._list_all_relationships(
        input={"memory_subdir": memory_subdir}, request=_req()
    ))

    assert result["success"] is True, f"list-all must not fail: {result}"
    assert result["memory_subdir"] == memory_subdir
    assert result["count"] == 2
    keys = {(e["from_id"], e["to_id"]) for e in result["edges"]}
    assert {("doc-a", "doc-b"), ("doc-c", "doc-d")} == keys
    types_seen = {e["type"] for e in result["edges"]}
    assert types_seen == {"related_to", "supports"}


def test_list_all_real_graph_store_empty_returns_success(memory_subdir):
    """An empty real store must return success:true with zero edges — the
    empty graph is a valid state, distinct from an API failure."""
    GraphStore(memory_subdir)  # ensures the store path exists but is empty
    handler = api_mod.RelationshipsApi()
    result = _run(handler._list_all_relationships(
        input={"memory_subdir": memory_subdir}, request=_req()
    ))
    assert result["success"] is True
    assert result["edges"] == []
    assert result["count"] == 0


def test_list_all_real_store_roundtrip_persistence(memory_subdir, tmp_path):
    """Edges added through the real store must be listed after re-instantiating
    GraphStore (load-on-first-access), pinning that list-all reads the
    persisted adjacency, not just instance memory."""
    GraphStore(memory_subdir).add_edge(_edge("doc-x", "doc-y"))
    # Fresh instance — the handler constructs its own store in production.
    handler = api_mod.RelationshipsApi()
    result = _run(handler._list_all_relationships(
        input={"memory_subdir": memory_subdir}, request=_req()
    ))
    assert result["success"] is True
    assert result["count"] == 1
    assert result["edges"][0]["from_id"] == "doc-x"
    assert result["edges"][0]["to_id"] == "doc-y"


# ---------------------------------------------------------------------------
# KI-018-AD — panel node data-shape alignment (source-level pinning)
# ---------------------------------------------------------------------------


def test_panel_render_reads_doc_id_from_context_graph_api():
    """renderGraph must read node identity from the API's serialized GraphNode
    key (doc_id, helpers/context_graph.py:52-71), with an id fallback for the
    advanced_filters serialization (api/advanced_filters.py:204-205)."""
    text = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "n.doc_id || n.id" in text


def test_relationships_api_has_no_private_store_access():
    """The list-all handler must never reach into nonexistent private store
    internals again (the _data AttributeError class of defect)."""
    text = RELATIONSHIPS_API.read_text(encoding="utf-8")
    assert "store._data" not in text
    assert "store.get_edges()" in text


# ---------------------------------------------------------------------------
# KI-018-AE — first-open journey and error surfacing (source-level pinning)
# ---------------------------------------------------------------------------


def test_first_open_defaults_subdir_before_auto_search():
    """x-init must not send an empty memory_subdir: it defaults to the first
    saved chip (established panel default 'projects/neuro_core') before the
    auto-search runs, so the first-open journey reaches a valid subdir."""
    text = PANEL_CONTENT.read_text(encoding="utf-8")
    assert (
        "if (!sub.trim()) sub = this.subdirs[0] || 'projects/neuro_core';" in text
    )


def test_search_surfaces_success_false_payload():
    """search() must treat {success: false} at HTTP 200 as an error and clear
    stale nodes — the silent empty-state rendering must not return."""
    text = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "if (data.success === false) throw new Error(data.error || 'search failed');" in text


def test_apply_advanced_filters_surfaces_success_false_payload():
    """applyAdvancedFilters() must surface {success: false} payloads the same
    way — no silent empty-state rendering from the filters path."""
    text = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "if (data.success === false) throw new Error(data.error || 'filter query failed');" in text
