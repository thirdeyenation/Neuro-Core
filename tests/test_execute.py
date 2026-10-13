"""Tests for the Neuro Core one-shot migration (``execute.py``).

The migration is invoked by Agent Zero's plugin manager when the plugin is
upgraded or first enabled. It walks every existing memory subdirectory,
finds documents that are missing one or more Neuro Core metadata fields,
seeds defaults via ``apply_defaults()`` (preserving existing values), and
persists the changes back to the FAISS index.

Contract being tested (from ``/a0/usr/plugins/neuro_core/execute.py``):

- ``_NEURO_CORE_FIELDS = ("memory_type", "importance", "confidence",
  "stability", "validation_status")`` — these are the five fields the
  migration seeds / checks.
- ``_needs_migration(metadata) -> bool`` — returns ``True`` when ANY of the
  five fields is absent.
- ``async _migrate_subdir(memory_subdir, memory_cls) -> tuple[int, int]``
  — returns ``(docs_scanned, docs_updated)``; calls
  ``await memory_cls.get_by_subdir(subdir, log_item=None)``,
  ``mem.db.get_all_docs()`` and ``await mem.update_documents(modified)``
  only when at least one doc needed migration.
- ``async _run_migration(get_existing_memory_subdirs_fn, memory_cls)``
  — returns ``(subdirs_processed, total_scanned, total_updated)``;
  prints a per-subdir line ``[neuro_core]   subdir 'X': scanned=N updated=M``.
- ``main() -> int`` — returns 0 on success, 1 on ``ImportError``; prints
  a final summary line ``[neuro_core] migration complete:``
  ``subdirs_processed=N docs_scanned=M docs_updated=K``.
- The migration is **non-destructive** — ``apply_defaults`` only seeds
  missing fields and never overwrites existing values.

Coverage:

1. ``test_migration_seeds_neuro_core_fields_on_fresh_memory``
2. ``test_migration_is_idempotent_on_second_run``
3. ``test_migration_preserves_partial_existing_fields``
4. ``test_migration_on_empty_subdir_returns_zero_counts``
5. ``test_run_migration_processes_all_subdirs_with_per_subdir_counts``
6. ``test_main_returns_zero_and_prints_summary``
7. ``test_main_returns_one_when_memory_plugin_missing``

Stubbing strategy (no live FAISS):
    A ``stub_memory_registry`` fixture provides a tiny ``_StubMemory``
    class plus a mutable ``registry`` (``subdir -> {doc_id: _StubDoc}``)
    and a call-record dict for ``update_documents``. This mirrors the
    inline-mock pattern used in ``test_memory_score_tool.py`` and
    avoids booting the real ``plugins._memory.helpers.memory`` module.
    The ``main()`` tests additionally monkey-patch
    ``execute._import_memory`` to return the stub dependencies without
    requiring the conftest to expose a real ``get_existing_memory_subdirs``.
"""

from __future__ import annotations

import asyncio

import pytest

from usr.plugins.neuro_core import execute
from usr.plugins.neuro_core.helpers.metadata import (
    MemoryType,
    ValidationStatus,
    apply_defaults,
)


# ---------------------------------------------------------------------------
# Stub memory backend (no live FAISS)
# ---------------------------------------------------------------------------


class _StubDoc:
    """Minimal stand-in for a FAISS ``Document``.

    The migration only ever reads ``doc.metadata`` and writes back to it;
    the test cares about the post-migration state of the doc, not the
    embedding payload.
    """

    def __init__(self, doc_id: str, metadata: dict, content: str = ""):
        self.id = doc_id
        self.metadata = dict(metadata)
        self.page_content = content


@pytest.fixture
def stub_memory_registry():
    """Yield a ``(_StubMemory, registry, update_calls, get_by_subdir_calls)`` tuple.

    ``registry`` maps ``memory_subdir`` -> ``{doc_id: _StubDoc}``. Tests
    mutate the registry directly to set up state. ``update_calls`` records
    every ``update_documents`` call grouped by subdir; ``get_by_subdir_calls``
    records the subdir names passed to ``Memory.get_by_subdir`` in order.
    """
    registry: dict = {}
    update_calls: dict = {}
    get_by_subdir_calls: list = []

    class _StubFaiss:
        def __init__(self, subdir: str):
            self._subdir = subdir

        def get_all_docs(self) -> dict:
            # Return a copy so the migration's iteration is decoupled from
            # later mutations of ``registry``.
            return dict(registry.get(self._subdir, {}))

    class _StubMem:
        def __init__(self, subdir: str):
            self._subdir = subdir
            self.db = _StubFaiss(subdir)

        async def update_documents(self, docs: list) -> None:
            # ``execute.py`` calls ``mem.update_documents(modified)`` on the
            # ``Memory`` wrapper, NOT on ``mem.db``. The wrapper delegates
            # to FAISS, so we record the call here at the wrapper level.
            update_calls.setdefault(self._subdir, []).append(list(docs))

    class _StubMemory:
        @staticmethod
        async def get_by_subdir(memory_subdir, log_item=None, **kwargs):
            get_by_subdir_calls.append((memory_subdir, dict(kwargs)))
            return _StubMem(memory_subdir)

    yield _StubMemory, registry, update_calls, get_by_subdir_calls


# ---------------------------------------------------------------------------
# 1. Fields seeded on a fresh memory
# ---------------------------------------------------------------------------


class TestMigrationSeedsFields:
    def test_migration_seeds_neuro_core_fields_on_fresh_memory(
        self, stub_memory_registry
    ):
        """A doc with no Neuro Core fields gets all five defaults."""
        _StubMemory, registry, update_calls, _ = stub_memory_registry
        registry["main"] = {
            "doc1": _StubDoc("doc1", metadata={"area": "main"}),
        }

        scanned, updated = asyncio.run(
            execute._migrate_subdir("main", _StubMemory)
        )

        assert scanned == 1
        assert updated == 1

        # The doc was mutated in place — all five Neuro Core fields are now
        # present, with the exact defaults produced by ``apply_defaults``.
        meta = registry["main"]["doc1"].metadata
        assert meta["memory_type"] == MemoryType.NOTE.value
        assert meta["memory_type"] == "note"
        assert meta["importance"] == pytest.approx(0.5)
        assert meta["confidence"] == pytest.approx(0.7)
        assert meta["stability"] == pytest.approx(0.5)
        assert meta["validation_status"] == ValidationStatus.UNVALIDATED.value
        assert meta["validation_status"] == "unvalidated"

        # Pre-existing non-Neuro fields must NOT be touched.
        assert meta["area"] == "main"

        # update_documents was called once with exactly the modified doc.
        assert "main" in update_calls
        assert len(update_calls["main"]) == 1
        assert [d.id for d in update_calls["main"][0]] == ["doc1"]


# ---------------------------------------------------------------------------
# 2. Idempotency
# ---------------------------------------------------------------------------


class TestMigrationIdempotency:
    def test_migration_is_idempotent_on_second_run(
        self, stub_memory_registry
    ):
        """A second migration run is a no-op and does not re-persist."""
        _StubMemory, registry, update_calls, _ = stub_memory_registry
        registry["main"] = {
            "doc1": _StubDoc("doc1", metadata={"area": "main"}),
        }

        # First run — seeds defaults.
        scanned1, updated1 = asyncio.run(
            execute._migrate_subdir("main", _StubMemory)
        )
        assert scanned1 == 1
        assert updated1 == 1
        assert len(update_calls["main"]) == 1

        # Snapshot the metadata after the first run.
        snapshot = dict(registry["main"]["doc1"].metadata)
        assert "memory_type" in snapshot
        assert "importance" in snapshot

        # Second run — should be a no-op.
        scanned2, updated2 = asyncio.run(
            execute._migrate_subdir("main", _StubMemory)
        )
        assert scanned2 == 1
        assert updated2 == 0
        # The metadata dict is byte-for-byte the same as the snapshot.
        assert registry["main"]["doc1"].metadata == snapshot
        # update_documents is NOT called when nothing changed.
        assert len(update_calls["main"]) == 1  # still only the first call


# ---------------------------------------------------------------------------
# 3. Partial fields preserved
# ---------------------------------------------------------------------------


class TestMigrationPreservesExisting:
    def test_migration_preserves_partial_existing_fields(
        self, stub_memory_registry
    ):
        """Existing Neuro Core fields are kept; only missing ones are seeded."""
        _StubMemory, registry, update_calls, _ = stub_memory_registry
        registry["main"] = {
            "doc1": _StubDoc(
                "doc1",
                metadata={
                    "area": "main",
                    # Custom values the user already set — must NOT be overwritten.
                    "memory_type": "fact",
                    "importance": 0.99,
                },
            ),
        }

        scanned, updated = asyncio.run(
            execute._migrate_subdir("main", _StubMemory)
        )
        assert scanned == 1
        assert updated == 1  # the doc was missing 3 fields, so it counts as updated

        meta = registry["main"]["doc1"].metadata
        # Existing values preserved.
        assert meta["memory_type"] == "fact"
        assert meta["importance"] == pytest.approx(0.99)
        assert meta["area"] == "main"
        # Missing values seeded with defaults.
        assert meta["confidence"] == pytest.approx(0.7)
        assert meta["stability"] == pytest.approx(0.5)
        assert meta["validation_status"] == "unvalidated"

        # Verify this matches what a direct apply_defaults call would
        # produce — the migration should be a thin pass-through.
        expected = {"area": "main", "memory_type": "fact", "importance": 0.99}
        apply_defaults(expected)
        assert meta == expected


# ---------------------------------------------------------------------------
# 4. Empty memory store
# ---------------------------------------------------------------------------


class TestMigrationEmptySubdir:
    def test_migration_on_empty_subdir_returns_zero_counts(
        self, stub_memory_registry
    ):
        """A subdir with zero documents completes with (0, 0) and no I/O."""
        _StubMemory, registry, update_calls, get_by_subdir_calls = (
            stub_memory_registry
        )
        # Subdir exists in the registry but contains no docs.
        registry["empty"] = {}
        # A completely unknown subdir is also valid — get_all_docs()
        # returns {} and the migration is a no-op.

        # --- Case A: empty registry entry ---
        scanned, updated = asyncio.run(
            execute._migrate_subdir("empty", _StubMemory)
        )
        assert scanned == 0
        assert updated == 0
        assert update_calls == {}

        # --- Case B: unknown subdir ---
        scanned_b, updated_b = asyncio.run(
            execute._migrate_subdir("never_seen", _StubMemory)
        )
        assert scanned_b == 0
        assert updated_b == 0
        assert update_calls == {}

        # get_by_subdir was still called (the migration enters the subdir),
        # but update_documents never was.
        assert [call[0] for call in get_by_subdir_calls] == [
            "empty",
            "never_seen",
        ]


# ---------------------------------------------------------------------------
# 5. Multiple subdirs
# ---------------------------------------------------------------------------


class TestRunMigrationMultiSubdir:
    def test_run_migration_processes_all_subdirs_with_per_subdir_counts(
        self, stub_memory_registry, capsys
    ):
        """``_run_migration`` walks every subdir and aggregates counts."""
        _StubMemory, registry, update_calls, get_by_subdir_calls = (
            stub_memory_registry
        )

        # Subdir 'a': 2 docs, 1 already fully migrated, 1 needs migration.
        registry["a"] = {
            "a_migrated": _StubDoc(
                "a_migrated",
                metadata={
                    "memory_type": "note",
                    "importance": 0.5,
                    "confidence": 0.7,
                    "stability": 0.5,
                    "validation_status": "unvalidated",
                },
            ),
            "a_pending": _StubDoc(
                "a_pending",
                metadata={"area": "main"},
            ),
        }
        # Subdir 'b': 1 doc, needs migration.
        registry["b"] = {
            "b_pending": _StubDoc("b_pending", metadata={}),
        }
        # Subdir 'c': empty.
        registry["c"] = {}

        def _get_existing():
            return ["a", "b", "c"]

        processed, total_scanned, total_updated = asyncio.run(
            execute._run_migration(_get_existing, _StubMemory)
        )

        # All 3 subdirs were processed.
        assert processed == 3
        # 2 + 1 + 0 = 3 docs scanned.
        assert total_scanned == 3
        # 1 (a_pending) + 1 (b_pending) + 0 = 2 docs updated.
        assert total_updated == 2

        # Per-subdir get_by_subdir calls.
        assert sorted(call[0] for call in get_by_subdir_calls) == [
            "a",
            "b",
            "c",
        ]

        # Per-subdir update_documents calls: only 'a' and 'b' (c is empty).
        assert set(update_calls.keys()) == {"a", "b"}
        a_ids = [d.id for d in update_calls["a"][0]]
        b_ids = [d.id for d in update_calls["b"][0]]
        assert a_ids == ["a_pending"]
        assert b_ids == ["b_pending"]

        # Per-subdir print lines.
        captured = capsys.readouterr()
        assert "subdir 'a': scanned=2 updated=1" in captured.out
        assert "subdir 'b': scanned=1 updated=1" in captured.out
        assert "subdir 'c': scanned=0 updated=0" in captured.out


# ---------------------------------------------------------------------------
# 6. main() return value and reporting
# ---------------------------------------------------------------------------


class TestMainReturnAndReporting:
    def test_main_returns_zero_and_prints_summary(
        self, stub_memory_registry, capsys, monkeypatch
    ):
        """``main()`` returns 0, prints the migration-complete summary."""
        _StubMemory, registry, _, _ = stub_memory_registry
        registry["main"] = {
            "d1": _StubDoc("d1", metadata={}),
            "d2": _StubDoc("d2", metadata={"memory_type": "fact"}),
        }
        registry["other"] = {
            "d3": _StubDoc(
                "d3",
                metadata={
                    "memory_type": "note",
                    "importance": 0.5,
                    "confidence": 0.7,
                    "stability": 0.5,
                    "validation_status": "unvalidated",
                },
            ),
        }

        def _get_existing():
            return ["main", "other"]

        # WI-P79: enumeration is now a real lightweight pre-pass — pin it
        # so the detection phase sees exactly the stubbed subdirs.
        monkeypatch.setattr(
            execute, "_list_memory_subdirs", lambda: ["main", "other"]
        )

        # Replace the import guard so main() does not need the real
        # ``plugins._memory.helpers.memory`` module. This is exactly the
        # contract ``main()`` enforces: it must call ``_import_memory()``
        # and abort with exit 1 if the deps are missing — and proceed
        # when they are present.
        monkeypatch.setattr(
            execute,
            "_import_memory",
            lambda: (_get_existing, _StubMemory),
        )

        exit_code = execute.main()

        assert exit_code == 0

        captured = capsys.readouterr()
        # "Running migration..." banner.
        assert "execute.main() invoked" in captured.out
        # Per-subdir lines.
        assert "subdir 'main': scanned=2 updated=2" in captured.out
        assert "subdir 'other': scanned=1 updated=0" in captured.out
        # Final summary line with the aggregated counts.
        assert "migration complete:" in captured.out
        assert "subdirs_processed=2" in captured.out
        assert "docs_scanned=3" in captured.out
        assert "docs_updated=2" in captured.out

    def test_main_returns_one_when_memory_plugin_missing(
        self, capsys, monkeypatch
    ):
        """``main()`` returns 1 and prints a fatal error when the
        ``_memory`` plugin cannot be imported.
        """
        # WI-P79: pin the lightweight enumeration; the autouse fixture
        # forces detection to report migration-needed, so the full path
        # (and its import guard) is reached.
        monkeypatch.setattr(
            execute, "_list_memory_subdirs", lambda: ["default"]
        )
        # Force the import guard to fail.
        monkeypatch.setattr(
            execute,
            "_import_memory",
            lambda: (None, None),
        )

        exit_code = execute.main()

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "FATAL" in captured.out
        assert "_memory" in captured.out


# ---------------------------------------------------------------------------
# KI-004 regression (WI-P2-DEFECT-BATCH): the neuro_* tools must resolve
# their SQLite DB path from the plugin config chain — never a hardcoded
# absolute path.
# ---------------------------------------------------------------------------


_PLUGIN_ROOT = "/a0/usr/plugins/neuro_core"


def _load_neuro_tool(name):
    """Import a neuro_* tool module with the plugin root on sys.path."""
    import importlib
    import sys

    if _PLUGIN_ROOT not in sys.path:
        sys.path.insert(0, _PLUGIN_ROOT)
    return importlib.import_module(f"tools.{name}")


def _stub_get_plugin_config(monkeypatch, impl):
    """Install a fake ``helpers.plugins`` module in sys.modules.

    The conftest ``helpers`` stub has an empty ``__path__``, so the real
    ``helpers.plugins`` submodule cannot be imported in this suite; the
    tools' lazy ``from helpers.plugins import get_plugin_config``
    resolves through this stub instead.
    """
    import sys

    import types

    fake = types.ModuleType("helpers.plugins")
    fake.get_plugin_config = impl
    monkeypatch.setitem(sys.modules, "helpers.plugins", fake)


class TestConfigRelativeDbPath:
    def test_configured_absolute_path_is_honored(self, monkeypatch):
        """KI-004: a configured absolute database_path must be honored
        verbatim — proving the path comes from the config chain, not a
        hardcoded literal.
        """
        _stub_get_plugin_config(
            monkeypatch,
            lambda *a, **kw: {"database_path": "/tmp/custom/named.db"},
        )
        # WI-P43 (ADR-NC1-003 §3, D-NC1-106): neuro_retrieve is re-homed to
        # the explainable hybrid pipeline and no longer resolves a
        # SQLite-domain db path; capture/validate keep _resolve_db_path.
        for name in ("neuro_capture", "neuro_validate"):
            mod = _load_neuro_tool(name)
            assert mod._resolve_db_path() == "/tmp/custom/named.db", name

    def test_relative_config_path_resolves_plugin_relative(self, monkeypatch):
        """A relative configured database_path resolves against the
        plugin root (config-relative target state per ADR-NC1-002).
        """
        import os

        _stub_get_plugin_config(
            monkeypatch,
            lambda *a, **kw: {"database_path": "data/named.db"},
        )
        # WI-P43 re-homing: see test_configured_absolute_path_is_honored.
        for name in ("neuro_capture", "neuro_validate"):
            mod = _load_neuro_tool(name)
            assert mod._resolve_db_path() == os.path.join(
                _PLUGIN_ROOT, "data", "named.db"
            ), name

    def test_config_failure_falls_back_to_plugin_relative_default(
        self, monkeypatch
    ):
        """If the config chain is unavailable, the fallback is the
        plugin-relative default — never a hardcoded absolute path.
        """
        import os

        def _boom(*a, **kw):
            raise RuntimeError("simulated config chain failure")

        _stub_get_plugin_config(monkeypatch, _boom)
        # WI-P43 re-homing: see test_configured_absolute_path_is_honored.
        for name in ("neuro_capture", "neuro_validate"):
            mod = _load_neuro_tool(name)
            assert mod._resolve_db_path() == os.path.join(
                _PLUGIN_ROOT, "neuro_core.db"
            ), name

    def test_bundled_default_config_is_relative(self):
        """The bundled default_config.yaml must carry a RELATIVE
        database_path (no hardcoded local absolute paths).
        """
        import os

        import yaml

        with open(os.path.join(_PLUGIN_ROOT, "default_config.yaml")) as fh:
            cfg = yaml.safe_load(fh)
        assert "database_path" in cfg
        assert not os.path.isabs(str(cfg["database_path"])), (
            "bundled default database_path must be config-relative, "
            "not a hardcoded absolute path"
        )


# ---------------------------------------------------------------------------
# 7. WI-P74 (KI-052): preload_knowledge=False + timing visibility
# ---------------------------------------------------------------------------


class TestMigrationSkipsKnowledgePreload:
    """WI-P74 (KI-052): the migration must call ``Memory.get_by_subdir``
    with ``preload_knowledge=False``.

    The migration only scans/updates document metadata; knowledge preload
    (full agent init + embedding-model cold load + knowledge re-embedding)
    is irrelevant to it and, inside the framework's fixed 120s execute.py
    subprocess window, is wasted work that can push a fresh-container
    first run into a timeout.
    """

    def test_migrate_subdir_passes_preload_knowledge_false(
        self, stub_memory_registry
    ):
        """``_migrate_subdir`` invokes ``get_by_subdir`` with
        ``preload_knowledge=False`` (recorded via the stub's kwargs).
        """
        _StubMemory, registry, _, get_by_subdir_calls = stub_memory_registry
        registry["main"] = {
            "doc1": _StubDoc("doc1", metadata={"area": "main"}),
        }

        scanned, updated = asyncio.run(
            execute._migrate_subdir("main", _StubMemory)
        )

        assert scanned == 1
        assert updated == 1
        # The stub records (subdir, kwargs) tuples.
        assert len(get_by_subdir_calls) == 1
        subdir, kwargs = get_by_subdir_calls[0]
        assert subdir == "main"
        assert kwargs.get("preload_knowledge") is False

    def test_migration_results_unchanged_with_preload_disabled(
        self, stub_memory_registry
    ):
        """Migration results are identical with ``preload_knowledge=False``:
        defaults are seeded, existing values preserved, non-destructive.
        """
        _StubMemory, registry, update_calls, _ = stub_memory_registry
        registry["main"] = {
            "fresh": _StubDoc("fresh", metadata={"area": "main"}),
            "partial": _StubDoc(
                "partial",
                metadata={"memory_type": "fact", "importance": 0.99},
            ),
            "migrated": _StubDoc(
                "migrated",
                metadata={
                    "memory_type": "note",
                    "importance": 0.5,
                    "confidence": 0.7,
                    "stability": 0.5,
                    "validation_status": "unvalidated",
                },
            ),
        }

        scanned, updated = asyncio.run(
            execute._migrate_subdir("main", _StubMemory)
        )

        # All three docs scanned; the two incomplete ones updated; the
        # fully-migrated one untouched.
        assert scanned == 3
        assert updated == 2

        fresh = registry["main"]["fresh"].metadata
        assert fresh["memory_type"] == "note"
        assert fresh["importance"] == pytest.approx(0.5)
        assert fresh["confidence"] == pytest.approx(0.7)
        assert fresh["stability"] == pytest.approx(0.5)
        assert fresh["validation_status"] == "unvalidated"
        assert fresh["area"] == "main"  # pre-existing field preserved

        partial = registry["main"]["partial"].metadata
        assert partial["memory_type"] == "fact"  # existing value preserved
        assert partial["importance"] == pytest.approx(0.99)
        assert partial["confidence"] == pytest.approx(0.7)  # seeded

        migrated = registry["main"]["migrated"].metadata
        assert migrated["memory_type"] == "note"  # untouched

        # Exactly one persistence call, containing only the modified docs.
        assert len(update_calls["main"]) == 1
        assert [d.id for d in update_calls["main"][0]] == [
            "fresh",
            "partial",
        ]

    def test_run_migration_prints_per_subdir_timing(
        self, stub_memory_registry, capsys
    ):
        """Per-subdir result lines carry an ``elapsed=`` timing field so
        partial progress is visible within any future timeout window.
        """
        _StubMemory, registry, _, _ = stub_memory_registry
        registry["main"] = {
            "doc1": _StubDoc("doc1", metadata={}),
        }

        def _get_existing():
            return ["main"]

        processed, total_scanned, total_updated = asyncio.run(
            execute._run_migration(_get_existing, _StubMemory)
        )

        assert processed == 1
        assert total_scanned == 1
        assert total_updated == 1

        captured = capsys.readouterr()
        assert "subdir 'main': scanned=1 updated=1 elapsed=" in captured.out

    def test_main_prints_total_elapsed_in_summary(
        self, stub_memory_registry, capsys, monkeypatch
    ):
        """The final summary line carries a ``total_elapsed=`` field."""
        _StubMemory, registry, _, _ = stub_memory_registry
        registry["main"] = {
            "d1": _StubDoc("d1", metadata={}),
        }

        def _get_existing():
            return ["main"]

        # WI-P79: pin the lightweight enumeration; the autouse fixture
        # forces detection to report migration-needed, so the full path runs.
        monkeypatch.setattr(
            execute, "_list_memory_subdirs", lambda: ["main"]
        )

        monkeypatch.setattr(
            execute,
            "_import_memory",
            lambda: (_get_existing, _StubMemory),
        )

        exit_code = execute.main()

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "migration complete:" in captured.out
        assert "subdirs_processed=1" in captured.out
        assert "docs_scanned=1" in captured.out
        assert "docs_updated=1" in captured.out
        assert "total_elapsed=" in captured.out


# ---------------------------------------------------------------------------
# Model-less early-exit detection (KI-054, WI-P75-KI054-MODELLESS-EXIT)
# ---------------------------------------------------------------------------

# Capture the real detection function BEFORE any fixture patches it, so
# model-less tests can restore genuine detection behavior.
_REAL_DETECTION = execute._modelless_subdir_needs_migration


class InMemoryDocstore:
    """Module-level picklable docstore stand-in — REAL persisted shape.

    The class name matches the framework docstore allowlist
    (``LocalFileStore`` / ``InMemoryDocstore``) so pickled fixtures exercise
    the real structure checks in ``_modelless_subdir_needs_migration``.

    KI-057-AA fidelity fix (WI-P80, ARC condition C4): the document mapping
    lives in ``_dict`` — the langchain_community InMemoryDocstore attribute
    verified in real persisted stores on BOTH framework v2.8 (commit
    5ff106a2) and current (direct probe of .a0proj/memory/index.pkl:
    attrs ['_dict'], no '.store'). Earlier '.store'-shaped mocks diverged
    from real persisted data and masked the v2.8 fail-closed defect.
    """

    def __init__(self, store: dict | None = None):
        self._dict = store if store is not None else {}


class LocalFileStore:
    """Picklable stand-in for the '.store'-shaped structural family
    (LocalFileStore-style mapping attribute) — also recognized by the
    broadened detector (WI-P80, ARC condition C1). The class name matches
    the detector allowlist so pickled fixtures exercise the real structure
    checks.
    """

    def __init__(self, store: dict | None = None):
        self.store = store if store is not None else {}


InMemoryDocstoreNoStore = type("InMemoryDocstore", (), {"__module__": __name__})
"""Picklable allowlisted class whose instances expose NEITHER recognized
dict attribute ('.store' nor '_dict') — the fail-closed negative fixture
(WI-P80, ARC condition C4)."""


class _PDoc:
    """Module-level picklable Document stand-in (``.metadata`` dict)."""

    def __init__(self, metadata: dict | None = None, content: str = "x"):
        self.metadata = metadata or {}
        self.page_content = content


def _migrated_meta() -> dict:
    return {
        "memory_type": "note",
        "importance": 0.5,
        "confidence": 0.7,
        "stability": 0.5,
        "validation_status": "unvalidated",
    }


def _write_store(db_dir, docs: dict[str, _PDoc], index_bytes: bytes = b"FAISS"):
    """Create a disposable fixture store: index.pkl + index.faiss."""
    os.makedirs(db_dir, exist_ok=True)
    with open(os.path.join(db_dir, "index.pkl"), "wb") as f:
        pickle.dump((InMemoryDocstore(dict(docs)), {"k1": "d1"}), f)
    with open(os.path.join(db_dir, "index.faiss"), "wb") as f:
        f.write(index_bytes)


def _store_fingerprint(db_dir) -> dict:
    """Byte-identity fingerprint of every file in a store directory (C2)."""
    snap = {}
    for name in sorted(os.listdir(db_dir)):
        p = os.path.join(db_dir, name)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                data = f.read()
            snap[name] = (len(data), hashlib.sha256(data).hexdigest(), os.stat(p).st_mtime_ns)
    return snap


@pytest.fixture(autouse=True)
def _default_detection_to_full_path(monkeypatch):
    """Keep every test on the full migration path by default.

    Pre-existing tests stub the Memory layer and must remain isolated from
    the real filesystem (a live store exists at /a0/usr/memory). Model-less
    test classes below re-enable genuine detection explicitly.
    """
    monkeypatch.setattr(
        execute,
        "_modelless_subdir_needs_migration",
        lambda subdir, abs_db_dir_fn=None: (True, 0),
    )


import hashlib  # noqa: E402  (used by the model-less test section)
import os  # noqa: E402
import pickle  # noqa: E402


class TestModellessDetectionUnit:
    """Direct unit tests for ``_modelless_subdir_needs_migration``.

    All stores are disposable ``tmp_path`` fixtures; paths are injected via
    ``abs_db_dir_fn`` — the real framework ``abs_db_dir`` is never invoked
    and no live store data is touched.
    """

    def test_absent_store_dir_skips_model_less(self, tmp_path):
        status, scanned = _REAL_DETECTION(
            "nope", abs_db_dir_fn=lambda s: str(tmp_path / "absent")
        )
        assert status is False
        assert scanned == 0

    def test_store_without_index_pkl_skips_model_less(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        status, scanned = _REAL_DETECTION("s", abs_db_dir_fn=lambda s: str(d))
        assert status is False
        assert scanned == 0

    def test_fully_migrated_store_reports_no_migration_needed(self, tmp_path):
        d = tmp_path / "migrated"
        _write_store(d, {"d1": _PDoc(_migrated_meta())})
        status, scanned = _REAL_DETECTION("s", abs_db_dir_fn=lambda s: str(d))
        assert status is False
        assert scanned == 1

    def test_doc_missing_field_reports_migration_needed(self, tmp_path):
        d = tmp_path / "needs"
        _write_store(d, {"d1": _PDoc(_migrated_meta()), "d2": _PDoc({})})
        status, scanned = _REAL_DETECTION("s", abs_db_dir_fn=lambda s: str(d))
        assert status is True
        assert scanned == 2

    def test_corrupt_pkl_falls_back(self, tmp_path, capsys):
        d = tmp_path / "corrupt"
        d.mkdir()
        with open(d / "index.pkl", "wb") as f:
            f.write(b"this is not a pickle")
        status, _ = _REAL_DETECTION("s", abs_db_dir_fn=lambda s: str(d))
        assert status is None
        assert "falling back" in capsys.readouterr().out

    def test_structure_drift_non_tuple_payload_falls_back(self, tmp_path, capsys):
        d = tmp_path / "drift1"
        d.mkdir()
        with open(d / "index.pkl", "wb") as f:
            pickle.dump(["not", "a", "tuple"], f)
        assert _REAL_DETECTION("s", abs_db_dir_fn=lambda s: str(d))[0] is None
        assert "unexpected index.pkl structure" in capsys.readouterr().out

    def test_structure_drift_wrong_arity_falls_back(self, tmp_path, capsys):
        d = tmp_path / "drift2"
        d.mkdir()
        with open(d / "index.pkl", "wb") as f:
            pickle.dump((InMemoryDocstore(), {}, "extra"), f)
        assert _REAL_DETECTION("s", abs_db_dir_fn=lambda s: str(d))[0] is None
        assert "unexpected index.pkl structure" in capsys.readouterr().out

    def test_structure_drift_unknown_docstore_type_falls_back(self, tmp_path, capsys):
        d = tmp_path / "drift3"
        d.mkdir()
        with open(d / "index.pkl", "wb") as f:
            pickle.dump((object(), {}), f)
        assert _REAL_DETECTION("s", abs_db_dir_fn=lambda s: str(d))[0] is None
        assert "unexpected docstore type" in capsys.readouterr().out

    def test_structure_drift_missing_store_attr_falls_back(self, tmp_path, capsys):
        d = tmp_path / "drift4"
        d.mkdir()
        # KI-057-AA fidelity fix (WI-P80, ARC condition C4): instance of the
        # ALLOWLISTED class exposing NEITHER recognized dict attribute
        # ('.store' nor '_dict') — the fail-closed negative must hold for
        # allowlisted-but-unrecognized shapes too. The instance attribute
        # is deleted before pickling, so the unpickled object carries the
        # allowlisted class name but no recognized mapping.
        broken_docstore = InMemoryDocstore()
        del broken_docstore._dict
        with open(d / "index.pkl", "wb") as f:
            pickle.dump((broken_docstore, {}), f)
        assert _REAL_DETECTION("s", abs_db_dir_fn=lambda s: str(d))[0] is None
        assert "without a recognized dict mapping" in capsys.readouterr().out

    def test_v28_shaped_dict_docstore_fast_path(self, tmp_path):
        """ARC condition C4: the v2.8-shaped persisted docstore
        (InMemoryDocstore exposing '_dict') is recognized via the fast
        path — migrated docs report no migration needed, never None.
        """
        d = tmp_path / "v28shaped"
        _write_store(d, {"d1": _PDoc(_migrated_meta())})
        status, scanned = _REAL_DETECTION("s", abs_db_dir_fn=lambda s: str(d))
        assert status is False
        assert scanned == 1

    def test_v28_shaped_dict_docstore_migration_needed(self, tmp_path):
        """ARC condition C4: the v2.8-shaped '_dict' docstore with an
        unmigrated doc reports migration needed via the fast path.
        """
        d = tmp_path / "v28shaped2"
        _write_store(d, {"d1": _PDoc({})})
        status, scanned = _REAL_DETECTION("s", abs_db_dir_fn=lambda s: str(d))
        assert status is True
        assert scanned == 1

    def test_current_shaped_dict_docstore_fast_path(self, tmp_path):
        """ARC condition C4: the current-version persisted shape (the same
        InMemoryDocstore '_dict' family, verified against the live
        .a0proj/memory/index.pkl) is recognized via the fast path.
        """
        d = tmp_path / "currentshaped"
        _write_store(d, {"d1": _PDoc(_migrated_meta()), "d2": _PDoc(_migrated_meta())})
        status, scanned = _REAL_DETECTION("s", abs_db_dir_fn=lambda s: str(d))
        assert status is False
        assert scanned == 2

    def test_store_shaped_family_still_recognized(self, tmp_path):
        """ARC condition C1: the '.store'-shaped structural family
        (LocalFileStore-style) remains recognized by the broadened
        detector — no regression of the original recognition path.
        """
        d = tmp_path / "storeshaped"
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(str(d), "index.pkl"), "wb") as f:
            pickle.dump((LocalFileStore({"d1": _PDoc(_migrated_meta())}), {"k1": "d1"}), f)
        with open(os.path.join(str(d), "index.faiss"), "wb") as f:
            f.write(b"FAISS")
        status, scanned = _REAL_DETECTION("s", abs_db_dir_fn=lambda s: str(d))
        assert status is False
        assert scanned == 1

    def test_detection_is_read_only_store_byte_identity(self, tmp_path):
        """ARC condition C2: detection writes NOTHING to the store dir."""
        d = tmp_path / "readonly"
        _write_store(d, {"d1": _PDoc(_migrated_meta())})
        before = _store_fingerprint(d)
        _REAL_DETECTION("s", abs_db_dir_fn=lambda s: str(d))
        assert _store_fingerprint(d) == before


class TestModellessSkipPath:
    """End-to-end skip behavior through ``main()`` with genuine detection."""

    def test_migrated_store_skips_model_less_with_byte_identity(
        self, tmp_path, capsys, monkeypatch
    ):
        """Design scenario 3 + C2 + C6: an already-migrated store is skipped
        model-less; the store is byte-identical afterwards; no Memory
        machinery is constructed (the stub raises if ``get_by_subdir`` —
        the only route to ``Memory.initialize`` and the embedding model —
        is ever reached).
        """
        d = tmp_path / "migrated"
        _write_store(d, {"d1": _PDoc(_migrated_meta())})
        before = _store_fingerprint(d)

        # WI-P79: pin the lightweight enumeration so the Phase-1 pre-pass
        # sees exactly this fixture subdir.
        monkeypatch.setattr(
            execute, "_list_memory_subdirs", lambda: ["migrated"]
        )

        def _confined_detection(subdir, abs_db_dir_fn=None):
            return _REAL_DETECTION(subdir, abs_db_dir_fn=lambda s: str(d))

        monkeypatch.setattr(
            execute, "_modelless_subdir_needs_migration", _confined_detection
        )

        class _MustNotConstruct:
            @staticmethod
            async def get_by_subdir(*a, **k):
                raise AssertionError(
                    "model machinery must not be constructed on the skip path"
                )

        monkeypatch.setattr(
            execute, "_import_memory", lambda: (lambda: ["s"], _MustNotConstruct)
        )

        exit_code = execute.main()

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "skipped (nothing to migrate) [model-less]" in out
        assert _store_fingerprint(d) == before  # C2 byte-identity

    def test_fresh_container_empty_store_never_loads_model(
        self, tmp_path, capsys, monkeypatch
    ):
        """Design scenario 1 + C6 (WI-P79 strengthened): absent stores skip
        model-less in the Phase-1 pre-pass and the process exits BEFORE the
        heavy ``plugins._memory.helpers.memory`` module is ever imported —
        asserted by a raising import stub (binding import-blocking
        assertion).
        """
        # WI-P79: pin the lightweight enumeration to a fresh-container-like
        # subdir set; detection is genuine and confined to absent tmp dirs.
        monkeypatch.setattr(
            execute, "_list_memory_subdirs", lambda: ["default", "main"]
        )

        def _confined_detection(subdir, abs_db_dir_fn=None):
            # Absent dirs under tmp_path: genuine detection, confined fixture.
            return _REAL_DETECTION(
                subdir, abs_db_dir_fn=lambda s: str(tmp_path / subdir.replace("/", "_") / "store")
            )

        monkeypatch.setattr(
            execute, "_modelless_subdir_needs_migration", _confined_detection
        )

        def _forbidden_import():
            raise AssertionError(
                "plugins._memory.helpers.memory must never be imported on "
                "the fresh-empty-store early-exit path (WI-P79 C5)"
            )

        monkeypatch.setattr(execute, "_import_memory", _forbidden_import)

        exit_code = execute.main()

        assert exit_code == 0
        out = capsys.readouterr().out
        assert out.count("skipped (nothing to migrate) [model-less]") == 2
        assert "migration complete:" in out


class TestModellessFallback:
    """Fail-closed fallback: any model-less failure degrades to the exact
    current full path, never corrupts the store (design scenario 5, C5)."""

    def test_corrupt_pkl_warns_and_uses_full_path(
        self, tmp_path, capsys, monkeypatch
    ):
        d = tmp_path / "corrupt"
        d.mkdir()
        with open(d / "index.pkl", "wb") as f:
            f.write(b"garbage not a pickle")
        before = _store_fingerprint(d)

        def _confined_detection(subdir, abs_db_dir_fn=None):
            return _REAL_DETECTION(subdir, abs_db_dir_fn=lambda s: str(d))

        monkeypatch.setattr(
            execute, "_modelless_subdir_needs_migration", _confined_detection
        )

        _StubMemory, registry, update_calls, get_calls = _make_registry()
        registry["s"] = {"d1": _StubDoc("d1", metadata={})}
        monkeypatch.setattr(
            execute, "_import_memory", lambda: (lambda: ["s"], _StubMemory)
        )

        exit_code = execute.main()

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "falling back to the full migration path" in out
        assert "subdir 's': scanned=1 updated=1" in out  # full path ran
        assert _store_fingerprint(d) == before  # store uncorrupted

    def test_mixed_run_skips_one_and_migrates_other(
        self, tmp_path, capsys, monkeypatch
    ):
        """Design scenario 6: one skippable subdir + one migration-needing
        subdir in a single invocation."""
        skip_dir = tmp_path / "skipme"
        _write_store(skip_dir, {"d1": _PDoc(_migrated_meta())})

        def _fake_abs_db_dir(subdir: str) -> str:
            return str(skip_dir)  # only consulted for the skip subdir

        def _detection(subdir, abs_db_dir_fn=None):
            if subdir == "skipme":
                return _REAL_DETECTION(subdir, abs_db_dir_fn=_fake_abs_db_dir)
            return (True, 0)  # other subdir: full path

        monkeypatch.setattr(
            execute, "_modelless_subdir_needs_migration", _detection
        )

        _StubMemory, registry, update_calls, get_calls = _make_registry()
        registry["work"] = {"d1": _StubDoc("d1", metadata={})}
        monkeypatch.setattr(
            execute,
            "_import_memory",
            lambda: (lambda: ["skipme", "work"], _StubMemory),
        )

        exit_code = execute.main()

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "subdir 'skipme': skipped (nothing to migrate) [model-less]" in out
        assert "subdir 'work': scanned=1 updated=1" in out
        assert "subdirs_processed=1" in out  # only 'work' was processed


def _make_registry():
    """Local registry helper mirroring the ``stub_memory_registry`` fixture
    (usable inside classes that need custom subdir lists)."""
    registry: dict = {}
    update_calls: dict = {}
    get_by_subdir_calls: list = []

    class _StubFaiss:
        def __init__(self, subdir):
            self._subdir = subdir

        def get_all_docs(self):
            return dict(registry.get(self._subdir, {}))

    class _StubMem:
        def __init__(self, subdir):
            self._subdir = subdir
            self.db = _StubFaiss(subdir)

        async def update_documents(self, docs):
            update_calls.setdefault(self._subdir, []).append(list(docs))

    class _StubMemory:
        @staticmethod
        async def get_by_subdir(memory_subdir, log_item=None, **kwargs):
            get_by_subdir_calls.append((memory_subdir, dict(kwargs)))
            return _StubMem(memory_subdir)

    return _StubMemory, registry, update_calls, get_by_subdir_calls


# ---------------------------------------------------------------------------
# WI-P79-KI054-DEFERRED-IMPORT: lightweight enumeration/resolution, deferred
# heavy import, flushed output (ARC conditions C2, C3, C4).
# ---------------------------------------------------------------------------


class TestLightweightPrePassConditions:
    """Binding tests for the WI-P79 ARC conditions.

    C2 (drift detection): the lightweight resolver and enumerator must stay
    equivalent to the framework's ``abs_db_dir`` standard branch and
    ``get_existing_memory_subdirs`` standard enumeration. The comparison
    runs in a clean subprocess (the conftest ``helpers`` stub makes the
    real framework module unimportable in-process).
    """

    def test_drift_detection_against_framework_reference(self):
        """C2 (BINDING): _abs_db_dir_light matches abs_db_dir's standard
        branch; _list_memory_subdirs is a conservative superset of the
        framework's enumeration (identical standard subdirs; projects/
        entries listed by name, never silently dropped)."""
        import subprocess
        import sys as _sys

        probe_resolver = """
import sys
sys.path.insert(0, "/a0")
from helpers.files import get_abs_path
import usr.plugins.neuro_core.execute as ex

# Resolver: standard branch must match the framework primitive exactly.
for s in ["default", "main", "custom-x"]:
    assert ex._abs_db_dir_light(s) == get_abs_path("usr/memory", s), s
# projects/-scoped must fail closed.
assert ex._abs_db_dir_light("projects/anything") is None
print("DRIFT-OK-RESOLVER")
"""
        result = subprocess.run(
            [_sys.executable, "-c", probe_resolver],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": "/a0"},
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        assert "DRIFT-OK-RESOLVER" in result.stdout

        # Enumeration comparison needs the REAL framework module (heavy:
        # langchain et al.), which only exists in the framework runtime.
        probe_enum = """
import sys
sys.path.insert(0, "/a0")
from plugins._memory.helpers.memory import get_existing_memory_subdirs
import usr.plugins.neuro_core.execute as ex

fw = get_existing_memory_subdirs()
light = ex._list_memory_subdirs()
fw_std = sorted(s for s in fw if not s.startswith("projects/"))
light_std = sorted(s for s in light if not s.startswith("projects/"))
assert fw_std == light_std, (fw_std, light_std)
for s in fw:
    if s.startswith("projects/"):
        assert s in light, s
assert "default" in light
print("DRIFT-OK-ENUM")
"""
        result = subprocess.run(
            ["/opt/venv-a0/bin/python", "-c", probe_enum],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": "/a0"},
            timeout=300,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        assert "DRIFT-OK-ENUM" in result.stdout

    def test_detection_call_site_signature_against_real_helpers(self):
        """WI-P79 rev 2 (VAL defect D1/D2, BINDING): the detection call site
        must honor _abs_db_dir_light's actual return contract (a PATH string,
        not a callable) against the REAL helpers.files — with NO conftest
        stub. The pytest helpers stub returns a callable Mock for
        get_abs_path, which masked a 'str' object is not callable TypeError
        at the call site and made the model-less skip unreachable in real
        execution while the full suite stayed green. This probe runs the
        un-injected detection path in a clean subprocess under the real
        framework and pins: absent store → (False, 0) skip; projects/-scoped
        subdir → (None, 0) fail-closed fallback; never a TypeError."""
        import subprocess
        import sys as _sys

        probe_call_site = """
import sys
sys.path.insert(0, "/a0")
import usr.plugins.neuro_core.execute as ex

# Un-injected call site: exercises the real _abs_db_dir_light -> db_dir
# contract (rev 2 fix). Absent store must skip model-less, not raise.
r = ex._modelless_subdir_needs_migration('nonexistent-subdir-probe')
assert r == (False, 0), r
# projects/-scoped must fail closed to the full path, not raise.
r2 = ex._modelless_subdir_needs_migration('projects/anything')
assert r2 == (None, 0), r2
print("CALLSITE-OK")
"""
        result = subprocess.run(
            [_sys.executable, "-c", probe_call_site],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": "/a0"},
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        assert "CALLSITE-OK" in result.stdout

    def test_resolver_projects_scoped_fails_closed(self):
        """C2: projects/-scoped subdirs never resolve through the
        lightweight resolver — they fail closed to the full path."""
        assert execute._abs_db_dir_light("projects/neuro_core_2") is None
        assert execute._abs_db_dir_light("projects/") is None

    def test_enumeration_failure_falls_back_to_full_path(
        self, stub_memory_registry, capsys, monkeypatch
    ):
        """C2 fail-closed: when lightweight enumeration fails (None),
        main() warns and runs the exact full migration path."""
        _StubMemory, registry, _, _ = stub_memory_registry
        registry["main"] = {"d1": _StubDoc("d1", metadata={})}
        monkeypatch.setattr(execute, "_list_memory_subdirs", lambda: None)
        monkeypatch.setattr(
            execute,
            "_import_memory",
            lambda: (lambda: ["main"], _StubMemory),
        )

        exit_code = execute.main()

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "falling back to the full migration path" in out
        assert "subdir 'main': scanned=1 updated=1" in out

    def test_deferred_import_called_exactly_once_before_writes(
        self, stub_memory_registry, monkeypatch
    ):
        """C3: the heavy module is imported exactly once, only after
        detection reports a migration-needing subdir, and before any
        persistence write."""
        _StubMemory, registry, update_calls, _ = stub_memory_registry
        registry["main"] = {"d1": _StubDoc("d1", metadata={})}
        monkeypatch.setattr(execute, "_list_memory_subdirs", lambda: ["main"])

        import_calls = []

        def _counting_import():
            import_calls.append(1)
            return (lambda: ["main"], _StubMemory)

        monkeypatch.setattr(execute, "_import_memory", _counting_import)

        exit_code = execute.main()

        assert exit_code == 0
        assert len(import_calls) == 1  # exactly once
        # The write happened (necessarily after the single import).
        assert len(update_calls["main"]) == 1

    def test_every_print_call_is_flushed(self):
        """C4 (BINDING): every ``print()`` call in execute.py passes
        ``flush=True`` so progress output survives the framework runner's
        PIPE capture up to any timeout kill."""
        import ast
        import inspect

        source = inspect.getsource(execute)
        tree = ast.parse(source)
        unflushed = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
            and not any(kw.arg == "flush" for kw in node.keywords)
        ]
        assert unflushed == [], (
            "print() calls without flush=True at lines "
            f"{unflushed} — output would be lost under PIPE capture"
        )
