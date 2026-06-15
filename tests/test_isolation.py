"""Tests for Neuro Core memory_subdir isolation.

These tests close the cross-subdir contamination gap. The current 191 tests
all use a single ``memory_subdir`` (the ``memory_subdir`` fixture from
``conftest.py``) and therefore never prove that writes in one subdir are
invisible to reads in another. This file adds explicit cross-subdir tests
for both ``GraphStore`` (``relationships.json`` sidecar) and
``ScoreStore`` (``scores.json`` sidecar).

Coverage:

1. ``GraphStore`` — adding an edge in subdir A is invisible to subdir B.
2. ``GraphStore`` — removing edges in subdir A does not affect subdir B.
3. ``ScoreStore`` — setting a score in subdir A is invisible to subdir B.
4. ``ScoreStore`` — updating a score in subdir A does not affect subdir B.
5. Simultaneous subdir operations — independent state after writes to both.
6. ``GraphStore`` — reading from a never-written subdir returns empty state
   (and never returns data from another subdir).
7. ``ScoreStore`` — reading from a never-written subdir returns default
   scores (and never returns data from another subdir).

Fixture pattern:
    This file uses an ``isolated_subdirs`` fixture (function-scoped) that
    takes ``tmp_path`` and patches ``plugins._memory.helpers.memory.abs_db_dir``
    via ``unittest.mock.patch`` exactly the way ``conftest.py`` does for the
    single-subdir ``memory_subdir`` fixture. The side effect routes
    ``"a"`` and ``"b"`` to two sibling directories under ``tmp_path``,
    matching the per-subdir isolation contract that the production
    helpers (``_relationships_path``, ``_scores_path``) rely on.
"""

from __future__ import annotations

import unittest.mock as mock

import pytest

from usr.plugins.neuro_core.helpers.graph_store import GraphEdge, GraphStore
from usr.plugins.neuro_core.helpers.scores import MemoryScores, ScoreStore


# ---------------------------------------------------------------------------
# Fixture: two isolated memory_subdirs backed by separate tmp_path dirs
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_subdirs(tmp_path):
    """Yield a mapping of subdir name → on-disk directory.

    Patches ``plugins._memory.helpers.memory.abs_db_dir`` with a side
    effect that routes ``"a"`` to one tmp_path subdirectory and ``"b"``
    to another. This is the same ``mock.patch("plugins._memory.helpers.memory.abs_db_dir", ...)``
    pattern that ``conftest.py`` uses for the single-subdir
    ``memory_subdir`` fixture — see conftest.py for the rationale
    (the helpers import ``abs_db_dir`` lazily inside their path
    resolver functions, so patching the source module attribute is
    sufficient).
    """
    dir_a = tmp_path / "subdir_a"
    dir_b = tmp_path / "subdir_b"
    dir_a.mkdir(parents=True, exist_ok=True)
    dir_b.mkdir(parents=True, exist_ok=True)

    mapping = {"a": dir_a, "b": dir_b}

    def _resolve(memory_subdir: str) -> str:
        try:
            return str(mapping[memory_subdir])
        except KeyError as exc:
            raise KeyError(
                f"isolated_subdirs fixture only knows about {list(mapping)!r}, "
                f"got {memory_subdir!r}"
            ) from exc

    with mock.patch(
        "plugins._memory.helpers.memory.abs_db_dir",
        side_effect=_resolve,
    ):
        yield mapping


# ---------------------------------------------------------------------------
# GraphStore isolation
# ---------------------------------------------------------------------------


class TestGraphStoreSubdirIsolation:
    def test_graph_store_subdir_a_does_not_affect_subdir_b(
        self, isolated_subdirs
    ):
        """Writing an edge in subdir A is invisible when reading subdir B."""
        gs_a = GraphStore("a")
        gs_b = GraphStore("b")

        gs_a.add_edge(GraphEdge("a1", "a2", "supports"))

        # Subdir A sees its edge.
        edges_a = gs_a.get_edges("a1")
        assert len(edges_a) == 1
        assert edges_a[0].from_id == "a1"
        assert edges_a[0].to_id == "a2"
        assert edges_a[0].type == "supports"

        # Subdir B is empty — no contamination.
        assert len(gs_b) == 0
        assert gs_b.get_edges("a1") == []
        assert gs_b.get_edges("a2") == []
        assert gs_b.get_edges("any_node") == []

        # B's underlying file is also not present (no sidecar was created).
        from plugins._memory.helpers.memory import abs_db_dir
        b_path = abs_db_dir("b")
        import os
        assert not os.path.exists(os.path.join(b_path, "relationships.json"))

    def test_graph_store_remove_in_subdir_a_does_not_affect_subdir_b(
        self, isolated_subdirs
    ):
        """Removing edges in subdir A must not cascade into subdir B."""
        gs_a = GraphStore("a")
        gs_b = GraphStore("b")

        # Populate both subdirs with non-overlapping nodes.
        gs_a.add_edge(GraphEdge("a1", "a2", "supports"))
        gs_a.add_edge(GraphEdge("a1", "a3", "depends_on"))
        gs_b.add_edge(GraphEdge("b1", "b2", "contradicts"))
        gs_b.add_edge(GraphEdge("b2", "b3", "part_of"))

        # Remove a node in A.
        removed = gs_a.remove_edges_for_id("a1")
        assert removed == 2
        assert gs_a.get_edges("a1") == []
        assert gs_a.get_edges("a2") == []  # incoming cascaded too
        assert len(gs_a) == 0

        # B is untouched.
        assert len(gs_b.get_edges("b1")) == 1
        assert gs_b.get_edges("b1")[0].to_id == "b2"
        assert gs_b.get_edges("b1")[0].type == "contradicts"
        assert len(gs_b.get_edges("b2")) == 1
        assert gs_b.get_edges("b2")[0].to_id == "b3"
        assert gs_b.get_edges("b2")[0].type == "part_of"
        assert len(gs_b) == 2

        # Reload B from disk to be sure the writes did not bleed.
        gs_b_reload = GraphStore("b")
        gs_b_reload.load()
        assert len(gs_b_reload.get_edges("b1")) == 1
        assert len(gs_b_reload.get_edges("b2")) == 1
        assert len(gs_b_reload) == 2


# ---------------------------------------------------------------------------
# ScoreStore isolation
# ---------------------------------------------------------------------------


class TestScoreStoreSubdirIsolation:
    def test_score_store_subdir_a_does_not_affect_subdir_b(
        self, isolated_subdirs
    ):
        """Setting a score in subdir A is invisible when reading subdir B."""
        ss_a = ScoreStore("a")
        ss_b = ScoreStore("b")

        ss_a.set("memory_a1", importance=0.9, confidence=0.8, stability=0.7)

        # A returns what we set.
        a_record = ss_a.get("memory_a1")
        assert a_record.importance == pytest.approx(0.9)
        assert a_record.confidence == pytest.approx(0.8)
        assert a_record.stability == pytest.approx(0.7)
        assert "memory_a1" in ss_a.all_ids()

        # B has no knowledge of A's memory — defaults are returned, no error.
        b_record = ss_b.get("memory_a1")
        # Default MemoryScores values, not A's values.
        assert b_record.importance == pytest.approx(0.5)
        assert b_record.confidence == pytest.approx(0.7)
        assert b_record.stability == pytest.approx(0.5)
        assert b_record.access_count == 0
        assert b_record.last_accessed_at is None

        assert len(ss_b) == 0
        assert ss_b.all_ids() == []

        import os
        from plugins._memory.helpers.memory import abs_db_dir
        b_path = abs_db_dir("b")
        assert not os.path.exists(os.path.join(b_path, "scores.json"))

    def test_score_store_update_in_subdir_a_does_not_affect_subdir_b(
        self, isolated_subdirs
    ):
        """Updating a score in subdir A must not affect subdir B."""
        ss_a = ScoreStore("a")
        ss_b = ScoreStore("b")

        # Seed both subdirs with the same memory_id but distinct values.
        ss_a.set("shared_id", importance=0.3, confidence=0.4)
        ss_b.set("shared_id", importance=0.6, confidence=0.7, stability=0.5)

        # Update A.
        ss_a.set("shared_id", importance=0.95)
        ss_a.update_access("shared_id")

        # A reflects the update.
        a_record = ss_a.get("shared_id")
        assert a_record.importance == pytest.approx(0.95)
        assert a_record.confidence == pytest.approx(0.4)  # unchanged
        assert a_record.access_count == 1
        assert a_record.last_accessed_at is not None

        # B is untouched: its values are exactly what we set, and access_count
        # was never incremented.
        b_record = ss_b.get("shared_id")
        assert b_record.importance == pytest.approx(0.6)
        assert b_record.confidence == pytest.approx(0.7)
        assert b_record.stability == pytest.approx(0.5)
        assert b_record.access_count == 0
        assert b_record.last_accessed_at is None

        # Reload B from disk to confirm the on-disk file is independent.
        ss_b_reload = ScoreStore("b")
        ss_b_reload.load()
        b_reloaded = ss_b_reload.get("shared_id")
        assert b_reloaded.importance == pytest.approx(0.6)
        assert b_reloaded.confidence == pytest.approx(0.7)
        assert b_reloaded.stability == pytest.approx(0.5)
        assert b_reloaded.access_count == 0


# ---------------------------------------------------------------------------
# Simultaneous subdir operations + empty-subdir defaults
# ---------------------------------------------------------------------------


class TestSimultaneousSubdirOperations:
    def test_simultaneous_subdir_operations(
        self, isolated_subdirs
    ):
        """Concurrent / interleaved writes to two subdirs must each keep
        their own independent state.

        The two stores are created, written to, and re-read in the same
        test. After the writes settle, both stores must report the state
        they own and nothing from the other.
        """
        gs_a = GraphStore("a")
        gs_b = GraphStore("b")
        ss_a = ScoreStore("a")
        ss_b = ScoreStore("b")

        # Interleaved writes — A and B both get touched in alternation.
        gs_a.add_edge(GraphEdge("node_a1", "node_a2", "supports"))
        gs_b.add_edge(GraphEdge("node_b1", "node_b2", "contradicts"))
        ss_a.set("mem_a1", importance=0.91, confidence=0.81)
        ss_b.set("mem_b1", importance=0.11, confidence=0.21)
        gs_a.add_edge(GraphEdge("node_a1", "node_a3", "related_to"))
        gs_b.add_edge(GraphEdge("node_b1", "node_b3", "depends_on"))
        ss_a.set("mem_a2", stability=0.77)
        ss_b.set("mem_b2", stability=0.33)
        ss_a.update_access("mem_a1")
        ss_b.update_access("mem_b1")

        # ---- A holds only A's state -----------------------------------------
        assert len(gs_a) == 2
        a_edges = gs_a.get_edges("node_a1")
        assert {e.to_id for e in a_edges} == {"node_a2", "node_a3"}
        assert {e.type for e in a_edges} == {"supports", "related_to"}

        a_score_1 = ss_a.get("mem_a1")
        assert a_score_1.importance == pytest.approx(0.91)
        assert a_score_1.confidence == pytest.approx(0.81)
        assert a_score_1.access_count == 1
        a_score_2 = ss_a.get("mem_a2")
        assert a_score_2.stability == pytest.approx(0.77)

        # A has no knowledge of B's data.
        assert gs_a.get_edges("node_b1") == []
        assert gs_a.get_edges("node_b2") == []
        assert ss_a.get("mem_b1").importance == pytest.approx(0.5)  # default
        assert ss_a.get("mem_b1").access_count == 0

        # ---- B holds only B's state -----------------------------------------
        assert len(gs_b) == 2
        b_edges = gs_b.get_edges("node_b1")
        assert {e.to_id for e in b_edges} == {"node_b2", "node_b3"}
        assert {e.type for e in b_edges} == {"contradicts", "depends_on"}

        b_score_1 = ss_b.get("mem_b1")
        assert b_score_1.importance == pytest.approx(0.11)
        assert b_score_1.confidence == pytest.approx(0.21)
        assert b_score_1.access_count == 1
        b_score_2 = ss_b.get("mem_b2")
        assert b_score_2.stability == pytest.approx(0.33)

        # B has no knowledge of A's data.
        assert gs_b.get_edges("node_a1") == []
        assert gs_b.get_edges("node_a2") == []
        assert ss_b.get("mem_a1").importance == pytest.approx(0.5)  # default
        assert ss_b.get("mem_a1").access_count == 0

        # ---- Reload both from disk to prove persistence is per-subdir ------
        gs_a_reload = GraphStore("a")
        gs_a_reload.load()
        gs_b_reload = GraphStore("b")
        gs_b_reload.load()
        ss_a_reload = ScoreStore("a")
        ss_a_reload.load()
        ss_b_reload = ScoreStore("b")
        ss_b_reload.load()

        assert len(gs_a_reload) == 2
        assert len(gs_b_reload) == 2
        assert len(ss_a_reload) == 2
        assert len(ss_b_reload) == 2

        assert gs_a_reload.get_edges("node_b1") == []
        assert gs_b_reload.get_edges("node_a1") == []
        assert ss_a_reload.get("mem_b1").importance == pytest.approx(0.5)
        assert ss_b_reload.get("mem_a1").importance == pytest.approx(0.5)


class TestEmptySubdirDefaults:
    def test_graph_store_empty_subdir_returns_empty_state(
        self, isolated_subdirs
    ):
        """A never-written subdir returns empty state, never data from
        another subdir."""
        # Write to A.
        gs_a = GraphStore("a")
        gs_a.add_edge(GraphEdge("x", "y", "supports"))
        assert len(gs_a) == 1

        # Instantiate B for the first time — no writes have ever happened.
        gs_b = GraphStore("b")
        assert len(gs_b) == 0
        assert gs_b.get_edges("x") == []
        assert gs_b.get_edges("y") == []
        assert gs_b.get_edges("anything") == []
        # BFS over a never-written subdir returns no neighbors.
        assert gs_b.neighbors("x", max_hops=3) == []
        assert gs_b.neighbors("any_seed", max_hops=2) == []

        # Forcing a reload from disk does not change the empty state.
        gs_b.load()
        assert len(gs_b) == 0
        assert gs_b.get_edges("x") == []

    def test_score_store_empty_subdir_returns_default_state(
        self, isolated_subdirs
    ):
        """A never-written subdir returns default MemoryScores, never data
        from another subdir."""
        # Write to A.
        ss_a = ScoreStore("a")
        ss_a.set("x", importance=0.99, confidence=0.99, stability=0.99)
        ss_a.update_access("x")
        assert len(ss_a) == 1

        # Instantiate B for the first time — no writes have ever happened.
        ss_b = ScoreStore("b")
        assert len(ss_b) == 0
        assert ss_b.all_ids() == []

        # The default MemoryScores dataclass, NOT a copy of A's record.
        default = MemoryScores()
        b_record = ss_b.get("x")
        assert b_record.importance == pytest.approx(default.importance)
        assert b_record.confidence == pytest.approx(default.confidence)
        assert b_record.stability == pytest.approx(default.stability)
        assert b_record.access_count == 0
        assert b_record.last_accessed_at is None

        # Forcing a reload from disk preserves the default state.
        ss_b.load()
        assert len(ss_b) == 0
        reloaded = ss_b.get("x")
        assert reloaded.importance == pytest.approx(default.importance)
        assert reloaded.access_count == 0
        assert reloaded.last_accessed_at is None
