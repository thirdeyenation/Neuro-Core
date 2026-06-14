"""Tests for helpers/reflection.py — Neuro Core episode reflection.

Five mandatory cases from the Phase 4 spec, plus extra coverage for the
edge cases that the reflection helper is contractually required to
handle (None inputs, LLM error, attribute-style response, sync
wrappers).
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, List

import pytest


# ----------------------------------------------------------------- helpers


class _Doc:
    """Minimal Document stand-in: page_content + metadata."""

    def __init__(self, page_content: str = "", metadata: dict | None = None):
        self.page_content = page_content
        self.metadata = dict(metadata or {})


def _make_memory(docs: List[_Doc]) -> Any:
    """Return a stub Memory whose ``db.get_by_ids(...)`` yields ``docs``.

    ``collect_episode_memories`` (see D21 final) uses exhaustive
    enumeration rather than semantic search: it reads all doc IDs from
    ``memory.db.index_to_docstore_id`` and then fetches them via
    ``memory.db.get_by_ids(all_ids)``. The stub provides both:

    - ``db.index_to_docstore_id`` — a dict mapping FAISS index
      positions to docstore IDs, populated from the test docs.
    - ``db.get_by_ids(ids)`` — synchronous, returns the full doc list
      regardless of the requested ids. The helper does the
      client-side ``episode_id`` filter and the limit cap.
    """

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

    class _Memory:
        db = _DB()
        Area = types.SimpleNamespace(MAIN="main")

    return _Memory()


def _run(coro):
    """Drive an async coroutine to completion in a fresh loop."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ----------------------------------------------------------- collect tests


class TestCollectEpisodeMemories:
    """Spec cases 1, 2, 3: filter, empty, limit."""

    def test_returns_only_matching_episode(self):
        from usr.plugins.neuro_core.helpers.reflection import collect_episode_memories

        docs = [
            _Doc("a1", {"id": "d1", "episode_id": "ep1", "timestamp": "2026-01-01T00:00:00"}),
            _Doc("a2", {"id": "d2", "episode_id": "ep2", "timestamp": "2026-01-01T00:00:01"}),
            _Doc("a3", {"id": "d3", "episode_id": "ep1", "timestamp": "2026-01-01T00:00:02"}),
            _Doc("a4", {"id": "d4", "timestamp": "2026-01-01T00:00:03"}),  # no episode
        ]
        mem = _make_memory(docs)

        result = _run(collect_episode_memories("default", "ep1", mem, limit=10))

        ids = [_doc_id(d) for d in result]
        assert ids == ["d1", "d3"]

    def test_returns_empty_list_when_no_matches(self):
        from usr.plugins.neuro_core.helpers.reflection import collect_episode_memories

        docs = [
            _Doc("x", {"id": "a", "episode_id": "epX"}),
            _Doc("y", {"id": "b", "episode_id": "epY"}),
        ]
        mem = _make_memory(docs)

        result = _run(collect_episode_memories("default", "ep_nope", mem))
        assert result == []

    def test_respects_limit_parameter(self):
        from usr.plugins.neuro_core.helpers.reflection import collect_episode_memories

        docs = [
            _Doc(
                f"text-{i}",
                {"id": f"id{i}", "episode_id": "epL", "timestamp": f"2026-01-01T00:00:{i:02d}"},
            )
            for i in range(5)
        ]
        mem = _make_memory(docs)

        result = _run(collect_episode_memories("default", "epL", mem, limit=2))
        assert len(result) == 2
        # The 2 earliest timestamps should be returned first.
        assert [_doc_id(d) for d in result] == ["id0", "id1"]

    def test_empty_episode_id_returns_empty(self):
        from usr.plugins.neuro_core.helpers.reflection import collect_episode_memories

        mem = _make_memory([_Doc("a", {"id": "x", "episode_id": "ep1"})])
        assert _run(collect_episode_memories("default", "", mem)) == []
        assert _run(collect_episode_memories("default", None, mem)) == []  # type: ignore[arg-type]

    def test_memory_db_failure_returns_empty(self):
        from usr.plugins.neuro_core.helpers.reflection import collect_episode_memories

        class _Broken:
            class db:
                # Non-empty mapping so the helper does not return early
                # at the ``index_to_docstore_id`` guard and actually
                # reaches the ``get_by_ids`` call.
                index_to_docstore_id = {0: "d0"}

                @staticmethod
                def get_by_ids(ids):
                    raise RuntimeError("FAISS offline")

                # Kept for completeness; no longer the retrieval path
                # used by the helper (see D21 final).
                @staticmethod
                def get_all_docs():
                    raise RuntimeError("FAISS offline")

        assert _run(collect_episode_memories("default", "ep1", _Broken())) == []

    def test_sync_wrapper_matches_async(self):
        from usr.plugins.neuro_core.helpers.reflection import (
            collect_episode_memories,
            collect_episode_memories_sync,
        )

        docs = [
            _Doc("a", {"id": "d1", "episode_id": "ep1"}),
            _Doc("b", {"id": "d2", "episode_id": "ep1"}),
        ]
        mem = _make_memory(docs)
        sync = collect_episode_memories_sync("default", "ep1", mem, limit=5)
        async_ = _run(collect_episode_memories("default", "ep1", mem, limit=5))
        assert len(sync) == len(async_) == 2


# ----------------------------------------------------------- reflect tests


class TestReflectMemories:
    """Spec case 5: LLM exception returns empty string."""

    def test_returns_empty_string_when_llm_raises(self):
        from usr.plugins.neuro_core.helpers.reflection import reflect_memories

        class _Boom:
            def call_utility_model(self, *a, **kw):
                raise RuntimeError("model unavailable")

        docs = [_Doc("alpha", {"id": "d1"}), _Doc("beta", {"id": "d2"})]
        result = _run(reflect_memories(docs, _Boom()))
        assert result == ""

    def test_returns_empty_string_when_docs_empty(self):
        from usr.plugins.neuro_core.helpers.reflection import reflect_memories

        # The LLM is never even consulted for empty input.
        class _NeverCalled:
            def call_utility_model(self, *a, **kw):
                raise AssertionError("LLM should not be called")

        result = _run(reflect_memories([], _NeverCalled()))
        assert result == ""

    def test_returns_empty_string_when_agent_is_none(self):
        from usr.plugins.neuro_core.helpers.reflection import reflect_memories

        docs = [_Doc("alpha", {"id": "d1"})]
        result = _run(reflect_memories(docs, None))
        assert result == ""

    def test_returns_string_from_call_utility_model(self):
        from usr.plugins.neuro_core.helpers.reflection import reflect_memories

        class _Echo:
            def call_utility_model(self, *, system, message, **kw):
                # Sanity check: the helper should pass a non-empty
                # system prompt and a message derived from the
                # doc contents.
                assert "memory reflection assistant" in system.lower() or system
                assert "alpha" in message
                return "  The agent learned to prefer X.  \n"

        docs = [_Doc("alpha", {"id": "d1"})]
        result = _run(reflect_memories(docs, _Echo()))
        assert result == "The agent learned to prefer X."

    def test_handles_object_response_with_content_attr(self):
        from usr.plugins.neuro_core.helpers.reflection import reflect_memories

        class _Response:
            content = "  A durable insight emerged.  "

        class _Agent:
            def call_utility_model(self, *, system, message, **kw):
                return _Response()

        docs = [_Doc("alpha", {"id": "d1"})]
        result = _run(reflection_call := reflect_memories(docs, _Agent()))
        assert result == "A durable insight emerged."

    def test_handles_dict_response(self):
        from usr.plugins.neuro_core.helpers.reflection import reflect_memories

        class _Agent:
            def call_utility_model(self, *, system, message, **kw):
                return {"content": "  dict-shaped response  "}

        docs = [_Doc("alpha", {"id": "d1"})]
        result = _run(reflect_memories(docs, _Agent()))
        assert result == "dict-shaped response"


# ---------------------------------------------------------- write tests


class TestWriteReflection:
    """Spec case 4: write_reflection uses the right metadata."""

    def test_writes_doc_with_required_metadata(self):
        from usr.plugins.neuro_core.helpers.reflection import write_reflection

        captured = {}

        class _Memory:
            Area = types.SimpleNamespace(MAIN="main")

            async def insert_text(self, content, metadata):
                captured["content"] = content
                captured["metadata"] = dict(metadata)
                return "new-id-001"

        mem = _Memory()
        new_id = _run(write_reflection("default", "some reflection text", "ep1", mem))

        assert new_id == "new-id-001"
        assert captured["content"] == "some reflection text"
        md = captured["metadata"]
        assert md["memory_type"] == "concept"
        assert md["stability"] == 0.9
        assert md["importance"] == 0.8
        assert md["episode_id"] == "ep1"
        assert md["source"] == "neuro_reflect"
        assert md["area"] == "main"

    def test_empty_content_returns_empty_id(self):
        from usr.plugins.neuro_core.helpers.reflection import write_reflection

        class _Memory:
            Area = types.SimpleNamespace(MAIN="main")
            async def insert_text(self, *a, **kw):
                raise AssertionError("should not be called")

        new_id = _run(write_reflection("default", "   ", "ep1", _Memory()))
        assert new_id == ""

    def test_memory_none_returns_empty_id(self):
        from usr.plugins.neuro_core.helpers.reflection import write_reflection

        new_id = _run(write_reflection("default", "content", "ep1", None))
        assert new_id == ""

    def test_insert_failure_returns_empty_id(self):
        from usr.plugins.neuro_core.helpers.reflection import write_reflection

        class _Memory:
            Area = types.SimpleNamespace(MAIN="main")
            async def insert_text(self, *a, **kw):
                raise RuntimeError("FAISS write failed")

        new_id = _run(write_reflection("default", "content", "ep1", _Memory()))
        assert new_id == ""

    def test_insert_documents_path_used_when_insert_text_missing(self):
        from usr.plugins.neuro_core.helpers.reflection import write_reflection

        captured = {}

        class _Memory:
            Area = types.SimpleNamespace(MAIN="main")

            async def insert_documents(self, docs):
                captured["doc"] = docs[0]
                return ["id-via-documents"]

        new_id = _run(write_reflection("default", "content", "ep9", _Memory()))
        assert new_id == "id-via-documents"
        assert captured["doc"].page_content == "content"
        assert captured["doc"].metadata["memory_type"] == "concept"
        assert captured["doc"].metadata["episode_id"] == "ep9"


# ----------------------------------------------------------- module level


def _doc_id(d):
    return d.metadata.get("id") if isinstance(d.metadata, dict) else None


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
