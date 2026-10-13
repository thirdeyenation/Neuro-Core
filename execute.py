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
import pickle
import sys
import time

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


# --- Model-less early-exit detection (KI-054, WI-P75) ---------------------
#
# TRUST-BOUNDARY NOTE (ARC condition C3): the pickle load below mirrors the
# existing MyFaiss.load_local(allow_dangerous_deserialization=True) trust
# boundary on the same framework-owned store. Store paths are resolved ONLY
# via the framework's own ``abs_db_dir(memory_subdir)`` — no independently
# constructed paths. This is a detection-only, READ-ONLY read sanctioned by
# D-NC1-153, scoped to execute.py migration detection; all migration writes
# continue to flow exclusively through the decorated Memory facade.


def _abs_db_dir_light(memory_subdir: str):
    """Lightweight stand-in for ``abs_db_dir``'s STANDARD branch (WI-P79).

    PATH-CONFINEMENT NOTE (WI-P75 ARC condition C3 intent, sanctioned by the
    WI-P79 steward-design-decision rev 1, condition C2): this resolver
    replicates ``abs_db_dir``'s standard branch VERBATIM through the same
    framework primitive (``helpers.files.get_abs_path("usr/memory", subdir)``)
    so the model-less detection phase never imports the heavy
    ``plugins._memory.helpers.memory`` module (KI-054: that import alone can
    exceed the framework's fixed 120s subprocess window). No path is ever
    constructed by string assembly outside ``helpers.files`` primitives.

    Fail-closed by design: ``projects/``-scoped subdirs (whose correct
    resolution requires the heavy ``helpers.projects`` module) and ANY
    ``helpers.files`` import failure return ``None`` — the caller must fall
    back to the full migration path, which resolves paths via the framework's
    own ``abs_db_dir``. A binding drift-detection unit test asserts this
    resolver stays equivalent to the framework's standard branch.
    """
    if memory_subdir.startswith("projects/"):
        return None
    try:
        from helpers import files

        return files.get_abs_path("usr/memory", memory_subdir)
    except Exception:
        return None


def _list_memory_subdirs():
    """Lightweight enumeration of memory subdirs (WI-P79).

    ENUMERATION NOTE (WI-P79 steward-design-decision rev 1, condition C2
    scope): replicates ``get_existing_memory_subdirs``' standard enumeration
    through ``helpers.files`` primitives so the model-less detection pre-pass
    never imports the heavy ``plugins._memory.helpers.memory`` module.
    ``projects/``-scoped subdirs are listed by NAME ONLY (ARC-approved): the
    authoritative ``index.faiss`` existence check requires the heavy
    ``helpers.projects`` module, so project subdirs are intentionally
    conservative here — their model-less detection fails closed via
    ``_abs_db_dir_light`` and they are resolved by the full path's heavy
    enumeration when migration actually runs. Returns ``None`` on any
    enumeration failure (fail-closed → full migration path).
    """
    try:
        from helpers import files

        subdirs = files.get_subdirectories("usr/memory")
        for project_subdir in files.get_subdirectories("usr/projects"):
            subdirs.append(f"projects/{project_subdir}")
        if "default" not in subdirs:
            subdirs.insert(0, "default")
        return subdirs
    except Exception:
        return None


def _modelless_subdir_needs_migration(
    memory_subdir: str,
    abs_db_dir_fn=None,
) -> tuple[bool | None, int]:
    """Read-only, model-less check whether a subdir still needs migration.

    Reads the persisted docstore directly from ``index.pkl`` (a plain
    ``pickle.dump((docstore, index_to_docstore_id))`` tuple written by
    ``FAISS.save_local``) without constructing the embedding model or any
    ``Memory``/``MyFaiss`` object.

    Returns:
        ``(status, docs_scanned)`` where ``status`` is:
        - ``False`` — every doc already carries all Neuro Core fields (or the
          store is absent/empty): the caller may skip the subdir model-less.
        - ``True`` — at least one doc is missing a field: the full migration
          path must run.
        - ``None`` — the model-less read could not be completed for ANY reason
          (missing/corrupt pickle, unpickling error, unexpected structure,
          version drift): the caller must fall back to the current full path.

    This function never writes anything to the store directory (ARC condition
    C2): the file is opened with a read-only binary handle and no marker,
    hash, temp, or re-saved files are produced.
    """
    if abs_db_dir_fn is None:
        # Confined to the framework's own path resolution semantics
        # (WI-P75 ARC condition C3 intent; WI-P79 condition C2): the
        # lightweight resolver replicates abs_db_dir's standard branch
        # verbatim via helpers.files and fails closed (None) on
        # projects/-scoped subdirs and any import failure — the heavy
        # plugins._memory.helpers.memory module is NOT imported here
        # (KI-054: its import alone can exceed the 120s window).
        #
        # WI-P79 rev 2 (VAL defect D1): _abs_db_dir_light returns the
        # resolved PATH (a string), not a callable — use it directly as
        # ``db_dir``. The injected-callable branch below keeps the
        # ``abs_db_dir_fn(memory_subdir)`` contract for test injection.
        db_dir = _abs_db_dir_light(memory_subdir)
        if db_dir is None:
            print(
                f"[neuro_core] WARNING: model-less path resolution failed "
                f"for '{memory_subdir}' — falling back to the full "
                f"migration path.",
                flush=True,
            )
            return None, 0
    else:
        db_dir = abs_db_dir_fn(memory_subdir)

    # Absent or empty store: nothing to migrate — skip model-less.
    if not os.path.isdir(db_dir):
        return False, 0
    pkl_path = os.path.join(db_dir, "index.pkl")
    if not os.path.isfile(pkl_path):
        return False, 0

    try:
        # READ-ONLY handle (ARC condition C2): "rb" only; nothing in this
        # function writes to the store directory.
        with open(pkl_path, "rb") as f:
            payload = pickle.load(f)  # noqa: S301 — same trust boundary as MyFaiss.load_local(allow_dangerous_deserialization=True)
    except Exception as exc:
        print(
            f"[neuro_core] WARNING: model-less read of '{memory_subdir}' "
            f"failed (corrupt/unreadable index.pkl): {exc} — falling back "
            f"to the full migration path.",
            flush=True,
        )
        return None, 0

    # Structure check (ARC condition C5): fail closed on ANY unexpected
    # docstore structure — non-tuple payload, wrong arity, unknown docstore
    # class, or a missing/non-dict ``.store`` attribute.
    if not isinstance(payload, tuple) or len(payload) != 2:
        print(
            f"[neuro_core] WARNING: model-less read of '{memory_subdir}' "
            f"found unexpected index.pkl structure (arity "
            f"{len(payload) if isinstance(payload, tuple) else 'n/a'}) — "
            f"falling back to the full migration path.",
            flush=True,
        )
        return None, 0
    docstore = payload[0]
    docstore_cls_name = type(docstore).__name__
    if docstore_cls_name not in ("LocalFileStore", "InMemoryDocstore"):
        print(
            f"[neuro_core] WARNING: model-less read of '{memory_subdir}' "
            f"found unexpected docstore type '{docstore_cls_name}' — "
            f"falling back to the full migration path.",
            flush=True,
        )
        return None, 0
    # Structural docstore-shape resolution (WI-P80, ARC condition C1):
    # recognition requires BOTH guards — the class-name allowlist above AND
    # a dict-valued attribute from the verified structural families below.
    #   '.store' — the LocalFileStore-style mapping attribute.
    #   '_dict'  — the langchain_community InMemoryDocstore mapping attribute
    #              (the persisted shape on BOTH framework v2.8 (commit
    #              5ff106a2, plugins/_memory/helpers/memory.py:219/:44-51)
    #              and current; a langchain-community 0.3.19 implementation
    #              detail — recorded in ADR-NC1-005).
    # Recognition is by STRUCTURE, never by version string (ADR-NC1-005):
    # any allowlisted docstore exposing neither recognized dict attribute,
    # and any other unexpected structure, fails closed (None → full
    # migration path) exactly as before.
    store = getattr(docstore, "store", None)
    if not isinstance(store, dict):
        store = getattr(docstore, "_dict", None)
    if not isinstance(store, dict):
        print(
            f"[neuro_core] WARNING: model-less read of '{memory_subdir}' "
            f"found a docstore without a recognized dict mapping "
            f"('.store' or '_dict') — falling back to the full "
            f"migration path.",
            flush=True,
        )
        return None, 0

    docs_scanned = 0
    for doc_id, doc in store.items():
        docs_scanned += 1
        meta = getattr(doc, "metadata", None) or {}
        if _needs_migration(meta):
            return True, docs_scanned
    return False, docs_scanned

# ---------------------------------------------------------------------------


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
    # ``preload_knowledge=False``: the migration only scans/updates doc
    # metadata — knowledge preload (full agent init + embedding-model cold
    # load + knowledge re-embedding) is irrelevant here and, inside the
    # framework's fixed 120s execute.py subprocess window, is wasted work
    # that can push a fresh-container first run into a timeout (KI-052).
    mem = await memory_cls.get_by_subdir(
        memory_subdir, log_item=None, preload_knowledge=False
    )

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
        subdir_start = time.perf_counter()
        # Model-less early-exit (KI-054, WI-P75): before constructing any
        # Memory/embedding-model machinery, try a read-only direct scan of
        # the persisted docstore. Only a definitive "nothing to migrate"
        # (False) skips the subdir; "needs migration" (True) and "cannot
        # determine" (None — any read/structure failure, already warned)
        # both fall back to the exact current full path below.
        try:
            skip_status, _ml_scanned = _modelless_subdir_needs_migration(subdir)
        except Exception as exc:  # pragma: no cover - defensive
            print(
                f"[neuro_core] WARNING: model-less detection failed for "
                f"subdir '{subdir}': {exc} — falling back to the full "
                f"migration path.",
                flush=True,
            )
            skip_status = None
        if skip_status is False:
            print(
                f"[neuro_core]   subdir '{subdir}': skipped "
                f"(nothing to migrate) [model-less]",
                flush=True,
            )
            continue
        try:
            scanned, updated = await _migrate_subdir(subdir, memory_cls)
        except Exception as exc:  # pragma: no cover - defensive
            print(
                f"[neuro_core] WARNING: failed to migrate subdir "
                f"'{subdir}': {exc}",
                flush=True,
            )
            continue
        processed += 1
        total_scanned += scanned
        total_updated += updated
        subdir_elapsed = time.perf_counter() - subdir_start
        print(
            f"[neuro_core]   subdir '{subdir}': scanned={scanned} "
            f"updated={updated} elapsed={subdir_elapsed:.2f}s",
            flush=True,
        )
    return processed, total_scanned, total_updated


def main() -> int:
    """Run the one-shot migration.

    WI-P79 (KI-054): the model-less detection pre-pass runs WITHOUT
    importing the heavy ``plugins._memory.helpers.memory`` module — the
    deferred import happens exactly once, only when at least one subdir
    needs the full migration path, and always BEFORE any migration write
    (ARC condition C3). On a fresh/empty store the process exits before
    any heavy import, completing in seconds inside the framework's fixed
    120s subprocess window.

    Returns:
        Process exit code (0 on success, 1 on fatal error).
    """
    print(
        "[neuro_core] execute.main() invoked — running migration...",
        flush=True,
    )
    main_start = time.perf_counter()

    # --- Phase 1: model-less detection pre-pass (no heavy imports) -------
    subdirs = _list_memory_subdirs()
    if subdirs is None:
        # Fail-closed: enumeration failed — run the exact full path below,
        # whose heavy enumeration is authoritative.
        print(
            "[neuro_core] WARNING: lightweight subdir enumeration failed "
            "— falling back to the full migration path.",
            flush=True,
        )
        needs_migration = None
    else:
        needs_migration = []
        for subdir in subdirs:
            try:
                skip_status, _ml_scanned = _modelless_subdir_needs_migration(
                    subdir
                )
            except Exception as exc:  # pragma: no cover - defensive
                print(
                    f"[neuro_core] WARNING: model-less detection failed for "
                    f"subdir '{subdir}': {exc} — falling back to the full "
                    f"migration path.",
                    flush=True,
                )
                skip_status = None
            if skip_status is False:
                print(
                    f"[neuro_core]   subdir '{subdir}': skipped "
                    f"(nothing to migrate) [model-less]",
                    flush=True,
                )
                continue
            # True (needs migration) and None (cannot determine —
            # structure drift / resolution failure, already warned) both
            # require the full path (fail-closed, WI-P75 semantics).
            needs_migration.append(subdir)
        if not needs_migration:
            # Early exit: nothing to migrate anywhere — the heavy module
            # was never imported (WI-P79; binding import-blocking-stub
            # assertion in the test suite).
            total_elapsed = time.perf_counter() - main_start
            print(
                f"[neuro_core] migration complete: subdirs_processed=0 "
                f"docs_scanned=0 docs_updated=0 "
                f"total_elapsed={total_elapsed:.2f}s",
                flush=True,
            )
            return 0

    # --- Phase 2: deferred heavy import (exactly once, before any write) -
    get_existing_memory_subdirs, Memory = _import_memory()
    if get_existing_memory_subdirs is None or Memory is None:
        print(
            "[neuro_core] FATAL: `plugins._memory.helpers.memory` is not "
            "importable. The `_memory` plugin is a required dependency for "
            "`neuro_core`. Enable the `_memory` plugin in Settings → "
            "Plugins and try again.",
            flush=True,
        )
        return 1
    subdirs, scanned, updated = asyncio.run(
        _run_migration(get_existing_memory_subdirs, Memory)
    )
    total_elapsed = time.perf_counter() - main_start
    print(
        f"[neuro_core] migration complete: subdirs_processed={subdirs} "
        f"docs_scanned={scanned} docs_updated={updated} "
        f"total_elapsed={total_elapsed:.2f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
