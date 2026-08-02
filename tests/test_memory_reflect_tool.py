"""Tests for tools/memory_reflect.py — Neuro Core episode reflection tool.

Four mandatory cases from the Phase 4 spec:

1. Empty episode returns informative message, does not call reflect_memories.
2. LLM failure returns informative message, does not call write_reflection.
3. Successful flow returns confirmation string containing the new memory ID.
4. ``limit`` parameter is passed through to collect_episode_memories.

Plus extra coverage for the edge cases the tool is contractually
required to handle (missing episode_id, Memory.get failure).
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, List

import pytest


# ----------------------------------------------------------------- helpers


class _Doc:
    def __init__(self, page_content: str = "", metadata: dict | None = None):
        self.page_content = page_content
        self.metadata = dict(metadata or {})


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _StubMemory:
    """Bare-bones stand-in for ``plugins._memory.helpers.memory.Memory``.

    The reflection tool needs ``Memory.get(agent)`` to return an object
    whose ``.memory_subdir`` attribute is readable and whose
    ``.search_similarity_threshold(...)`` coroutine returns a list of
    ``_Doc``-like objects. This matches the framework's validated search
    path used by ``collect_episode_memories`` (see decision D21). We do
    not exercise FAISS here.
    """

    Area = types.SimpleNamespace(MAIN="main")

    @staticmethod
    async def get(agent):
        return _StubMemory._instance


_StubMemory._instance = None  # type: ignore[attr-defined]


def _make_tool(docs: List[_Doc]):
    """Build a fully-wired MemoryReflect instance and return it.

    We bypass ``Memory.get`` by stubbing the helper at the module
    level (it was imported as a name binding) and by setting
    ``_StubMemory._instance`` to a stub object that exposes
    ``memory_subdir`` and ``db.get_by_ids`` / ``db.index_to_docstore_id``.

    ``collect_episode_memories`` (see D21 final) uses exhaustive
    enumeration: it reads all doc IDs from ``memory.db.index_to_docstore_id``
    and then fetches them via ``memory.db.get_by_ids(all_ids)``. The
    stub provides both a populated ``index_to_docstore_id`` mapping
    and a ``get_by_ids`` that returns the full doc list.
    """
    from usr.plugins.neuro_core.tools import memory_reflect as mod

    # Build index_to_docstore_id: FAISS index positions → doc IDs.
    index_to_docstore_id = {
        i: d.metadata.get("id", f"d{i}") for i, d in enumerate(docs)
    }

    class _DB:
        # NOTE: ``index_to_docstore_id`` is assigned *after* the class
        # body. Python class bodies have their own scope, so writing
        # ``index_to_docstore_id = index_to_docstore_id`` inside the
        # class body fails with ``NameError`` (the class body shadows
        # the enclosing function's name for the RHS of the assignment).
        # Post-assignment avoids that pitfall.

        @staticmethod
        def get_by_ids(ids):
            # Return the full doc list regardless of the requested ids.
            # ``collect_episode_memories`` does the client-side
            # episode_id filter and the limit cap.
            return list(docs)

        # Kept for completeness; no longer the retrieval path used by
        # the helper (see D21 final).
        @staticmethod
        def get_all_docs():
            return list(docs)

    _DB.index_to_docstore_id = index_to_docstore_id

    instance = types.SimpleNamespace(
        db=_DB(),
        memory_subdir="default",
    )
    _StubMemory._instance = instance  # type: ignore[attr-defined]

    # Patch the name binding in the tool module.
    mod.Memory = _StubMemory  # type: ignore[assignment]

    tool = mod.MemoryReflect()

    # Provide the minimum agent surface the tool touches.
    agent = types.SimpleNamespace(
        config=types.SimpleNamespace(memory_subdir="default"),
    )
    tool.agent = agent
    return tool, mod


# ------------------------------------------------------------- 1. empty ep


class TestEmptyEpisode:
    """Spec case 1: empty episode → informative message."""

    def test_empty_episode_returns_informative_message(self, monkeypatch):
        from usr.plugins.neuro_core.helpers import reflection as rh

        calls = {"reflect": 0, "write": 0}

        async def _reflect(*a, **kw):
            calls["reflect"] += 1
            return "should not be called"

        async def _write(*a, **kw):
            calls["write"] += 1
            return "should not be called"

        # Patch the symbol the tool imported (not the helper module).
        from usr.plugins.neuro_core.tools import memory_reflect as mod
        monkeypatch.setattr(mod, "reflect_memories", _reflect)
        monkeypatch.setattr(mod, "write_reflection", _write)

        tool, _ = _make_tool([])  # no memories at all
        resp = _run(tool.execute(episode_id="ep_empty"))

        assert "No memories found" in resp.message
        assert "ep_empty" in resp.message
        assert resp.break_loop is False
        assert calls["reflect"] == 0
        assert calls["write"] == 0


# ----------------------------------------------------------- 2. LLM fails


class TestLLMFailure:
    """Spec case 2: LLM failure → informative message, no write."""

    def test_llm_failure_returns_informative_message(self, monkeypatch):
        from usr.plugins.neuro_core.tools import memory_reflect as mod

        write_calls = {"count": 0}

        async def _write(*a, **kw):
            write_calls["count"] += 1
            return "should not be called"

        async def _reflect(*a, **kw):
            return ""  # simulate LLM empty / failure

        monkeypatch.setattr(mod, "reflect_memories", _reflect)
        monkeypatch.setattr(mod, "write_reflection", _write)

        docs = [_Doc("alpha", {"id": "d1", "episode_id": "ep1"})]
        tool, _ = _make_tool(docs)
        resp = _run(tool.execute(episode_id="ep1"))

        assert "Reflection failed" in resp.message
        assert "LLM did not return content" in resp.message
        assert write_calls["count"] == 0
        assert resp.break_loop is False


# -------------------------------------------------------- 3. happy path


class TestHappyPath:
    """Spec case 3: success → confirmation string with new memory id."""

    def test_successful_flow_returns_confirmation(self, monkeypatch):
        from usr.plugins.neuro_core.tools import memory_reflect as mod

        async def _reflect(*a, **kw):
            return "Synthesised insight paragraph."

        async def _write(*a, **kw):
            # _write_reflection(subdir, content, episode_id, memory) → str
            return "new-mem-id-999"

        monkeypatch.setattr(mod, "reflect_memories", _reflect)
        monkeypatch.setattr(mod, "write_reflection", _write)

        docs = [
            _Doc("a", {"id": "d1", "episode_id": "ep1"}),
            _Doc("b", {"id": "d2", "episode_id": "ep1"}),
            _Doc("c", {"id": "d3", "episode_id": "ep1"}),
        ]
        tool, _ = _make_tool(docs)
        resp = _run(tool.execute(episode_id="ep1"))

        assert "new-mem-id-999" in resp.message
        assert "ep1" in resp.message
        assert "3 source memories" in resp.message
        assert resp.break_loop is False

    def test_success_response_includes_neuro_core_ack(self, monkeypatch):
        """F2: success-path Response must include additional['neuro_core_ack']."""
        from usr.plugins.neuro_core.tools import memory_reflect as mod

        async def _reflect(*a, **kw):
            return "Synthesised insight paragraph."

        async def _write(*a, **kw):
            return "new-mem-id-ack"

        monkeypatch.setattr(mod, "reflect_memories", _reflect)
        monkeypatch.setattr(mod, "write_reflection", _write)

        docs = [
            _Doc("a", {"id": "d1", "episode_id": "ep_ack"}),
            _Doc("b", {"id": "d2", "episode_id": "ep_ack"}),
        ]
        tool, _ = _make_tool(docs)
        resp = _run(tool.execute(episode_id="ep_ack"))

        assert resp.additional is not None
        assert "neuro_core_ack" in resp.additional
        ack = resp.additional["neuro_core_ack"]
        assert "ep_ack" in ack
        assert "new-mem-id-ack" in ack
        assert "2 memories" in ack


# ------------------------------------------------------- 4. limit passthru


class TestLimitPassthrough:
    """Spec case 4: limit arg must reach collect_episode_memories."""

    def test_limit_is_passed_to_collect(self, monkeypatch):
        from usr.plugins.neuro_core.tools import memory_reflect as mod

        seen = {}

        async def _collect(subdir, episode_id, memory, limit=20):
            seen["limit"] = limit
            seen["episode_id"] = episode_id
            seen["subdir"] = subdir
            return [_Doc("x", {"id": "d1", "episode_id": episode_id})]

        async def _reflect(*a, **kw):
            return "ok"

        async def _write(*a, **kw):
            return "w-id"

        monkeypatch.setattr(mod, "collect_episode_memories", _collect)
        monkeypatch.setattr(mod, "reflect_memories", _reflect)
        monkeypatch.setattr(mod, "write_reflection", _write)

        tool, _ = _make_tool([])
        resp = _run(tool.execute(episode_id="ep_pass", limit=7))

        assert seen["limit"] == 7
        assert seen["episode_id"] == "ep_pass"
        assert seen["subdir"] == "default"
        # Should also have succeeded end-to-end.
        assert "w-id" in resp.message

    def test_limit_is_clamped(self, monkeypatch):
        from usr.plugins.neuro_core.tools import memory_reflect as mod

        seen = {}

        async def _collect(subdir, episode_id, memory, limit=20):
            seen["limit"] = limit
            return [_Doc("x", {"id": "d1", "episode_id": episode_id})]

        async def _reflect(*a, **kw):
            return "ok"

        async def _write(*a, **kw):
            return "w-id"

        monkeypatch.setattr(mod, "collect_episode_memories", _collect)
        monkeypatch.setattr(mod, "reflect_memories", _reflect)
        monkeypatch.setattr(mod, "write_reflection", _write)

        tool, _ = _make_tool([])
        _run(tool.execute(episode_id="ep_clamp", limit=9999))
        # Hard ceiling is 100.
        assert seen["limit"] == 100


# ------------------------------------------------- 5. missing episode_id


class TestMissingArgs:
    """Tool must reject missing or empty episode_id without touching FAISS."""

    def test_missing_episode_id_returns_error(self, monkeypatch):
        from usr.plugins.neuro_core.tools import memory_reflect as mod

        called = {"get": False}

        async def _get(agent):
            called["get"] = True
            return object()

        mod.Memory = types.SimpleNamespace(get=_get, Area=types.SimpleNamespace(MAIN="main"))

        tool = mod.MemoryReflect()
        tool.agent = types.SimpleNamespace(config=types.SimpleNamespace(memory_subdir="default"))

        resp = _run(tool.execute(episode_id=""))
        assert "episode_id" in resp.message.lower()
        assert "required" in resp.message.lower()
        assert called["get"] is False


# ----------------------------------------------------------- module level


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
