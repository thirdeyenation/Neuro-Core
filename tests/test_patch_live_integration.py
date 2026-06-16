"""Live integration test for the Neuro Core Memory monkey-patch.

The patch lives at ``usr/plugins/neuro_core/helpers/_patch.py`` and wraps
three ``Memory`` methods at plugin init time.  These tests verify the
wrappers actually fire and produce the expected side-effects on the
sidecar files (``scores.json``, ``relationships.json``) when the real
``Memory`` methods are called.

The tests construct a real ``Memory``-like instance whose ``db`` is a
mock that returns a known async result, so we can exercise the full
wrapper path (original method → side-effect) without booting a full
FAISS index.

This is a regression guard: if the monkey-patch is ever broken (wrong
method name, signature mismatch, missing idempotency check), these
tests will catch it immediately.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub langchain modules so `from plugins._memory.helpers.memory import
# Memory` succeeds in the test environment (which may not have langchain
# installed). The real Memory class is needed only to verify the wrapper
# is bound to the class attribute; the wrapper itself uses a mock db.
# ---------------------------------------------------------------------------
for _mod_name in (
    "langchain",
    "langchain.storage",
    "langchain_community",
    "langchain_community.vectorstores",
    "langchain_community.vectorstores.faiss",
    "langchain_community.docstore",
    "langchain_community.docstore.in_memory",
):
    if _mod_name not in sys.modules:
        _stub = types.ModuleType(_mod_name)
        if _mod_name.endswith("storage"):
            _stub.InMemoryByteStore = MagicMock
            _stub.LocalFileStore = MagicMock
        sys.modules[_mod_name] = _stub


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


TEST_SUBDIR = "patch_live_integration_test"


def _scores_path_for(subdir: str) -> str:
    from usr.plugins.neuro_core.helpers.scores import _scores_path
    return _scores_path(subdir)


def _relationships_path_for(subdir: str) -> str:
    from usr.plugins.neuro_core.helpers.graph_store import _relationships_path
    return _relationships_path(subdir)


def _clean_sidecars(subdir: str) -> None:
    for p in (_scores_path_for(subdir), _relationships_path_for(subdir)):
        if os.path.exists(p):
            os.remove(p)


@pytest.fixture
def installed_patches():
    """Install the monkey-patch and clean up after the test."""
    from usr.plugins.neuro_core.helpers._patch import install_patches, uninstall_patches
    install_patches()
    _clean_sidecars(TEST_SUBDIR)
    yield
    uninstall_patches()
    _clean_sidecars(TEST_SUBDIR)


class _FakeDoc:
    """Minimal Document stand-in."""
    def __init__(self, doc_id: str, **md):
        self.metadata = {"id": doc_id, **md}


class _FakeMemory:
    """A Memory-shaped object whose ``db`` is a mock.

    The real ``Memory.search_similarity_threshold`` only touches
    ``self.db.asearch(...)`` and ``self.memory_subdir``.  We provide
    those so the original method (called by the wrapper) can run end
    to end without a real FAISS index.
    """
    def __init__(self, subdir: str, docs: list[_FakeDoc]):
        self.memory_subdir = subdir
        self.db = MagicMock()
        self.db.asearch = AsyncMock(return_value=docs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_wrappers_installed_on_memory_class(installed_patches):
    """All three Memory methods must carry the _neuro_patched flag."""
    from plugins._memory.helpers.memory import Memory
    for name in ("insert_text", "search_similarity_threshold", "delete_documents_by_ids"):
        fn = getattr(Memory, name)
        assert getattr(fn, "_neuro_patched", False) is True, (
            f"Memory.{name} was not patched"
        )


def test_search_wrapper_bumps_access_count(installed_patches):
    """search_similarity_threshold wrapper must update scores.json."""
    from plugins._memory.helpers.memory import Memory
    from usr.plugins.neuro_core.helpers.scores import ScoreStore

    ss = ScoreStore(TEST_SUBDIR)
    doc_id = "live-search-doc-1"
    ss.set(doc_id, importance=0.5, confidence=0.5, stability=0.5)
    before = ss.get(doc_id).access_count
    assert before == 0

    fake_self = _FakeMemory(TEST_SUBDIR, [_FakeDoc(doc_id), _FakeDoc("live-search-doc-2")])
    result = asyncio.run(
        Memory.search_similarity_threshold(fake_self, "q", limit=5, threshold=0.5)
    )

    # Sidecar was updated
    ss_after = ScoreStore(TEST_SUBDIR)
    after = ss_after.get(doc_id).access_count
    assert after == 1, f"expected access_count=1, got {after}"

    # In-memory metadata mirror was also updated
    assert result[0].metadata["access_count"] == 1


def test_delete_wrapper_removes_edges(installed_patches):
    """delete_documents_by_ids wrapper must remove edges from relationships.json."""
    from plugins._memory.helpers.memory import Memory
    from usr.plugins.neuro_core.helpers.graph_store import GraphStore, GraphEdge

    gs = GraphStore(TEST_SUBDIR)
    doc_id = "live-delete-doc-1"
    other = "live-delete-doc-2"
    # GraphStore.add_edge takes a single GraphEdge dataclass, not positional args.
    gs.add_edge(GraphEdge(from_id=doc_id, to_id=other, type="related_to", weight=0.8))
    gs.add_edge(GraphEdge(from_id="third-doc", to_id=doc_id, type="supports", weight=0.5))
    # doc_id appears as from_id in edge 1 and as to_id in edge 2.
    # get_edges(doc_id) returns only outgoing edges (where doc_id is from_id).
    # The full adjacency map (load()) should contain both edges.
    assert len(gs.get_edges(doc_id)) == 1  # outgoing only
    assert len(gs.load()) == 2  # full map has both

    # Build a Memory-shaped object whose delete_documents_by_ids does nothing
    # (we're testing the wrapper's side-effect, not the real FAISS delete).
    fake_self = MagicMock()
    fake_self.memory_subdir = TEST_SUBDIR
    fake_self.delete_documents_by_ids = AsyncMock(return_value=[])
    # The stub's delete_documents_by_ids delegates to self.db.delete,
    # which must be awaitable.  Use AsyncMock for that.
    fake_self.db = MagicMock()
    fake_self.db.delete = AsyncMock(return_value=[])

    # Temporarily swap the saved original to a no-op so the wrapper calls our mock
    from usr.plugins.neuro_core.helpers._patch import _originals
    saved = _originals["delete_documents_by_ids"]
    async def noop(self, ids):
        return []
    _originals["delete_documents_by_ids"] = noop
    try:
        asyncio.run(Memory.delete_documents_by_ids(fake_self, [doc_id]))
    finally:
        _originals["delete_documents_by_ids"] = saved

    gs_after = GraphStore(TEST_SUBDIR)
    assert len(gs_after.get_edges(doc_id)) == 0, (
        f"edges not removed; remaining: {gs_after.get_edges(doc_id)}"
    )
    assert len(gs_after.load()) == 0


def test_insert_wrapper_seeds_metadata(installed_patches):
    """insert_text wrapper must validate and seed Neuro Core metadata."""
    from plugins._memory.helpers.memory import Memory

    # The patch wrapper mutates the metadata dict in place (validate + seed)
    # before passing it to the original.  We just check the dict after the
    # call — no spy needed because the closure-captured original is
    # irrelevant to this test's assertion.
    #
    # apply_seeding() heuristics:
    #   - importance  <- area  (area='main' -> 0.5)
    #   - confidence  <- source
    #   - stability   <- consolidation_action
    # memory_type and validation_status are NOT seeded; they must be
    # provided by the caller and are only validated/normalized if present.
    metadata = {"area": "main"}
    fake_self = MagicMock()
    fake_self.memory_subdir = TEST_SUBDIR

    asyncio.run(Memory.insert_text(fake_self, "hello world", metadata))

    # apply_defaults() unconditionally seeds all 5 Neuro Core fields
    assert metadata.get("memory_type") == "note", f"memory_type missing or wrong: {metadata}"
    assert metadata.get("importance") == 0.5, f"importance missing or wrong: {metadata}"
    assert metadata.get("confidence") == 0.7, f"confidence missing or wrong: {metadata}"
    assert metadata.get("stability") == 0.5, f"stability missing or wrong: {metadata}"
    assert metadata.get("validation_status") == "unvalidated", f"validation_status missing or wrong: {metadata}"


def test_wrappers_are_idempotent(installed_patches):
    """Calling install_patches() twice must not double-wrap."""
    from plugins._memory.helpers.memory import Memory
    from usr.plugins.neuro_core.helpers._patch import install_patches

    # The fixture already called install_patches once.  Capture the current
    # patched method, then call install_patches again and verify it's the
    # same function object (idempotent).
    first = Memory.insert_text
    install_patches()
    second = Memory.insert_text
    assert first is second, "install_patches() is not idempotent"


def test_uninstall_restores_originals(installed_patches):
    """uninstall_patches() must restore the original Memory methods."""
    from plugins._memory.helpers.memory import Memory
    from usr.plugins.neuro_core.helpers._patch import (
        install_patches,
        uninstall_patches,
        _originals,
    )

    patched = Memory.insert_text
    assert getattr(patched, "_neuro_patched", False) is True

    uninstall_patches()
    # After uninstall, the method on Memory should be the original again
    restored = Memory.insert_text
    assert getattr(restored, "_neuro_patched", False) is False
    assert "insert_text" not in _originals

    # Re-install for the fixture's teardown
    install_patches()
