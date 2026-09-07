"""NC1-owned native Memory access layer (WI-2026-09-04-PHASE0-PATCH-ARCH, B).

Behavior-identical reconstruction of the VAL-validated prototype (probe5b,
validation-report.yaml rev 3, Gap 5b: 8/8). Signatures/constants extracted
from the surviving .pyc; re-validated via the probe5b oracle after rebuild.

Routes NC1 capture/search/delete through the REAL framework Memory API with
full signature fidelity (KI-001/KI-002 structurally impossible: no signature
replica exists) and tracks access on BOTH search paths (closes KI-021).
Bookkeeping contracts: delete cascade runs sidecar-FIRST (D39-A/D53, ARC
cond 2); every bookkeeping step is exception-safe and non-fatal (ARC cond 3).
No host monkey-patching: the Memory class is never modified.
"""

from __future__ import annotations

import logging

log = logging.getLogger("neuro_core.native_access")


def _memory_class():
    from plugins._memory.helpers.memory import Memory

    return Memory


def _subdir(self_obj):
    return getattr(self_obj, "memory_subdir", None) or "default"


async def capture(agent_memory, text: str, metadata: dict | None = None):
    """NC1 capture: seed metadata defaults, then call the REAL insert_text."""
    metadata = metadata if isinstance(metadata, dict) else {}
    try:
        from usr.plugins.neuro_core.helpers.metadata import apply_defaults, validate_neuro_metadata

        validate_neuro_metadata(metadata)
        apply_defaults(metadata)
    except Exception as e:
        log.warning(f"[neuro_core] capture metadata non-fatal: {e}")
    Memory = _memory_class()
    return await Memory.insert_text(agent_memory, text, metadata=metadata)


async def search(agent_memory, query, limit=10, threshold=0.6, filter="", embedding=None):
    """NC1 search: REAL search with FULL signature (incl. embedding), then access tracking."""
    Memory = _memory_class()
    result = await Memory.search_similarity_threshold(
        agent_memory, query, limit=limit, threshold=threshold, filter=filter, embedding=embedding
    )
    _track_access(agent_memory, result)
    return result


async def search_with_scores(agent_memory, query, limit=10, threshold=0.6, filter=""):
    """NC1 with-scores search: REAL method, then access tracking (closes the Gap 3 gap)."""
    Memory = _memory_class()
    result = await Memory.search_similarity_threshold_with_scores(
        agent_memory, query, limit=limit, threshold=threshold, filter=filter
    )
    docs = [d for (d, _s) in (result or [])]
    _track_access(agent_memory, docs)
    return result


async def delete(agent_memory, ids, cascade=False, filter=""):
    """NC1 delete: sidecar cascade FIRST (D39-A ordering preserved), then REAL delete
    with cascade/filter passed through to the real signature."""
    try:
        from usr.plugins.neuro_core.helpers.graph_store import GraphStore

        store = GraphStore(_subdir(agent_memory))
        for doc_id in ids or []:
            store.remove_edges_for_id(doc_id)
    except Exception as e:
        log.warning(f"[neuro_core] delete cascade non-fatal: {e}")
    Memory = _memory_class()
    return await Memory.delete_documents_by_ids(agent_memory, ids, cascade=cascade, filter=filter)


def _track_access(agent_memory, docs):
    try:
        from usr.plugins.neuro_core.helpers.scores import ScoreStore

        store = ScoreStore(_subdir(agent_memory))
        for doc in docs or []:
            if not hasattr(doc, "metadata"):
                continue
            doc_id = getattr(doc, "id", None) or (doc.metadata or {}).get("id")
            if doc_id:
                updated = store.update_access(doc_id)
                try:
                    doc.metadata["access_count"] = updated.access_count
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"[neuro_core] access tracking non-fatal: {e}")
