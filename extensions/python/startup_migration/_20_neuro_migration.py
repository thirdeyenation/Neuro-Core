"""NC2 automatic startup migration extension (WI-004, D-NCM-004).

Executed by the framework's startup_migration pass, AFTER the NC1 sibling
``_10_neuro_decoration.py`` (alphabetical sibling ordering under
``extensions/python/startup_migration/``):

    initialize.py:initialize_migration()
      -> helpers/migration.py:startup_migration()
      -> helpers.extension.call_extensions_sync("startup_migration", None)

Contract:
- Model-less, fail-closed detection via the shared engine
  (``usr.plugins.neuro_core.helpers.migration_engine``): ``memory.sqlite3``
  is inspected FIRST in read-only URI mode; ``index.pkl`` only when the
  SQLite store is absent (dual-marker precedence). Detection is strictly
  READ-ONLY (D-NC1-153).
- Project-scoped subdirs (``projects/<name>`` -> ``.a0proj/memory/``) are
  resolved model-lessly via ``helpers.files`` (CAP-006): a marker-less
  project dir returns ``(False, 0)`` -- no heavy fallback.
- COLD-BOOT MODEL-LOAD STORM PREVENTION: the heavy ``Memory`` machinery
  (embedding model) is initialized ONLY when at least one subdir reports
  ``True`` (needs backfill) or ``None`` (fail-closed fallback). A clean
  ``False``-everywhere sweep exits before any heavy import.
- ALL backfill writes flow exclusively through the decorated ``Memory``
  facade (``Memory.update_documents``) -- never direct SQLite writes.
- Sync contract: ``execute()`` returns ``None`` (never an awaitable -- the
  framework's ``call_extensions_sync`` raises ``ValueError`` on awaitables).
  The async backfill is driven by a sync-safe runner that works whether or
  not an event loop is already running in this thread.
- Non-fatal: any failure is logged and NEVER crashes framework startup
  (including when no embedding model is configured yet).
"""

from __future__ import annotations

import asyncio
import logging
import threading

from helpers.extension import Extension

log = logging.getLogger("neuro_core.startup_migration")


def _import_engine():
    """Dual-path engine import (plugin mode + standalone src checkout)."""
    try:
        from usr.plugins.neuro_core.helpers import migration_engine
        return migration_engine
    except ImportError:
        from helpers import migration_engine
        return migration_engine


def _import_memory():
    """Resolve (get_existing_memory_subdirs, Memory); (None, None) on failure."""
    try:
        from plugins._memory.helpers.memory import (
            Memory,
            get_existing_memory_subdirs,
        )
        return get_existing_memory_subdirs, Memory
    except ImportError:
        return None, None


def _run_async(coro):
    """Sync-safe runner: drive ``coro`` to completion on this thread.

    When no event loop is running, ``asyncio.run`` is used directly. When a
    loop IS already running in this thread (in-process startup contexts),
    the coroutine is driven on a dedicated worker thread with its own loop
    (``asyncio.run`` would raise ``RuntimeError`` there).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list = []
    error: list = []

    def _worker():
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001 - propagated to caller
            error.append(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else None


class NeuroStartupMigration(Extension):
    """Automatic model-less Neuro Core metadata backfill at startup."""

    def execute(self, **kwargs):
        try:
            engine = _import_engine()
        except Exception as e:  # non-fatal: never break framework startup
            log.warning(
                "[neuro_core] startup migration skipped (engine import failed): %s", e
            )
            return None
        try:
            self._run_detection_and_backfill(engine)
        except Exception as e:  # non-fatal: never break framework startup
            log.warning("[neuro_core] startup migration non-fatal: %s", e)
        return None

    def _run_detection_and_backfill(self, engine) -> None:
        # --- Phase 1: model-less detection pre-pass (no heavy imports) ----
        subdirs = engine.list_memory_subdirs()
        if subdirs is None:
            # Fail-closed: enumeration failed -- treat as undetermined so the
            # heavy authoritative path below decides.
            log.warning(
                "[neuro_core] startup migration: lightweight subdir "
                "enumeration failed -- falling back to the full migration path."
            )
            pending = None
        else:
            pending = []
            for subdir in subdirs:
                try:
                    status, _scanned = engine.detect_subdir(
                        subdir, projects_fail_closed=False
                    )
                except Exception as exc:
                    log.warning(
                        "[neuro_core] startup migration: model-less detection "
                        "failed for subdir '%s': %s -- falling back to the "
                        "full migration path.",
                        subdir,
                        exc,
                    )
                    status = None
                if status is False:
                    log.info(
                        "[neuro_core] startup migration: subdir '%s' skipped "
                        "(nothing to migrate) [model-less]",
                        subdir,
                    )
                    continue
                # True (needs backfill) and None (cannot determine) both
                # require the heavy path (fail-closed semantics).
                pending.append(subdir)
            if not pending:
                log.info(
                    "[neuro_core] startup migration: nothing to migrate "
                    "(model-less sweep clean) -- heavy Memory machinery not "
                    "initialized."
                )
                return

        # --- Phase 2: deferred heavy import (exactly once, before writes) --
        get_existing_memory_subdirs, Memory = _import_memory()
        if get_existing_memory_subdirs is None or Memory is None:
            # Non-fatal: the _memory plugin may not be enabled yet.
            log.warning(
                "[neuro_core] startup migration skipped: "
                "plugins._memory.helpers.memory is not importable (the "
                "_memory plugin is a required dependency for neuro_core)."
            )
            return

        if pending is None:
            # Enumeration failed model-less: use the heavy authoritative
            # enumeration for the sweep.
            pending = list(get_existing_memory_subdirs())

        processed, scanned, updated = _run_async(
            engine.backfill_all(pending, Memory)
        )
        log.info(
            "[neuro_core] startup migration complete: subdirs_processed=%d "
            "docs_scanned=%d docs_updated=%d",
            processed,
            scanned,
            updated,
        )
