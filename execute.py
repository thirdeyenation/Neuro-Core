"""Neuro Core one-shot migration entry point.

Invoked by Agent Zero's plugin manager when the plugin is upgraded or
first enabled. The job is to migrate pre-existing memory documents into
the Neuro Core schema:

- Seed the typed ``memory_type`` field (default ``"note"``).
- Seed the three score fields (``importance`` / ``confidence`` /
  ``stability``) with safe fallbacks.
- Seed the ``validation_status`` field (default ``"unvalidated"``).

The migration is **non-destructive**: existing values are never
overwritten. Only documents that are missing one or more Neuro Core
fields are touched.

Usage:
    python -m usr.plugins.neuro_core.execute
"""

from __future__ import annotations

import asyncio
import os
import sys

# --- Path bootstrap (must run before any plugin-local imports) -----------
# ``execute.py`` lives at ``/a0/usr/plugins/neuro_core/execute.py``.
# The Agent Zero root (``/a0``) is three parents up. It must be on
# ``sys.path`` BEFORE any ``usr.plugins.neuro_core.*`` or
# ``plugins._memory.*`` import is attempted — otherwise Python raises
# ``ModuleNotFoundError: No module named 'usr'`` (or ``'plugins'``)
# when the script is invoked from a context where the A0 root is not
# already on the path (e.g., when Agent Zero runs the script directly
# from the plugin directory).
_A0_ROOT: str = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _A0_ROOT not in sys.path:
    sys.path.insert(0, _A0_ROOT)
# --------------------------------------------------------------------------


# All plugin-local and framework imports come AFTER the path bootstrap so
# they can resolve ``usr.plugins.neuro_core.*`` and ``plugins._memory.*``
# regardless of how this script was invoked.
from usr.plugins.neuro_core.helpers.metadata import apply_defaults  # noqa: E402


def _import_memory():
    """Import :mod:`plugins._memory.helpers.memory`.

    The module-level path bootstrap above already added the A0 root to
    ``sys.path``, so a standard import is sufficient. If the ``_memory``
    plugin is not enabled in this Agent Zero instance, the import will
    raise ``ImportError`` and we return ``(None, None)`` so ``main()``
    can exit cleanly with an actionable error message.

    Returns a ``(get_existing_memory_subdirs, Memory)`` tuple on success,
    or ``(None, None)`` if the import cannot be resolved. Never raises.
    """
    try:
        from plugins._memory.helpers.memory import (
            Memory,
            get_existing_memory_subdirs,
        )
        return get_existing_memory_subdirs, Memory
    except ImportError:
        return None, None


# The set of Neuro Core fields that mark a document as "migrated".
# A document is migrated only if **all** of these are present on its
# metadata; otherwise we re-run ``apply_defaults()`` which fills in
# any missing ones (preserving existing values).
_NEURO_CORE_FIELDS: tuple[str, ...] = (
    "memory_type",
    "importance",
    "confidence",
    "stability",
    "validation_status",
)


def _needs_migration(metadata: dict) -> bool:
    """Return ``True`` if any Neuro Core field is absent from ``metadata``."""
    if not isinstance(metadata, dict):
        return True
    return any(field_name not in metadata for field_name in _NEURO_CORE_FIELDS)


async def _migrate_subdir(
    memory_subdir: str,
    memory_cls,
) -> tuple[int, int]:
    """Migrate one ``memory_subdir``.

    Args:
        memory_subdir: The subdirectory identifier to migrate.
        memory_cls: The resolved ``Memory`` class (from
            ``_import_memory()``). Passed as a parameter so the
            function never needs to look up ``Memory`` in its own
            scope — ``_import_memory()`` may have returned ``None``
            and the caller's reference is the authoritative binding.

    Returns:
        ``(docs_scanned, docs_updated)``.
    """
    # ``memory_cls.get_by_subdir`` returns a ``Memory`` wrapper around the
    # already-loaded (or freshly-initialized) FAISS index for this subdir.
    mem = await memory_cls.get_by_subdir(memory_subdir, log_item=None)

    # ``MyFaiss.get_all_docs()`` returns the raw docstore dict keyed by
    # memory id. Values are ``Document`` instances.
    all_docs = mem.db.get_all_docs()

    docs_scanned = 0
    docs_updated = 0
    modified: list = []

    for doc_id, doc in all_docs.items():
        docs_scanned += 1
        meta = doc.metadata or {}
        if not _needs_migration(meta):
            continue
        # ``apply_defaults`` mutates the dict in place and returns it.
        # Existing values are preserved; only missing fields are seeded.
        apply_defaults(meta)
        doc.metadata = meta
        modified.append(doc)
        docs_updated += 1

    if modified:
        # ``update_documents`` deletes + re-adds the docs to the FAISS
        # index, then calls ``_save_db()`` to persist atomically.
        await mem.update_documents(modified)

    return docs_scanned, docs_updated


async def _run_migration(
    get_existing_memory_subdirs_fn,
    memory_cls,
) -> tuple[int, int, int]:
    """Migrate every existing memory subdir.

    Args:
        get_existing_memory_subdirs_fn: The resolved function for listing
            subdirs (from ``_import_memory()``).
        memory_cls: The resolved ``Memory`` class (from
            ``_import_memory()``).

    Returns:
        ``(subdirs_processed, total_docs_scanned, total_docs_updated)``.
    """
    subdirs = get_existing_memory_subdirs_fn()
    total_scanned = 0
    total_updated = 0
    processed = 0
    for subdir in subdirs:
        try:
            scanned, updated = await _migrate_subdir(subdir, memory_cls)
        except Exception as exc:  # pragma: no cover - defensive
            print(
                f"[neuro_core] WARNING: failed to migrate subdir "
                f"'{subdir}': {exc}"
            )
            continue
        processed += 1
        total_scanned += scanned
        total_updated += updated
        print(
            f"[neuro_core]   subdir '{subdir}': scanned={scanned} "
            f"updated={updated}"
        )
    return processed, total_scanned, total_updated


def main() -> int:
    """Run the one-shot migration.

    Returns:
        Process exit code (0 on success, 1 on fatal error).
    """
    print("[neuro_core] execute.main() invoked — running migration...")
    get_existing_memory_subdirs, Memory = _import_memory()
    if get_existing_memory_subdirs is None or Memory is None:
        print(
            "[neuro_core] FATAL: `plugins._memory.helpers.memory` is not "
            "importable. The `_memory` plugin is a required dependency for "
            "`neuro_core`. Enable the `_memory` plugin in Settings → "
            "Plugins and try again."
        )
        return 1
    subdirs, scanned, updated = asyncio.run(
        _run_migration(get_existing_memory_subdirs, Memory)
    )
    print(
        f"[neuro_core] migration complete: subdirs_processed={subdirs} "
        f"docs_scanned={scanned} docs_updated={updated}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
