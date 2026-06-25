"""Tests for the Neuro Core ``memory_score`` tool.

Covers:
- The tool updates ``ScoreStore`` correctly when valid scores are passed.
- Score clamping to [0.0, 1.0] is enforced by ``validate_neuro_metadata``
  before persistence.
- ``task_status`` is rejected with a clear error when the memory is not
  of type ``"task"`` (guard enforced in the tool, not just the prompt).
- Invalid ``validation_status`` values are coerced to ``"unvalidated"``.
- A non-existent ``id`` returns a clear error string and does not raise.

Implementation note:
    ``langchain_core`` is not directly importable in the standalone
    pytest environment (it lives in the framework venv, not the test
    runtime). We mimic a ``Document`` with a tiny ``types.SimpleNamespace``
    that exposes ``.page_content`` and ``.metadata``, which is all the
    tool ever reads.
"""

from __future__ import annotations

import asyncio
import types
import unittest.mock as mock
from pathlib import Path

import pytest

# --- paths ---------------------------------------------------------------
# The conftest.py at this level adds /a0 to sys.path; we rely on that.

from usr.plugins.neuro_core.tools.memory_score import MemoryScore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers: a Document stand-in that satisfies the tool's read-only contract
# ---------------------------------------------------------------------------


class _FakeDoc:
    """Minimal stand-in for ``langchain_core.documents.Document``."""

    def __init__(self, metadata: dict, page_content: str = "test content"):
        self.metadata = dict(metadata)
        self.page_content = page_content


# ---------------------------------------------------------------------------
# Test fixtures: minimal agent + Memory + ScoreStore mocks
# ---------------------------------------------------------------------------


def _make_doc(meta: dict) -> _FakeDoc:
    return _FakeDoc(meta)


def _make_tool(existing_doc, memory_subdir: str = "default"):
    """Build a MemoryScore instance wired to in-memory mock backends."""
    # Inner FAISS-like store
    inner_db = mock.MagicMock()
    if existing_doc is not None:
        inner_db.get_by_ids = mock.MagicMock(return_value=[existing_doc])
    else:
        inner_db.get_by_ids = mock.MagicMock(return_value=[])

    # The Memory wrapper used by the tool
    mem_wrap = mock.MagicMock()
    mem_wrap.db = inner_db
    mem_wrap.memory_subdir = memory_subdir
    # update_documents is async
    mem_wrap.update_documents = mock.AsyncMock(return_value=None)

    # The agent just needs a placeholder (the tool only calls .read_prompt
    # indirectly via before_execution, which we do not invoke here).
    agent = mock.MagicMock()

    tool = MemoryScore(
        agent=agent,
        name="memory_score",
        method=None,
        args={},
        message="",
        loop_data=None,
    )
    return tool, mem_wrap, inner_db


def _patch_memory(monkeypatch, mem_wrap):
    """Patch ``Memory.get`` so the tool sees our mock wrapper."""
    async def fake_get(agent):
        return mem_wrap
    monkeypatch.setattr(
        "usr.plugins.neuro_core.tools.memory_score.Memory.get",
        fake_get,
    )


def _patch_score_store_to_tmp(monkeypatch, tmp_path: Path, subdir: str):
    """Point ScoreStore's filesystem root at ``tmp_path`` for this test."""
    monkeypatch.setattr(
        "usr.plugins.neuro_core.helpers.scores._scores_path",
        lambda s: str(tmp_path / s / "scores.json"),
    )


# ---------------------------------------------------------------------------
# 1. Valid scores are persisted to ScoreStore (and FAISS untouched)
# ---------------------------------------------------------------------------


class TestValidScoreUpdate:
    def test_valid_scores_persisted_to_sidestore(
        self, monkeypatch, tmp_path
    ):
        doc = _make_doc({"memory_type": "fact"})
        tool, mem_wrap, _ = _make_tool(doc, memory_subdir="default")
        _patch_memory(monkeypatch, mem_wrap)
        _patch_score_store_to_tmp(monkeypatch, tmp_path, "default")

        from usr.plugins.neuro_core.helpers.scores import ScoreStore

        result = asyncio.run(
            tool.execute(
                id="mem-1", importance=0.9, confidence=0.8, stability=0.7
            )
        )

        # Confirmation message lists the id and every changed field.
        assert "mem-1" in result.message
        assert "importance: 0.9" in result.message
        assert "confidence: 0.8" in result.message
        assert "stability: 0.7" in result.message
        assert result.break_loop is False

        # ScoreStore has the new values on disk.
        store = ScoreStore("default")
        record = store.get("mem-1")
        assert record.importance == pytest.approx(0.9)
        assert record.confidence == pytest.approx(0.8)
        assert record.stability == pytest.approx(0.7)

        # D41 fix: score updates now also write back to FAISS docstore.
        mem_wrap.update_documents.assert_called_once()
        called_doc = mem_wrap.update_documents.call_args.args[0][0]
        assert called_doc.metadata.get("importance") == pytest.approx(0.9)
        assert called_doc.metadata.get("confidence") == pytest.approx(0.8)
        assert called_doc.metadata.get("stability") == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# 2. Clamping: out-of-range values are normalized before persist
# ---------------------------------------------------------------------------


class TestScoreClamping:
    def test_out_of_range_clamped_before_persist(
        self, monkeypatch, tmp_path
    ):
        doc = _make_doc({"memory_type": "fact"})
        tool, mem_wrap, _ = _make_tool(doc, memory_subdir="default")
        _patch_memory(monkeypatch, mem_wrap)
        _patch_score_store_to_tmp(monkeypatch, tmp_path, "default")

        from usr.plugins.neuro_core.helpers.scores import ScoreStore

        # 1.5 and -0.4 should be clamped to 1.0 and 0.0 respectively.
        result = asyncio.run(
            tool.execute(
                id="mem-2",
                importance=1.5,
                confidence=-0.4,
                stability=2.0,
            )
        )

        assert result.break_loop is False

        store = ScoreStore("default")
        record = store.get("mem-2")
        assert record.importance == pytest.approx(1.0)
        assert record.confidence == pytest.approx(0.0)
        assert record.stability == pytest.approx(1.0)

        # Confirmation lists the clamped (post-normalization) values.
        assert "importance: 1.0" in result.message
        assert "confidence: 0.0" in result.message


# ---------------------------------------------------------------------------
# 3. task_status is rejected when memory_type is not "task"
# ---------------------------------------------------------------------------


class TestTaskStatusGuard:
    def test_task_status_rejected_for_non_task_memory(self, monkeypatch):
        doc = _make_doc({"memory_type": "fact"})
        tool, mem_wrap, _ = _make_tool(doc, memory_subdir="default")
        _patch_memory(monkeypatch, mem_wrap)

        result = asyncio.run(tool.execute(id="mem-3", task_status="active"))

        # The tool MUST return a clear error string and NOT raise.
        assert result.break_loop is False
        assert "Error" in result.message
        assert "task" in result.message.lower()

        # Neither the FAISS store nor the sidecar should have been touched.
        mem_wrap.update_documents.assert_not_called()

    def test_task_status_accepted_for_task_memory(self, monkeypatch):
        doc = _make_doc({"memory_type": "task"})
        tool, mem_wrap, _ = _make_tool(doc, memory_subdir="default")
        _patch_memory(monkeypatch, mem_wrap)

        result = asyncio.run(
            tool.execute(
                id="mem-4",
                task_status="active",
                validation_status="validated",
            )
        )

        assert result.break_loop is False
        assert "Updated memory 'mem-4'" in result.message
        assert "task_status: active" in result.message
        assert "validation_status: validated" in result.message

        # FAISS was updated (metadata fields changed).
        mem_wrap.update_documents.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. Invalid validation_status is normalized to "unvalidated"
# ---------------------------------------------------------------------------


class TestInvalidValidationStatus:
    def test_invalid_validation_status_normalized(self, monkeypatch):
        doc = _make_doc({"memory_type": "fact"})
        tool, mem_wrap, _ = _make_tool(doc, memory_subdir="default")
        _patch_memory(monkeypatch, mem_wrap)

        result = asyncio.run(
            tool.execute(
                id="mem-5", validation_status="bogus_status_value"
            )
        )

        # Bogus value is coerced to "unvalidated" by validate_neuro_metadata.
        assert result.break_loop is False
        assert "validation_status: unvalidated" in result.message

        # The doc was updated with the normalized value.
        assert doc.metadata.get("validation_status") == "unvalidated"
        mem_wrap.update_documents.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. Non-existent id returns a clear error, does not raise
# ---------------------------------------------------------------------------


class TestMissingId:
    def test_missing_id_returns_error_string(self, monkeypatch):
        tool, mem_wrap, _ = _make_tool(
            existing_doc=None, memory_subdir="default"
        )
        _patch_memory(monkeypatch, mem_wrap)

        # Must not raise even though the id does not exist.
        result = asyncio.run(
            tool.execute(id="ghost-id", importance=0.5)
        )

        assert result.break_loop is False
        assert "Error" in result.message
        assert "ghost-id" in result.message

        # Nothing should have been persisted.
        mem_wrap.update_documents.assert_not_called()

    def test_empty_id_rejected(self, monkeypatch):
        tool, mem_wrap, _ = _make_tool(
            existing_doc=None, memory_subdir="default"
        )
        _patch_memory(monkeypatch, mem_wrap)

        result = asyncio.run(tool.execute(id="", importance=0.5))
        assert result.break_loop is False
        assert "Error" in result.message
        mem_wrap.update_documents.assert_not_called()

    def test_no_updatable_fields_rejected(self, monkeypatch):
        doc = _make_doc({"memory_type": "fact"})
        tool, mem_wrap, _ = _make_tool(doc, memory_subdir="default")
        _patch_memory(monkeypatch, mem_wrap)

        result = asyncio.run(tool.execute(id="mem-6"))
        assert result.break_loop is False
        assert "Error" in result.message
        mem_wrap.update_documents.assert_not_called()
