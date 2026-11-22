"""Model-less Neuro Core migration detection engine (WI-004, D-NCM-004).

In-process-safe, fail-closed, model-less detection of memory subdirectories
that still need Neuro Core metadata backfill. This module is the shared
detection core consumed by BOTH:

- ``usr.plugins.neuro_core.execute`` (the optional manual CLI wrapper), and
- ``usr.plugins.neuro_core.extensions/python/startup_migration/_20_neuro_migration.py``
  (the automatic sibling startup extension).

Import-time hygiene (binding contract):
- ZERO ``sys.path`` mutation at import time.
- ZERO ``print()`` calls (logging only; ``logging`` is configured by the
  host application, never here).
- ZERO ``sys.exit()`` and ZERO ``asyncio.run()`` at import time (the module
  imports no asyncio at all; the backfill coroutines are scheduled by the
  caller).

Detection order (binding contract, D-NCM-004):
1. When ``memory.sqlite3`` (v3 NativeStore, ``plugins/_memory/helpers/native_store.py``)
   is present in the store directory, it is inspected FIRST via stdlib
   ``sqlite3`` in READ-ONLY URI mode (``?mode=ro``). This performs ZERO
   writes and produces ZERO journal/WAL/SHM sidecar artifacts (byte-identity).
2. ``index.pkl`` (legacy MyFaiss ``FAISS.save_local`` docstore dump) is
   inspected ONLY when ``memory.sqlite3`` is absent. Dual-marker directories
   (migrated ``memory.sqlite3`` + stale unmigrated ``index.pkl``) persist
   because the v3 ``_LegacyReader`` never deletes legacy files — the SQLite
   store is therefore authoritative and the stale pickle must never win.

Fail-closed semantics: any SQLite/schema/JSON/pickle/structure error returns
``(None, 0)`` so the caller falls back to the full (heavy) migration path.
A missing or empty store returns ``(False, 0)`` — never blindly on file
existence, only after a successful scan or a confirmed-absent store.

Scan roots (CAP-006): standard ``/a0/usr/memory/*`` subdirs AND
project-scoped ``/a0/usr/projects/<proj>/.a0proj/memory/`` directories.
Project-scoped resolution is policy-driven:
- ``projects_fail_closed=True`` (execute.py CLI contract): ``projects/``
  subdirs resolve to ``None`` → ``(None, 0)`` (the binding un-injected probe
  asserts ``execute._abs_db_dir_light('projects/neuro_core_2') is None``).
- ``projects_fail_closed=False`` (startup extension scan): ``projects/``
  subdirs resolve via ``helpers.files`` primitives to the project's
  ``.a0proj/memory/`` directory, so a marker-less project dir returns
  ``(False, 0)`` model-lessly.

Write boundary (D-NC1-153): detection is strictly READ-ONLY. ALL backfill
writes flow exclusively through the decorated ``Memory`` facade
(``Memory.update_documents``) — never through direct SQLite writes. The
``backfill_subdir``/``backfill_all`` coroutines below only orchestrate
facade calls; the caller owns scheduling them.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Callable, Optional

_logger = logging.getLogger("neuro_core.migration_engine")

# The set of Neuro Core fields that mark a document as "migrated".
# Mirrors ``usr.plugins.neuro_core.execute._NEURO_CORE_FIELDS`` verbatim.
NEURO_CORE_FIELDS: tuple[str, ...] = (
    "memory_type",
    "importance",
    "confidence",
    "stability",
    "validation_status",
)

# v3 NativeStore single-file database name (native_store.py: ``filename =
# "memory.sqlite3"``) and the legacy MyFaiss docstore dump name.
SQLITE_FILENAME = "memory.sqlite3"
PKL_FILENAME = "index.pkl"

WarningEmitter = Callable[[str], None]


def _resolve_emitter(emit_warning: Optional[WarningEmitter]) -> WarningEmitter:
    """Return the caller's emitter, defaulting to module logging."""
    return emit_warning if emit_warning is not None else _logger.warning


def needs_migration(metadata: object) -> bool:
    """Return ``True`` if any Neuro Core field is absent from ``metadata``."""
    if not isinstance(metadata, dict):
        return True
    return any(field_name not in metadata for field_name in NEURO_CORE_FIELDS)


def resolve_db_dir(
    memory_subdir: str,
    projects_fail_closed: bool = True,
    resolve_fn: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Resolve a ``memory_subdir`` to its store directory (model-less).

    PATH-CONFINEMENT: paths are resolved ONLY through the framework's own
    ``helpers.files.get_abs_path`` primitive — no path is ever constructed
    by string assembly outside ``helpers.files``.

    Args:
        memory_subdir: Subdir identifier (``"main"``, ``"default"``, or
            ``"projects/<name>"``).
        projects_fail_closed: When ``True`` (execute.py CLI contract),
            ``projects/``-scoped subdirs return ``None`` (fail closed).
            When ``False`` (startup extension scan), they resolve to the
            project's ``.a0proj/memory/`` directory via ``helpers.files``.
        resolve_fn: Test-injection override; when provided, it is the sole
            resolver and the framework import is never attempted.

    Returns:
        The absolute store directory path, or ``None`` on any resolution
        failure (fail closed → the caller falls back to the full path).
    """
    if resolve_fn is not None:
        return resolve_fn(memory_subdir)
    if memory_subdir.startswith("projects/"):
        if projects_fail_closed:
            return None
        try:
            from helpers import files

            project_name = memory_subdir.split("/", 1)[1]
            return files.get_abs_path(
                "usr/projects", project_name, ".a0proj", "memory"
            )
        except Exception:
            return None
    try:
        from helpers import files

        return files.get_abs_path("usr/memory", memory_subdir)
    except Exception:
        return None


def list_memory_subdirs(
    list_fn: Optional[Callable[[], Optional[list]]] = None,
) -> Optional[list]:
    """Lightweight enumeration of memory subdirs (standard + project-scoped).

    Replicates ``get_existing_memory_subdirs``' standard enumeration through
    ``helpers.files`` primitives so the model-less detection pre-pass never
    imports the heavy ``plugins._memory.helpers.memory`` module. Project
    subdirs are listed by NAME ONLY (``projects/<name>``); their store
    resolution is governed by ``resolve_db_dir``'s policy flag.

    Returns ``None`` on any enumeration failure (fail closed → full path).
    """
    if list_fn is not None:
        return list_fn()
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


def _detect_sqlite(
    db_path: str,
    memory_subdir: str,
    warn: WarningEmitter,
) -> tuple[Optional[bool], int]:
    """Read-only model-less scan of a v3 NativeStore ``memory.sqlite3``.

    The database is opened in READ-ONLY URI mode (``?mode=ro``) on the
    resolved path — ZERO writes, ZERO journal/WAL/SHM sidecar artifacts
    (byte-identity, D-NCM-004). Table/column names mirror
    ``plugins/_memory/helpers/native_store.py`` exactly:
    ``docs(id TEXT PRIMARY KEY, text TEXT, metadata TEXT, vector BLOB)``.
    """
    docs_scanned = 0
    try:
        # Resolved-URI encoding + explicit close via ``closing``; the
        # read-only URI mode makes any write attempt fail at the SQLite
        # level, so detection can never mutate the store or spawn sidecars.
        uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            for (metadata_json,) in conn.execute("SELECT metadata FROM docs"):
                docs_scanned += 1
                try:
                    meta = json.loads(metadata_json)
                except Exception as exc:
                    warn(
                        f"[neuro_core] WARNING: model-less read of "
                        f"'{memory_subdir}' failed (corrupt/unreadable "
                        f"metadata JSON in {SQLITE_FILENAME}): {exc} — "
                        f"falling back to the full migration path."
                    )
                    return None, 0
                if not isinstance(meta, dict):
                    warn(
                        f"[neuro_core] WARNING: model-less read of "
                        f"'{memory_subdir}' found non-dict metadata JSON "
                        f"in {SQLITE_FILENAME} — falling back to the full "
                        f"migration path."
                    )
                    return None, 0
                if needs_migration(meta):
                    return True, docs_scanned
        # Empty table or all rows fully migrated.
        return False, docs_scanned
    except Exception as exc:
        # Any SQLite/schema error fails closed (never a blind (False, 0)).
        warn(
            f"[neuro_core] WARNING: model-less read of '{memory_subdir}' "
            f"failed (SQLite/schema error on {SQLITE_FILENAME}): {exc} — "
            f"falling back to the full migration path."
        )
        return None, 0


def _detect_pickle(
    pkl_path: str,
    memory_subdir: str,
    warn: WarningEmitter,
) -> tuple[Optional[bool], int]:
    """Read-only model-less scan of a legacy ``index.pkl`` docstore dump.

    TRUST-BOUNDARY NOTE (ARC condition C3): the pickle load mirrors the
    existing ``MyFaiss.load_local(allow_dangerous_deserialization=True)``
    trust boundary on the same framework-owned store. Detection-only,
    READ-ONLY (``rb`` handle, no marker/hash/temp/re-saved files).
    """
    try:
        with open(pkl_path, "rb") as f:
            payload = pickle.load(f)  # noqa: S301 — same trust boundary as MyFaiss.load_local(allow_dangerous_deserialization=True)
    except Exception as exc:
        warn(
            f"[neuro_core] WARNING: model-less read of '{memory_subdir}' "
            f"failed (corrupt/unreadable index.pkl): {exc} — falling back "
            f"to the full migration path."
        )
        return None, 0

    # Structure check (ARC condition C5): fail closed on ANY unexpected
    # docstore structure — non-tuple payload, wrong arity, unknown docstore
    # class, or a missing/non-dict mapping attribute.
    if not isinstance(payload, tuple) or len(payload) != 2:
        warn(
            f"[neuro_core] WARNING: model-less read of '{memory_subdir}' "
            f"found unexpected index.pkl structure (arity "
            f"{len(payload) if isinstance(payload, tuple) else 'n/a'}) — "
            f"falling back to the full migration path."
        )
        return None, 0
    docstore = payload[0]
    docstore_cls_name = type(docstore).__name__
    if docstore_cls_name not in ("LocalFileStore", "InMemoryDocstore"):
        warn(
            f"[neuro_core] WARNING: model-less read of '{memory_subdir}' "
            f"found unexpected docstore type '{docstore_cls_name}' — "
            f"falling back to the full migration path."
        )
        return None, 0
    # Structural docstore-shape resolution (WI-P80, ARC condition C1):
    # recognition requires BOTH guards — the class-name allowlist above AND
    # a dict-valued attribute from the verified structural families
    # ('.store' — LocalFileStore-style; '_dict' — langchain_community
    # InMemoryDocstore, the persisted shape on framework v2.8 and current;
    # ADR-NC1-005). Recognition is by STRUCTURE, never by version string.
    store = getattr(docstore, "store", None)
    if not isinstance(store, dict):
        store = getattr(docstore, "_dict", None)
    if not isinstance(store, dict):
        warn(
            f"[neuro_core] WARNING: model-less read of '{memory_subdir}' "
            f"found a docstore without a recognized dict mapping "
            f"('.store' or '_dict') — falling back to the full "
            f"migration path."
        )
        return None, 0

    docs_scanned = 0
    for doc_id, doc in store.items():
        docs_scanned += 1
        meta = getattr(doc, "metadata", None) or {}
        if needs_migration(meta):
            return True, docs_scanned
    return False, docs_scanned


def detect_subdir(
    memory_subdir: str,
    abs_db_dir_fn: Optional[Callable[[str], Optional[str]]] = None,
    projects_fail_closed: bool = True,
    emit_warning: Optional[WarningEmitter] = None,
    resolve_fn: Optional[Callable[[str], Optional[str]]] = None,
) -> tuple[Optional[bool], int]:
    """Read-only, model-less check whether a subdir still needs migration.

    Returns ``(status, docs_scanned)`` where ``status`` is:
    - ``False`` — every doc already carries all Neuro Core fields (or the
      store is absent/empty): the caller may skip the subdir model-less.
    - ``True`` — at least one doc is missing a field: the full (heavy)
      migration path must run.
    - ``None`` — the model-less read could not be completed for ANY reason:
      the caller must fall back to the full migration path (fail closed).

    Detection order: ``memory.sqlite3`` FIRST when present (read-only URI
    mode); ``index.pkl`` ONLY when ``memory.sqlite3`` is absent (dual-marker
    precedence, D-NCM-004).

    This function never writes anything to the store directory (ARC
    condition C2 / D-NC1-153).
    """
    warn = _resolve_emitter(emit_warning)
    if abs_db_dir_fn is not None:
        db_dir = abs_db_dir_fn(memory_subdir)
    else:
        db_dir = resolve_db_dir(
            memory_subdir,
            projects_fail_closed=projects_fail_closed,
            resolve_fn=resolve_fn,
        )
        if db_dir is None:
            warn(
                f"[neuro_core] WARNING: model-less path resolution failed "
                f"for '{memory_subdir}' — falling back to the full "
                f"migration path."
            )
            return None, 0

    # Absent or empty store: nothing to migrate — skip model-less.
    if not os.path.isdir(db_dir):
        return False, 0

    sqlite_path = os.path.join(db_dir, SQLITE_FILENAME)
    if os.path.isfile(sqlite_path):
        # v3 NativeStore is authoritative when present (dual-marker dirs:
        # the stale legacy index.pkl must never win, D-NCM-004).
        return _detect_sqlite(sqlite_path, memory_subdir, warn)

    pkl_path = os.path.join(db_dir, PKL_FILENAME)
    if not os.path.isfile(pkl_path):
        return False, 0
    return _detect_pickle(pkl_path, memory_subdir, warn)


# ---------------------------------------------------------------------------
# Backfill orchestration (facade-only writes, D-NC1-153)
# ---------------------------------------------------------------------------


def _import_apply_defaults():
    """Dual-path import of ``apply_defaults`` (plugin mode + standalone)."""
    try:
        from usr.plugins.neuro_core.helpers.metadata import apply_defaults
        return apply_defaults
    except ImportError:
        from helpers.metadata import apply_defaults
        return apply_defaults


async def backfill_subdir(
    memory_subdir: str,
    memory_cls,
    emit_warning: Optional[WarningEmitter] = None,
) -> tuple[int, int]:
    """Backfill one subdir through the decorated ``Memory`` facade.

    ALL writes flow through ``Memory.get_by_subdir`` +
    ``mem.update_documents`` — never through direct SQLite/pickle writes
    (D-NC1-153). ``preload_knowledge=False`` keeps the embedding-model cold
    load out of the metadata-only backfill (KI-052).

    Returns ``(docs_scanned, docs_updated)``.
    """
    apply_defaults = _import_apply_defaults()

    mem = await memory_cls.get_by_subdir(
        memory_subdir, log_item=None, preload_knowledge=False
    )
    all_docs = mem.db.get_all_docs()

    docs_scanned = 0
    docs_updated = 0
    modified: list = []

    for doc_id, doc in all_docs.items():
        docs_scanned += 1
        meta = doc.metadata or {}
        if not needs_migration(meta):
            continue
        # ``apply_defaults`` mutates the dict in place and returns it.
        # Existing values are preserved; only missing fields are seeded.
        apply_defaults(meta)
        doc.metadata = meta
        modified.append(doc)
        docs_updated += 1

    if modified:
        await mem.update_documents(modified)

    return docs_scanned, docs_updated


async def backfill_all(
    subdirs: list,
    memory_cls,
    emit_warning: Optional[WarningEmitter] = None,
) -> tuple[int, int, int]:
    """Backfill every pending subdir; returns ``(processed, scanned, updated)``.

    Per-subdir failures are logged non-fatally and never abort the sweep.
    """
    warn = _resolve_emitter(emit_warning)
    processed = 0
    total_scanned = 0
    total_updated = 0
    for subdir in subdirs:
        try:
            scanned, updated = await backfill_subdir(
                subdir, memory_cls, emit_warning=emit_warning
            )
        except Exception as exc:
            warn(
                f"[neuro_core] WARNING: startup backfill failed for subdir "
                f"'{subdir}': {exc}"
            )
            continue
        processed += 1
        total_scanned += scanned
        total_updated += updated
        _logger.info(
            "[neuro_core] startup backfill subdir '%s': scanned=%d updated=%d",
            subdir,
            scanned,
            updated,
        )
    return processed, total_scanned, total_updated
