"""Tests for ``tools/neuro_capture.py`` (KI-019, WI-P6-TOOLTESTS).

Contract coverage grounded in the actual source (not docs):

1. Happy path: valid ``text``/``project`` stores the memory and returns
   ``{"memory_id", "outcome": "stored", "scope": project}``; the memory is
   actually persisted in the SQLite store at the resolved DB path.
2. Invalid input: empty ``text`` or empty ``project`` raises
   ``ValueError("text and project are required")`` — and does so BEFORE any
   store is opened (no DB file is created on the failure path).
3. Edge: non-numeric ``importance`` fails at ``float(...)`` conversion with
   ``ValueError`` (grounded: ``execute`` calls ``float(importance)`` and
   ``float(confidence)``).
4. Edge: ``agent=""`` normalizes to ``Scope.agent = None`` (grounded:
   ``Scope(project, agent or None)``).
5. The store connection is closed in a ``finally`` block after success.

DB isolation: the module-level ``_resolve_db_path`` is monkeypatched to a
``tmp_path``-rooted path for every test — the live ``neuro_core.db`` is
never touched (fixture policy: disposable/synthetic).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_PLUGIN_ROOT = "/a0/usr/plugins/neuro_core"
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)


def _load_tool(monkeypatch, tmp_path: Path):
    """Import the tool module with its DB path pointed at a tmp file."""
    db_path = tmp_path / "capture_test.db"
    import importlib

    mod = importlib.import_module("tools.neuro_capture")
    monkeypatch.setattr(mod, "_resolve_db_path", lambda: str(db_path))
    return mod, db_path


def _make_tool(mod):
    return mod.NeuroCapture(
        agent=None, name="neuro_capture", method=None,
        args={}, message="", loop_data=None,
    )


@pytest.mark.asyncio
async def test_capture_stores_memory_and_returns_contract_shape(
    monkeypatch, tmp_path: Path
):
    """Valid capture returns the documented dict shape and persists the
    memory (text, source, project scope) in the SQLite store."""
    from sqlite_store import SQLiteStore

    mod, db_path = _load_tool(monkeypatch, tmp_path)
    tool = _make_tool(mod)

    result = await tool.execute(
        text="remember this fact", source="user", project="proj1"
    )

    assert isinstance(result, dict)
    assert result["outcome"] == "stored"
    assert result["scope"] == "proj1"
    assert isinstance(result["memory_id"], str) and result["memory_id"]

    # The memory is actually persisted at the resolved DB path.
    store = SQLiteStore(str(db_path))
    try:
        stored = store.get(result["memory_id"])
        assert stored is not None
        assert stored.text == "remember this fact"
        assert stored.source == "user"
        assert stored.scope.project == "proj1"
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"text": "t", "project": ""},
    {"text": "", "project": "p"},
])
async def test_capture_empty_required_args_raise_value_error(
    monkeypatch, tmp_path: Path, kwargs
):
    """Empty ``text`` and/or ``project`` raises ValueError before any
    store is opened — no DB file is created on the failure path.

    Grounded: the guard is ``if not text or not project`` — note that
    ``project`` defaults to "default", so an omitted project is valid;
    only an explicitly empty value triggers the error.
    """
    mod, db_path = _load_tool(monkeypatch, tmp_path)
    tool = _make_tool(mod)

    with pytest.raises(ValueError, match="text and project are required"):
        await tool.execute(**kwargs)

    assert not db_path.exists(), (
        "no DB file should be created when validation fails"
    )


@pytest.mark.asyncio
async def test_capture_non_numeric_importance_raises_value_error(
    monkeypatch, tmp_path: Path
):
    """A non-numeric ``importance`` fails at ``float(importance)`` with
    ValueError (grounded in the execute() source)."""
    mod, _db_path = _load_tool(monkeypatch, tmp_path)
    tool = _make_tool(mod)

    with pytest.raises(ValueError):
        await tool.execute(
            text="x", project="p", importance="not-a-number"
        )


@pytest.mark.asyncio
async def test_capture_empty_agent_normalizes_to_none_scope_agent(
    monkeypatch, tmp_path: Path
):
    """``agent=""`` maps to ``Scope(project, agent or None)`` — the stored
    scope agent is ``None``, not an empty string."""
    from sqlite_store import SQLiteStore

    mod, db_path = _load_tool(monkeypatch, tmp_path)
    tool = _make_tool(mod)

    result = await tool.execute(text="t", project="p", agent="")

    store = SQLiteStore(str(db_path))
    try:
        stored = store.get(result["memory_id"])
        assert stored.scope.agent is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_capture_closes_store_after_success(monkeypatch, tmp_path: Path):
    """The tool closes the SQLite connection in a ``finally`` block, so a
    fresh reader can open and read the DB after a successful capture."""
    import sqlite3

    mod, db_path = _load_tool(monkeypatch, tmp_path)
    tool = _make_tool(mod)

    await tool.execute(text="t", project="p")

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        assert row[0] == 1
    finally:
        conn.close()
