"""Tests for ``helpers/retrieval.py`` — the BFS-based ``ContextGraph``
retrieval pipeline.

Covers the four-step pipeline documented in ``NEURO_CORE_SPEC.md §8``:

  1. **Semantic seed resolution** via ``Memory.search_similarity_threshold``
  2. **Graph expansion** via ``GraphStore.neighbors`` BFS (configurable hops)
  3. **Importance-weighted re-ranking** (similarity / importance / recency)
  4. **ContextGraph assembly** (nodes, edges, seed_ids, query)

The tests stub ``Memory`` (no live FAISS) but use the REAL
``GraphStore`` and ``ScoreStore`` pointed at ``tmp_path`` sidecars —
this exercises the actual BFS algorithm and atomic-write sidecar logic
end-to-end. The ``memory_subdir`` fixture from ``conftest.py`` is reused
verbatim so ``abs_db_dir()`` resolves to a per-test temp directory.

Each test is independent (function-scoped fixtures, no shared mutable
state) and uses descriptive names matching the pattern established in
``test_isolation.py`` and ``test_execute.py``.
"""

from __future__ import annotations

import asyncio
import unittest.mock as mock
from dataclasses import dataclass, field
from typing import Any

import pytest

from usr.plugins.neuro_core.helpers.context_graph import (
    ContextGraph,
    GraphNode,
)
from usr.plugins.neuro_core.helpers.graph_store import (
    GraphEdge,
    GraphStore,
)
from usr.plugins.neuro_core.helpers.retrieval import search_context_graph
from usr.plugins.neuro_core.helpers.scores import ScoreStore


# ---------------------------------------------------------------------------
# Test stubs — no live FAISS / langchain_core import
# ---------------------------------------------------------------------------


@dataclass
class _FakeDoc:
    """Minimal Document stand-in (avoids ``langchain_core`` import)."""

    doc_id: str
    content: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # ``_doc_id()`` in retrieval.py reads ``metadata['id']`` first.
        self.metadata.setdefault("id", self.doc_id)
        # ``Document.page_content`` is the canonical content attribute.
        self.page_content = self.content


class _FakeDB:
    """Stub for ``memory.db`` — only ``get_by_ids()`` is exercised."""

    def __init__(self, by_ids: dict[str, _FakeDoc] | None = None) -> None:
        self._by_ids = by_ids or {}

    def get_by_ids(self, ids: list[str]) -> list[_FakeDoc]:
        out: list[_FakeDoc] = []
        for i in ids:
            if i in self._by_ids:
                out.append(self._by_ids[i])
        return out


class _FakeMemory:
    """Stub for ``Memory`` — only ``search_similarity_threshold`` and ``db``
    are exercised by ``search_context_graph``.
    """

    def __init__(
        self,
        seeds: list[_FakeDoc] | None = None,
        by_ids: dict[str, _FakeDoc] | None = None,
        raise_on_search: bool = False,
    ) -> None:
        self._seeds = seeds or []
        self._db = _FakeDB(by_ids or {})
        self._raise = raise_on_search

    async def search_similarity_threshold(
        self,
        query: str,
        limit: int,
        threshold: float,
    ) -> list[_FakeDoc]:
        if self._raise:
            raise RuntimeError("simulated FAISS failure")
        return self._seeds

    @property
    def db(self) -> _FakeDB:
        return self._db


def _edge(
    from_id: str,
    to_id: str,
    type_: str = "supports",
    confidence: float = 0.9,
    source: str = "test",
) -> GraphEdge:
    """Build a ``GraphEdge`` with the field name retrieval.py expects."""
    return GraphEdge(
        from_id=from_id,
        to_id=to_id,
        type=type_,
        confidence=confidence,
        source=source,
    )


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gs_ss(memory_subdir):
    """Return a real ``(GraphStore, ScoreStore)`` pair pointed at the
    per-test tmp_path sidecars. Reuses ``conftest.py``'s ``memory_subdir``
    fixture, which monkey-patches ``abs_db_dir``.
    """
    return GraphStore(memory_subdir), ScoreStore(memory_subdir)


def _run(coro: Any) -> Any:
    """Drive an async coroutine to completion in a fresh event loop."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Semantic seed resolution
# ---------------------------------------------------------------------------


class TestSemanticSeedResolution:
    def test_seed_resolution_returns_semantic_hits_as_hop_zero(self, gs_ss):
        """``search_similarity_threshold`` results become hop-0 seed nodes."""
        graph, scores = gs_ss
        seed_a = _FakeDoc("seed_a", "I am seed A", {"semantic_score": 0.9})
        seed_b = _FakeDoc("seed_b", "I am seed B", {"semantic_score": 0.7})
        memory = _FakeMemory(seeds=[seed_a, seed_b])

        result = _run(
            search_context_graph(memory, "find seeds", graph, scores)
        )

        assert isinstance(result, ContextGraph)
        assert result.query == "find seeds"
        assert sorted(result.seed_ids) == ["seed_a", "seed_b"]
        node_by_id = {n.doc_id: n for n in result.nodes}
        assert set(node_by_id) == {"seed_a", "seed_b"}
        # Seeds are always hop=0 with hop-ascending sort placing them first
        for n in result.nodes:
            assert n.hop == 0
            assert n.content  # page_content round-trips through the stub
        # No graph edges in this subdir, so result.edges is empty
        assert result.edges == []


# ---------------------------------------------------------------------------
# 2. BFS hop expansion (hop 1, hop 2, beyond max_hops)
# ---------------------------------------------------------------------------


class TestBFSExpansion:
    def test_bfs_expansion_includes_hop_one_neighbors(self, gs_ss):
        """Direct neighbors of any seed are included with ``hop=1``."""
        graph, scores = gs_ss
        seed = _FakeDoc("seed")
        nb1 = _FakeDoc("nb1", "neighbor 1 content")
        nb2 = _FakeDoc("nb2", "neighbor 2 content")
        graph.add_edge(_edge("seed", "nb1"))
        graph.add_edge(_edge("seed", "nb2"))
        memory = _FakeMemory(
            seeds=[seed], by_ids={"nb1": nb1, "nb2": nb2}
        )

        result = _run(
            search_context_graph(memory, "test", graph, scores)
        )

        node_by_id = {n.doc_id: n for n in result.nodes}
        assert node_by_id["seed"].hop == 0
        assert node_by_id["nb1"].hop == 1
        assert node_by_id["nb2"].hop == 1
        # ``memory.db.get_by_ids`` is called for each new neighbor;
        # the resulting ``page_content`` is what the agent sees.
        assert node_by_id["nb1"].content == "neighbor 1 content"
        assert node_by_id["nb2"].content == "neighbor 2 content"
        # Both edges are recorded in the ContextGraph
        edge_types = {(e.from_id, e.to_id) for e in result.edges}
        assert ("seed", "nb1") in edge_types
        assert ("seed", "nb2") in edge_types

    def test_bfs_expansion_includes_hop_two_neighbors(self, gs_ss):
        """A 2-hop BFS correctly tags far-side neighbors as ``hop=2``."""
        graph, scores = gs_ss
        seed = _FakeDoc("seed")
        mid = _FakeDoc("mid")
        far = _FakeDoc("far")
        graph.add_edge(_edge("seed", "mid"))
        graph.add_edge(_edge("mid", "far"))
        memory = _FakeMemory(
            seeds=[seed], by_ids={"mid": mid, "far": far}
        )

        result = _run(
            search_context_graph(
                memory, "test", graph, scores,
                config={"graph_max_hops": 2},
            )
        )

        node_by_id = {n.doc_id: n for n in result.nodes}
        assert node_by_id["seed"].hop == 0
        assert node_by_id["mid"].hop == 1
        assert node_by_id["far"].hop == 2
        # Both edges are present
        assert len(result.edges) == 2

    def test_bfs_expansion_excludes_nodes_beyond_max_hops(self, gs_ss):
        """Nodes past ``graph_max_hops`` are NOT traversed or added."""
        graph, scores = gs_ss
        seed = _FakeDoc("seed")
        h1 = _FakeDoc("h1")
        h2 = _FakeDoc("h2")
        h3 = _FakeDoc("h3")  # beyond the cap — must NOT appear
        graph.add_edge(_edge("seed", "h1"))
        graph.add_edge(_edge("h1", "h2"))
        graph.add_edge(_edge("h2", "h3"))
        memory = _FakeMemory(
            seeds=[seed],
            by_ids={"h1": h1, "h2": h2, "h3": h3},
        )

        result = _run(
            search_context_graph(
                memory, "test", graph, scores,
                config={"graph_max_hops": 2},
            )
        )

        node_ids = {n.doc_id for n in result.nodes}
        assert {"seed", "h1", "h2"} <= node_ids
        assert "h3" not in node_ids
        # h2→h3 edge should not have been traversed
        assert not any(
            e.from_id == "h2" and e.to_id == "h3" for e in result.edges
        )


# ---------------------------------------------------------------------------
# 3. Importance-weighted re-ranking
# ---------------------------------------------------------------------------


class TestRanking:
    def test_ranking_orders_nodes_by_score_descending(self, gs_ss):
        """Final sort is ``(-score, doc_id)``: highest score first.

        Score formula (from ``retrieval.py``):
            score = sim_w * sem + imp_w * imp + rec_w * rec
        With default weights ``(0.5, 0.3, 0.2)`` and the same
        importance/recency for all docs, the ranking reduces to
        ``semantic_score`` order.
        """
        graph, scores = gs_ss
        high = _FakeDoc("high", metadata={"semantic_score": 1.0})
        mid = _FakeDoc("mid", metadata={"semantic_score": 0.7})
        low = _FakeDoc("low", metadata={"semantic_score": 0.3})
        # Intentionally out of order in the seed list — the retriever
        # must re-rank, not preserve insertion order.
        memory = _FakeMemory(seeds=[low, mid, high])

        result = _run(
            search_context_graph(memory, "test", graph, scores)
        )

        order = [n.doc_id for n in result.nodes]
        assert order.index("high") < order.index("mid") < order.index("low")
        # Highest score is 0.75, lowest is 0.40 — verify by node score
        score_by_id = {n.doc_id: n.score for n in result.nodes}
        assert score_by_id["high"] > score_by_id["mid"] > score_by_id["low"]

    def test_ranking_uses_importance_from_score_store_when_available(self, gs_ss):
        """``ScoreStore`` importance overrides the metadata fallback."""
        graph, scores = gs_ss
        # ``scores.set()`` writes a per-id importance into scores.json
        scores.set("important_seed", importance=1.0, confidence=1.0, stability=1.0)
        scores.set("trivial_seed", importance=0.0, confidence=1.0, stability=1.0)
        # Both seeds carry the same semantic_score, so their rank must be
        # decided entirely by the score-store importance.
        a = _FakeDoc("important_seed", metadata={"semantic_score": 0.5})
        b = _FakeDoc("trivial_seed", metadata={"semantic_score": 0.5})
        memory = _FakeMemory(seeds=[b, a])  # intentionally reversed

        result = _run(
            search_context_graph(memory, "test", graph, scores)
        )

        order = [n.doc_id for n in result.nodes]
        assert order.index("important_seed") < order.index("trivial_seed")


# ---------------------------------------------------------------------------
# 4. rel_type filtering
# ---------------------------------------------------------------------------


class TestRelTypeFiltering:
    def test_rel_type_filter_traverses_only_matching_edges(self, gs_ss):
        """``GraphStore.neighbors(rel_type=...)`` returns only matching
        edges. This is the BFS primitive that ``search_context_graph``
        would use if/when rel_type is added to the config surface; the
        test pins the underlying behavior so a future call site is safe.
        """
        graph, scores = gs_ss
        # Same source node, three different edge types.
        graph.add_edge(_edge("seed", "sup", type_="supports"))
        graph.add_edge(_edge("seed", "con", type_="contradicts"))
        graph.add_edge(_edge("seed", "rel", type_="related_to"))

        # No filter: all three neighbors are returned.
        all_n = graph.neighbors(from_id="seed", max_hops=1)
        assert {n[0] for n in all_n} == {"sup", "con", "rel"}

        # ``rel_type='supports'`` keeps only the supports edge.
        sup_n = graph.neighbors(
            from_id="seed", max_hops=1, rel_type="supports"
        )
        assert len(sup_n) == 1
        assert sup_n[0][0] == "sup"
        assert sup_n[0][2].type == "supports"

        # ``rel_type='contradicts'`` keeps only the contradicts edge.
        con_n = graph.neighbors(
            from_id="seed", max_hops=1, rel_type="contradicts"
        )
        assert len(con_n) == 1
        assert con_n[0][0] == "con"

        # A type with no matches returns an empty list (not an error).
        none_n = graph.neighbors(
            from_id="seed", max_hops=1, rel_type="depends_on"
        )
        assert none_n == []


# ---------------------------------------------------------------------------
# 5. ``graph_neighbors_max`` capacity cap
# ---------------------------------------------------------------------------


class TestCapacityLimits:
    def test_graph_neighbors_max_caps_additional_nodes(self, gs_ss):
        """``graph_neighbors_max`` caps *additional* graph neighbors;
        seeds are always retained regardless of capacity.

        The flat neighbor list is sorted ``(hop asc, -confidence desc)``
        before the cap is applied, so the highest-confidence neighbors
        win the capacity battle.
        """
        graph, scores = gs_ss
        seed = _FakeDoc("seed")
        # 10 neighbors with strictly decreasing confidence (n0 highest).
        for i in range(10):
            graph.add_edge(
                _edge("seed", f"n{i}", confidence=1.0 - i * 0.1)
            )
        memory = _FakeMemory(seeds=[seed])

        result = _run(
            search_context_graph(
                memory, "test", graph, scores,
                config={"graph_neighbors_max": 3},
            )
        )

        node_ids = {n.doc_id for n in result.nodes}
        # Seed is always retained (does not count toward the cap).
        assert "seed" in node_ids
        # Exactly 3 additional neighbors survived the cap.
        added = node_ids - {"seed"}
        assert len(added) == 3
        # The 3 highest-confidence neighbors win (n0, n1, n2).
        assert added == {"n0", "n1", "n2"}
        # All 10 edges are still recorded in the result (the edge
        # registry is decoupled from the node cap).
        assert len(result.edges) == 10


# ---------------------------------------------------------------------------
# 6. No neighbors / empty graph
# ---------------------------------------------------------------------------


class TestNoNeighbors:
    def test_seed_with_no_graph_edges_returns_just_seeds(self, gs_ss):
        """A graph that exists but has no edges touching the seed
        returns just the seed nodes (no expansion, no error).
        """
        graph, scores = gs_ss
        # Add edges that don't touch the seed.
        graph.add_edge(_edge("a", "b"))
        graph.add_edge(_edge("c", "d"))
        seed = _FakeDoc("seed")
        memory = _FakeMemory(seeds=[seed])

        result = _run(
            search_context_graph(memory, "test", graph, scores)
        )

        assert [n.doc_id for n in result.nodes] == ["seed"]
        assert result.edges == []
        assert result.seed_ids == ["seed"]

    def test_empty_graph_subdir_returns_seed_only(self, gs_ss):
        """A subdir with no relationships whatsoever returns seeds
        only, with empty edges and an empty edges registry — no error.
        """
        graph, scores = gs_ss  # no add_edge() calls
        seed = _FakeDoc("seed")
        memory = _FakeMemory(seeds=[seed])

        result = _run(
            search_context_graph(memory, "test", graph, scores)
        )

        assert [n.doc_id for n in result.nodes] == ["seed"]
        assert result.edges == []
        assert result.seed_ids == ["seed"]
        assert result.query == "test"


# ---------------------------------------------------------------------------
# 7. Deduplication across multiple BFS paths
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_node_reachable_via_multiple_paths_appears_once(self, gs_ss):
        """A node reachable through several BFS paths appears exactly
        once in the result. The first (highest-confidence) path wins,
        so the node keeps the *earliest* hop distance observed.
        """
        graph, scores = gs_ss
        # Both ``a`` and ``b`` are hop-1 neighbors of ``seed``; both
        # point to ``c`` at hop-2. ``a→c`` has higher confidence.
        graph.add_edge(_edge("seed", "a"))
        graph.add_edge(_edge("seed", "b"))
        graph.add_edge(_edge("a", "c", confidence=0.95))
        graph.add_edge(_edge("b", "c", confidence=0.50))
        memory = _FakeMemory(
            seeds=[_FakeDoc("seed")],
            by_ids={
                "a": _FakeDoc("a"),
                "b": _FakeDoc("b"),
                "c": _FakeDoc("c"),
            },
        )

        result = _run(
            search_context_graph(memory, "test", graph, scores)
        )

        doc_ids = [n.doc_id for n in result.nodes]
        # Each id appears exactly once.
        assert doc_ids.count("seed") == 1
        assert doc_ids.count("a") == 1
        assert doc_ids.count("b") == 1
        assert doc_ids.count("c") == 1
        # ``c`` was added via ``a`` (higher confidence) so hop=2.
        c_node = next(n for n in result.nodes if n.doc_id == "c")
        assert c_node.hop == 2


# ---------------------------------------------------------------------------
# 8. Error tolerance / missing dependencies
# ---------------------------------------------------------------------------


class TestErrorTolerance:
    def test_search_similarity_threshold_exception_returns_empty(self, gs_ss):
        """If the semantic search raises, retrieval must NOT crash.

        ``retrieval.py`` wraps the search call in a broad ``except``
        and falls back to an empty seed list, so the rest of the
        pipeline (graph expansion, ranking) is skipped gracefully.
        """
        graph, scores = gs_ss
        # Edges that would normally produce graph expansion.
        graph.add_edge(_edge("a", "b"))
        graph.add_edge(_edge("b", "c"))
        memory = _FakeMemory(raise_on_search=True)

        result = _run(
            search_context_graph(memory, "test", graph, scores)
        )

        assert isinstance(result, ContextGraph)
        assert result.nodes == []
        assert result.edges == []
        assert result.seed_ids == []
        assert result.query == "test"

    def test_no_graph_store_returns_seeds_only(self, gs_ss):
        """Passing ``graph_store=None`` skips expansion but still
        returns the semantic seeds with their scores.
        """
        _, scores = gs_ss
        seed = _FakeDoc("seed", metadata={"semantic_score": 0.8})
        memory = _FakeMemory(seeds=[seed])

        result = _run(
            search_context_graph(
                memory, "test", graph_store=None, score_store=scores
            )
        )

        assert [n.doc_id for n in result.nodes] == ["seed"]
        assert result.edges == []
        assert result.seed_ids == ["seed"]
        # Re-ranking still applied to the seed node.
        assert result.nodes[0].score > 0.0

    def test_no_score_store_falls_back_to_metadata_importance(self, gs_ss):
        """With no ``ScoreStore``, importance comes from the doc's
        own metadata (``importance``) or defaults to 0.5.
        """
        graph, _ = gs_ss
        seed = _FakeDoc("seed", metadata={"semantic_score": 0.5})
        memory = _FakeMemory(seeds=[seed])

        result = _run(
            search_context_graph(
                memory, "test", graph, score_store=None
            )
        )

        # No crash, seed present, score computed with the 0.5 fallback.
        assert len(result.nodes) == 1
        assert result.nodes[0].score == pytest.approx(
            0.5 * 0.5 + 0.3 * 0.5 + 0.2 * 0.5  # 0.60
        )
