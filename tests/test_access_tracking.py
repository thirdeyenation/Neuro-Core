"""Tests for the ``_10_access_tracking`` end-hook.

Three required test cases:
1. ``update_access`` is called once per tracked doc on each search hit.
2. The hook does NOT mutate ``doc.metadata`` (no ``access_count`` or
   ``last_accessed_at`` keys are injected into the returned Document).
3. Multiple docs in a single search result are all tracked independently.

The conftest provides:
- A ``ScoreStore`` stub whose ``update_access`` records each call.
- The framework ``Memory`` stub is overridable per test.
- ``sys.path`` is already patched to make plugin-local imports work.

The hook is imported as a class by the framework; we instantiate it
directly here and call ``end()`` with a list of fake documents and a
fake Memory instance.

Note (v2 contract): the hook persists access to the sidecar
``scores.json`` via ``ScoreStore.update_access`` ONLY. It does not
mutate the returned documents' ``metadata`` dicts in-place, because
framework callers hold shared references to those Document objects and
mutating their metadata breaks Agent Zero's own context retrieval.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_doc(doc_id: str) -> types.SimpleNamespace:
    """Create a Document-like object with mutable metadata."""
    return types.SimpleNamespace(
        id=doc_id,
        page_content=f"content for {doc_id}",
        metadata={"id": doc_id, "area": "main"},
    )


def _fake_memory(subdir: str = "default") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        memory_subdir=subdir,
        db=types.SimpleNamespace(get_by_ids=lambda ids: []),
    )


def _install_score_store_spy(
    monkeypatch: pytest.MonkeyPatch, hook_mod: types.ModuleType
) -> Dict[str, list]:
    """Install a fake ``ScoreStore`` and return its captured-call record.

    The caller is expected to have already loaded the hook module via
    :func:`_import_hook` (which uses file-based import, since the hook's
    directory name contains literal dots that break Python's dotted
    import resolution).
    """
    captured: Dict[str, list] = {"ctor_args": [], "update_calls": []}

    class _FakeScoreStore:
        def __init__(self, subdir: str):
            captured["ctor_args"].append(subdir)
            self.subdir = subdir

        def update_access(self, memory_id: str) -> None:
            captured["update_calls"].append(memory_id)

    monkeypatch.setattr(hook_mod, "_resolve_score_store", lambda: _FakeScoreStore)
    return captured


def _import_hook():
    """Load the hook module by file path.

    The hook lives under a directory whose name contains literal dots
    (``plugins._memory.helpers.memory``); Python's import machinery
    cannot resolve it as a dotted module path, so we use
    ``spec_from_file_location`` and a synthetic module name.
    """
    hook_path = (
        Path(__file__).resolve().parents[1]
        / "extensions"
        / "python"
        / "_functions"
        / "plugins._memory.helpers.memory"
        / "Memory"
        / "search_similarity_threshold"
        / "end"
        / "_10_access_tracking.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_neuro_core_access_tracking_hook", hook_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load hook spec from {hook_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Test 1: update_access is called once per tracked doc on each search hit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_count_increments_on_search_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook_mod = _import_hook()
    captured = _install_score_store_spy(monkeypatch, hook_mod)

    doc = _fake_doc("doc-1")
    memory = _fake_memory("default")

    result = await hook_mod.Memory_search_similarity_threshold().end(
        [doc],
        memory,
    )

    # Return value is the same list (no rebuild, no in-place mutation).
    assert result is not None
    # The hook must NOT mutate the caller's doc.metadata in-place.
    assert "access_count" not in doc.metadata
    assert "last_accessed_at" not in doc.metadata
    # ``update_access`` was called exactly once with the right id.
    assert captured["update_calls"] == ["doc-1"]
    assert captured["ctor_args"] == ["default"]

    # A second search should call update_access again — the sidecar
    # holds the cumulative count, the returned document stays untouched.
    await hook_mod.Memory_search_similarity_threshold().end(
        [doc],
        memory,
    )
    assert "access_count" not in doc.metadata
    assert "last_accessed_at" not in doc.metadata
    assert captured["update_calls"] == ["doc-1", "doc-1"]


# ---------------------------------------------------------------------------
# Test 2: hook does NOT mutate doc.metadata (no access_count,
# no last_accessed_at). Persistence is to the sidecar only.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_accessed_at_updates_to_current_iso(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook_mod = _import_hook()
    captured = _install_score_store_spy(monkeypatch, hook_mod)

    doc = _fake_doc("doc-2")
    memory = _fake_memory("default")

    await hook_mod.Memory_search_similarity_threshold().end(
        [doc],
        memory,
    )

    # The returned document's metadata must be untouched. The hook
    # writes timestamps to the sidecar, never to the doc.
    assert "last_accessed_at" not in doc.metadata
    assert "access_count" not in doc.metadata
    # Sidecar still received the access event for this doc id.
    assert captured["update_calls"] == ["doc-2"]
    # Original metadata keys are still present (no key was deleted).
    assert doc.metadata.get("id") == "doc-2"
    assert doc.metadata.get("area") == "main"


# ---------------------------------------------------------------------------
# Test 3: Multiple docs are all tracked independently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_docs_tracked_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook_mod = _import_hook()
    captured = _install_score_store_spy(monkeypatch, hook_mod)

    docs = [_fake_doc(f"d-{i}") for i in range(5)]
    memory = _fake_memory("default")

    await hook_mod.Memory_search_similarity_threshold().end(
        docs,
        memory,
    )

    # No doc was mutated — the hook persists to the sidecar only.
    for d in docs:
        assert "access_count" not in d.metadata
        assert "last_accessed_at" not in d.metadata
    # ``update_access`` was called once per doc, in order.
    assert captured["update_calls"] == [f"d-{i}" for i in range(5)]

    # A second search over a subset calls update_access for just
    # those ids; the returned documents still carry no extra metadata.
    subset = [docs[0], docs[2], docs[4]]
    await hook_mod.Memory_search_similarity_threshold().end(
        subset,
        memory,
    )
    for d in docs:
        assert "access_count" not in d.metadata
        assert "last_accessed_at" not in d.metadata
    assert captured["update_calls"] == [
        "d-0", "d-1", "d-2", "d-3", "d-4",
        "d-0", "d-2", "d-4",
    ]


# ---------------------------------------------------------------------------
# Bonus Test 4: docs with missing id are skipped, no crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_docs_without_id_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook_mod = _import_hook()
    captured = _install_score_store_spy(monkeypatch, hook_mod)

    good = _fake_doc("good-1")
    no_id = types.SimpleNamespace(
        id="no-id-doc",
        page_content="x",
        metadata={"area": "main"},  # no "id" key
    )

    await hook_mod.Memory_search_similarity_threshold().end(
        [good, no_id],
        _fake_memory("default"),
    )

    # The doc with an id was tracked via the sidecar; the caller's
    # metadata dicts must NOT have been mutated.
    assert "access_count" not in good.metadata
    assert "last_accessed_at" not in good.metadata
    assert "access_count" not in no_id.metadata
    assert "last_accessed_at" not in no_id.metadata
    assert captured["update_calls"] == ["good-1"]


# ---------------------------------------------------------------------------
# Bonus Test 5: dict-shaped response is also supported
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dict_shaped_response_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook_mod = _import_hook()
    captured = _install_score_store_spy(monkeypatch, hook_mod)

    docs = [_fake_doc("a"), _fake_doc("b")]
    response = {"documents": docs, "distances": [0.1, 0.2]}

    await hook_mod.Memory_search_similarity_threshold().end(
        response,
        _fake_memory("default"),
    )

    # Dict-shaped response: docs are extracted, tracked via sidecar,
    # and returned without metadata mutation.
    for d in docs:
        assert "access_count" not in d.metadata
        assert "last_accessed_at" not in d.metadata
    assert captured["update_calls"] == ["a", "b"]


# ---------------------------------------------------------------------------
# Bonus Test 6: sidecar failure does not crash the search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sidecar_failure_does_not_crash_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook_mod = _import_hook()

    class _BrokenStore:
        def __init__(self, subdir: str) -> None:
            pass
        def update_access(self, memory_id: str) -> None:
            raise RuntimeError("disk full")

    monkeypatch.setattr(hook_mod, "_resolve_score_store", lambda: _BrokenStore)

    doc = _fake_doc("x")
    # Should NOT raise; the hook swallows sidecar errors and returns
    # the original response. The caller's metadata is never mutated.
    await hook_mod.Memory_search_similarity_threshold().end(
        [doc],
        _fake_memory("default"),
    )
    assert "access_count" not in doc.metadata
    assert "last_accessed_at" not in doc.metadata
    # Original metadata is still intact (no key was deleted either).
    assert doc.metadata.get("id") == "x"
    assert doc.metadata.get("area") == "main"
