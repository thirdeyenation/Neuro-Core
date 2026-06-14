"""Tests for Neuro Core graph store (relationships.json sidecar).

Covers:
- 8-value ``RelationshipType`` enum.
- ``GraphEdge`` validation (self-loop rejection, empty IDs, type guard,
  confidence clamp).
- ``GraphStore.add_edge`` adds, replaces on duplicate (from,to,type).
- ``GraphStore.remove_edges_for_id`` cascades both outgoing and incoming
  edges and returns the count.
- ``GraphStore.neighbors`` BFS expansion with ``max_hops`` and
  ``rel_type`` filtering.
- Atomic write + reload: data survives a fresh ``GraphStore`` instance.
- Per-subdir isolation: a write to subdir A is invisible to subdir B.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from plugins._memory.helpers.memory import Memory
from usr.plugins.neuro_core.helpers.graph_store import (
    VALID_RELATIONSHIP_TYPES,
    GraphEdge,
    GraphStore,
    RelationshipType,
)


# ---------------------------------------------------------------------------
# Enum + dataclass
# ---------------------------------------------------------------------------


class TestRelationshipType:
    def test_eight_values(self):
        assert len(RelationshipType) == 8
        assert len(VALID_RELATIONSHIP_TYPES) == 8

    def test_canonical_values(self):
        assert VALID_RELATIONSHIP_TYPES == {
            "supports",
            "contradicts",
            "depends_on",
            "derived_from",
            "related_to",
            "precedes",
            "follows",
            "part_of",
        }


class TestGraphEdge:
    def test_minimal_construction(self):
        edge = GraphEdge(from_id="a", to_id="b", type="supports")
        assert edge.from_id == "a"
        assert edge.to_id == "b"
        assert edge.type == "supports"
        assert edge.confidence == 0.7  # default
        assert edge.source == "agent"
        assert edge.created_at  # auto-stamped

    def test_confidence_clamped_high(self):
        edge = GraphEdge(from_id="a", to_id="b", type="supports",
                         confidence=5.0)
        assert edge.confidence == 1.0

    def test_confidence_clamped_low(self):
        edge = GraphEdge(from_id="a", to_id="b", type="supports",
                         confidence=-2.0)
        assert edge.confidence == 0.0

    def test_invalid_type_rejected(self):
        with pytest.raises(ValueError):
            GraphEdge(from_id="a", to_id="b", type="friend")

    def test_empty_ids_rejected(self):
        with pytest.raises(ValueError):
            GraphEdge(from_id="", to_id="b", type="supports")
        with pytest.raises(ValueError):
            GraphEdge(from_id="a", to_id="", type="supports")

    def test_self_loop_rejected(self):
        with pytest.raises(ValueError):
            GraphEdge(from_id="x", to_id="x", type="supports")

    def test_round_trip_dict(self):
        edge = GraphEdge(from_id="a", to_id="b", type="contradicts",
                         confidence=0.42, source="imported",
                         created_at="2026-06-01T00:00:00Z")
        d = edge.to_dict()
        assert d == {
            "from_id": "a",
            "to_id": "b",
            "type": "contradicts",
            "confidence": 0.42,
            "source": "imported",
            "weight": 1.0,
            "created_at": "2026-06-01T00:00:00Z",
        }
        back = GraphEdge.from_dict("a", d)
        assert back.from_id == "a"
        assert back.to_id == "b"
        assert back.type == "contradicts"
        assert back.confidence == 0.42
        assert back.source == "imported"
        assert back.weight == 1.0
        assert back.created_at == "2026-06-01T00:00:00Z"


# ---------------------------------------------------------------------------
# GraphStore — add / get / remove / cascade
# ---------------------------------------------------------------------------


class TestGraphStoreAddAndGet:
    def test_add_then_get(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        gs.add_edge(GraphEdge("a", "b", "supports"))
        edges = gs.get_edges("a")
        assert len(edges) == 1
        assert edges[0].from_id == "a"
        assert edges[0].to_id == "b"
        assert edges[0].type == "supports"

    def test_add_multiple_outgoing(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        gs.add_edge(GraphEdge("a", "b", "supports"))
        gs.add_edge(GraphEdge("a", "c", "contradicts"))
        gs.add_edge(GraphEdge("a", "d", "related_to"))
        edges = gs.get_edges("a")
        assert len(edges) == 3
        assert {e.to_id for e in edges} == {"b", "c", "d"}

    def test_duplicate_edge_replaces_existing(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        gs.add_edge(GraphEdge("a", "b", "supports", confidence=0.3))
        gs.add_edge(GraphEdge("a", "b", "supports", confidence=0.9))
        edges = gs.get_edges("a")
        assert len(edges) == 1
        assert edges[0].confidence == 0.9

    def test_add_edge_duplicate_updates_weight(self, memory_subdir):
        """D25: two add_edge() calls with the same (from_id, to_id, type)
        must result in exactly one edge with the updated weight."""
        gs = GraphStore(memory_subdir)
        gs.add_edge(GraphEdge("a", "b", "related_to", weight=0.3, confidence=0.5))
        gs.add_edge(GraphEdge("a", "b", "related_to", weight=0.9, confidence=0.8))
        edges = gs.get_edges("a")
        assert len(edges) == 1
        assert edges[0].weight == 0.9
        assert edges[0].confidence == 0.8
        assert edges[0].type == "related_to"
        assert edges[0].from_id == "a"
        assert edges[0].to_id == "b"

    def test_different_type_not_deduped(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        gs.add_edge(GraphEdge("a", "b", "supports"))
        gs.add_edge(GraphEdge("a", "b", "contradicts"))
        edges = gs.get_edges("a")
        assert len(edges) == 2

    def test_get_unknown_returns_empty(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        assert gs.get_edges("nope") == []

    def test_add_edge_requires_instance(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        with pytest.raises(TypeError):
            gs.add_edge({"from_id": "a", "to_id": "b", "type": "supports"})


class TestGraphStoreRemoveCascade:
    def test_remove_outgoing_bucket(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        gs.add_edge(GraphEdge("a", "b", "supports"))
        gs.add_edge(GraphEdge("a", "c", "supports"))
        assert len(gs.get_edges("a")) == 2
        removed = gs.remove_edges_for_id("a")
        assert removed == 2
        assert gs.get_edges("a") == []

    def test_remove_incoming_only(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        gs.add_edge(GraphEdge("a", "x", "supports"))
        gs.add_edge(GraphEdge("b", "x", "supports"))
        # x has no outgoing edges of its own; deleting x must still
        # cascade-remove the two incoming edges.
        removed = gs.remove_edges_for_id("x")
        assert removed == 2
        assert gs.get_edges("a") == []
        assert gs.get_edges("b") == []

    def test_remove_mixed_in_and_out(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        # x has 1 outgoing, 2 incoming, 1 sibling incoming that points
        # elsewhere (should NOT be removed).
        gs.add_edge(GraphEdge("x", "y", "supports"))
        gs.add_edge(GraphEdge("a", "x", "contradicts"))
        gs.add_edge(GraphEdge("b", "x", "depends_on"))
        gs.add_edge(GraphEdge("c", "d", "related_to"))
        removed = gs.remove_edges_for_id("x")
        assert removed == 3
        assert gs.get_edges("x") == []
        assert gs.get_edges("a") == []
        assert gs.get_edges("b") == []
        # Unrelated edge remains.
        assert len(gs.get_edges("c")) == 1

    def test_remove_unknown_returns_zero(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        gs.add_edge(GraphEdge("a", "b", "supports"))
        assert gs.remove_edges_for_id("nope") == 0
        # Original edge still there.
        assert len(gs.get_edges("a")) == 1

    def test_remove_empty_id_returns_zero(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        assert gs.remove_edges_for_id("") == 0


# ---------------------------------------------------------------------------
# Atomic write + reload
# ---------------------------------------------------------------------------


def _sidecar_path(memory_subdir: str) -> str:
    from plugins._memory.helpers.memory import abs_db_dir
    return os.path.join(abs_db_dir(memory_subdir), "relationships.json")


class TestGraphStorePersistence:
    def test_file_created_on_add(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        gs.add_edge(GraphEdge("a", "b", "supports"))
        path = _sidecar_path(memory_subdir)
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert "a" in data
        assert len(data["a"]) == 1
        assert data["a"][0]["to_id"] == "b"
        assert data["a"][0]["type"] == "supports"

    def test_atomic_write_no_temp_leftovers(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        gs.add_edge(GraphEdge("a", "b", "supports"))
        gs.add_edge(GraphEdge("b", "c", "depends_on"))
        gs.add_edge(GraphEdge("a", "c", "related_to"))
        directory = os.path.dirname(_sidecar_path(memory_subdir))
        leftovers = [
            n for n in os.listdir(directory)
            if n.startswith(".relationships.") and n.endswith(".tmp")
        ]
        assert leftovers == []

    def test_reload_survives_new_instance(self, memory_subdir):
        gs1 = GraphStore(memory_subdir)
        gs1.add_edge(GraphEdge("a", "b", "supports"))
        gs1.add_edge(GraphEdge("b", "c", "contradicts"))

        gs2 = GraphStore(memory_subdir)
        # Force re-read from disk.
        gs2.load()
        edges_a = gs2.get_edges("a")
        edges_b = gs2.get_edges("b")
        assert len(edges_a) == 1
        assert edges_a[0].to_id == "b"
        assert len(edges_b) == 1
        assert edges_b[0].to_id == "c"

    def test_cascade_persists_to_disk(self, memory_subdir):
        gs1 = GraphStore(memory_subdir)
        gs1.add_edge(GraphEdge("a", "b", "supports"))
        gs1.add_edge(GraphEdge("c", "b", "supports"))
        gs1.remove_edges_for_id("b")

        # Re-read from a brand-new instance.
        gs2 = GraphStore(memory_subdir)
        gs2.load()
        assert gs2.get_edges("a") == []
        assert gs2.get_edges("c") == []
        assert len(gs2) == 0

    def test_corrupt_file_falls_back_to_empty(self, memory_subdir):
        path = _sidecar_path(memory_subdir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{not valid json")
        gs = GraphStore(memory_subdir)
        assert gs.load() == {}
        assert len(gs) == 0

    def test_non_dict_file_falls_back_to_empty(self, memory_subdir):
        path = _sidecar_path(memory_subdir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(["not", "a", "dict"], f)
        gs = GraphStore(memory_subdir)
        assert gs.load() == {}


# ---------------------------------------------------------------------------
# Neighbors (BFS expansion)
# ---------------------------------------------------------------------------


class TestGraphStoreNeighbors:
    def _build_chain(self, subdir: str) -> GraphStore:
        gs = GraphStore(subdir)
        # a -> b -> c -> d
        gs.add_edge(GraphEdge("a", "b", "supports"))
        gs.add_edge(GraphEdge("b", "c", "supports"))
        gs.add_edge(GraphEdge("c", "d", "supports"))
        # Cross branch a -> e
        gs.add_edge(GraphEdge("a", "e", "contradicts"))
        return gs

    def test_max_hops_one(self, memory_subdir):
        gs = self._build_chain(memory_subdir)
        out = gs.neighbors("a", max_hops=1)
        neighbors = {n for n, _, _ in out}
        assert neighbors == {"b", "e"}
        assert all(hop == 1 for _, hop, _ in out)

    def test_max_hops_two(self, memory_subdir):
        gs = self._build_chain(memory_subdir)
        out = gs.neighbors("a", max_hops=2)
        neighbors = {n for n, _, _ in out}
        assert neighbors == {"b", "c", "e"}
        # b and e at hop 1; c at hop 2.
        hops = {n: hop for n, hop, _ in out}
        assert hops["b"] == 1
        assert hops["e"] == 1
        assert hops["c"] == 2

    def test_max_hops_three(self, memory_subdir):
        gs = self._build_chain(memory_subdir)
        out = gs.neighbors("a", max_hops=3)
        neighbors = {n for n, _, _ in out}
        assert neighbors == {"b", "c", "d", "e"}
        hops = {n: hop for n, hop, _ in out}
        assert hops["d"] == 3

    def test_rel_type_filter(self, memory_subdir):
        gs = self._build_chain(memory_subdir)
        out = gs.neighbors("a", max_hops=1, rel_type="contradicts")
        neighbors = {n for n, _, _ in out}
        assert neighbors == {"e"}
        out2 = gs.neighbors("a", max_hops=1, rel_type="supports")
        neighbors2 = {n for n, _, _ in out2}
        assert neighbors2 == {"b"}

    def test_no_cycle(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        # a -> b -> a should only yield b once.
        gs.add_edge(GraphEdge("a", "b", "supports"))
        gs.add_edge(GraphEdge("b", "a", "supports"))
        out = gs.neighbors("a", max_hops=5)
        neighbors = {n for n, _, _ in out}
        assert neighbors == {"b"}

    def test_unknown_start_returns_empty(self, memory_subdir):
        gs = GraphStore(memory_subdir)
        assert gs.neighbors("ghost", max_hops=3) == []

    def test_zero_hops_returns_empty(self, memory_subdir):
        gs = self._build_chain(memory_subdir)
        assert gs.neighbors("a", max_hops=0) == []

    def test_edges_have_trigger(self, memory_subdir):
        gs = self._build_chain(memory_subdir)
        out = gs.neighbors("a", max_hops=1)
        for _, _, edge in out:
            assert edge.from_id == "a"
            assert edge.to_id in {"b", "e"}
            assert edge.type in VALID_RELATIONSHIP_TYPES


# ---------------------------------------------------------------------------
# Per-subdir isolation
# ---------------------------------------------------------------------------


class TestGraphStorePerSubdirIsolation:
    def test_separate_subdirs_have_separate_state(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        from unittest import mock
        with mock.patch.object(
            Memory,
            "_get_abs_db_dir",
            side_effect=lambda s: str(dir_a if s == "a" else dir_b),
        ):
            gs_a = GraphStore("a")
            gs_b = GraphStore("b")
            gs_a.add_edge(GraphEdge("a1", "a2", "supports"))
            gs_b.add_edge(GraphEdge("b1", "b2", "supports"))

            assert len(gs_a.get_edges("a1")) == 1
            assert gs_a.get_edges("b1") == []
            assert len(gs_b.get_edges("b1")) == 1
            assert gs_b.get_edges("a1") == []
