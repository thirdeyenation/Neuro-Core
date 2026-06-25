"""
Neuro Core runtime patch — wraps Memory methods that lack ``@extensible``.

The Agent Zero ``_memory`` plugin's ``Memory`` class has no ``@extensible``
decorators on any of its methods.  The hook directory tree under
``extensions/python/_functions/plugins._memory.helpers.memory/`` is
structurally correct but the framework never invokes it for ``Memory``
methods because the decorator that triggers hook discovery is absent from
the framework source (read-only).

This module installs thin wrappers on three ``Memory`` methods at plugin
init time (called from ``hooks.py → install()``).  Each wrapper replicates
the behavior the corresponding hook file was designed to provide.

Wrappers installed:
    Memory.insert_text          — seed Neuro Core metadata fields before
                                  Document construction (replaces
                                  insert_text/start/_10_neuro_metadata.py
                                  AND insert_documents/start/_10_neuro_metadata.py)
    Memory.search_similarity_threshold — update access counts in scores.json
                                  (replaces search_similarity_threshold/end/
                                  _10_access_tracking.py)
    Memory.delete_documents_by_ids — cascade-delete graph edges when memories
                                  are deleted (replaces
                                  delete_documents_by_ids/end/
                                  _10_graph_cascade.py)

Safety contracts:
    - Each wrapper is idempotent: re-calling install_patches() is a no-op
      if the wrapper is already installed (detected via _neuro_patched attr).
    - Each wrapper catches all exceptions and logs a warning; a patch error
      NEVER blocks the underlying Memory operation.
    - Wrappers are stored so uninstall_patches() can restore originals cleanly.
    - No import of plugin-local modules at module level — all plugin imports
      are deferred to call time inside each wrapper.

D39-A closure contract (D53, 2026-06-22):
    - delete_documents_by_ids wrapper performs sidecar cascade BEFORE the
      underlying FAISS delete. This eliminates the orphan-sidecar-entry window
      in which a process kill between FAISS save and sidecar delete could leave
      a relationship.json entry pointing at a non-existent FAISS vector.
    - On exception in the sidecar cascade, the wrapper logs a warning and
      continues to call the original (FAISS delete) — preserving correctness
      of the underlying Memory operation even if the sidecar write fails.
    - Order verified by tests/test_patch_live_integration.py and is the
      explicit pattern documented in the delete_patched wrapper below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

log = logging.getLogger("neuro_core.patch")

_originals: dict[str, Any] = {}


def install_patches() -> None:
    """Install all three Memory method wrappers. Idempotent."""
    try:
        from plugins._memory.helpers.memory import Memory  # framework — read-only import only
    except ImportError as e:
        log.warning(f"[neuro_core] patch: could not import Memory — {e}")
        return

    _patch_insert_text(Memory)
    _patch_search_similarity_threshold(Memory)
    _patch_delete_documents_by_ids(Memory)
    log.debug("[neuro_core] patch: all Memory wrappers installed")


def uninstall_patches() -> None:
    """Restore original Memory methods. Called from hooks.py → uninstall()."""
    if not _originals:
        return
    try:
        from plugins._memory.helpers.memory import Memory
    except ImportError:
        return

    for name, original in _originals.items():
        setattr(Memory, name, original)
    _originals.clear()
    log.debug("[neuro_core] patch: all Memory wrappers removed")


# ---------------------------------------------------------------------------
# insert_text — seed metadata before Document construction
# ---------------------------------------------------------------------------

def _patch_insert_text(Memory: type) -> None:
    if getattr(Memory.insert_text, "_neuro_patched", False):
        return

    original = Memory.insert_text
    _originals["insert_text"] = original

    async def insert_text_patched(self, text, metadata: dict = {}):  # type: ignore[override]
        try:
            from usr.plugins.neuro_core.helpers.metadata import (
                apply_defaults,
                validate_neuro_metadata,
            )
            if isinstance(metadata, dict):
                validate_neuro_metadata(metadata)
                apply_defaults(metadata)
        except Exception as e:
            log.warning(f"[neuro_core] insert_text patch non-fatal: {e}")
        return await original(self, text, metadata)

    insert_text_patched._neuro_patched = True  # type: ignore[attr-defined]
    Memory.insert_text = insert_text_patched


# ---------------------------------------------------------------------------
# search_similarity_threshold — update access tracking in scores.json
# ---------------------------------------------------------------------------

def _patch_search_similarity_threshold(Memory: type) -> None:
    if getattr(Memory.search_similarity_threshold, "_neuro_patched", False):
        return

    original = Memory.search_similarity_threshold
    _originals["search_similarity_threshold"] = original

    async def search_patched(self, query, limit=10, threshold=0.6, filter=None):  # type: ignore[override]
        result = await original(self, query, limit=limit, threshold=threshold, filter=filter)
        try:
            from usr.plugins.neuro_core.helpers.scores import ScoreStore
            memory_subdir = getattr(self, "memory_subdir", None) or "default"
            store = ScoreStore(memory_subdir)
            for doc in (result or []):
                doc_id = getattr(doc, "metadata", {}).get("id")
                if doc_id:
                    updated = store.update_access(doc_id)
                    # Mirror the new access_count onto the in-memory metadata
                    # so callers that read the returned doc see the current value.
                    # The sidecar scores.json is the source of truth.
                    try:
                        doc.metadata["access_count"] = updated.access_count
                    except Exception:
                        pass
        except Exception as e:
            log.warning(f"[neuro_core] search patch non-fatal: {e}")
        return result

    search_patched._neuro_patched = True  # type: ignore[attr-defined]
    Memory.search_similarity_threshold = search_patched


# ---------------------------------------------------------------------------
# delete_documents_by_ids — cascade graph edge removal
# ---------------------------------------------------------------------------

def _patch_delete_documents_by_ids(Memory: type) -> None:
    if getattr(Memory.delete_documents_by_ids, "_neuro_patched", False):
        return

    original = Memory.delete_documents_by_ids
    _originals["delete_documents_by_ids"] = original

    async def delete_patched(self, ids: list[str]):  # type: ignore[override]
        # D39-A / D53 closure: sidecar cascade runs BEFORE the FAISS delete.
        # This eliminates the orphan-sidecar-entry window — if the process is
        # killed between the FAISS delete and the sidecar delete, the
        # relationships.json entry would otherwise reference a non-existent
        # FAISS vector. Sidecar-first ordering means any crash leaves
        # at most a dangling FAISS vector (harmless) but never a dangling
        # sidecar entry (which would surface as a phantom edge in
        # context_graph retrieval).
        try:
            from usr.plugins.neuro_core.helpers.graph_store import GraphStore
            memory_subdir = getattr(self, "memory_subdir", None) or "default"
            store = GraphStore(memory_subdir)
            for doc_id in (ids or []):
                store.remove_edges_for_id(doc_id)
        except Exception as e:
            log.warning(f"[neuro_core] delete patch non-fatal: {e}")
        result = await original(self, ids)
        return result

    delete_patched._neuro_patched = True  # type: ignore[attr-defined]
    Memory.delete_documents_by_ids = delete_patched


# ---------------------------------------------------------------------------
# D55 (D39-D closure) — startup sidecar reconciliation
# ---------------------------------------------------------------------------


def reconcile_sidecars() -> dict:
    """D55: Read-only FAISS <-> sidecar consistency scan.

    Detects three structural orphan types per subdir:

    1. **Orphan scores sidecar** — ``scores.json`` present, ``index.faiss`` absent.
       Sidecar file exists with no FAISS index to back it; data is stranded.
    2. **Orphan relationships sidecar** — ``relationships.json`` present,
       ``index.faiss`` absent. Phantom edges that can never resolve.
    3. **Unseeded FAISS index** — ``index.faiss`` present, ``scores.json``
       absent. Insert hook may have been bypassed or pre-dates the patch.
       Informational only — these are NOT orphans in the strict sense.

    Read-only: orphans are logged via ``log.warning()`` but no file is
    modified. Auto-fix is intentionally out of scope for v1.

    Non-fatal: every step is wrapped in try/except; partial results are
    returned even if individual subdirs fail. The function NEVER raises.

    Strict UUID-level validation (per-Document membership) is intentionally
    deferred — it would require bootstrapping a full ``Memory`` instance per
    subdir, which is too expensive for a startup-time scan. v1 reports
    structural orphans only; per-ID checks remain a future enhancement.

    Returns:
        ``{
            "subdirs_scanned": int,
            "orphan_scores": int,
            "orphan_relationships": int,
            "unseeded_faiss": int,
            "errors": list[str],
        }``
    """
    result: dict[str, Any] = {
        "subdirs_scanned": 0,
        "orphan_scores": 0,
        "orphan_relationships": 0,
        "unseeded_faiss": 0,
        "errors": [],
    }
    try:
        from plugins._memory.helpers.memory import abs_db_dir  # framework — read-only import
    except Exception as exc:
        log.warning(f"[neuro_core] reconcile_sidecars import non-fatal: {exc}")
        return result

    try:
        memory_root = os.path.dirname(abs_db_dir("default"))
    except Exception as exc:
        log.warning(f"[neuro_core] reconcile_sidecars: cannot resolve memory root: {exc}")
        return result

    if not os.path.isdir(memory_root):
        return result

    try:
        subdirs = [
            d
            for d in os.listdir(memory_root)
            if os.path.isdir(os.path.join(memory_root, d))
        ]
    except Exception as exc:
        result["errors"].append(f"subdir discovery: {exc}")
        log.warning(
            f"[neuro_core] reconcile_sidecars: subdir discovery failed: {exc}"
        )
        return result

    for subdir in subdirs:
        result["subdirs_scanned"] += 1
        try:
            subdir_path = abs_db_dir(subdir)
            has_faiss = os.path.isfile(os.path.join(subdir_path, "index.faiss"))
            has_scores = os.path.isfile(os.path.join(subdir_path, "scores.json"))
            has_rels = os.path.isfile(os.path.join(subdir_path, "relationships.json"))

            if has_scores and not has_faiss:
                result["orphan_scores"] += 1
                log.warning(
                    f"[neuro_core] reconcile: orphan scores sidecar "
                    f"in subdir={subdir} (no index.faiss)"
                )
            if has_rels and not has_faiss:
                result["orphan_relationships"] += 1
                log.warning(
                    f"[neuro_core] reconcile: orphan relationships sidecar "
                    f"in subdir={subdir} (no index.faiss)"
                )
            if has_faiss and not has_scores:
                result["unseeded_faiss"] += 1
                log.info(
                    f"[neuro_core] reconcile: unseeded FAISS in subdir={subdir} "
                    f"(no scores.json — informational)"
                )
        except Exception as exc:
            result["errors"].append(f"{subdir}: {exc}")
            log.warning(
                f"[neuro_core] reconcile_sidecars subdir {subdir} non-fatal: {exc}"
            )
            continue

    if result["errors"]:
        log.warning(
            f"[neuro_core] reconcile_sidecars complete: "
            f"subdirs={result['subdirs_scanned']} "
            f"orphan_scores={result['orphan_scores']} "
            f"orphan_relationships={result['orphan_relationships']} "
            f"unseeded_faiss={result['unseeded_faiss']} "
            f"errors={len(result['errors'])}"
        )
    else:
        log.info(
            f"[neuro_core] reconcile_sidecars complete: "
            f"subdirs={result['subdirs_scanned']} "
            f"orphan_scores={result['orphan_scores']} "
            f"orphan_relationships={result['orphan_relationships']} "
            f"unseeded_faiss={result['unseeded_faiss']}"
        )
    return result

    delete_patched._neuro_patched = True  # type: ignore[attr-defined]
    Memory.delete_documents_by_ids = delete_patched
