"""WI-004 integration tests - lifecycle repositioning & model-less migration.

Covers the AC-1 scenarios (a)-(e) from work-spec rev 2:

(a) fresh v3 ``memory.sqlite3`` with unmigrated docs -> ``(True, N)``
    model-lessly + automatic ``_20_neuro_migration.py`` backfill triggered;
(b) already-migrated docs and a second run after backfill -> ``(False, N)``
    + zero heavy Memory import / model load (idempotency);
(c) dual-marker dir (migrated ``memory.sqlite3`` + stale unmigrated
    ``index.pkl``) -> ``(False, N)`` proving memory.sqlite3 precedence;
(d) marker-less project dir under ``usr/projects/`` -> zero heavy fallback;
(e) read-only byte-identity + zero journal/WAL/SHM sidecar artifacts on
    ``memory.sqlite3``.

All fixtures are hermetic (``tmp_path``); detection is exercised through the
shared engine (``helpers/migration_engine.py``) with injected resolvers, and
the ``_20`` extension is driven with a stub ``Memory`` facade so no embedding
model is ever loaded.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from usr.plugins.neuro_core import execute
from usr.plugins.neuro_core.helpers import migration_engine


# ---------------------------------------------------------------------------
# _20 extension loading (sibling file, not a package module)
# ---------------------------------------------------------------------------


def _load_ext():
    ext_path = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "python"
        / "startup_migration"
        / "_20_neuro_migration.py"
    )
    spec = importlib.util.spec_from_file_location("_20_neuro_migration", ext_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# v3 NativeStore fixture builders (schema per native_store.py)
# ---------------------------------------------------------------------------


def _make_sqlite_store(db_dir: Path, docs: list[dict]) -> Path:
    """Create a v3 NativeStore ``memory.sqlite3`` with the production schema."""
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE docs (id TEXT PRIMARY KEY, text TEXT, "
            "metadata TEXT, vector BLOB)"
        )
        for i, doc in enumerate(docs):
            conn.execute(
                "INSERT INTO docs (id, text, metadata, vector) VALUES (?, ?, ?, ?)",
                (f"doc-{i}", doc.get("text", "body"),
                 json.dumps(doc["metadata"]), b"\x00" * 4),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _unmigrated_meta() -> dict:
    return {"source": "legacy", "area": "main"}


def _migrated_meta() -> dict:
    return {
        "source": "legacy",
        "memory_type": "note",
        "importance": 0.5,
        "confidence": 0.5,
        "stability": 0.5,
        "validation_status": "unvalidated",
    }


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# (a) fresh v3 sqlite with unmigrated docs -> (True, N) + backfill triggered
# ---------------------------------------------------------------------------


class _StubMemoryFacade:
    """Stub decorated Memory facade recording update_documents calls."""

    instances: list = []

    def __init__(self, docs: dict):
        self.docs = docs
        self.updated: list = []

        class _DB:
            def __init__(self, outer):
                self._outer = outer

            def get_all_docs(self):
                return dict(self._outer.docs)

        self.db = _DB(self)

    async def update_documents(self, docs):
        self.updated.extend(docs)

    @classmethod
    async def get_by_subdir(cls, subdir, log_item=None, preload_knowledge=False):
        assert preload_knowledge is False  # KI-052: no embedding-model load
        inst = cls.instances[-1]
        return inst


def test_a_fresh_sqlite_detects_true_and_extension_backfills(
    tmp_path, monkeypatch, caplog
):
    db_dir = tmp_path / "main"
    _make_sqlite_store(
        db_dir,
        [{"metadata": _unmigrated_meta()}, {"metadata": _unmigrated_meta()}],
    )

    # Model-less detection: (True, N) with zero heavy imports. Detection
    # short-circuits at the FIRST unmigrated row, so docs_scanned counts the
    # rows examined up to the decision (fail-fast semantics).
    status, scanned = migration_engine.detect_subdir(
        "main", resolve_fn=lambda s: str(db_dir)
    )
    assert status is True
    assert scanned == 1

    # Automatic _20 extension backfill through the Memory facade.
    ext = _load_ext()
    facade = _StubMemoryFacade(
        {
            "d1": type("D", (), {"metadata": dict(_unmigrated_meta())})(),
            "d2": type("D", (), {"metadata": dict(_migrated_meta())})(),
        }
    )
    _StubMemoryFacade.instances.append(facade)

    monkeypatch.setattr(
        migration_engine, "list_memory_subdirs", lambda: ["main"], raising=True
    )
    monkeypatch.setattr(
        migration_engine,
        "resolve_db_dir",
        lambda s, projects_fail_closed=True, resolve_fn=None: str(db_dir),
        raising=True,
    )
    monkeypatch.setattr(
        ext,
        "_import_memory",
        lambda: (lambda: ["main"], _StubMemoryFacade),
        raising=True,
    )

    with caplog.at_level(logging.INFO, logger="neuro_core.startup_migration"):
        result = ext.NeuroStartupMigration().execute()
    assert result is None  # sync contract: never an awaitable
    assert len(facade.updated) == 1  # only the unmigrated doc
    meta = facade.updated[0].metadata
    assert meta["memory_type"] == "note"
    assert meta["validation_status"] == "unvalidated"
    assert meta["source"] == "legacy"  # existing values preserved

    # Regression (WI-004-AA): the cold-boot backfill must complete its success
    # path -- zero WARNING/ERROR records (no masked TypeError from a bare
    # `return` in _run_async's no-running-loop branch) and the explicit
    # "startup migration complete" INFO record emitted.
    warning_or_error = [
        r for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert warning_or_error == [], [
        (r.levelname, r.getMessage()) for r in warning_or_error
    ]
    assert not any(
        "startup migration non-fatal" in r.getMessage()
        or "TypeError" in r.getMessage()
        for r in caplog.records
    )
    assert any(
        "startup migration complete: subdirs_processed=" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO
    )


# ---------------------------------------------------------------------------
# (b) already-migrated docs -> (False, N); second run: zero heavy import
# ---------------------------------------------------------------------------


def test_b_migrated_sqlite_is_false_and_second_run_skips_heavy(
    tmp_path, monkeypatch
):
    db_dir = tmp_path / "main"
    _make_sqlite_store(
        db_dir,
        [{"metadata": _migrated_meta()}, {"metadata": _migrated_meta()}],
    )

    status, scanned = migration_engine.detect_subdir(
        "main", resolve_fn=lambda s: str(db_dir)
    )
    assert status is False
    assert scanned == 2

    # Second run after backfill: the heavy Memory import must never happen.
    ext = _load_ext()
    heavy_called = []

    def _forbidden_heavy_import():
        heavy_called.append(True)
        return (None, None)

    monkeypatch.setattr(
        migration_engine, "list_memory_subdirs", lambda: ["main"], raising=True
    )
    monkeypatch.setattr(
        migration_engine,
        "resolve_db_dir",
        lambda s, projects_fail_closed=True, resolve_fn=None: str(db_dir),
        raising=True,
    )
    monkeypatch.setattr(ext, "_import_memory", _forbidden_heavy_import, raising=True)

    result = ext.NeuroStartupMigration().execute()
    assert result is None
    assert heavy_called == []  # cold-boot model-load storm prevention


# ---------------------------------------------------------------------------
# (c) dual-marker dir: migrated sqlite + stale unmigrated pickle -> (False, N)
# ---------------------------------------------------------------------------


class _PDoc:
    """Pickle-safe doc stand-in with a metadata attribute."""

    def __init__(self, metadata):
        self.metadata = metadata


class _InMemoryDocstore:
    def __init__(self, _dict=None):
        self._dict = _dict or {}


def test_c_dual_marker_sqlite_wins_over_stale_pickle(tmp_path):
    db_dir = tmp_path / "main"
    _make_sqlite_store(db_dir, [{"metadata": _migrated_meta()}])

    # Stale legacy marker: UNMIGRATED docs in index.pkl (the v3 _LegacyReader
    # never deletes legacy files, so this dir legitimately carries both).
    stale_store = _InMemoryDocstore(
        {"k1": _PDoc(dict(_unmigrated_meta()))}
    )
    with open(db_dir / "index.pkl", "wb") as f:
        import pickle

        pickle.dump((stale_store, {"k1": "d1"}), f)

    status, scanned = migration_engine.detect_subdir(
        "main", resolve_fn=lambda s: str(db_dir)
    )
    assert status is False  # memory.sqlite3 precedence: stale pickle never wins
    assert scanned == 1


# ---------------------------------------------------------------------------
# (d) marker-less project dir -> (False, 0), zero heavy fallback
# ---------------------------------------------------------------------------


def test_d_markerless_project_dir_is_false_zero(tmp_path, monkeypatch):
    proj_mem = tmp_path / "projects" / "proj1" / ".a0proj" / "memory"
    proj_mem.mkdir(parents=True)  # exists but carries NO markers

    # Engine-level: projects_fail_closed=False resolves model-lessly.
    status, scanned = migration_engine.detect_subdir(
        "projects/proj1",
        projects_fail_closed=False,
        resolve_fn=lambda s: str(proj_mem),
    )
    assert status is False
    assert scanned == 0

    # Binding execute.py CLI contract: projects/ fails closed (None).
    assert execute._abs_db_dir_light("projects/neuro_core_2") is None

    # Extension sweep: a marker-less project dir never triggers the heavy path.
    ext = _load_ext()
    heavy_called = []

    def _forbidden_heavy_import():
        heavy_called.append(True)
        return (None, None)

    monkeypatch.setattr(
        migration_engine,
        "list_memory_subdirs",
        lambda: ["projects/proj1"],
        raising=True,
    )
    monkeypatch.setattr(
        migration_engine,
        "resolve_db_dir",
        lambda s, projects_fail_closed=True, resolve_fn=None: (
            str(proj_mem) if not projects_fail_closed else None
        ),
        raising=True,
    )
    monkeypatch.setattr(ext, "_import_memory", _forbidden_heavy_import, raising=True)

    result = ext.NeuroStartupMigration().execute()
    assert result is None
    assert heavy_called == []


# ---------------------------------------------------------------------------
# (e) read-only byte-identity + zero journal/WAL/SHM sidecars
# ---------------------------------------------------------------------------


def test_e_sqlite_detection_is_byte_identical_and_sidecar_free(tmp_path):
    db_dir = tmp_path / "main"
    db_path = _make_sqlite_store(
        db_dir,
        [{"metadata": _unmigrated_meta()}, {"metadata": _migrated_meta()}],
    )

    before = _file_hash(db_path)
    status, scanned = migration_engine.detect_subdir(
        "main", resolve_fn=lambda s: str(db_dir)
    )
    after = _file_hash(db_path)

    assert status is True  # first row is unmigrated
    assert scanned == 1
    assert before == after  # byte-identity: detection performed ZERO writes

    sidecars = [
        p.name
        for p in db_dir.iterdir()
        if p.name != "memory.sqlite3"
    ]
    assert sidecars == []  # zero journal/WAL/SHM artifacts


# ---------------------------------------------------------------------------
# Sync-safety: _run_async works with an event loop already running
# ---------------------------------------------------------------------------


def test_run_async_safe_inside_running_loop():
    ext = _load_ext()

    async def _coro():
        await asyncio.sleep(0)
        return 42

    async def _driver():
        # A loop IS running here; _run_async must still complete the coro.
        return ext._run_async(_coro())

    assert asyncio.run(_driver()) == 42


def test_run_async_returns_coroutine_value_in_both_branches():
    """WI-004-AA regression: _run_async returns the coroutine's value in BOTH
    the no-running-loop branch (direct asyncio.run) and the running-loop
    worker-thread branch. The cold-boot branch previously did a bare
    ``return`` (None), causing a TypeError at the 3-tuple unpack in
    _run_detection_and_backfill, masked as a non-fatal WARNING.
    """
    ext = _load_ext()

    async def _coro(value):
        await asyncio.sleep(0)
        return value

    # Branch 1: no running loop in this thread -> direct asyncio.run path.
    assert ext._run_async(_coro((1, 2, 3))) == (1, 2, 3)

    # Branch 2: a loop IS running in this thread -> worker-thread path.
    async def _driver():
        return ext._run_async(_coro((4, 5, 6)))

    assert asyncio.run(_driver()) == (4, 5, 6)


# ---------------------------------------------------------------------------
# Fail-closed: corrupt sqlite metadata -> (None, 0)
# ---------------------------------------------------------------------------


def test_corrupt_sqlite_metadata_fails_closed(tmp_path):
    db_dir = tmp_path / "main"
    _make_sqlite_store(db_dir, [{"metadata": "not-json{"}])
    status, scanned = migration_engine.detect_subdir(
        "main", resolve_fn=lambda s: str(db_dir)
    )
    assert status is None
    assert scanned == 0
