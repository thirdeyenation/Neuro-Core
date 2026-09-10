"""Pinning tests for the relationships API GET list-all ``memory_subdir``
query-arg fallback (WI-P5A-RELATIONSHIPS-QARG).

Defect fixed: ``_list_all_relationships`` parsed ``memory_subdir`` from
POST/JSON input only, so a host ``GET /relationships?memory_subdir=X``
silently read the default subdir instead of X — inconsistent with the same
handler's GET-with-id path and all sibling handlers
(context_graph.py:101, advanced_filters.py:95-98), which use the
established fallback order: ``input.get(...) or request.args.get(...) or ''``.

These tests pin the fixed, consistent behavior: query args are honored on
GET list-all, POST/JSON input keeps precedence (behavior-preserving for
existing callers), and missing sources still return the required-arg error.

D42: Relationship routes live in ``api/relationships.py`` (their own
ApiHandler file). Tests follow the ``test_api.py`` stub-store convention:
``GraphStore`` is patched in the relationships module and records the
memory_subdir it was constructed with.
"""

from __future__ import annotations

import asyncio
import types

from usr.plugins.neuro_core.api import relationships as api_mod


class _RecordingGraphStore:
    """Stub GraphStore that records the memory_subdir it is built with."""

    constructed_with: list[str] = []

    def __init__(self, memory_subdir: str):
        _RecordingGraphStore.constructed_with.append(memory_subdir)
        self.memory_subdir = memory_subdir

    def get_edges(self, from_id: str):
        return []

    def neighbors(self, from_id: str, hops: int = 1):
        return []

    # _list_all_relationships reads store._data.values() directly.
    _data: dict = {}


def _install_stub(monkeypatch):
    _RecordingGraphStore.constructed_with = []
    monkeypatch.setattr(api_mod, "GraphStore", _RecordingGraphStore)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _req(args: dict | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        path="/api/plugins/neuro_core/relationships",
        method="GET",
        args=args or {},
    )


def test_list_all_honors_memory_subdir_query_arg(monkeypatch):
    """GET /relationships?memory_subdir=X must read subdir X (the fix)."""
    _install_stub(monkeypatch)
    handler = api_mod.RelationshipsApi()
    result = _run(handler._list_all_relationships(
        input={}, request=_req({"memory_subdir": "X"})
    ))
    assert result["success"] is True
    assert result["memory_subdir"] == "X"
    assert _RecordingGraphStore.constructed_with == ["X"]


def test_list_all_input_takes_precedence_over_query_arg(monkeypatch):
    """POST/JSON input must keep precedence (established fallback order:
    input.get(...) or request.args.get(...)). Existing callers unchanged."""
    _install_stub(monkeypatch)
    handler = api_mod.RelationshipsApi()
    result = _run(handler._list_all_relationships(
        input={"memory_subdir": "from-input"},
        request=_req({"memory_subdir": "from-args"}),
    ))
    assert result["success"] is True
    assert result["memory_subdir"] == "from-input"
    assert _RecordingGraphStore.constructed_with == ["from-input"]


def test_list_all_input_only_callers_unchanged(monkeypatch):
    """Existing input-only callers (no query arg) behave exactly as before."""
    _install_stub(monkeypatch)
    handler = api_mod.RelationshipsApi()
    result = _run(handler._list_all_relationships(
        input={"memory_subdir": "main"},
        request=_req(),  # empty args
    ))
    assert result["success"] is True
    assert result["memory_subdir"] == "main"
    assert _RecordingGraphStore.constructed_with == ["main"]


def test_list_all_missing_everywhere_returns_required_error(monkeypatch):
    """Neither input nor query arg present -> the required-arg error."""
    _install_stub(monkeypatch)
    handler = api_mod.RelationshipsApi()
    result = _run(handler._list_all_relationships(input={}, request=_req()))
    assert result == {"success": False, "error": "`memory_subdir` is required"}
    assert _RecordingGraphStore.constructed_with == []


def test_process_dispatch_honors_query_arg_on_list_all(monkeypatch):
    """Full process() path: GET with no id routes to list-all and the
    request.args memory_subdir must survive the dispatch."""
    _install_stub(monkeypatch)
    handler = api_mod.RelationshipsApi()
    request = _req({"memory_subdir": "querydir"})
    result = _run(handler.process(input={}, request=request))
    assert result["success"] is True
    assert result["memory_subdir"] == "querydir"
    assert _RecordingGraphStore.constructed_with == ["querydir"]


def test_get_with_id_path_fallback_order_unchanged(monkeypatch):
    """Guard: the GET-with-id path's existing fallback order is not altered.
    Query args must NOT override input on the with-id path either."""
    _install_stub(monkeypatch)
    handler = api_mod.RelationshipsApi()
    result = _run(handler._get_relationships(
        input={"memory_subdir": "in-dir", "id": "X"},
        request=_req({"memory_subdir": "arg-dir"}),
    ))
    assert result["success"] is True
    assert result["memory_id"] == "X"
    assert result["memory_subdir"] == "in-dir"
