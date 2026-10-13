"""Tests for ``tools/neuro_validate.py`` (KI-019, WI-P6-TOOLTESTS).

Contract coverage grounded in the actual source (not docs):

1. Happy path: ``memory_id`` + valid ``state`` performs the lifecycle
   transition and returns ``{"memory_id", "validation", "outcome":
   "updated"}`` (grounded: execute() return shape; service.validate).
2. Invalid input: missing ``memory_id`` or missing ``state`` raises
   ``ValueError("memory_id and state are required")`` before any store is
   opened.
3. Edge: an unknown ``state`` string raises
   ``ValueError("state must be unreviewed, validated, disputed, or
   superseded")`` — the tool maps the ValidationState construction error
   to this message (grounded: try/except ValueError in execute()).
4. Edge: a memory_id that does not exist in the store raises ``KeyError``
   (grounded: NeuroCoreService.validate raises ``KeyError(memory_id)``).
5. Edge: an invalid lifecycle transition (e.g., validated -> unreviewed,
   not in ``_ALLOWED``) raises ``ValueError`` with the transition message
   (grounded: ``memory_lifecycle.transition``).

DB isolation: module-level ``_resolve_db_path`` is monkeypatched to a
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
    db_path = tmp_path / "validate_test.db"
    import importlib

    mod = importlib.import_module("tools.neuro_validate")
    monkeypatch.setattr(mod, "_resolve_db_path", lambda: str(db_path))
    return mod, db_path


def _seed_one(mod, db_path: Path):
    """Seed one memory in the disposable store; return its memory_id."""
    from neuro_core import Memory, Scope
    from neuro_service import NeuroCoreService
    from sqlite_store import SQLiteStore

    store = SQLiteStore(str(db_path))
    try:
        memory = NeuroCoreService(store).capture(
            Memory("seeded memory", "agent_zero", Scope("p1"))
        )
        return memory.memory_id
    finally:
        store.close()


def _make_tool(mod):
    return mod.NeuroValidate(
        agent=None, name="neuro_validate", method=None,
        args={}, message="", loop_data=None,
    )


@pytest.mark.asyncio
async def test_validate_transitions_to_validated(
    monkeypatch, tmp_path: Path
):
    """A valid transition returns the contract shape and the stored
    validation state actually changes to the target state."""
    from sqlite_store import SQLiteStore

    mod, db_path = _load_tool(monkeypatch, tmp_path)
    memory_id = _seed_one(mod, db_path)
    tool = _make_tool(mod)

    result = await tool.execute(memory_id=memory_id, state="validated")

    assert isinstance(result, dict)
    assert result == {
        "memory_id": memory_id,
        "validation": "validated",
        "outcome": "updated",
    }

    # The transition is persisted, not just echoed back.
    store = SQLiteStore(str(db_path))
    try:
        stored = store.get(memory_id)
        assert stored.validation.value == "validated"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_validate_transitions_to_disputed_and_back_family(
    monkeypatch, tmp_path: Path
):
    """Each state in the documented four-state vocabulary is accepted
    (disputed exercised here; unreviewed->superseded covered by the
    _ALLOWED table in memory_lifecycle)."""
    mod, db_path = _load_tool(monkeypatch, tmp_path)
    memory_id = _seed_one(mod, db_path)
    tool = _make_tool(mod)

    result = await tool.execute(memory_id=memory_id, state="disputed")
    assert result["validation"] == "disputed"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {},
    {"memory_id": "only-id"},
    {"state": "validated"},
])
async def test_validate_missing_required_args_raise_value_error(
    monkeypatch, tmp_path: Path, kwargs
):
    """Missing ``memory_id`` and/or ``state`` raises ValueError before any
    store is opened — no DB file is created on the failure path."""
    mod, db_path = _load_tool(monkeypatch, tmp_path)
    tool = _make_tool(mod)

    with pytest.raises(ValueError, match="memory_id and state are required"):
        await tool.execute(**kwargs)

    assert not db_path.exists()


@pytest.mark.asyncio
async def test_validate_unknown_state_raises_value_error_with_contract_message(
    monkeypatch, tmp_path: Path
):
    """An unrecognized state maps the ValidationState construction error
    to the documented contract message (grounded in execute())."""
    mod, db_path = _load_tool(monkeypatch, tmp_path)
    memory_id = _seed_one(mod, db_path)
    tool = _make_tool(mod)

    with pytest.raises(
        ValueError,
        match="state must be unreviewed, validated, disputed, or superseded",
    ):
        await tool.execute(memory_id=memory_id, state="bogus-state")


@pytest.mark.asyncio
async def test_validate_unknown_memory_id_raises_key_error(
    monkeypatch, tmp_path: Path
):
    """A memory_id absent from the store raises KeyError (grounded:
    NeuroCoreService.validate raises KeyError(memory_id))."""
    mod, db_path = _load_tool(monkeypatch, tmp_path)
    _seed_one(mod, db_path)
    tool = _make_tool(mod)

    with pytest.raises(KeyError):
        await tool.execute(memory_id="does-not-exist", state="validated")


@pytest.mark.asyncio
async def test_validate_invalid_transition_raises_value_error(
    monkeypatch, tmp_path: Path
):
    """validated -> unreviewed is not in ``_ALLOWED``; the tool surfaces
    the lifecycle ValueError instead of corrupting state."""
    mod, db_path = _load_tool(monkeypatch, tmp_path)
    memory_id = _seed_one(mod, db_path)
    tool = _make_tool(mod)

    # First move the memory to validated (allowed from unreviewed).
    await tool.execute(memory_id=memory_id, state="validated")

    # Then attempt the forbidden reverse transition.
    with pytest.raises(ValueError, match="invalid lifecycle transition"):
        await tool.execute(memory_id=memory_id, state="unreviewed")
