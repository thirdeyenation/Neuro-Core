"""Tests for ``ContextGraph`` serialization and the ``search_context_graph``
retrieval pipeline.

Covers:
- ``to_prompt_text()`` serializes all nodes and edges.
- Hop-0 nodes appear before hop-1 nodes in the rendered output.
- A ``ContextGraph`` with empty edges serializes cleanly (no crash,
  explicit "no edges" marker).
- An empty ``ContextGraph`` (no nodes, no edges) serializes without
  crashing.
- ``search_context_graph()`` returns the correct node count when both
  the semantic search and the graph expansion are wired up to mocks.
- ``search_context_graph()`` handles a missing graph_store gracefully
  (semantic seeds only).
- Re-ranking weights influence the final node order.
"""

from __future__ import annotations

import asyncio
import unittest.mock as mock

import pytest

from usr.plugins.neuro_core.helpers.context_graph import (
    ContextGraph,
    GraphEdge,
    GraphNode,
)
from usr.plugins.neuro_core.helpers.graph_store import GraphStore
from usr.plugins.neuro_core.helpers.retrieval import search_context_graph
from usr.plugins.neuro_core.helpers.scores import MemoryScores, ScoreStore


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _FakeDoc:
    """Minimal Document stand-in (avoids langchain_core import)."""

    def __init__(self, doc_id: str, content: str = "", metadata: dict | None = None):
        self.metadata = dict(metadata or {})
        self.metadata.setdefault("id", doc_id)
        self.page_content = content


def _edge(from_id: str, to_id: str, type_: str = "supports", confidence: float = 0.9):
    return GraphEdge(from_id=from_id, to_id=to_id, type=type_, confidence=confidence)


# ---------------------------------------------------------------------------
# ContextGraph.to_prompt_text() — serialization
# ---------------------------------------------------------------------------


class TestContextGraphSerialization:
    def _full_graph(self) -> ContextGraph:
        """Build a 3-hop graph with 2 nodes per hop and 3 edges."""
        nodes = [
            GraphNode(doc_id="A", content="Seed A", metadata={"memory_type": "fact"}, score=0.9, hop=0),
            GraphNode(doc_id="B", content="Seed B", metadata={"memory_type": "fact"}, score=0.7, hop=0),
            GraphNode(doc_id="C", content="Hop1 C", metadata={"memory_type": "concept"}, score=0.5, hop=1),
            GraphNode(doc_id="D", content="Hop1 D", metadata={"memory_type": "concept"}, score=0.4, hop=1),
            GraphNode(doc_id="E", content="Hop2 E", metadata={"memory_type": "event"}, score=0.2, hop=2),
        ]
        edges = [
            _edge("A", "C", "supports", 0.9),
            _edge("A", "D", "related_to", 0.7),
            _edge("C", "E", "derived_from", 0.8),
        ]
        return ContextGraph(
            nodes=nodes,
            edges=edges,
            query="what is A?",
            seed_ids=["A", "B"],
        )

    def test_to_prompt_text_serializes_all_nodes(self):
        g = self._full_graph()
        text = g.to_prompt_text()
        for n in g.nodes:
            assert n.doc_id in text, f"Node {n.doc_id} missing from output"

    def test_to_prompt_text_serializes_all_edges(self):
        g = self._full_graph()
        text = g.to_prompt_text()
        for e in g.edges:
            assert f"{e.from_id} --{e.type}--> {e.to_id}" in text, (
                f"Edge {e.from_id}--{e.type}-->{e.to_id} missing"
            )

    def test_hop0_renders_before_hop1(self):
        g = self._full_graph()
        text = g.to_prompt_text()
        pos_hop0 = text.find("[A]")
        pos_hop1 = text.find("[C]")
        assert pos_hop0 != -1, "hop-0 node A missing"
        assert pos_hop1 != -1, "hop-1 node C missing"
        assert pos_hop0 < pos_hop1, "hop-0 should render before hop-1"

    def test_hops_in_ascending_order(self):
        g = self._full_graph()
        text = g.to_prompt_text()
        pos_hop0 = text.find("[A]")
        pos_hop1 = text.find("[C]")
        pos_hop2 = text.find("[E]")
        assert pos_hop0 < pos_hop1 < pos_hop2

    def test_empty_graph_does_not_crash(self):
        g = ContextGraph()
        text = g.to_prompt_text()
        assert "# ContextGraph" in text
        assert "no nodes or edges found" in text

    def test_graph_with_empty_edges_serializes_cleanly(self):
        g = ContextGraph(
            nodes=[
                GraphNode(doc_id="X", content="x", metadata={}, score=0.5, hop=0),
                GraphNode(doc_id="Y", content="y", metadata={}, score=0.3, hop=1),
            ],
            edges=[],
            query="q",
            seed_ids=["X"],
        )
        text = g.to_prompt_text()
        assert "[X]" in text and "[Y]" in text
        assert "## Edges" in text
        assert "no typed relationships" in text

    def test_nodes_within_hop_sorted_by_score_desc(self):
        g = ContextGraph(
            nodes=[
                GraphNode(doc_id="low",  content="", metadata={}, score=0.1, hop=0),
                GraphNode(doc_id="high", content="", metadata={}, score=0.9, hop=0),
            ],
            edges=[],
            query="q",
            seed_ids=["high"],
        )
        text = g.to_prompt_text()
        pos_high = text.find("[high]")
        pos_low = text.find("[low]")
        assert pos_high < pos_low, "higher score should render first within hop"

    def test_edges_between_unknown_nodes_are_excluded(self):
        g = ContextGraph(
            nodes=[GraphNode(doc_id="A", content="", metadata={}, score=1.0, hop=0)],
            edges=[_edge("X", "Y")],  # neither X nor Y in nodes
            query="q",
            seed_ids=["A"],
        )
        text = g.to_prompt_text()
        assert "X --supports--> Y" not in text


# ---------------------------------------------------------------------------
# ContextGraph — helper methods
# ---------------------------------------------------------------------------


class TestContextGraphHelpers:
    def test_get_node(self):
        g = ContextGraph(
            nodes=[
                GraphNode(doc_id="A", content="", metadata={}, score=1.0, hop=0),
                GraphNode(doc_id="B", content="", metadata={}, score=0.5, hop=0),
            ]
        )
        assert g.get_node("A") is not None
        assert g.get_node("A").doc_id == "A"
        assert g.get_node("missing") is None

    def test_edges_for_includes_both_directions(self):
        g = ContextGraph(
            nodes=[
                GraphNode(doc_id="A", content="", metadata={}, score=1.0, hop=0),
                GraphNode(doc_id="B", content="", metadata={}, score=0.5, hop=0),
            ],
            edges=[
                _edge("A", "B"),
                _edge("B", "A", "related_to"),
            ],
        )
        a_edges = g.edges_for("A")
        assert len(a_edges) == 2


# ---------------------------------------------------------------------------
# search_context_graph — pipeline integration
# ---------------------------------------------------------------------------


def _make_memory_with_seed(seed_docs):
    memory = mock.MagicMock()
    memory.search_similarity_threshold = mock.AsyncMock(return_value=seed_docs)
    memory.db.get_by_ids = mock.MagicMock(return_value=[])
    return memory


def _make_graph_store(neighbor_lists):
    gs = mock.MagicMock(spec=GraphStore)
    gs.neighbors = mock.MagicMock(return_value=neighbor_lists)
    return gs


def _make_score_store(scores_by_id):
    ss = mock.MagicMock(spec=ScoreStore)
    def _get(memory_id):
        if memory_id in scores_by_id:
            return scores_by_id[memory_id]
        return MemoryScores()
    ss.get = mock.MagicMock(side_effect=_get)
    return ss


class TestSearchContextGraph:
    def test_returns_seed_nodes_when_graph_store_is_none(self):
        seed = [
            _FakeDoc("A", content="fact A", metadata={"memory_type": "fact"}),
            _FakeDoc("B", content="fact B", metadata={"memory_type": "fact"}),
        ]
        memory = _make_memory_with_seed(seed)
        g = asyncio.run(
            search_context_graph(
                memory=memory,
                query="what is A?",
                graph_store=None,
                score_store=None,
                config=None,
            )
        )
        assert isinstance(g, ContextGraph)
        assert g.query == "what is A?"
        assert set(g.seed_ids) == {"A", "B"}
        assert {n.doc_id for n in g.nodes} == {"A", "B"}
        assert all(n.hop == 0 for n in g.nodes)
        assert g.edges == []

    def test_expands_graph_up_to_max_hops(self):
        seed = [
            _FakeDoc("A", content="A", metadata={"memory_type": "fact"}),
            _FakeDoc("B", content="B", metadata={"memory_type": "fact"}),
        ]
        nl_a = [
            ("C", 1, _edge("A", "C", "supports", 0.9)),
            ("D", 1, _edge("A", "D", "related_to", 0.7)),
            ("E", 2, _edge("C", "E", "derived_from", 0.8)),
        ]
        nl_b: list = []

        memory = _make_memory_with_seed(seed)
        gs = _make_graph_store([nl_a, nl_b])

        g = asyncio.run(
            search_context_graph(
                memory=memory,
                query="q",
                graph_store=gs,
                score_store=_make_score_store({}),
                config={"graph_max_hops": 2, "graph_neighbors_max": 40},
            )
        )
        ids = {n.doc_id for n in g.nodes}
        assert ids == {"A", "B", "C", "D", "E"}
        by_id = {n.doc_id: n.hop for n in g.nodes}
        assert by_id["A"] == 0 and by_id["B"] == 0
        assert by_id["C"] == 1 and by_id["D"] == 1
        assert by_id["E"] == 2
        edge_keys = {(e.from_id, e.to_id, e.type) for e in g.edges}
        assert ("A", "C", "supports") in edge_keys
        assert ("A", "D", "related_to") in edge_keys
        assert ("C", "E", "derived_from") in edge_keys

    def test_respects_graph_neighbors_max_cap(self):
        seed = [_FakeDoc("A", content="A", metadata={"memory_type": "fact"})]
        nl_a = [
            (f"N{i}", 1, _edge("A", f"N{i}", "related_to", 0.5 + 0.05 * i))
            for i in range(5)
        ]
        memory = _make_memory_with_seed(seed)
        gs = _make_graph_store([nl_a])
        g = asyncio.run(
            search_context_graph(
                memory=memory,
                query="q",
                graph_store=gs,
                score_store=_make_score_store({}),
                config={"graph_max_hops": 1, "graph_neighbors_max": 2},
            )
        )
        assert len(g.nodes) == 3
        ids = {n.doc_id for n in g.nodes}
        assert "A" in ids
        # N4 and N3 have the highest confidence (0.7 and 0.65)
        assert "N4" in ids and "N3" in ids

    def test_recency_score_uses_last_accessed_at(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        old = "2020-01-01T00:00:00+00:00"
        seed = [
            _FakeDoc("recent", content="r", metadata={
                "memory_type": "fact",
                "last_accessed_at": now,
            }),
            _FakeDoc("old", content="o", metadata={
                "memory_type": "fact",
                "last_accessed_at": old,
            }),
        ]
        memory = _make_memory_with_seed(seed)
        g = asyncio.run(
            search_context_graph(
                memory=memory,
                query="q",
                graph_store=None,
                score_store=_make_score_store({}),
                config=None,
            )
        )
        by_id = {n.doc_id: n for n in g.nodes}
        assert by_id["recent"].score > by_id["old"].score

    def test_importance_score_influences_ranking(self):
        seed = [
            _FakeDoc("hi", content="high importance", metadata={"memory_type": "fact"}),
            _FakeDoc("lo", content="low importance", metadata={"memory_type": "fact"}),
        ]
        memory = _make_memory_with_seed(seed)
        scores = {
            "hi": MemoryScores(importance=0.95, confidence=0.7, stability=0.5),
            "lo": MemoryScores(importance=0.05, confidence=0.7, stability=0.5),
        }
        g = asyncio.run(
            search_context_graph(
                memory=memory,
                query="q",
                graph_store=None,
                score_store=_make_score_store(scores),
                config=None,
            )
        )
        by_id = {n.doc_id: n for n in g.nodes}
        assert by_id["hi"].score > by_id["lo"].score

    def test_search_failure_falls_back_to_empty(self):
        memory = mock.MagicMock()
        memory.search_similarity_threshold = mock.AsyncMock(
            side_effect=RuntimeError("simulated backend failure")
        )
        g = asyncio.run(
            search_context_graph(
                memory=memory,
                query="q",
                graph_store=None,
                score_store=None,
                config=None,
            )
        )
        assert g.nodes == []
        assert g.edges == []
        assert g.seed_ids == []

    def test_returns_correct_node_count(self):
        seed = [
            _FakeDoc(f"S{i}", content=str(i), metadata={"memory_type": "fact"})
            for i in range(3)
        ]
        memory = _make_memory_with_seed(seed)
        g = asyncio.run(
            search_context_graph(
                memory=memory,
                query="q",
                graph_store=None,
                score_store=None,
                config=None,
            )
        )
        assert len(g.nodes) == 3
        assert g.seed_ids == ["S0", "S1", "S2"]
