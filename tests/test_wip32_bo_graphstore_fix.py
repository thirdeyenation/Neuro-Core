"""WI-P32 (KI-018-BO backend + KI-018-BP) — regression tests.

BO: ``GraphStore.neighbors()`` BFS previously dropped any edge whose target
was already discovered, so a second parallel edge of a DIFFERENT type between
an already-connected node pair never reached retrieval/panel (user-visible:
"second edge does not take"). The fix emits edges governed solely by the
full ``(from_id, to_id, type)`` triple — the same key the retrieval build
(``search_context_graph`` edges_by_key) uses — while node expansion remains
governed by the visited set.

BP: the graph panel Advanced Filters "Memory Type" checkbox list previously
used the spec-draft vocabulary (concept/episode/reflection/task/solution/
fragment/observation/summary — mostly nonexistent in the implemented enum);
it must match the IMPLEMENTED ``MemoryType`` enum (helpers/metadata.py)
exactly.

All fixtures use the conftest ``memory_subdir`` disposable tmp_path sandbox;
live neuro_core.db / scores.json / relationships.json are never touched.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from usr.plugins.neuro_core.helpers.graph_store import GraphEdge, GraphStore

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"


# ---------------------------------------------------------------------------
# BO — neighbors() parallel-edge preservation (read path only)
# ---------------------------------------------------------------------------


def _edge(from_id, to_id, type_, seq):
    return GraphEdge(
        from_id=from_id,
        to_id=to_id,
        type=type_,
        weight=1.0,
        confidence=1.0,
        source="test",
        created_at="2026-09-23T00:00:%02dZ" % seq,
    )


class TestBoParallelEdges:
    def _two_parallel_edges(self, subdir: str) -> GraphStore:
        """Required fixture probe scenario: 2 stored edges A->B with
        different rel_types (write path dedupes only on (to_id, type),
        so both persist)."""
        gs = GraphStore(subdir)
        gs.add_edge(_edge("A", "B", "supports", 0))
        gs.add_edge(_edge("A", "B", "contradicts", 1))
        assert len(gs.get_edges("A")) == 2, "store lost an edge at add time"
        return gs

    def test_parallel_edges_preserved_single_seed_two_hops(self, memory_subdir):
        """Probe scenario: neighbors(from_id=['A'], hops=2) yields BOTH
        parallel-edge tuples (previously only the first was emitted)."""
        gs = self._two_parallel_edges(memory_subdir)
        out = gs.neighbors(from_id=["A"], hops=2)
        assert [(t[0], t[1], t[2].type) for t in out] == [
            ("B", 1, "supports"),
            ("B", 1, "contradicts"),
        ]

    def test_parallel_edges_preserved_multi_seed(self, memory_subdir):
        """Seed-pair semantics preserved: with seeds [A, B] the parallel
        A->B edges are still emitted (B is a seed, never re-enqueued)."""
        gs = self._two_parallel_edges(memory_subdir)
        out = gs.neighbors(from_id=["A", "B"], hops=1)
        assert [(t[0], t[1], t[2].type) for t in out] == [
            ("B", 1, "supports"),
            ("B", 1, "contradicts"),
        ]

    def test_triple_dedupe_identical_triple_single_tuple(self, memory_subdir):
        """Dedupe is on the FULL (from, to, type) triple only: add_edge
        replaces on the identical triple, so exactly one tuple is emitted."""
        gs = self._two_parallel_edges(memory_subdir)
        gs.add_edge(_edge("A", "B", "supports", 2))  # same triple, replaced
        out = gs.neighbors(from_id=["A"], hops=1)
        assert [(t[0], t[1], t[2].type) for t in out] == [
            ("B", 1, "supports"),
            ("B", 1, "contradicts"),
        ]

    def test_single_edge_adjacency_unchanged(self, memory_subdir):
        """No-regression: single-edge adjacency output is unchanged."""
        gs = GraphStore(memory_subdir)
        gs.add_edge(_edge("a", "b", "supports", 0))
        out = gs.neighbors("a", max_hops=1)
        assert [(n, hop, e.type) for n, hop, e in out] == [("b", 1, "supports")]
        out2 = gs.neighbors("a", max_hops=2)
        assert [(n, hop, e.type) for n, hop, e in out2] == [("b", 1, "supports")]

    def test_cycle_termination_preserved(self, memory_subdir):
        """Back-edge to an already-visited node is emitted once and the BFS
        still terminates (visited governs expansion, not emission)."""
        gs = GraphStore(memory_subdir)
        gs.add_edge(_edge("a", "b", "supports", 0))
        gs.add_edge(_edge("b", "a", "supports", 1))
        out = gs.neighbors("a", max_hops=5)
        assert [(n, hop, e.type) for n, hop, e in out] == [
            ("b", 1, "supports"),
            ("a", 2, "supports"),
        ]


# ---------------------------------------------------------------------------
# BP — Advanced Filters Memory Type list sourced from implemented enum
# ---------------------------------------------------------------------------


class TestBpMemoryTypeFilters:
    def _memory_type_list(self) -> list[str]:
        src = PANEL.read_text(encoding="utf-8")
        m = re.search(r"x-for=\"t in \[([^\]]*)\]\"", src)
        assert m, "Memory Type checkbox x-for list not found in panel"
        return re.findall(r"'([a-z_]+)'", m.group(1))

    def test_filters_list_matches_implemented_enum_exactly(self):
        """The panel's Memory Type checkbox list must equal the implemented
        MemoryType enum values, in enum declaration order."""
        from usr.plugins.neuro_core.helpers.metadata import MemoryType

        expected = [m.value for m in MemoryType]
        assert self._memory_type_list() == expected

    def test_filters_list_is_the_implemented_eight_value_enum(self):
        """Cross-check the expected list against helpers/metadata.py
        MemoryType (8 values) — the implemented enum, not the spec draft."""
        from usr.plugins.neuro_core.helpers.metadata import MemoryType

        assert {m.value for m in MemoryType} == {
            "fact",
            "concept",
            "task",
            "event",
            "decision",
            "skill",
            "preference",
            "note",
        }

    def test_spec_draft_vocabulary_absent_from_memory_type_filters(self):
        """Spec-draft-only values must not appear in the Memory Type list."""
        got = self._memory_type_list()
        spec_draft = {
            "episode",
            "reflection",
            "solution",
            "fragment",
            "observation",
            "summary",
        }
        assert not (spec_draft & set(got)), got
