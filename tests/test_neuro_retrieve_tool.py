"""Tests for ``tools/neuro_retrieve.py`` (KI-019, WI-P6-TOOLTESTS).

Contract coverage grounded in the actual source (not docs):

1. Happy path: valid ``query``/``project`` returns a list of dicts with
   ``memory_id``, ``text``, ``source``, ``score``, ``factors`` keys, sorted
   by score descending (grounded: ``retrieve()`` sorts by score desc).
2. Invalid input: empty ``query`` or empty ``project`` raises
   ``ValueError("query and project are required")`` before any store is
   opened. Grounded nuance: ``query`` defaults to "" and ``project``
   defaults to "default", so omitted args are valid - only explicitly
   empty values trigger the error.
3. Edge: memories in a different project scope are excluded (grounded:
   ``retrieve()`` skips ``memory.scope != scope``).
4. Edge: SUPERSEDED memories are excluded even within scope (grounded:
   ``memory_lifecycle.retrievable()`` returns False for SUPERSEDED).
5. Edge: results include only exact-scope matches - an agent-scoped memory
   is not returned for the same project without agent (grounded: frozen
   dataclass ``Scope`` equality in ``retrieve()``).
6. Edge: zero-overlap in-scope memory still returns a result - there is no
   score threshold in ``retrieve()``; the floor is
   ``0.25*importance + 0.25*confidence`` (grounded in the source).

DB isolation: module-level ``_resolve_db_path`` is monkeypatched to a
``tmp_path``-rooted path for every test - the live ``neuro_core.db`` is
never touched (fixture policy: disposable/synthetic).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_PLUGIN_ROOT = "/a0/usr/plugins/neuro_core"
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)


def _load_tool(monkeypatch, tmp_path):
    """Import the tool module with its DB path pointed at a tmp file."""
    db_path = tmp_path / "retrieve_test.db"
    import importlib

    mod = importlib.import_module("tools.neuro_retrieve")
    monkeypatch.setattr(mod, "_resolve_db_path", lambda: str(db_path))
    return mod, db_path


def _seed(mod, db_path, memories):
    """Seed the disposable store directly through the service layer."""
    from neuro_core import Memory, Scope
    from neuro_service import NeuroCoreService
    from sqlite_store import SQLiteStore

    store = SQLiteStore(str(db_path))
    try:
        service = NeuroCoreService(store)
        for mem in memories:
            service.capture(mem)
    finally:
        store.close()


def _make_tool(mod):
    return mod.NeuroRetrieve(
        agent=None, name="neuro_retrieve", method=None,
        args={}, message="", loop_data=None,
    )


@pytest.mark.asyncio
async def test_retrieve_returns_contract_shape_sorted_desc(
    monkeypatch, tmp_path
):
    """Valid retrieve returns dicts with the exact contract keys, ordered
    by score descending (grounded: sorted(..., reverse=True))."""
    from neuro_core import Memory, Scope

    mod, db_path = _load_tool(monkeypatch, tmp_path)
    _seed(
        mod,
        db_path,
        [
            Memory("alpha beta", "agent_zero", Scope("p1"), 0.9, 0.9),
            Memory("alpha gamma", "user", Scope("p1"), 0.3, 0.3),
        ],
    )
    tool = _make_tool(mod)

    result = await tool.execute(query="alpha", project="p1")

    assert isinstance(result, list) and len(result) == 2
    for item in result:
        assert set(item.keys()) == {"memory_id", "text", "source", "score", "factors"}
        assert set(item["factors"].keys()) == {
            "overlap", "importance", "confidence", "validation"
        }
    scores = [item["score"] for item in result]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"query": "q", "project": ""},
    {"query": "", "project": "p"},
])
async def test_retrieve_empty_required_args_raise_value_error(
    monkeypatch, tmp_path, kwargs
):
    """Empty ``query`` and/or ``project`` raises ValueError before any
    store is opened - no DB file is created on the failure path.

    Grounded: the guard is ``if not query or not project``; note that
    ``project`` defaults to "default", so an omitted project is valid.
    """
    mod, db_path = _load_tool(monkeypatch, tmp_path)
    tool = _make_tool(mod)

    with pytest.raises(ValueError, match="query and project are required"):
        await tool.execute(**kwargs)

    assert not db_path.exists()


@pytest.mark.asyncio
async def test_retrieve_excludes_other_project_scope(
    monkeypatch, tmp_path
):
    """Memories in a different project are excluded (scope != scope skip,
    grounded in neuro_core.retrieve)."""
    from neuro_core import Memory, Scope

    mod, db_path = _load_tool(monkeypatch, tmp_path)
    _seed(
        mod,
        db_path,
        [Memory("alpha secret other", "agent_zero", Scope("p2"))],
    )
    tool = _make_tool(mod)

    result = await tool.execute(query="alpha secret other", project="p1")

    assert result == []


@pytest.mark.asyncio
async def test_retrieve_excludes_superseded_within_scope(
    monkeypatch, tmp_path
):
    """A SUPERSEDED memory in scope is not retrievable (grounded:
    retrievable() is False for SUPERSEDED)."""
    from memory_lifecycle import ValidationState
    from neuro_core import Memory, Scope

    mod, db_path = _load_tool(monkeypatch, tmp_path)
    _seed(
        mod,
        db_path,
        [
            Memory(
                "alpha superseded", "agent_zero", Scope("p1"),
                validation=ValidationState.SUPERSEDED,
            ),
        ],
    )
    tool = _make_tool(mod)

    result = await tool.execute(query="alpha superseded", project="p1")

    assert result == []


@pytest.mark.asyncio
async def test_retrieve_agent_scoped_memory_requires_matching_agent(
    monkeypatch, tmp_path
):
    """An agent-scoped memory (project, agent) is NOT returned for a
    bare project query (frozen Scope equality in retrieve())."""
    from neuro_core import Memory, Scope

    mod, db_path = _load_tool(monkeypatch, tmp_path)
    _seed(
        mod,
        db_path,
        [Memory("alpha agent memory", "agent_zero", Scope("p1", "agentA"))],
    )
    tool = _make_tool(mod)

    result = await tool.execute(query="alpha agent memory", project="p1")
    assert result == []

    scoped = await tool.execute(
        query="alpha agent memory", project="p1", agent="agentA"
    )
    assert len(scoped) == 1
    assert scoped[0]["text"] == "alpha agent memory"


@pytest.mark.asyncio
async def test_retrieve_zero_overlap_in_scope_still_returns_with_floor_score(
    monkeypatch, tmp_path
):
    """Grounded edge: retrieve() applies NO score threshold — an in-scope
    retrievable memory always returns, even with zero term overlap. The
    score floor is 0.25*importance + 0.25*confidence."""
    from neuro_core import Memory, Scope

    mod, db_path = _load_tool(monkeypatch, tmp_path)
    _seed(
        mod,
        db_path,
        [Memory("unrelated words", "agent_zero", Scope("p1"), 0.5, 0.5)],
    )
    tool = _make_tool(mod)

    result = await tool.execute(query="zzz qqq", project="p1")

    assert len(result) == 1
    assert result[0]["text"] == "unrelated words"
    assert result[0]["score"] == 0.25
    assert result[0]["factors"]["overlap"] == 0.0
