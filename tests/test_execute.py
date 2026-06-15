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
        async def get_by_subdir(memory_subdir, log_item=None):
            get_by_subdir_calls.append(memory_subdir)
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
        assert get_by_subdir_calls == ["empty", "never_seen"]


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
        assert sorted(get_by_subdir_calls) == ["a", "b", "c"]

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
