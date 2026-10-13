"""Tests for the NC1-owned native Memory access layer (helpers/native_access.py).

Covers the WI-2026-09-04-PHASE0-PATCH-ARCH Checkpoint B acceptance criteria:
- Full signature fidelity with the REAL framework Memory API: embedding
  (KI-001) and cascade/filter (KI-002) pass through structurally.
- Access tracking on BOTH search paths, including with-scores (KI-021).
- Sidecar-first delete ordering (D39-A/D53, ARC condition 2).
- Exception-safe, non-fatal bookkeeping (ARC condition 3).
- The Memory class is never patched (no ``_neuro_patched`` attributes).

Runs against the conftest stub of ``plugins._memory.helpers.memory``;
recorders are installed on the stub class per test.
"""

from __future__ import annotations

import asyncio

import pytest

from usr.plugins.neuro_core.helpers import native_access as na


def _doc(doc_id):
    return type("D", (), {"metadata": {"id": doc_id}, "id": doc_id})()


def _install_recorders(monkeypatch, calls):
    import plugins._memory.helpers.memory as mem_mod

    Memory = mem_mod.Memory

    async def rec_insert(self, text, metadata=None):
        calls["insert"] = {"text": text, "metadata": dict(metadata or {})}
        return ["id-ok"]

    async def rec_search(self, query, limit=10, threshold=0.6, filter="", embedding=None):
        calls["search"] = {"query": query, "limit": limit, "threshold": threshold,
                           "filter": filter, "embedding": embedding}
        return [_doc("mem1")]

    async def rec_with_scores(self, query, limit=10, threshold=0.6, filter=""):
        calls["with_scores"] = {"query": query, "filter": filter}
        return [(_doc("mem1"), 0.9)]

    async def rec_delete(self, ids, cascade=False, filter=""):
        calls["delete"] = {"ids": ids, "cascade": cascade, "filter": filter}
        return []

    monkeypatch.setattr(Memory, "insert_text", rec_insert)
    monkeypatch.setattr(Memory, "search_similarity_threshold", rec_search)
    monkeypatch.setattr(Memory, "search_similarity_threshold_with_scores", rec_with_scores, raising=False)
    monkeypatch.setattr(Memory, "delete_documents_by_ids", rec_delete)
    return Memory


class _FakeMem:
    memory_subdir = "neuro_test"


def test_capture_seeds_defaults_and_reaches_real_insert(monkeypatch, memory_subdir):
    calls = {}
    _install_recorders(monkeypatch, calls)
    result = asyncio.run(na.capture(_FakeMem(), "hello", {"area": "main"}))
    assert result == ["id-ok"]
    md = calls["insert"]["metadata"]
    assert md["area"] == "main"
    assert md["memory_type"] == "note"
    assert md["importance"] == 0.5
    assert md["validation_status"] == "unvalidated"


def test_capture_tolerates_non_dict_metadata(monkeypatch, memory_subdir):
    calls = {}
    _install_recorders(monkeypatch, calls)
    asyncio.run(na.capture(_FakeMem(), "hello", None))
    md = calls["insert"]["metadata"]
    assert md["memory_type"] == "note"
    assert md["validation_status"] == "unvalidated"


def test_search_passes_embedding_through_full_signature(monkeypatch, memory_subdir):
    """KI-001: embedding kwarg reaches the real method — no signature replica to drift."""
    calls = {}
    Memory = _install_recorders(monkeypatch, calls)
    asyncio.run(na.search(_FakeMem(), "q", limit=5, threshold=0.7, filter="", embedding=[0.1, 0.2]))
    assert calls["search"] == {"query": "q", "limit": 5, "threshold": 0.7, "filter": "", "embedding": [0.1, 0.2]}
    # Sanity: the recorder itself accepts embedding, proving the wrapper never
    # re-declares a narrower signature that could raise KI-001's TypeError.
    assert not hasattr(Memory.search_similarity_threshold, "_neuro_patched")


def test_search_tracks_access(monkeypatch, memory_subdir):
    """WI-P12: access tracking records the sidecar; the FAISS metadata mirror
    mutation (ADR-NC1-002 boundary 4) is gone — metadata must stay untouched."""
    calls = {}
    _install_recorders(monkeypatch, calls)
    result = asyncio.run(na.search(_FakeMem(), "q"))
    from usr.plugins.neuro_core.helpers.scores import ScoreStore

    ms = ScoreStore(memory_subdir).get("mem1")
    assert ms.access_count == 1
    assert ms.last_accessed_at
    assert "access_count" not in result[0].metadata


def test_search_with_scores_passes_through_and_tracks(monkeypatch, memory_subdir):
    """KI-021: the with-scores path is tracked (the retired patch layer never covered it)."""
    calls = {}
    _install_recorders(monkeypatch, calls)
    result = asyncio.run(na.search_with_scores(_FakeMem(), "q2"))
    assert calls["with_scores"] == {"query": "q2", "filter": ""}
    from usr.plugins.neuro_core.helpers.scores import ScoreStore

    ms = ScoreStore(memory_subdir).get("mem1")
    assert ms.access_count == 1
    assert "access_count" not in result[0][0].metadata


def test_access_tracking_updates_sidecar(monkeypatch, memory_subdir):
    calls = {}
    _install_recorders(monkeypatch, calls)
    asyncio.run(na.search(_FakeMem(), "q"))
    from usr.plugins.neuro_core.helpers.scores import ScoreStore

    store = ScoreStore(memory_subdir)
    ms = store.get("mem1")
    assert ms.access_count == 1
    assert ms.last_accessed_at


def test_delete_passes_cascade_and_filter_through(monkeypatch, memory_subdir):
    """KI-002: cascade=True + filter kwarg reach the real method — no TypeError."""
    calls = {}
    _install_recorders(monkeypatch, calls)
    asyncio.run(na.delete(_FakeMem(), ["idX"], cascade=True, filter="area=='main'"))
    assert calls["delete"] == {"ids": ["idX"], "cascade": True, "filter": "area=='main'"}


def test_delete_sidecar_cascade_not_before_real_delete(monkeypatch, memory_subdir):
    """WI-P10-DELETE-ORDERING (S1, ARC pre-design C2 realignment).

    REALIGNMENT RECORD: this test previously encoded the superseded D39-A
    sidecar-before-delete ordering as an ARC condition of the ADR-NC1-001
    native-integration ratification ('ARC cond 2'). That ordering is the
    KI-011 defect: a failing framework delete left edges of still-live
    memories destroyed and unrecoverable. Superseded per WI-P10 ARC
    pre-design C1 (open-question closure per D-NC1-035; WI-P8
    stub-realignment precedent — corrected, never silently deleted).

    NEW contract pinned: a failing framework delete leaves ALL sidecar
    edges intact; cascade ownership belongs solely to the ratified
    ``_10_graph_cascade`` end-hook, which fires strictly AFTER confirmed
    deletion.
    """
    import plugins._memory.helpers.memory as mem_mod

    calls = {}
    Memory = _install_recorders(monkeypatch, calls)

    from usr.plugins.neuro_core.helpers.graph_store import GraphEdge, GraphStore

    gs = GraphStore(memory_subdir)
    gs.add_edge(GraphEdge(from_id="mem3", to_id="mem4", type="related_to"))
    before_hash = gs.load().get("mem3", [])

    async def failing_delete(self, ids, cascade=False, filter=""):
        raise RuntimeError("simulated FAISS failure before any deletion")

    monkeypatch.setattr(Memory, "delete_documents_by_ids", failing_delete)
    with pytest.raises(RuntimeError):
        asyncio.run(na.delete(_FakeMem(), ["mem3"]))
    # KI-011 signature pinned: no pre-delete cascade — sidecars intact.
    after = gs.load().get("mem3", [])
    after_tuples = [(e.get("from_id"), e.get("to_id"), e.get("type")) for e in after]
    before_tuples = [(e.get("from_id"), e.get("to_id"), e.get("type")) for e in before_hash]
    assert after_tuples == before_tuples
    assert len(after_tuples) == 1


def test_bookkeeping_failure_is_non_fatal(monkeypatch, memory_subdir):
    """ARC cond 3: a bookkeeping error never breaks the underlying Memory operation."""
    import plugins._memory.helpers.memory as mem_mod

    calls = {}
    Memory = _install_recorders(monkeypatch, calls)

    import usr.plugins.neuro_core.helpers.scores as scores_mod

    class BoomStore:
        def __init__(self, *a, **k):
            pass

        def update_access(self, doc_id):
            raise RuntimeError("sidecar unavailable")

    monkeypatch.setattr(scores_mod, "ScoreStore", BoomStore)
    result = asyncio.run(na.search(_FakeMem(), "q"))
    assert calls["search"]["query"] == "q"
    assert result[0].metadata.get("access_count") is None


def test_memory_class_is_never_patched(monkeypatch, memory_subdir):
    """The native layer must never set _neuro_patched on any Memory method."""
    import plugins._memory.helpers.memory as mem_mod

    calls = {}
    Memory = _install_recorders(monkeypatch, calls)
    asyncio.run(na.capture(_FakeMem(), "x"))
    asyncio.run(na.search(_FakeMem(), "q"))
    asyncio.run(na.search_with_scores(_FakeMem(), "q"))
    asyncio.run(na.delete(_FakeMem(), ["id1"]))
    for m in (
        Memory.insert_text,
        Memory.search_similarity_threshold,
        Memory.search_similarity_threshold_with_scores,
        Memory.delete_documents_by_ids,
    ):
        assert not getattr(m, "_neuro_patched", False)
