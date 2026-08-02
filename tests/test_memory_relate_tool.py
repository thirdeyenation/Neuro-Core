"""Tests for ``tools/memory_relate.py``.

Five required test cases:
1. Valid edge creation succeeds and ``GraphStore`` contains the edge.
2. Self-referential edge (``from_id == to_id``) returns error string,
   does not write to store.
3. Invalid ``rel_type`` returns error string, does not write to store.
4. Edge removal with ``remove=True`` succeeds and edge is gone.
5. Non-existent memory ID returns clear error string without raising.

The conftest provides:
- A ``Memory`` stub class whose static ``get`` is an ``AsyncMock``
  returning a wrapper with a ``db`` whose ``get_by_ids`` returns
  caller-configured lists of doc-like objects.
- A ``Response`` dataclass that captures ``message`` and ``break_loop``.
- A ``Tool`` base class with the same constructor as the framework.
- A monkeypatched ``GraphStore`` whose constructor returns a real
  instance with a ``tmp_path``-rooted sidecar.

Each test builds the tool via the standard ``__init__`` signature
``Tool.__init__(agent, name, method, args, message, loop_data)`` and
invokes ``await tool.execute(**kwargs)``.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any, List, Optional

import pytest


# Ensure the plugin is importable as ``usr.plugins.neuro_core``
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_doc(doc_id: str, content: str = "x") -> types.SimpleNamespace:
    """Create a Document-like object that the tool can introspect."""
    return types.SimpleNamespace(
        id=doc_id,
        page_content=content,
        metadata={"id": doc_id, "area": "main"},
    )


def _make_tool(
    *,
    graph_store: Any,
    available_ids: Optional[List[str]] = None,
    agent: Any = None,
):
    """Build a ``MemoryRelate`` instance with stubbed dependencies."""
    from usr.plugins.neuro_core.tools import memory_relate as mod

    # Monkeypatch ``GraphStore`` so the tool sees our tmp_path-backed
    # instance. This must happen BEFORE the tool is constructed.
    mod.GraphStore = lambda subdir: graph_store  # type: ignore[assignment]

    # Stub the framework ``Memory.get`` so it returns a fake db.
    available = set(available_ids or [])

    fake_db = types.SimpleNamespace(
        get_by_ids=lambda ids: [
            _make_fake_doc(i) for i in ids if i in available
        ],
    )
    fake_mem = types.SimpleNamespace(
        db=fake_db,
        memory_subdir="default",
    )

    # The conftest-installed ``Memory`` stub has a static ``get`` that
    # is an AsyncMock returning a default wrapper. Override it here.
    from plugins._memory.helpers import memory as memory_mod  # type: ignore

    async def _fake_get(*a, **kw):  # noqa: ANN001
        return fake_mem

    memory_mod.Memory.get = staticmethod(_fake_get)  # type: ignore[attr-defined]

    return mod.MemoryRelate(
        agent=agent or object(),
        name="memory_relate",
        method=None,
        args={},
        message=types.SimpleNamespace(
            content="",
            metadata={"memory_relate_args": {}},
        ),
        loop_data=None,
    )


def _memory_subdir_path(tmp_path: Path, subdir: str = "default") -> Path:
    """Mirror the sidecar path used by the real GraphStore."""
    return tmp_path / subdir / "relationships.json"


# ---------------------------------------------------------------------------
# Test 1: Valid edge creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_edge_creation_succeeds(tmp_path: Path) -> None:
    from usr.plugins.neuro_core.helpers.graph_store import (
        GraphEdge,
        GraphStore,
        RelationshipType,
    )

    store = GraphStore.__new__(GraphStore)
    store.memory_subdir = "default"
    store._path = _memory_subdir_path(tmp_path, "default")
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._data = {}
    store._lock = __import__("threading").RLock()

    saved: list[GraphEdge] = []

    def _fake_add_edge(edge: GraphEdge) -> None:
        saved.append(edge)
        store._data.setdefault(edge.from_id, []).append(edge)

    store.add_edge = _fake_add_edge  # type: ignore[assignment]

    tool = _make_tool(
        graph_store=store,
        available_ids=["a", "b"],
    )

    resp = await tool.execute(
        from_id="a",
        to_id="b",
        rel_type="supports",
        weight=0.75,
    )

    assert "Error" not in resp.message
    assert "Edge added" in resp.message
    assert "a -[supports]-> b" in resp.message
    assert len(saved) == 1
    assert saved[0].from_id == "a"
    assert saved[0].to_id == "b"
    assert saved[0].type == RelationshipType.SUPPORTS.value
    assert saved[0].weight == 0.75
    assert resp.break_loop is False


# ---------------------------------------------------------------------------
# Test 2: Self-referential edge rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_referential_edge_rejected(tmp_path: Path) -> None:
    from usr.plugins.neuro_core.helpers.graph_store import GraphStore

    store = GraphStore.__new__(GraphStore)
    store.memory_subdir = "default"
    store._path = _memory_subdir_path(tmp_path, "default")
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._data = {}
    store._lock = __import__("threading").RLock()
    write_called: list[bool] = []

    def _fake_add_edge(edge: object) -> None:
        write_called.append(True)

    store.add_edge = _fake_add_edge  # type: ignore[assignment]

    tool = _make_tool(graph_store=store, available_ids=["a"])

    resp = await tool.execute(
        from_id="a",
        to_id="a",
        rel_type="supports",
    )

    assert "Error" in resp.message
    assert "self-referential" in resp.message.lower()
    assert write_called == [], "GraphStore.add_edge must NOT be called"


# ---------------------------------------------------------------------------
# Test 3: Invalid rel_type rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_rel_type_rejected(tmp_path: Path) -> None:
    from usr.plugins.neuro_core.helpers.graph_store import GraphStore

    store = GraphStore.__new__(GraphStore)
    store.memory_subdir = "default"
    store._path = _memory_subdir_path(tmp_path, "default")
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._data = {}
    store._lock = __import__("threading").RLock()
    write_called: list[bool] = []

    def _fake_add_edge(edge: object) -> None:
        write_called.append(True)

    store.add_edge = _fake_add_edge  # type: ignore[assignment]

    tool = _make_tool(graph_store=store, available_ids=["a", "b"])

    resp = await tool.execute(
        from_id="a",
        to_id="b",
        rel_type="not-a-real-type",
    )

    assert "Error" in resp.message
    assert "unknown rel_type" in resp.message.lower()
    assert write_called == [], "GraphStore.add_edge must NOT be called"


# ---------------------------------------------------------------------------
# Test 4: Edge removal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edge_removal_succeeds(tmp_path: Path) -> None:
    from usr.plugins.neuro_core.helpers.graph_store import (
        GraphEdge,
        GraphStore,
        RelationshipType,
    )

    store = GraphStore.__new__(GraphStore)
    store.memory_subdir = "default"
    store._path = _memory_subdir_path(tmp_path, "default")
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._lock = __import__("threading").RLock()

    # Seed two edges from "a"; we will remove one of them.
    e1 = GraphEdge(
        from_id="a",
        to_id="b",
        type=RelationshipType.SUPPORTS.value,
        weight=1.0,
        confidence=1.0,
        source="agent",
        created_at="2026-01-01T00:00:00Z",
    )
    e2 = GraphEdge(
        from_id="a",
        to_id="c",
        type=RelationshipType.RELATED_TO.value,
        weight=1.0,
        confidence=1.0,
        source="agent",
        created_at="2026-01-01T00:00:00Z",
    )
    store._data = {"a": [e1, e2], "b": [e1], "c": [e2]}

    # Track all mutations.
    added: list[GraphEdge] = []
    removed_ids: list[str] = []

    def _fake_add_edge(edge: GraphEdge) -> None:
        added.append(edge)

    def _fake_remove_edges_for_id(mid: str) -> None:
        removed_ids.append(mid)
        # Mimic real behavior: drop all entries with this from_id.
        store._data.pop(mid, None)

    def _fake_get_edges(mid: str) -> list[GraphEdge]:
        return list(store._data.get(mid, []))

    store.add_edge = _fake_add_edge  # type: ignore[assignment]
    store.remove_edges_for_id = _fake_remove_edges_for_id  # type: ignore[assignment]
    store.get_edges = _fake_get_edges  # type: ignore[assignment]

    tool = _make_tool(graph_store=store, available_ids=["a", "b", "c"])

    resp = await tool.execute(
        from_id="a",
        to_id="b",
        rel_type="supports",
        remove=True,
    )

    assert "Error" not in resp.message
    assert "Removed" in resp.message
    assert "a -[supports]-> b" in resp.message
    # Only "a" was bulk-removed; "c" edge should be re-added.
    assert removed_ids == ["a"]
    assert any(
        e.from_id == "a" and e.to_id == "c" for e in added
    ), "The other (a,c) edge should be re-added after bulk remove"


# ---------------------------------------------------------------------------
# Test 5: Non-existent memory ID returns error without raising
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonexistent_id_returns_error(tmp_path: Path) -> None:
    from usr.plugins.neuro_core.helpers.graph_store import GraphStore

    store = GraphStore.__new__(GraphStore)
    store.memory_subdir = "default"
    store._path = _memory_subdir_path(tmp_path, "default")
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._data = {}
    store._lock = __import__("threading").RLock()
    write_called: list[bool] = []

    def _fake_add_edge(edge: object) -> None:
        write_called.append(True)

    store.add_edge = _fake_add_edge  # type: ignore[assignment]

    # Only "a" exists; "b" does NOT.
    tool = _make_tool(graph_store=store, available_ids=["a"])

    resp = await tool.execute(
        from_id="a",
        to_id="missing-b",
        rel_type="supports",
    )

    assert "Error" in resp.message
    assert "missing-b" in resp.message
    assert "not found" in resp.message.lower()
    assert write_called == [], "GraphStore.add_edge must NOT be called"


# ---------------------------------------------------------------------------
# Additional test: weight clamping (bonus)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weight_is_clamped_to_unit_interval(tmp_path: Path) -> None:
    from usr.plugins.neuro_core.helpers.graph_store import (
        GraphEdge,
        GraphStore,
    )

    store = GraphStore.__new__(GraphStore)
    store.memory_subdir = "default"
    store._path = _memory_subdir_path(tmp_path, "default")
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._data = {}
    store._lock = __import__("threading").RLock()
    saved: list[GraphEdge] = []

    def _fake_add_edge(edge: GraphEdge) -> None:
        saved.append(edge)

    store.add_edge = _fake_add_edge  # type: ignore[assignment]

    tool = _make_tool(graph_store=store, available_ids=["a", "b"])

    # Out-of-range weight should be clamped, not rejected.
    resp = await tool.execute(
        from_id="a",
        to_id="b",
        rel_type="supports",
        weight=5.0,  # > 1.0
    )

    assert "Error" not in resp.message
    assert len(saved) == 1
    assert 0.0 <= saved[0].weight <= 1.0
    assert saved[0].weight == 1.0  # clamped to upper bound


# ---------------------------------------------------------------------------
# Additional test: missing required args returns error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_required_args_returns_error(tmp_path: Path) -> None:
    from usr.plugins.neuro_core.helpers.graph_store import GraphStore

    store = GraphStore.__new__(GraphStore)
    store.memory_subdir = "default"
    store._path = _memory_subdir_path(tmp_path, "default")
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._data = {}
    store._lock = __import__("threading").RLock()

    tool = _make_tool(graph_store=store, available_ids=["a", "b"])

    resp = await tool.execute(from_id="", to_id="b", rel_type="supports")
    assert "Error" in resp.message
    assert "from_id" in resp.message


# ---------------------------------------------------------------------------
# Test 7 (D24): related_to edge creation writes both forward AND reverse edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_related_to_writes_forward_and_reverse_edges(tmp_path: Path) -> None:
    """D24: ``related_to`` is the only symmetric rel_type.

    A successful ``related_to`` edge creation must write BOTH:
        - the forward edge (from_id -> to_id)
        - the reverse edge (to_id -> from_id)
    to ``GraphStore.add_edge`` in the same atomic write.
    """
    from usr.plugins.neuro_core.helpers.graph_store import (
        GraphEdge,
        GraphStore,
        RelationshipType,
    )

    store = GraphStore.__new__(GraphStore)
    store.memory_subdir = "default"
    store._path = _memory_subdir_path(tmp_path, "default")
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._data = {}
    store._lock = __import__("threading").RLock()

    saved: list[GraphEdge] = []

    def _fake_add_edge(edge: GraphEdge) -> None:
        saved.append(edge)
        store._data.setdefault(edge.from_id, []).append(edge)

    store.add_edge = _fake_add_edge  # type: ignore[assignment]

    tool = _make_tool(graph_store=store, available_ids=["a", "b"])

    resp = await tool.execute(
        from_id="a",
        to_id="b",
        rel_type="related_to",
        weight=0.9,
    )

    assert "Error" not in resp.message
    assert "Edge added" in resp.message
    assert "a -[related_to]-> b" in resp.message

    # Exactly TWO edges were written: forward + reverse.
    assert len(saved) == 2, (
        f"expected 2 edges (forward + reverse), got {len(saved)}"
    )

    # Edge 0: forward a -> b
    fwd = saved[0]
    assert fwd.from_id == "a"
    assert fwd.to_id == "b"
    assert fwd.type == RelationshipType.RELATED_TO.value
    assert fwd.weight == 0.9

    # Edge 1: reverse b -> a
    rev = saved[1]
    assert rev.from_id == "b"
    assert rev.to_id == "a"
    assert rev.type == RelationshipType.RELATED_TO.value
    assert rev.weight == 0.9


# ---------------------------------------------------------------------------
# Test F2: success-path Response includes additional['neuro_core_ack']
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_response_includes_neuro_core_ack(tmp_path: Path) -> None:
    """F2: success-path Response must include additional['neuro_core_ack']."""
    from usr.plugins.neuro_core.helpers.graph_store import (
        GraphEdge,
        GraphStore,
    )

    store = GraphStore.__new__(GraphStore)
    store.memory_subdir = "default"
    store._path = _memory_subdir_path(tmp_path, "default")
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._data = {}
    store._lock = __import__("threading").RLock()

    def _fake_add_edge(edge: GraphEdge) -> None:
        store._data.setdefault(edge.from_id, []).append(edge)

    store.add_edge = _fake_add_edge  # type: ignore[assignment]

    tool = _make_tool(graph_store=store, available_ids=["a", "b"])

    resp = await tool.execute(
        from_id="a",
        to_id="b",
        rel_type="supports",
        weight=0.75,
    )

    assert resp.additional is not None
    assert "neuro_core_ack" in resp.additional
    ack = resp.additional["neuro_core_ack"]
    assert "a" in ack and "b" in ack
    assert "supports" in ack
    assert "0.75" in ack


# ---------------------------------------------------------------------------
# Test 8 (D24): directional type (supports) does NOT write a reverse edge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_directional_type_does_not_write_reverse_edge(tmp_path: Path) -> None:
    """D24: all rel_types EXCEPT ``related_to`` are directional.

    A successful ``supports`` edge creation must write ONLY the forward
    edge (from_id -> to_id). No reverse edge should be created.
    """
    from usr.plugins.neuro_core.helpers.graph_store import (
        GraphEdge,
        GraphStore,
        RelationshipType,
    )

    store = GraphStore.__new__(GraphStore)
    store.memory_subdir = "default"
    store._path = _memory_subdir_path(tmp_path, "default")
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._data = {}
    store._lock = __import__("threading").RLock()

    saved: list[GraphEdge] = []

    def _fake_add_edge(edge: GraphEdge) -> None:
        saved.append(edge)
        store._data.setdefault(edge.from_id, []).append(edge)

    store.add_edge = _fake_add_edge  # type: ignore[assignment]

    tool = _make_tool(graph_store=store, available_ids=["a", "b"])

    resp = await tool.execute(
        from_id="a",
        to_id="b",
        rel_type="supports",
        weight=0.75,
    )

    assert "Error" not in resp.message
    assert "Edge added" in resp.message

    # Exactly ONE edge was written: forward only, no reverse.
    assert len(saved) == 1, (
        f"expected 1 edge (forward only), got {len(saved)}"
    )

    fwd = saved[0]
    assert fwd.from_id == "a"
    assert fwd.to_id == "b"
    assert fwd.type == RelationshipType.SUPPORTS.value
    assert fwd.weight == 0.75

    # Explicitly assert no reverse edge was created.
    reverse_edges = [e for e in saved if e.from_id == "b" and e.to_id == "a"]
    assert reverse_edges == [], (
        f"directional type must not create reverse edge, found: {reverse_edges}"
    )
