"""WI-P10-DELETE-ORDERING (S1): failing-then-fixed signature tests for
KI-003, KI-011, KI-013 — against the REAL GraphStore (no mocks on success
paths; tmp_path disposable fixtures only).

Signatures pinned:
- KI-003: reverse-direction unlink destroys ONLY the matched edge — never a
  bulk delete of every edge touching the anchor.
- KI-011: a failing framework delete leaves ALL sidecar edges intact;
  cascade ownership belongs solely to the ratified _10_graph_cascade
  end-hook, strictly AFTER confirmed deletion. A success-then-crash window
  leaves ORPHANED (recoverable) edges — never loss of live memories' edges.
- KI-013: single-edge unlink is ONE locked atomic write — no
  wipe-and-rewrite, no re-add loop.
- C7: sha256 store-file hash invariance where the removal effect must be
  empty (0-match no-write), extending the WI-P9 pin to the bulk-removal
  site family.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from usr.plugins.neuro_core.tools import memory_relate as mr
from usr.plugins.neuro_core.helpers.graph_store import GraphEdge, GraphStore


def _seed(store, edges):
    for f, t, typ in edges:
        store.add_edge(GraphEdge(from_id=f, to_id=t, type=typ))


def _edge_tuple(e):
    if isinstance(e, dict):
        return (e.get("from_id"), e.get("to_id"), e.get("type"))
    return (e.from_id, e.to_id, e.type)


def _snap(store):
    return {
        k: sorted(((_edge_key(e)) for e in v))
        for k, v in (store.load() or {}).items()
    }


def _edge_key(e):
    if isinstance(e, dict):
        return (e.get("from_id"), e.get("to_id"), e.get("type"))
    return (e.from_id, e.to_id, e.type)


def _file_hash(store):
    return hashlib.sha256(Path(store._path).read_bytes()).hexdigest()


class _FakeMem:
    memory_subdir = None


# ------------------------------------------------------------------
# KI-003: targeted removal preserves unrelated edges (both directions)
# ------------------------------------------------------------------


def test_ki003_reverse_unlink_preserves_all_unrelated_edges(memory_subdir):
    store = GraphStore(memory_subdir)
    _seed(store, [
        ("A", "B", "related_to"),
        ("A", "C", "related_to"),
        ("A", "D", "supports"),
        ("C", "A", "related_to"),
        ("D", "A", "related_to"),
    ])
    removed = mr._remove_specific_edge(store, "B", "A", "related_to")
    assert removed == 1
    after = _snap(store)
    assert after["A"] == sorted([("A", "C", "related_to"), ("A", "D", "supports")])
    assert after["C"] == [("C", "A", "related_to")]
    assert after["D"] == [("D", "A", "related_to")]
    assert "B" not in after


def test_ki003_forward_unlink_preserves_incoming_and_unrelated(memory_subdir):
    store = GraphStore(memory_subdir)
    _seed(store, [
        ("A", "B", "related_to"),
        ("A", "C", "related_to"),
        ("C", "A", "related_to"),
        ("D", "A", "supports"),
    ])
    removed = mr._remove_specific_edge(store, "A", "B", "related_to")
    assert removed == 1
    after = _snap(store)
    assert after["A"] == sorted([("A", "C", "related_to")])
    assert after["C"] == [("C", "A", "related_to")]
    assert after["D"] == [("D", "A", "supports")]


def test_ki003_no_match_returns_zero(memory_subdir):
    store = GraphStore(memory_subdir)
    _seed(store, [("A", "B", "related_to")])
    before = _file_hash(store)
    removed = mr._remove_specific_edge(store, "X", "Y", "related_to")
    assert removed == 0
    assert _file_hash(store) == before
    assert _snap(store)["A"] == [("A", "B", "related_to")]


def test_ki003_non_symmetric_type_has_no_reverse_pass_effect(memory_subdir):
    store = GraphStore(memory_subdir)
    _seed(store, [("A", "B", "supports")])
    removed = mr._remove_specific_edge(store, "B", "A", "supports")
    assert removed == 1
    assert "A" not in _snap(store)


# ------------------------------------------------------------------
# KI-013: single atomic write, no re-add loop
# ------------------------------------------------------------------


def test_ki013_removal_is_exactly_one_atomic_write(memory_subdir, monkeypatch):
    store = GraphStore(memory_subdir)
    _seed(store, [
        ("A", "B", "related_to"),
        ("A", "C", "related_to"),
        ("C", "A", "related_to"),
    ])
    writes = []
    orig = GraphStore._atomic_write

    def spy(self, data):
        writes.append(1)
        return orig(self, data)

    monkeypatch.setattr(GraphStore, "_atomic_write", spy)
    removed = mr._remove_specific_edge(store, "B", "A", "related_to")
    assert removed == 1
    assert len(writes) == 1  # KI-013 signature: exactly one write, no re-add loop


def test_ki013_no_readd_loop_and_no_bulk_call(memory_subdir, monkeypatch):
    store = GraphStore(memory_subdir)
    _seed(store, [("A", "B", "related_to"), ("A", "C", "related_to")])

    def forbidden_add(self, edge):
        raise AssertionError("re-add loop must not exist (KI-013)")

    def forbidden_bulk(self, memory_id):
        raise AssertionError("bulk remove_edges_for_id must not be used by the surgical helper (KI-003/KI-013)")

    monkeypatch.setattr(GraphStore, "add_edge", forbidden_add)
    monkeypatch.setattr(GraphStore, "remove_edges_for_id", forbidden_bulk)
    removed = mr._remove_specific_edge(store, "B", "A", "related_to")
    assert removed == 1
    after = _snap(store)
    assert after["A"] == [("A", "C", "related_to")]


# ------------------------------------------------------------------
# C7: sha256 hash invariance on no-op removals (bulk-site family)
# ------------------------------------------------------------------


def test_c7_bulk_site_zero_removed_is_nowrite_hash_invariant(memory_subdir):
    store = GraphStore(memory_subdir)
    _seed(store, [("A", "B", "related_to")])
    before = _file_hash(store)
    assert store.remove_edges_for_id("no-such-id") == 0
    assert _file_hash(store) == before  # KI-003-style no-write pin at the bulk site
    assert store.remove_edge("X", "Y", "related_to") == 0
    assert _file_hash(store) == before


def test_c7_idempotent_retry_is_hash_invariant(memory_subdir):
    store = GraphStore(memory_subdir)
    _seed(store, [("A", "B", "related_to"), ("A", "C", "related_to")])
    assert mr._remove_specific_edge(store, "B", "A", "related_to") == 1
    after_first = _file_hash(store)
    assert mr._remove_specific_edge(store, "B", "A", "related_to") == 0
    assert _file_hash(store) == after_first  # retry performs NO write


# ------------------------------------------------------------------
# KI-011: failing delete leaves sidecars intact; orphan window recoverable
# ------------------------------------------------------------------


def test_ki011_failing_delete_leaves_sidecars_intact(memory_subdir, monkeypatch):
    import plugins._memory.helpers.memory as mem_mod
    from usr.plugins.neuro_core.helpers import native_access as na

    store = GraphStore(memory_subdir)
    _seed(store, [("memA", "memB", "related_to"), ("memC", "memA", "supports")])
    before = _snap(store)

    async def failing_delete(self, ids, cascade=False, filter=""):
        raise RuntimeError("simulated FAISS failure before any deletion")

    monkeypatch.setattr(mem_mod.Memory, "delete_documents_by_ids", failing_delete)
    with pytest.raises(RuntimeError):
        asyncio.run(na.delete(_FakeMem(), ["memA"]))
    assert _snap(store) == before  # KI-011 signature pinned: NO pre-delete cascade


def test_ki011_delete_does_no_sidecar_writes_itself(memory_subdir, monkeypatch):
    """The native delete path performs NO sidecar writes of its own; cascade
    ownership is exclusively the end-hook's (post-success)."""
    import plugins._memory.helpers.memory as mem_mod
    from usr.plugins.neuro_core.helpers import native_access as na

    store = GraphStore(memory_subdir)
    _seed(store, [("memA", "memB", "related_to")])

    bulk_calls = []
    orig_bulk = GraphStore.remove_edges_for_id

    def spy_bulk(self, memory_id):
        bulk_calls.append(memory_id)
        return orig_bulk(self, memory_id)

    monkeypatch.setattr(GraphStore, "remove_edges_for_id", spy_bulk)

    async def ok_delete(self, ids, cascade=False, filter=""):
        return []

    monkeypatch.setattr(mem_mod.Memory, "delete_documents_by_ids", ok_delete)
    result = asyncio.run(na.delete(_FakeMem(), ["memA"]))
    assert result == []
    assert bulk_calls == []  # no pre-delete cascade; hook is the only cascade owner


def test_ki011_success_then_crash_before_hook_leaves_recoverable_orphans(memory_subdir, monkeypatch):
    """Residual-window contract: if deletion succeeds but the process crashes
    before the end-hook runs, edges remain as ORPHANED (visible, recoverable)
    state — never destroyed. Simulated: delete succeeds, no hook runs."""
    import plugins._memory.helpers.memory as mem_mod
    from usr.plugins.neuro_core.helpers import native_access as na

    store = GraphStore(memory_subdir)
    _seed(store, [("memA", "memB", "related_to")])

    async def ok_delete(self, ids, cascade=False, filter=""):
        return []

    monkeypatch.setattr(mem_mod.Memory, "delete_documents_by_ids", ok_delete)
    asyncio.run(na.delete(_FakeMem(), ["memA"]))
    after = _snap(store)
    assert after["memA"] == [("memA", "memB", "related_to")]  # orphaned, recoverable


def test_ki011_hook_cascade_untouched_single_write(memory_subdir, monkeypatch):
    """C3/C7 guard: the hook's bulk cascade primitive remains a single-locked
    single-atomic-write bulk removal (legitimate post-success use)."""
    store = GraphStore(memory_subdir)
    _seed(store, [
        ("gone1", "other", "related_to"),
        ("other", "gone1", "related_to"),
        ("other", "gone2", "supports"),
    ])
    writes = []
    orig = GraphStore._atomic_write

    def spy(self, data):
        writes.append(1)
        return orig(self, data)

    monkeypatch.setattr(GraphStore, "_atomic_write", spy)
    removed = store.remove_edges_for_id("gone1")
    assert removed == 2
    assert len(writes) == 1
    after = _snap(store)
    assert "gone1" not in after
    assert after["other"] == [("other", "gone2", "supports")]


# ------------------------------------------------------------------
# D24 symmetry preserved (non-fatal second targeted pass)
# ------------------------------------------------------------------


def test_d24_symmetric_second_pass_is_targeted(memory_subdir, monkeypatch):
    store = GraphStore(memory_subdir)
    _seed(store, [
        ("A", "B", "related_to"),
        ("B", "A", "related_to"),
        ("A", "C", "related_to"),
    ])

    def forbidden_bulk(self, memory_id):
        raise AssertionError("D24 second pass must be targeted, not bulk")

    monkeypatch.setattr(GraphStore, "remove_edges_for_id", forbidden_bulk)
    assert mr._remove_specific_edge(store, "A", "B", "related_to") == 1
    assert mr._remove_specific_edge(store, "B", "A", "related_to") == 1
    after = _snap(store)
    assert after["A"] == [("A", "C", "related_to")]
    assert "B" not in after
