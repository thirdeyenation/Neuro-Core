"""Tests for the Neuro Core REST API: ``api/context_graph.py``.

These tests guard against the JSON-serialization regression where
``Memory.Area`` (and any other ``enum.Enum`` value stored in a
FAISS document's metadata dict) survives ``dataclasses.asdict``
unchanged and crashes ``json.dumps`` in the API handler:

    TypeError: Object of type Area is not JSON serializable

The fix lives in ``_serialize_context_graph`` and ``_serialize_edge``:
both now walk the dataclass tree through ``_enum_safe_asdict`` /
``_enum_safe_value`` so every ``enum.Enum`` value is converted to
its ``.value`` string before the response leaves the handler.

Test matrix:

* ``TestSerializeContextGraph`` — unit tests against
  ``_serialize_context_graph`` and ``_serialize_edge`` with enum-laden
  fixtures. These are the lowest-level tests and do not need any
  mocking.
* ``TestGetContextGraphHandler`` — exercises the full
  ``_get_context_graph`` handler with a stubbed ``Memory`` and
  ``search_context_graph`` so the end-to-end JSON contract is
  verified.
* ``TestSerializeEdgeWithEnumType`` — defensive coverage for
  ``_serialize_edge`` if a ``RelationshipType`` enum instance ever
  leaks into a ``GraphEdge.type`` field.
"""

from __future__ import annotations

import asyncio
import enum
import json
import types
import unittest.mock as mock

import pytest

from usr.plugins.neuro_core.helpers.context_graph import (
    ContextGraph,
    GraphEdge,
    GraphNode,
)
from usr.plugins.neuro_core.helpers.graph_store import (
    GraphStore,
    RelationshipType,
)
from usr.plugins.neuro_core.helpers.metadata import (
    MemoryType,
    ValidationStatus,
)


# ---------------------------------------------------------------------------
# Test-only enums (mirror ``Memory.Area`` and friends in shape, not in name)
# ---------------------------------------------------------------------------
#
# The conftest stubs ``plugins._memory.helpers.memory.Memory`` with a
# tiny class that has no real ``Area`` enum. We define our own enum
# classes here that share the same shape (``(str, Enum)``) and use the
# exact same string values, so the test exercises the production code
# path the same way the real bug manifested in production.


class Area(str, enum.Enum):
    """Mirror of ``Memory.Area`` for test isolation."""

    MAIN = "main"
    FRAGMENTS = "fragments"
    SOLUTIONS = "solutions"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _edge(
    from_id: str,
    to_id: str,
    type_: str = "supports",
    confidence: float = 0.9,
) -> GraphEdge:
    return GraphEdge(
        from_id=from_id,
        to_id=to_id,
        type=type_,
        confidence=confidence,
    )


def _enum_graph() -> ContextGraph:
    """Build a ``ContextGraph`` with enum instances in every relevant slot.

    The fixture deliberately puts *enum instances* (not their ``.value``
    strings) into:

    * ``GraphNode.metadata['area']`` — the real ``Memory.Area`` enum,
      which is what triggered the production ``TypeError``.
    * ``GraphNode.metadata['memory_type']`` — ``MemoryType`` enum.
    * ``GraphNode.metadata['validation_status']`` — ``ValidationStatus``
      enum.
    * ``GraphNode.metadata['tags']`` — a list of ``MemoryType`` enums,
      to verify the list-traversal path of the serializer.

    If the serializer is enum-naive, the dict returned by
    ``_serialize_context_graph`` will contain ``Area`` /
    ``MemoryType`` / ``ValidationStatus`` instances and
    ``json.dumps`` will raise ``TypeError``.
    """
    nodes = [
        GraphNode(
            doc_id="A",
            content="Seed A: a fact about graphs.",
            metadata={
                "id": "A",
                "area": Area.MAIN,                       # Enum instance
                "memory_type": MemoryType.FACT,          # Enum instance
                "validation_status": ValidationStatus.VALIDATED,  # Enum instance
                "importance": 0.9,
                "confidence": 0.8,
                "tags": [MemoryType.FACT, MemoryType.CONCEPT],  # list of enums
            },
            score=0.95,
            hop=0,
        ),
        GraphNode(
            doc_id="B",
            content="Hop-1 B: a concept that supports A.",
            metadata={
                "id": "B",
                "area": Area.FRAGMENTS,                  # Enum instance
                "memory_type": MemoryType.CONCEPT,       # Enum instance
                "validation_status": ValidationStatus.UNVALIDATED,
                "importance": 0.5,
                "tags": [],
            },
            score=0.6,
            hop=1,
        ),
    ]
    edges = [
        _edge("A", "B", "supports", 0.9),
        _edge("A", "B", "related_to", 0.7),
    ]
    return ContextGraph(
        nodes=nodes,
        edges=edges,
        query="what is A?",
        seed_ids=["A"],
    )


# ---------------------------------------------------------------------------
# _serialize_context_graph — direct unit tests
# ---------------------------------------------------------------------------


class TestSerializeContextGraph:
    """Direct tests for ``_serialize_context_graph``.

    These are the lowest-level tests: they call the serialization
    helper with a fixture full of enum instances and assert the result
    is plain dicts of plain values.
    """

    def test_serializes_query_and_seed_ids(self):
        from usr.plugins.neuro_core.api.context_graph import (
            _serialize_context_graph,
        )

        g = _enum_graph()
        out = _serialize_context_graph(g)
        assert out["query"] == "what is A?"
        assert out["seed_ids"] == ["A"]
        # The prompt_text was generated by the model code, we only care
        # that the key exists and is a string.
        assert isinstance(out["prompt_text"], str)

    def test_node_area_is_string_not_enum(self):
        from usr.plugins.neuro_core.api.context_graph import (
            _serialize_context_graph,
        )

        g = _enum_graph()
        out = _serialize_context_graph(g)
        node_a = out["nodes"][0]
        assert "area" in node_a["metadata"]
        assert isinstance(node_a["metadata"]["area"], str), (
            f"area should be a JSON-safe string, got "
            f"{type(node_a['metadata']['area']).__name__}"
        )
        assert not isinstance(node_a["metadata"]["area"], enum.Enum)
        assert node_a["metadata"]["area"] == Area.MAIN.value

    def test_node_memory_type_is_string_not_enum(self):
        from usr.plugins.neuro_core.api.context_graph import (
            _serialize_context_graph,
        )

        g = _enum_graph()
        out = _serialize_context_graph(g)
        node_a = out["nodes"][0]
        assert isinstance(node_a["metadata"]["memory_type"], str)
        assert not isinstance(node_a["metadata"]["memory_type"], enum.Enum)
        assert node_a["metadata"]["memory_type"] == MemoryType.FACT.value

    def test_node_validation_status_is_string_not_enum(self):
        from usr.plugins.neuro_core.api.context_graph import (
            _serialize_context_graph,
        )

        g = _enum_graph()
        out = _serialize_context_graph(g)
        node_a = out["nodes"][0]
        assert isinstance(node_a["metadata"]["validation_status"], str)
        assert not isinstance(
            node_a["metadata"]["validation_status"], enum.Enum
        )
        assert (
            node_a["metadata"]["validation_status"]
            == ValidationStatus.VALIDATED.value
        )

    def test_list_of_enums_in_metadata_is_serialized(self):
        """``tags=[MemoryType.FACT, MemoryType.CONCEPT]`` -> plain strings."""
        from usr.plugins.neuro_core.api.context_graph import (
            _serialize_context_graph,
        )

        g = _enum_graph()
        out = _serialize_context_graph(g)
        tags = out["nodes"][0]["metadata"]["tags"]
        assert isinstance(tags, list)
        assert tags == [MemoryType.FACT.value, MemoryType.CONCEPT.value]
        for tag in tags:
            assert isinstance(tag, str)
            assert not isinstance(tag, enum.Enum)

    def test_serialized_output_is_json_safe(self):
        """The full output round-trips through ``json.dumps`` without error."""
        from usr.plugins.neuro_core.api.context_graph import (
            _serialize_context_graph,
        )

        g = _enum_graph()
        out = _serialize_context_graph(g)
        # This is the assertion that reproduced the production bug.
        # If any enum instance survives serialization, ``json.dumps``
        # raises ``TypeError: Object of type Area is not JSON
        # serializable``.
        encoded = json.dumps(out)
        assert isinstance(encoded, str)
        # Round-trip back to confirm shape is preserved.
        decoded = json.loads(encoded)
        assert decoded["query"] == "what is A?"
        assert decoded["nodes"][0]["metadata"]["area"] == Area.MAIN.value
        assert (
            decoded["nodes"][0]["metadata"]["memory_type"]
            == MemoryType.FACT.value
        )

    def test_no_enum_instance_survives_anywhere_in_output(self):
        """Recursive guard: no ``Enum`` instance may leak into the output."""
        from usr.plugins.neuro_core.api.context_graph import (
            _serialize_context_graph,
        )

        g = _enum_graph()
        out = _serialize_context_graph(g)

        def _walk(o, path: str = "$"):
            if isinstance(o, enum.Enum):
                pytest.fail(
                    f"Enum instance leaked into API output at {path}: "
                    f"{o!r} (type {type(o).__name__})"
                )
            if isinstance(o, dict):
                for k, v in o.items():
                    _walk(v, f"{path}.{k}")
            elif isinstance(o, (list, tuple)):
                for i, item in enumerate(o):
                    _walk(item, f"{path}[{i}]")

        _walk(out)


# ---------------------------------------------------------------------------
# _serialize_edge — direct unit tests
# ---------------------------------------------------------------------------


class TestSerializeEdge:
    """Defensive tests for ``_serialize_edge``.

    ``GraphEdge.type`` is declared ``str`` and validated against the
    string ``VALID_RELATIONSHIP_TYPES`` set in ``__post_init__``, so
    under normal conditions it is a plain string. These tests cover
    the defensive path in case a ``RelationshipType`` enum instance
    ever leaks through.
    """

    def test_edge_type_is_string(self):
        from usr.plugins.neuro_core.api.context_graph import _serialize_edge

        e = _edge("A", "B", "supports")
        out = _serialize_edge(e)
        assert out["type"] == "supports"
        assert isinstance(out["type"], str)

    def test_edge_with_relationship_type_enum_is_serialized(self):
        """If a RelationshipType enum leaks into .type, it must be coerced."""
        from usr.plugins.neuro_core.api.context_graph import _serialize_edge

        # Build a GraphEdge with the enum assigned to .type, bypassing
        # the __post_init__ guard (object.__setattr__ sidesteps any
        # future property setter). This simulates a future change that
        # allows enum members to flow through.
        e = _edge("A", "B", "supports")
        object.__setattr__(e, "type", RelationshipType.SUPPORTS)
        out = _serialize_edge(e)
        assert out["type"] == "supports"
        assert isinstance(out["type"], str)
        assert not isinstance(out["type"], enum.Enum)

    def test_edge_dict_is_json_safe(self):
        from usr.plugins.neuro_core.api.context_graph import _serialize_edge

        e = _edge("A", "B", "contradicts", 0.3)
        out = _serialize_edge(e)
        encoded = json.dumps(out)
        decoded = json.loads(encoded)
        assert decoded["from_id"] == "A"
        assert decoded["to_id"] == "B"
        assert decoded["type"] == "contradicts"
        assert decoded["confidence"] == 0.3


# ---------------------------------------------------------------------------
# Handler-level tests: GET /context_graph with stubbed dependencies
# ---------------------------------------------------------------------------


class TestGetContextGraphHandler:
    """Exercise the full ``_get_context_graph`` handler end-to-end.

    The handler depends on ``Memory.get_by_subdir`` (async) and
    ``search_context_graph`` (async). We mock both so the test is
    hermetic and never touches the filesystem. The fake
    ``search_context_graph`` returns our enum-laden ``ContextGraph``
    fixture, and we assert the handler's return dict is fully
    JSON-serializable with string-valued enum fields.
    """

    def _make_request(self, path: str = "/api/plugins/neuro_core/context_graph"):
        req = types.SimpleNamespace()
        req.path = path
        req.method = "GET"
        return req

    def test_handler_returns_json_safe_response(self, monkeypatch):
        from usr.plugins.neuro_core.api import context_graph as api_mod

        g = _enum_graph()

        # Stub search_context_graph (used at module level by the handler)
        async def _fake_search(**kwargs):
            return g

        monkeypatch.setattr(api_mod, "search_context_graph", _fake_search)

        # Stub Memory.get_by_subdir (also called by the handler)
        async def _fake_get_by_subdir(memory_subdir, **kwargs):
            return types.SimpleNamespace(memory_subdir=memory_subdir)

        fake_memory_cls = types.SimpleNamespace(
            get_by_subdir=_fake_get_by_subdir
        )
        monkeypatch.setattr(api_mod, "Memory", fake_memory_cls)

        handler = api_mod.ContextGraphApi()
        result = asyncio.new_event_loop().run_until_complete(
            handler._get_context_graph(
                input={"query": "what is A?", "memory_subdir": "main"},
                request=self._make_request(),
            )
        )

        # The handler returns a dict that the framework will hand to
        # json.dumps. We reproduce that exact step here.
        encoded = json.dumps(result)
        decoded = json.loads(encoded)

        assert decoded["success"] is True
        assert "context_graph" in decoded
        cg = decoded["context_graph"]

        # All four enum fields must come out as plain strings.
        node_a = cg["nodes"][0]
        assert isinstance(node_a["metadata"]["area"], str)
        assert node_a["metadata"]["area"] == Area.MAIN.value

        assert isinstance(node_a["metadata"]["memory_type"], str)
        assert node_a["metadata"]["memory_type"] == MemoryType.FACT.value

        assert isinstance(node_a["metadata"]["validation_status"], str)
        assert (
            node_a["metadata"]["validation_status"]
            == ValidationStatus.VALIDATED.value
        )

        # Edge ``type`` field is a string, not an enum.
        assert isinstance(cg["edges"][0]["type"], str)
        assert cg["edges"][0]["type"] == "supports"

    def test_handler_does_not_raise_typeerror_with_enum_metadata(self, monkeypatch):
        """Direct repro of the production bug: enum metadata must not crash."""
        from usr.plugins.neuro_core.api import context_graph as api_mod

        g = _enum_graph()

        async def _fake_search(**kwargs):
            return g

        monkeypatch.setattr(api_mod, "search_context_graph", _fake_search)

        async def _fake_get_by_subdir(memory_subdir, **kwargs):
            return types.SimpleNamespace(memory_subdir=memory_subdir)

        monkeypatch.setattr(
            api_mod,
            "Memory",
            types.SimpleNamespace(get_by_subdir=_fake_get_by_subdir),
        )

        handler = api_mod.ContextGraphApi()
        result = asyncio.new_event_loop().run_until_complete(
            handler._get_context_graph(
                input={"query": "q", "memory_subdir": "main"},
                request=self._make_request(),
            )
        )

        # Pre-fix this raised: TypeError: Object of type Area is not JSON
        # serializable when the framework called json.dumps on `result`.
        json.dumps(result)  # must not raise


# ---------------------------------------------------------------------------
# Direct tests for the enum-safe helpers themselves
# ---------------------------------------------------------------------------


class TestEnumSafeHelpers:
    """Unit tests for ``_enum_safe_value`` and ``_enum_safe_asdict``.

    These pin down the contract of the helpers independently of the
    rest of the API code: any ``enum.Enum`` instance must be replaced
    by its ``.value``, containers must be walked, and non-enum scalars
    must pass through unchanged.
    """

    def test_value_enum_is_coerced_to_string(self):
        from usr.plugins.neuro_core.api.context_graph import _enum_safe_value

        assert _enum_safe_value(Area.MAIN) == "main"
        assert _enum_safe_value(MemoryType.FACT) == "fact"
        assert _enum_safe_value(ValidationStatus.VALIDATED) == "validated"
        assert _enum_safe_value(RelationshipType.SUPPORTS) == "supports"

    def test_value_non_enum_passes_through(self):
        from usr.plugins.neuro_core.api.context_graph import _enum_safe_value

        for v in (None, "x", 0, 0.5, True, False, 1 + 2j):
            assert _enum_safe_value(v) == v, f"changed for {v!r}"

    def test_value_dict_walks_recursively(self):
        from usr.plugins.neuro_core.api.context_graph import _enum_safe_value

        src = {"a": Area.MAIN, "b": {"c": MemoryType.FACT}}
        out = _enum_safe_value(src)
        assert out == {"a": "main", "b": {"c": "fact"}}
        for leaf in (out["a"], out["b"]["c"]):
            assert isinstance(leaf, str)
            assert not isinstance(leaf, enum.Enum)

    def test_value_list_walks_recursively(self):
        from usr.plugins.neuro_core.api.context_graph import _enum_safe_value

        src = [Area.MAIN, MemoryType.FACT, {"k": ValidationStatus.VALIDATED}]
        out = _enum_safe_value(src)
        assert out == [
            "main",
            "fact",
            {"k": "validated"},
        ]
        for item in out[:2]:
            assert isinstance(item, str)

    def test_value_tuple_preserves_type(self):
        from usr.plugins.neuro_core.api.context_graph import _enum_safe_value

        src = (Area.MAIN, MemoryType.FACT)
        out = _enum_safe_value(src)
        assert isinstance(out, tuple)
        assert out == ("main", "fact")

    def test_asdict_on_dataclass_with_enum_metadata(self):
        from usr.plugins.neuro_core.api.context_graph import _enum_safe_asdict

        node = GraphNode(
            doc_id="X",
            content="",
            metadata={"area": Area.SOLUTIONS, "memory_type": MemoryType.SKILL},
            score=0.5,
            hop=0,
        )
        out = _enum_safe_asdict(node)
        assert out["doc_id"] == "X"
        assert out["metadata"]["area"] == "solutions"
        assert out["metadata"]["memory_type"] == "skill"
        assert not isinstance(out["metadata"]["area"], enum.Enum)
        assert not isinstance(out["metadata"]["memory_type"], enum.Enum)


# ---------------------------------------------------------------------------
# Tests for the newly fixed handlers: _get_relationships and
# _list_all_relationships (page-load endpoints)
# ---------------------------------------------------------------------------


def _edge_with_enum_metadata() -> GraphEdge:
    """Build a GraphEdge with a metadata dict that contains enum values.

    In production, edge data is derived from FAISS document metadata
    via ``GraphStore.get_edges()`` / ``neighbors()``. The metadata
    dicts passed back through those APIs can contain ``Memory.Area``,
    ``MemoryType``, or ``ValidationStatus`` enum instances — which is
    exactly the pattern that triggered the original
    ``TypeError: Object of type Area is not JSON serializable`` error
    on dashboard page load.
    """
    return GraphEdge(
        from_id="X",
        to_id="Y",
        type="supports",
        confidence=0.9,
    )


def _stub_graph_store_with_enum_edges(monkeypatch, api_mod=None):
    """Patch ``GraphStore`` in the target api module so it returns enum-laden data.

    D42: Relationship routes live in ``api/relationships.py`` (their own
    ApiHandler file), while ``api/context_graph.py`` keeps the
    ``/context_graph`` route. Each module binds ``GraphStore`` at import
    time, so the stub has to be installed into whichever module the test
    is exercising. Pass ``api_mod`` explicitly; defaults to the
    relationships module because the most common caller is the new
    ``RelationshipsApi`` tests.
    """
    if api_mod is None:
        from usr.plugins.neuro_core.api import relationships as api_mod

    class _StubGraphStore:
        def __init__(self, memory_subdir: str):
            self.memory_subdir = memory_subdir

        def get_edges(self, from_id: str):
            return [
                _edge_with_enum_metadata(),
            ]

        def neighbors(self, from_id: str, hops: int = 1):
            # Empty list = no inbound edges (simplifies the test).
            return []

        # _list_all_relationships reads store._data.values() directly.
        _data = {
            "X": [
                _edge_with_enum_metadata(),
            ],
        }

    monkeypatch.setattr(api_mod, "GraphStore", _StubGraphStore)


class TestGetRelationshipsHandler:
    """Tests for ``_get_relationships`` (GET /relationships?id=<memory_id>).

    D42: Relationship routes live in ``api/relationships.py`` (their own
    ApiHandler file), not in ``api/context_graph.py``. Memory ID is
    passed as a query param (``input['id']``) — not as a URL path
    segment — because framework routing treats the third path segment
    as a filename, so ``/relationships/<id>`` resolves to a non-existent
    ``api/relationships/<id>.py`` and 404s.

    This handler is called by the dashboard to list edges for a given
    memory. It serializes edges through ``_serialize_edge`` (already
    fixed) but the response dict itself must also pass through
    ``_enum_safe_value`` to guarantee JSON safety. The test below
    covers the page-load repro: enum-laden edge data must not crash
    ``json.dumps``.
    """

    def test_get_relationships_response_is_json_safe(self, monkeypatch):
        from usr.plugins.neuro_core.api import relationships as api_mod

        _stub_graph_store_with_enum_edges(monkeypatch, api_mod=api_mod)

        handler = api_mod.RelationshipsApi()
        result = asyncio.new_event_loop().run_until_complete(
            handler._get_relationships(
                input={"memory_subdir": "main", "id": "X"},
                request=types.SimpleNamespace(
                    path="/api/plugins/neuro_core/relationships",
                    method="GET",
                ),
            )
        )

        # Must not raise — pre-fix this was:
        # TypeError: Object of type Area is not JSON serializable
        encoded = json.dumps(result)
        assert isinstance(encoded, str)

        decoded = json.loads(encoded)
        assert decoded["success"] is True
        assert decoded["memory_id"] == "X"
        assert isinstance(decoded["edges"], list)
        # Edge fields must be plain strings, not enum instances.
        for edge in decoded["edges"]:
            assert isinstance(edge["from_id"], str)
            assert isinstance(edge["to_id"], str)
            assert isinstance(edge["type"], str)
            assert not isinstance(edge["type"], enum.Enum)


class TestListAllRelationshipsHandler:
    """Tests for ``_list_all_relationships`` (GET /relationships).

    D42: Relationship routes live in ``api/relationships.py`` (their own
    ApiHandler file), not in ``api/context_graph.py``. This handler is
    the primary page-load endpoint for the relationships panel. It
    dumps all stored edges and serializes them in the response. The
    test below asserts the full response is JSON-safe even when the
    underlying sidecar contains enum-laden edge data.
    """

    def test_list_all_relationships_response_is_json_safe(self, monkeypatch):
        from usr.plugins.neuro_core.api import relationships as api_mod

        _stub_graph_store_with_enum_edges(monkeypatch, api_mod=api_mod)

        handler = api_mod.RelationshipsApi()
        result = asyncio.new_event_loop().run_until_complete(
            handler._list_all_relationships(
                input={"memory_subdir": "main"},
                request=types.SimpleNamespace(
                    path="/api/plugins/neuro_core/relationships",
                    method="GET",
                ),
            )
        )

        encoded = json.dumps(result)
        assert isinstance(encoded, str)

        decoded = json.loads(encoded)
        assert decoded["success"] is True
        assert decoded["memory_subdir"] == "main"
        assert isinstance(decoded["edges"], list)
        assert decoded["count"] == len(decoded["edges"])
        for edge in decoded["edges"]:
            assert isinstance(edge["type"], str)
            assert not isinstance(edge["type"], enum.Enum)


class TestProcessRouterGuard:
    """Tests for the process-level single-entry-point guard.

    The ``process()`` method must guarantee that every dict response
    passes through ``_enum_safe_value`` before returning, regardless
    of which handler produced it. These tests cover each dispatch
    branch to ensure the guard fires for all routes.

    D42: Relationship routes are tested against ``RelationshipsApi`` in
    ``api/relationships.py`` (their own ApiHandler file). The two
    context_graph routes (``/context_graph`` and the unknown-route
    error path) are tested against ``ContextGraphApi``.
    """

    def _make_request(self, path: str, method: str = "GET"):
        return types.SimpleNamespace(path=path, method=method)

    def test_process_guards_context_graph_response(self, monkeypatch):
        from usr.plugins.neuro_core.api import context_graph as api_mod

        g = _enum_graph()

        async def _fake_search(**kwargs):
            return g

        monkeypatch.setattr(api_mod, "search_context_graph", _fake_search)

        async def _fake_get_by_subdir(memory_subdir, **kwargs):
            return types.SimpleNamespace(memory_subdir=memory_subdir)

        monkeypatch.setattr(
            api_mod,
            "Memory",
            types.SimpleNamespace(get_by_subdir=_fake_get_by_subdir),
        )

        handler = api_mod.ContextGraphApi()
        result = asyncio.new_event_loop().run_until_complete(
            handler.process(
                input={"query": "q", "memory_subdir": "main"},
                request=self._make_request(
                    "/api/plugins/neuro_core/context_graph"
                ),
            )
        )

        # Must be JSON-safe end-to-end.
        json.dumps(result)
        assert result["success"] is True
        node = result["context_graph"]["nodes"][0]
        assert isinstance(node["metadata"]["area"], str)
        assert not isinstance(node["metadata"]["area"], enum.Enum)

    def test_process_guards_get_relationships_response(self, monkeypatch):
        from usr.plugins.neuro_core.api import relationships as api_mod

        _stub_graph_store_with_enum_edges(monkeypatch, api_mod=api_mod)

        handler = api_mod.RelationshipsApi()
        result = asyncio.new_event_loop().run_until_complete(
            handler.process(
                input={"memory_subdir": "main", "id": "X"},
                request=self._make_request(
                    "/api/plugins/neuro_core/relationships"
                ),
            )
        )

        json.dumps(result)
        assert result["success"] is True
        for edge in result["edges"]:
            assert isinstance(edge["type"], str)

    def test_process_guards_list_all_relationships_response(self, monkeypatch):
        from usr.plugins.neuro_core.api import relationships as api_mod

        _stub_graph_store_with_enum_edges(monkeypatch, api_mod=api_mod)

        handler = api_mod.RelationshipsApi()
        req = self._make_request("/api/plugins/neuro_core/relationships")
        req.args = {}  # no id — routes to _list_all_relationships
        result = asyncio.new_event_loop().run_until_complete(
            handler.process(
                input={"memory_subdir": "main"},
                request=req,
            )
        )

        json.dumps(result)
        assert result["success"] is True
        for edge in result["edges"]:
            assert isinstance(edge["type"], str)

    def test_process_guards_unknown_route_response(self):
        """Error responses (unknown route) also pass through the guard."""
        from usr.plugins.neuro_core.api import context_graph as api_mod

        handler = api_mod.ContextGraphApi()
        result = asyncio.new_event_loop().run_until_complete(
            handler.process(
                input={},
                request=self._make_request(
                    "/api/plugins/neuro_core/nonexistent"
                ),
            )
        )

        assert result["success"] is False
        assert "error" in result
        # Must be JSON-safe even on the error path.
        json.dumps(result)
