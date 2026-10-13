"""Tests for ``tools/neuro_retrieve.py`` (re-homed, WI-P43, ADR-NC1-003).

The tool now serves the plugin's explainable hybrid pipeline
(``search_context_graph`` over the HOST memory universe) instead of the
retired SQLite-domain backend. Contract coverage:

1. Happy path: a valid ``query`` returns the structured context graph
   (``query``, ``seed_ids``, ``nodes``, ``edges``) with per-node factor
   records whose keys match the pipeline's ranking factors.
2. Invalid input: an empty ``query`` returns an error Response without
   constructing stores or touching host memory.
3. Factor fidelity: hop-0 seeds use ``_semantic_for`` metadata semantics;
   graph neighbors use the pipeline's ``sem = 0.5`` baseline; ``score``
   is the pipeline's node score VERBATIM; importance/confidence are
   sidecar-authoritative.
4. Honest degradation (KI-008): an unavailable ScoreStore yields
   ``neuro_degraded`` markers with metadata fallbacks - never fabricated
   healthy baselines.
5. Gate independence: factor records are served regardless of the
   ``recall_shaping_enabled`` gate (that gate governs only the end-hook
   re-ranking of ordinary host recall).

Store isolation: the conftest ``memory_subdir`` fixture patches
``abs_db_dir`` to a per-test tmp directory (disposable/synthetic fixture
policy); the live ``neuro_core.db`` is never touched. All pipeline and
store collaborators are patched at the modules where the tool lazily
imports them (verified by direct read of tools/neuro_retrieve.py).
"""

from __future__ import annotations

import sys
import types

import pytest


_PROJECT_ROOT = "/a0/usr/plugins/neuro_core"
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

pytestmark = pytest.mark.asyncio


class _FakeScoreStore:
    """Inert ScoreStore double with get_optional semantics."""

    instances: list = []

    def __init__(self, subdir: str):
        self.subdir = subdir
        self.records: dict = dict(_FakeScoreStore._seed_records)
        _FakeScoreStore.instances.append(self)

    _seed_records: dict = {}

    def get_optional(self, doc_id: str):
        return self.records.get(doc_id)


class _FakeGraphStore:
    """Inert GraphStore double (no filesystem access)."""

    instances: list = []

    def __init__(self, subdir: str):
        self.subdir = subdir
        _FakeGraphStore.instances.append(self)


def _make_tool(mod):
    return mod.NeuroRetrieve(
        agent=None, name="neuro_retrieve", method=None,
        args={}, message="", loop_data=None,
    )


def _load_tool_module():
    import importlib

    return importlib.import_module("tools.neuro_retrieve")


def _node(doc_id, hop, score, metadata=None, content="content"):
    return types.SimpleNamespace(
        doc_id=doc_id, hop=hop, score=score,
        metadata=dict(metadata or {}), content=content,
    )


def _edge(from_id, to_id, rel="related_to", confidence=0.9):
    return types.SimpleNamespace(
        from_id=from_id, to_id=to_id, type=rel, confidence=confidence,
    )


def _graph(nodes, edges=None, seed_ids=None, query="alpha"):
    return types.SimpleNamespace(
        query=query,
        seed_ids=list(seed_ids or [n.doc_id for n in nodes if n.hop == 0]),
        nodes=list(nodes),
        edges=list(edges or []),
    )


def _patch_collaborators(monkeypatch, graph, seed_records=None):
    """Patch every lazily-imported collaborator at its source module.

    - plugins._memory.helpers.memory.Memory.get_by_subdir -> fake memory
    - usr.plugins.neuro_core.helpers.graph_store.GraphStore -> inert double
    - usr.plugins.neuro_core.helpers.scores.ScoreStore -> inert double
    - usr.plugins.neuro_core.helpers.retrieval.search_context_graph
      -> returns the supplied ``graph`` (records stores/config seen)
    """
    from plugins._memory.helpers import memory as host_memory_mod
    from usr.plugins.neuro_core.helpers import graph_store as graph_mod
    from usr.plugins.neuro_core.helpers import scores as scores_mod
    from usr.plugins.neuro_core.helpers import retrieval as retrieval_mod

    _FakeScoreStore.instances = []
    _FakeGraphStore.instances = []
    _FakeScoreStore._seed_records = dict(seed_records or {})

    fake_memory = types.SimpleNamespace(memory_subdir="neuro_test")

    async def _fake_get_by_subdir(memory_subdir, log_item=None,
                                  preload_knowledge=True):
        return fake_memory

    monkeypatch.setattr(
        host_memory_mod.Memory, "get_by_subdir", _fake_get_by_subdir,
        raising=False,  # conftest stub class may not predefine this attr
    )
    monkeypatch.setattr(graph_mod, "GraphStore", _FakeGraphStore)
    monkeypatch.setattr(scores_mod, "ScoreStore", _FakeScoreStore)

    async def _fake_pipeline(memory, query, graph_store, score_store, config):
        graph._stores_seen = (graph_store, score_store)
        graph._config_seen = dict(config)
        return graph

    monkeypatch.setattr(
        retrieval_mod, "search_context_graph", _fake_pipeline, raising=True
    )
    return fake_memory


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


async def test_returns_structured_graph_with_factor_records(monkeypatch):
    doc_meta = {
        "id": "doc-1", "area": "main",
        "similarity": 0.8, "importance": 0.9, "confidence": 0.7,
        "validation_status": "validated",
        "timestamp": "2026-09-28T00:00:00+00:00",
    }
    graph = _graph([_node("doc-1", 0, 0.83, doc_meta)], seed_ids=["doc-1"])
    _patch_collaborators(monkeypatch, graph)
    tool = _make_tool(_load_tool_module())

    result = await tool.execute(query="alpha", memory_subdir="neuro_test")

    assert result["query"] == "alpha"
    assert result["seed_ids"] == ["doc-1"]
    node = result["nodes"][0]
    assert node["doc_id"] == "doc-1" and node["hop"] == 0
    factors = node["factors"]
    assert set(factors.keys()) == {
        "similarity", "importance", "confidence", "recency",
        "validation_status", "score",
    }
    # Pipeline's node score verbatim (JSON-rounded only).
    assert factors["score"] == pytest.approx(0.83)
    # No sidecar records exist -> honest degradation marker (KI-008).
    assert node["neuro_degraded"] is True
    assert factors["validation_status"] == "validated"
    assert 0.0 <= factors["recency"] <= 1.0
    # The tool passed the right stores and gated config to the pipeline.
    assert graph._config_seen["semantic_limit"] == 10
    assert graph._config_seen["semantic_threshold"] == pytest.approx(0.6)


async def test_edges_serialized_with_type_and_confidence(monkeypatch):
    graph = _graph(
        [
            _node("a", 0, 0.9, {"id": "a"}),
            _node("b", 1, 0.5, {"id": "b"}),
        ],
        edges=[_edge("a", "b", "supports", 0.85)],
        seed_ids=["a"],
    )
    _patch_collaborators(monkeypatch, graph)
    tool = _make_tool(_load_tool_module())

    result = await tool.execute(query="alpha", memory_subdir="neuro_test")

    assert result["edges"] == [
        {"from_id": "a", "to_id": "b", "type": "supports",
         "confidence": pytest.approx(0.85)},
    ]
    assert [n["hop"] for n in result["nodes"]] == [0, 1]


# ---------------------------------------------------------------------------
# 2. Invalid input
# ---------------------------------------------------------------------------


async def test_empty_query_returns_error_without_touching_stores(monkeypatch):
    from helpers.tool import Response

    _FakeScoreStore.instances = []
    _FakeGraphStore.instances = []

    tool = _make_tool(_load_tool_module())
    result = await tool.execute(query="   ")

    assert isinstance(result, Response)
    assert result.break_loop is False
    assert "query" in result.message.lower()
    assert _FakeScoreStore.instances == []
    assert _FakeGraphStore.instances == []


# ---------------------------------------------------------------------------
# 3. Factor fidelity
# ---------------------------------------------------------------------------


async def test_hop0_similarity_from_metadata_neighbors_get_baseline(monkeypatch):
    graph = _graph(
        [
            _node("seed", 0, 0.7, {"id": "seed", "similarity": 0.9}),
            _node("nbr", 1, 0.4, {"id": "nbr", "similarity": 0.99}),
        ],
        seed_ids=["seed"],
    )
    _patch_collaborators(monkeypatch, graph)
    tool = _make_tool(_load_tool_module())

    result = await tool.execute(query="alpha", memory_subdir="neuro_test")
    by_id = {n["doc_id"]: n for n in result["nodes"]}

    # Hop-0 seed: metadata similarity honored (pipeline semantics).
    assert by_id["seed"]["factors"]["similarity"] == pytest.approx(0.9)
    # Graph neighbor: pipeline's sem = 0.5 baseline — NOT its metadata
    # similarity, which the pipeline never consulted for expansion nodes.
    assert by_id["nbr"]["factors"]["similarity"] == pytest.approx(0.5)
    # Scores are the pipeline's node scores verbatim.
    assert by_id["seed"]["factors"]["score"] == pytest.approx(0.7)
    assert by_id["nbr"]["factors"]["score"] == pytest.approx(0.4)


async def test_sidecar_authoritative_importance_and_confidence(monkeypatch):
    graph = _graph(
        [_node("doc-1", 0, 0.6, {"id": "doc-1", "importance": 0.2})],
        seed_ids=["doc-1"],
    )
    _patch_collaborators(
        monkeypatch, graph,
        seed_records={
            "doc-1": types.SimpleNamespace(importance=0.95, confidence=0.8),
        },
    )
    tool = _make_tool(_load_tool_module())

    result = await tool.execute(query="alpha", memory_subdir="neuro_test")

    factors = result["nodes"][0]["factors"]
    # Sidecar wins over the stale metadata mirror (ADR-NC1-002 authority).
    assert factors["importance"] == pytest.approx(0.95)
    assert factors["confidence"] == pytest.approx(0.8)
    assert result["nodes"][0]["neuro_degraded"] is False


async def test_unavailable_score_store_degrades_honestly(monkeypatch):
    graph = _graph(
        [_node("doc-1", 0, 0.6, {"id": "doc-1"})], seed_ids=["doc-1"],
    )
    from usr.plugins.neuro_core.helpers import scores as scores_mod

    def _boom(subdir):
        raise RuntimeError("sidecar unavailable")

    _patch_collaborators(monkeypatch, graph)
    monkeypatch.setattr(scores_mod, "ScoreStore", _boom)
    tool = _make_tool(_load_tool_module())

    result = await tool.execute(query="alpha", memory_subdir="neuro_test")

    node = result["nodes"][0]
    # Missing store -> metadata fallbacks WITH the degraded marker,
    # never presented as healthy sidecar-backed data (KI-008).
    assert node["neuro_degraded"] is True
    assert node["factors"]["importance"] == pytest.approx(0.5)
    assert node["factors"]["confidence"] == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# 4. Gate independence + invocation defaults
# ---------------------------------------------------------------------------


async def test_tool_independent_of_recall_shaping_gate(monkeypatch):
    """Factor records are served with the gate on AND off."""
    graph = _graph([_node("doc-1", 0, 0.6, {"id": "doc-1"})],
                   seed_ids=["doc-1"])
    _patch_collaborators(monkeypatch, graph)
    tool = _make_tool(_load_tool_module())

    import sys as _sys
    import types as _types

    for gate in (False, True):
        stub = _types.ModuleType("helpers.plugins")
        stub.get_plugin_config = (
            lambda name, agent=None, _g=gate: {"recall_shaping_enabled": _g}
        )
        monkeypatch.setitem(_sys.modules, "helpers.plugins", stub)
        result = await tool.execute(query="alpha", memory_subdir="neuro_test")
        assert result["nodes"][0]["factors"]["score"] == pytest.approx(0.6)


async def test_invocation_args_override_config_defaults(monkeypatch):
    graph = _graph([_node("doc-1", 0, 0.6, {"id": "doc-1"})],
                   seed_ids=["doc-1"])
    _patch_collaborators(monkeypatch, graph)
    tool = _make_tool(_load_tool_module())

    import sys as _sys
    import types as _types

    stub = _types.ModuleType("helpers.plugins")
    stub.get_plugin_config = lambda name, agent=None: {}
    monkeypatch.setitem(_sys.modules, "helpers.plugins", stub)

    result = await tool.execute(query="alpha", limit=4, threshold=0.5,
                                memory_subdir="neuro_test")
    assert result["nodes"][0]["factors"]["score"] == pytest.approx(0.6)
    assert graph._config_seen["semantic_limit"] == 4
    assert graph._config_seen["semantic_threshold"] == pytest.approx(0.5)

    graph._config_seen = None
    await tool.execute(query="alpha", memory_subdir="neuro_test")
    # Documented tool defaults apply when args and config are silent.
    assert graph._config_seen["semantic_limit"] == 10
    assert graph._config_seen["semantic_threshold"] == pytest.approx(0.6)


async def test_unavailable_host_memory_returns_error_response(monkeypatch):
    from helpers.tool import Response
    from plugins._memory.helpers import memory as host_memory_mod

    async def _boom(memory_subdir, log_item=None, preload_knowledge=True):
        raise RuntimeError("faiss down")

    monkeypatch.setattr(host_memory_mod.Memory, "get_by_subdir", _boom,
                        raising=False)
    tool = _make_tool(_load_tool_module())

    result = await tool.execute(query="alpha", memory_subdir="neuro_test")
    assert isinstance(result, Response)
    assert result.break_loop is False
    assert "unavailable" in result.message.lower()
