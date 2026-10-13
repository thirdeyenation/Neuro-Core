"""NC1-owned native Memory access layer (WI-2026-09-04-PHASE0-PATCH-ARCH, B).

Behavior-identical reconstruction of the VAL-validated prototype (probe5b,
validation-report.yaml rev 3, Gap 5b: 8/8). Signatures/constants extracted
from the surviving .pyc; re-validated via the probe5b oracle after rebuild.

Routes NC1 capture/search/delete through the REAL framework Memory API with
full signature fidelity (KI-001/KI-002 structurally impossible: no signature
replica exists) and tracks access on BOTH search paths (closes KI-021).
Bookkeeping contracts: the sidecar cascade is owned by the ratified
_10_graph_cascade end-hook strictly AFTER confirmed deletion
(WI-P10-DELETE-ORDERING supersedes the former sidecar-first D39-A
ordering); every bookkeeping step is exception-safe and non-fatal
(ARC cond 3).
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
    """NC1 delete: delegate solely to the REAL delete with cascade/filter passed
    through to the real signature.

    WI-P10-DELETE-ORDERING (S1, ARC pre-design C1/C5): the former pre-delete
    sidecar cascade here (D39-A ordering, ``remove_edges_for_id`` per id BEFORE
    the framework delete) is superseded. Grounded provenance: D-NC1-035
    (decision_log/decisions.md:274) explicitly records the delete-ordering
    question as REMAINS OPEN after its transfer to the native path — closing it
    here is open-question closure, not ratified-policy amendment. The ratified
    ``_10_graph_cascade`` end-hook (extensions/python/_functions/plugins/_memory/
    helpers/memory/Memory/delete_documents_by_ids/end/_10_graph_cascade.py) owns
    the sidecar cascade strictly AFTER confirmed deletion; its contract
    (fires only on delete_documents_by_ids, never re-raises, logged-only) is
    preserved untouched. Failure asymmetry (KI-011 fix): framework-delete
    failure -> sidecars intact; success-then-crash-before-hook -> orphaned
    (recoverable) edges — never loss of live memories' edges.
    """
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
                # WI-P12-SCORE-AUTHORITY (S1, ARC C2): ScoreStore remains the
                # single writer of record for access_count. The former FAISS
                # metadata mirror mutation is removed per ADR-NC1-002
                # boundary 4. Non-fatal warning contract preserved below.
                store.update_access(doc_id)
    except Exception as e:
        log.warning(f"[neuro_core] access tracking non-fatal: {e}")
