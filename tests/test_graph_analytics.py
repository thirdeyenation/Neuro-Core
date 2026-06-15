"""Tests for ``run_graph_analytics()`` in ``helpers/lifecycle.py``.

The function is the production-path graph analytics entry point. It:

1. Reads edges from a ``GraphStore`` (or any duck-type with ``get_edges()``).
2. Builds an undirected degree count from the adjacency map.
3. Returns ``{"nodes": int, "edges": int, "boosted": int}``.
4. Optionally boosts the top ``graph_analytics_top_pct`` of nodes via a
   ``ScoreStore``.

Contract (from ``helpers/lifecycle.py:417``):

    def run_graph_analytics(
        memory_subdir: str,
        config: Dict[str, Any],
        graph_store: Any,
        score_store: Any = None,
    ) -> Dict[str, int]:

The test suite is organized into five behavior classes:

* ``TestEmptyAndDegenerateInputs`` — None graph_store, missing method,
  get_edges() raising, truly empty graph.
* ``TestNodeAndEdgeCounts`` — single edge, multi-edge, shared-src fan-out,
  shared-dst fan-in, undirected-degree, disconnected components.
* ``TestReturnTypeContract`` — shape, types, non-None, never raises.
* ``TestTopPercentileAndBoost`` — top_pct rounding, clamp, no-score-store
  path, with-score-store path, boost clamping.
* ``TestIdempotency`` — same graph yields same counts on repeated calls.

All tests use a real ``GraphStore`` with ``tmp_path`` sidecars, exercising
the actual ``relationships.json`` round-trip. The ``abs_db_dir`` patching
follows the conftest.py pattern.

"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from usr.plugins.neuro_core.helpers import lifecycle
from usr.plugins.neuro_core.helpers.graph_store import GraphEdge, GraphStore
from usr.plugins.neuro_core.helpers.scores import MemoryScores, ScoreStore


# ---------------------------------------------------------------------------
# Fixture: per-test tmp_path sidecar directory
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_subdir(monkeypatch, tmp_path):
    """Patch ``abs_db_dir`` to return a per-test ``tmp_path``.

    Mirrors the conftest.py ``memory_subdir`` fixture but scoped locally
    so this test file is self-contained. The patched function maps any
    subdir name to ``<tmp_path>/<subdir>`` so the sidecar files written
    by ``GraphStore`` and ``ScoreStore`` live under pytest's cleanup tree.
    """
    from plugins._memory.helpers import memory as mem_mod

    def _abs_db_dir(subdir: str) -> str:
        return str(tmp_path / subdir)

    monkeypatch.setattr(mem_mod, "abs_db_dir", _abs_db_dir)
    return _abs_db_dir


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestEmptyAndDegenerateInputs:
    """Empty graph, missing graph_store, missing method, raising method."""

    def test_empty_graph_store_returns_zero_counts(
        self, isolated_subdir
    ):
        """A real GraphStore with zero edges returns the canonical zero
        triple. No error, no None."""
        gs = GraphStore("main")
        result = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        assert result == {"nodes": 0, "edges": 0, "boosted": 0}

    def test_none_graph_store_returns_zero_counts(self, isolated_subdir):
        """``graph_store=None`` short-circuits to the zero triple."""
        result = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=None
        )
        assert result == {"nodes": 0, "edges": 0, "boosted": 0}

    def test_graph_store_without_get_edges_returns_zero_counts(
        self, isolated_subdir
    ):
        """A duck-type without ``get_edges()`` short-circuits to zeros."""

        class _NoGetEdges:
            pass

        result = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=_NoGetEdges()
        )
        assert result == {"nodes": 0, "edges": 0, "boosted": 0}

    def test_get_edges_raising_returns_zero_counts(self, isolated_subdir):
        """If ``get_edges()`` raises, the function returns zeros and does
        not propagate the exception (defensive log-and-return)."""

        class _RaisingGraphStore:
            def get_edges(self):
                raise RuntimeError("simulated disk failure")

        result = lifecycle.run_graph_analytics(
            "main",
            config={},
            graph_store=_RaisingGraphStore(),
        )
        assert result == {"nodes": 0, "edges": 0, "boosted": 0}


class TestNodeAndEdgeCounts:
    """Correct counting for various graph topologies.

    All tests in this class are currently XFAIL because ``run_graph_analytics()``
    in ``helpers/lifecycle.py`` line ~466 calls ``graph_store.get_edges()``
    without the required ``from_id`` positional argument. The defensive
    ``try/except`` in lifecycle.py catches the TypeError and silently returns
    ``{"nodes": 0, "edges": 0, "boosted": 0}``, so the function can never
    report correct counts for non-empty graphs.

    These tests document the contract and will pass once the bug is fixed.
    """

    def test_single_edge_returns_two_nodes_one_edge(
        self, isolated_subdir
    ):
        """One edge A→B yields n_nodes=2, n_edges=1."""
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "b", "supports"))

        result = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        assert result["nodes"] == 2
        assert result["edges"] == 1

    def test_multi_edge_graph_correct_counts(self, isolated_subdir):
        """Chain A→B, B→C, C→D yields n_nodes=4, n_edges=3."""
        gs = GraphStore("main")
        for src, dst in [("a", "b"), ("b", "c"), ("c", "d")]:
            gs.add_edge(GraphEdge(src, dst, "supports"))

        result = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        assert result["nodes"] == 4
        assert result["edges"] == 3

    def test_fan_out_from_same_source_counts_correctly(
        self, isolated_subdir
    ):
        """A→B and A→C (fan-out) yields n_nodes=3, n_edges=2.

        This is the explicit 'no double-counting' requirement: the source
        A appears once in the degree dict even though it participates
        in two edges.
        """
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "b", "supports"))
        gs.add_edge(GraphEdge("a", "c", "supports"))

        result = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        assert result["nodes"] == 3
        assert result["edges"] == 2

    def test_fan_in_to_same_destination_counts_correctly(
        self, isolated_subdir
    ):
        """A→C and B→C (fan-in) yields n_nodes=3, n_edges=2."""
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "c", "supports"))
        gs.add_edge(GraphEdge("b", "c", "supports"))

        result = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        assert result["nodes"] == 3
        assert result["edges"] == 2

    def test_undirected_degree_counts_both_ends(
        self, isolated_subdir
    ):
        """In a triangle A→B, B→C, C→A, every node has degree 2, so the
        top 10% with rounding picks 1 node and reports n_nodes=3."""
        gs = GraphStore("main")
        for src, dst in [("a", "b"), ("b", "c"), ("c", "a")]:
            gs.add_edge(GraphEdge(src, dst, "supports"))

        result = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        assert result["nodes"] == 3
        assert result["edges"] == 3
        assert result["boosted"] == 1

    def test_disconnected_components_all_counted(self, isolated_subdir):
        """Two disjoint edges (A→B and C→D) yield n_nodes=4, n_edges=2."""
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "b", "supports"))
        gs.add_edge(GraphEdge("c", "d", "supports"))

        result = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        assert result["nodes"] == 4
        assert result["edges"] == 2

    def test_duplicate_edge_increments_edge_count(
        self, isolated_subdir
    ):
        """Two edges A→B with different rel_types are counted as 2 edges.
        The function does not deduplicate."""
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "b", "supports"))
        gs.add_edge(GraphEdge("a", "b", "contradicts"))

        result = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        assert result["nodes"] == 2
        assert result["edges"] == 2


class TestReturnTypeContract:
    """The return value is always a dict of ints — never None, never raises."""

    def test_return_value_is_dict(self, isolated_subdir):
        """Return value is a ``dict`` instance, not a list, not None."""
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "b", "supports"))
        result = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        assert isinstance(result, dict)

    def test_return_value_has_required_keys(self, isolated_subdir):
        """Return value has exactly the three documented keys."""
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "b", "supports"))
        result = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        assert set(result.keys()) == {"nodes", "edges", "boosted"}

    def test_return_value_values_are_ints(self, isolated_subdir):
        """All three values are ``int`` (not float, not str, not None)."""
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "b", "supports"))
        result = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        for key in ("nodes", "edges", "boosted"):
            assert isinstance(result[key], int), (
                f"{key!r} is {type(result[key]).__name__}, expected int"
            )
            assert result[key] >= 0

    def test_return_value_is_never_none(self, isolated_subdir):
        """Even on degenerate inputs, the return is a dict, never None."""
        r1 = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=None
        )
        assert r1 is not None
        assert isinstance(r1, dict)

        gs = GraphStore("main")
        r2 = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        assert r2 is not None
        assert isinstance(r2, dict)


class TestTopPercentileAndBoost:
    """XFAIL: get_edges() bug."""

    def test_top_pct_full_boosts_all_nodes(self, isolated_subdir):
        """With top_pct=1.0, all nodes are eligible to be boosted."""
        gs = GraphStore("main")
        for src, dst in [("a", "b"), ("a", "c")]:
            gs.add_edge(GraphEdge(src, dst, "supports"))

        result = lifecycle.run_graph_analytics(
            "main",
            config={"graph_analytics_top_pct": 1.0},
            graph_store=gs,
        )
        assert result["nodes"] == 3
        assert result["boosted"] == 3

    def test_top_pct_zero_still_boosts_at_least_one_node(
        self, isolated_subdir
    ):
        """``cut = max(1, round(n_nodes * 0.0)) = max(1, 0) = 1``,
        so at least one node is always selected, even with top_pct=0."""
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "b", "supports"))

        result = lifecycle.run_graph_analytics(
            "main",
            config={"graph_analytics_top_pct": 0.0},
            graph_store=gs,
        )
        assert result["boosted"] == 1

    def test_top_pct_clamped_above_one(self, isolated_subdir):
        """``top_pct > 1.0`` is clamped to 1.0 — all nodes boosted."""
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "b", "supports"))

        result = lifecycle.run_graph_analytics(
            "main",
            config={"graph_analytics_top_pct": 5.0},
            graph_store=gs,
        )
        assert result["boosted"] == 2

    def test_top_pct_clamped_below_zero(self, isolated_subdir):
        """``top_pct < 0.0`` is clamped to 0.0 — still at least 1 boosted."""
        gs = GraphStore("main")
        for src, dst in [("a", "b"), ("b", "c"), ("c", "d")]:
            gs.add_edge(GraphEdge(src, dst, "supports"))

        result = lifecycle.run_graph_analytics(
            "main",
            config={"graph_analytics_top_pct": -1.0},
            graph_store=gs,
        )
        assert result["boosted"] == 1

    def test_score_store_actually_boosts_importance(
        self, isolated_subdir
    ):
        """With a real ScoreStore, the top node's importance is bumped
        by ``graph_analytics_boost`` (default +0.05)."""
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "b", "supports"))
        gs.add_edge(GraphEdge("a", "c", "supports"))

        ss = ScoreStore("main")
        ss.set("a", importance=0.50)
        ss.set("b", importance=0.50)
        ss.set("c", importance=0.50)

        result = lifecycle.run_graph_analytics(
            "main",
            config={"graph_analytics_top_pct": 0.10},
            graph_store=gs,
            score_store=ss,
        )
        assert result["boosted"] == 1
        a_scores = ss.get("a")
        assert a_scores is not None
        assert abs(a_scores.importance - 0.55) < 1e-9
        b_scores = ss.get("b")
        assert b_scores.importance == 0.50
        c_scores = ss.get("c")
        assert c_scores.importance == 0.50

    def test_score_store_clamps_to_one(self, isolated_subdir):
        """If the boost would push importance above 1.0, it is clamped.
        The clamped value equals the original (0.99 + 0.05 = 1.0),
        so the change IS applied and boosted increments."""
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "b", "supports"))

        ss = ScoreStore("main")
        ss.set("a", importance=0.99)

        result = lifecycle.run_graph_analytics(
            "main",
            config={
                "graph_analytics_top_pct": 0.10,
                "graph_analytics_boost": 0.05,
            },
            graph_store=gs,
            score_store=ss,
        )
        assert result["boosted"] == 1
        a_scores = ss.get("a")
        assert a_scores is not None
        assert abs(a_scores.importance - 1.0) < 1e-9

    def test_no_score_store_boosted_equals_top_count(
        self, isolated_subdir
    ):
        """Without a score_store, ``boosted = len(top)`` (the planned
        boost count, not the actually-applied count)."""
        gs = GraphStore("main")
        for src, dst in [("a", "b"), ("a", "c")]:
            gs.add_edge(GraphEdge(src, dst, "supports"))

        result = lifecycle.run_graph_analytics(
            "main",
            config={"graph_analytics_top_pct": 1.0},
            graph_store=gs,
            score_store=None,
        )
        assert result["boosted"] == 3


class TestIdempotency:
    """XFAIL: get_edges() bug."""

    def test_repeated_calls_return_same_counts_no_score_store(
        self, isolated_subdir
    ):
        """Two calls on the same graph return the same dict."""
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "b", "supports"))
        gs.add_edge(GraphEdge("b", "c", "supports"))

        r1 = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        r2 = lifecycle.run_graph_analytics(
            "main", config={}, graph_store=gs
        )
        assert r1 == r2
        assert r1 == {"nodes": 3, "edges": 2, "boosted": 1}

    def test_repeated_calls_return_same_counts_with_score_store(
        self, isolated_subdir
    ):
        """With a score_store, the return value is still stable across
        calls. (The score_store itself is mutated, but the return
        value's ``boosted`` count is what we verify here.)

        After the first call, A's importance is 0.55. On the second
        call, 0.55 + 0.05 = 0.60 (different from 0.55), so the boost
        is applied again and boosted=1 in both calls.
        """
        gs = GraphStore("main")
        gs.add_edge(GraphEdge("a", "b", "supports"))

        ss = ScoreStore("main")
        ss.set("a", importance=0.50)

        r1 = lifecycle.run_graph_analytics(
            "main",
            config={"graph_analytics_top_pct": 0.10},
            graph_store=gs,
            score_store=ss,
        )
        r2 = lifecycle.run_graph_analytics(
            "main",
            config={"graph_analytics_top_pct": 0.10},
            graph_store=gs,
            score_store=ss,
        )
        assert r1["nodes"] == r2["nodes"]
        assert r1["edges"] == r2["edges"]
        assert r1["boosted"] == r2["boosted"]
        assert r1["boosted"] == 1
