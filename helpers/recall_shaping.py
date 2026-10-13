"""Neuro Core Phase-1 recall shaping (WI-P43-PHASE1-RECALL-SHAPING).

Implements ADR-NC1-003 (recall-shaping policy), approved by HITL as proposed
(D-NC1-106) under ARC conditions C1-C7 of WI-P42-TWOSTORE-RECALL's
steward-design-decision.

Role: end-hook shaping of the framework host Memory search methods'
delivered recall. Neuro Core intelligence — sidecar-authoritative
importance/confidence (C5), validation-status demotion (annotation, NOT
gating, C12), recency, and bounded additive graph-neighbor expansion —
re-ranks the native search results. The native set is never dropped,
filtered, or thresholded; shaping only reorders and (bounded) appends.

Safety envelope (binding):
- C1: exception-safe. Every public entry point never re-raises and never
  sets ``data['exception']``. Any failure preserves the native result.
- C2: config-gated via ``recall_shaping_enabled`` (default false). Gate
  off => callers leave ``data['result']`` untouched => byte-identical
  baseline behavior (pinned by test).
- C3: exact return shapes preserved. Plain path returns ``list[Document]``;
  with-scores path returns ``list[tuple[Document, float]]`` with the NATIVE
  relevance score kept verbatim in the tuple — the shaped score lives only
  in ``neuro_factors.shaped_score``.
- C5: sidecar authority, read-side only. Scores come from ScoreStore
  (scores.json); the FAISS-metadata copy is a fallback, never authoritative
  (ADR-NC1-002). No write-side boundary is touched.
- No shared-object mutation: delivered Documents are shallow copies with a
  fresh metadata dict; original docstore objects are never touched
  (framework callers hold shared references; V1 write-back hazard).
- V2 (D-NC1-106 directive, position (b) PREVENTED): expanded neighbors are
  marked ``neuro_neighbor: True`` and are EXCLUDED from the access-tracking
  handlers' inputs, so graph proximity never inflates importance/decay
  access signals. Neighbor deliveries are recorded distinctly in the
  shaping telemetry sidecar only.

Telemetry: per-subdir ``shaping_telemetry.json`` sibling of scores.json /
relationships.json. ``_locked()`` / atomic-write concurrency pattern,
exception-safe, ZERO writes when the gate is off, and NEVER persists
memory text or query text (standing NC1 telemetry constraint).
"""

from __future__ import annotations

import copy as _copy
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("neuro_core.recall_shaping")

# Internal marker keys (delivered-document metadata, shallow copies only).
NEURO_SHAPED = "neuro_shaped"
NEURO_FACTORS = "neuro_factors"
NEURO_DEGRADED = "neuro_degraded"
NEURO_NEIGHBOR = "neuro_neighbor"

# Validation-status demotion map (ADR-NC1-003 §1 vf). NOTE: the ADR table
# originally named the lowest status "superseded"; the plugin's real
# ValidationStatus vocabulary (helpers/metadata.py:56-62) is "deprecated".
# The factor semantics are bound to the real enum value; the naming
# correction is recorded in ADR-NC1-003 "Amendment record (2026-09-28)"
# item 2 and the implementation report.
_VALIDATION_FACTOR_KEYS = {
    "validated": "validation_factor_validated",
    "unvalidated": "validation_factor_unvalidated",
    "disputed": "validation_factor_disputed",
    "deprecated": "validation_factor_deprecated",
}
_VALIDATION_FACTOR_DEFAULTS = {
    "validation_factor_validated": 1.0,
    "validation_factor_unvalidated": 0.8,
    "validation_factor_disputed": 0.6,
    "validation_factor_deprecated": 0.4,
}

_TELEMETRY_LOCKS: dict[str, threading.Lock] = {}
_TELEMETRY_LOCKS_GUARD = threading.Lock()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _plugin_config() -> dict:
    """Resolved plugin config as a plain dict (framework settings chain)."""
    try:
        from helpers.plugins import get_plugin_config

        cfg = get_plugin_config("neuro_core")
        if isinstance(cfg, dict):
            return cfg
    except Exception:  # pragma: no cover - defensive
        pass
    return {}


def shaping_enabled() -> bool:
    """C2 gate: ``recall_shaping_enabled``, default FALSE (D-NC1-106)."""
    return bool(_plugin_config().get("recall_shaping_enabled", False))


def _cfg_float(cfg: dict, key: str, default: float) -> float:
    try:
        v = cfg.get(key, default)
        return float(default if v is None else v)
    except (TypeError, ValueError):
        return default


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    try:
        v = cfg.get(key, default)
        return int(default if v is None else v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Telemetry sidecar (per-subdir shaping_telemetry.json)
# ---------------------------------------------------------------------------


def _telemetry_lock(memory_subdir: str) -> threading.Lock:
    if memory_subdir not in _TELEMETRY_LOCKS:
        with _TELEMETRY_LOCKS_GUARD:
            if memory_subdir not in _TELEMETRY_LOCKS:
                _TELEMETRY_LOCKS[memory_subdir] = threading.Lock()
    return _TELEMETRY_LOCKS[memory_subdir]


def _telemetry_path(memory_subdir: str) -> str:
    from plugins._memory.helpers.memory import abs_db_dir

    return os.path.join(abs_db_dir(memory_subdir), "shaping_telemetry.json")


def _atomic_write_json(path: str, payload: dict) -> None:
    """Atomic write: temp file in the same directory, then os.replace."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def record_shaping_event(memory_subdir: str, event: dict) -> None:
    """Append a bounded shaping event to the telemetry sidecar.

    Exception-safe and non-fatal (C1): any failure is swallowed and logged.
    NEVER persists memory text or query text — callers must pass only
    counters, ids, factor summaries, exception class names, and gate state.
    """
    try:
        lock = _telemetry_lock(memory_subdir)
        path = _telemetry_path(memory_subdir)
        event = dict(event)
        event["ts"] = datetime.now(timezone.utc).isoformat()
        with lock:
            data: dict = {"events": []}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        loaded = json.load(fh)
                    if isinstance(loaded, dict) and isinstance(loaded.get("events"), list):
                        data = loaded
                except Exception as exc:
                    log.warning("neuro_core shaping telemetry read failed: %s", exc)
            events = data["events"]
            events.append(event)
            # Bounded growth: keep the most recent 200 events.
            data["events"] = events[-200:]
            _atomic_write_json(path, data)
    except Exception as exc:
        log.warning("neuro_core shaping telemetry write failed: %s", exc)


# ---------------------------------------------------------------------------
# Factor computation (reuses helpers/retrieval.py factor helpers, C5)
# ---------------------------------------------------------------------------


def _confidence_for(doc_id: str, doc: Any, score_store: Any) -> tuple[float, bool]:
    """Sidecar-authoritative confidence; metadata fallback (0.7) + degraded.

    Mirrors ``helpers/retrieval.py:_importance_for`` semantics: sidecar
    record present -> ``rec.confidence``; read failure or legacy absence ->
    metadata fallback with an explicit degraded marker (KI-008 honesty).
    Metadata confidence default 0.7 per helpers/metadata.py:258-260.
    """
    meta = getattr(doc, "metadata", None) or {}
    if score_store is not None:
        try:
            if hasattr(score_store, "get_optional"):
                rec = score_store.get_optional(doc_id)
            else:
                rec = score_store.get(doc_id)
        except Exception as exc:
            log.warning("neuro_core shaping: score sidecar read failed for %r: %s", doc_id, exc)
            rec = None
            degraded = True
        else:
            degraded = rec is None
        if rec is not None:
            try:
                return max(0.0, min(1.0, float(rec.confidence))), False
            except (TypeError, ValueError):
                pass
    else:
        degraded = True
    conf = meta.get("confidence")
    if isinstance(conf, (int, float)):
        return max(0.0, min(1.0, float(conf))), degraded
    return 0.7, degraded


def _validation_factor(meta: dict, cfg: dict) -> tuple[float, str]:
    """Demotion factor for a validation status (annotation, NOT gating).

    Missing/unknown status takes the ``unvalidated`` factor (ADR: default
    seeding is ``unvalidated`` at capture). Returns (factor, status_used).
    """
    status = meta.get("validation_status")
    if not isinstance(status, str) or status not in _VALIDATION_FACTOR_KEYS:
        status = "unvalidated"
    key = _VALIDATION_FACTOR_KEYS[status]
    return _cfg_float(cfg, key, _VALIDATION_FACTOR_DEFAULTS[key]), status


def _factor_record(doc: Any, native_sem: float | None, score_store: Any, cfg: dict) -> tuple[dict, bool]:
    """Compute the full factor record for one document.

    Returns ``(factors, degraded)``. ``native_sem`` is the normalized
    semantic factor when the caller has a native relevance score
    (with-scores path); otherwise metadata keys are consulted with a 0.5
    default (plain path — constant across the result, order-neutral).
    """
    from usr.plugins.neuro_core.helpers.retrieval import (
        _doc_id,
        _importance_for,
        _recency_score,
        _semantic_for,
    )

    doc_id = _doc_id(doc)
    meta = getattr(doc, "metadata", None) or {}
    degraded = False

    if native_sem is not None:
        sem = max(0.0, min(1.0, float(native_sem)))
    else:
        sem = max(0.0, min(1.0, _semantic_for(doc, default=0.5)))

    imp, imp_deg = _importance_for(doc_id, doc, score_store)
    degraded = degraded or imp_deg
    conf, conf_deg = _confidence_for(doc_id, doc, score_store)
    degraded = degraded or conf_deg

    rec = _recency_score(meta.get("last_accessed_at") or meta.get("timestamp"))
    vf, status = _validation_factor(meta, cfg)

    sim_w = _cfg_float(cfg, "similarity_weight", 0.5)
    imp_w = _cfg_float(cfg, "importance_weight", 0.3)
    rec_w = _cfg_float(cfg, "recency_weight", 0.2)

    shaped = (sim_w * sem + imp_w * (imp * conf) + rec_w * rec) * vf
    factors = {
        "similarity": round(sem, 6),
        "importance": round(imp, 6),
        "confidence": round(conf, 6),
        "recency": round(rec, 6),
        "validation_status": status,
        "validation_factor": round(vf, 6),
        "shaped_score": round(max(0.0, min(1.0, shaped)), 6),
    }
    return factors, degraded


def _shallow_copy(doc: Any, factors: dict, degraded: bool, neighbor: bool) -> Any:
    """Shallow-copy a Document with explanation markers; originals untouched."""
    clone = _copy.copy(doc)
    meta = dict(getattr(doc, "metadata", None) or {})
    meta[NEURO_SHAPED] = True
    meta[NEURO_FACTORS] = factors
    if degraded:
        meta[NEURO_DEGRADED] = True
    if neighbor:
        meta[NEURO_NEIGHBOR] = True
    clone.metadata = meta
    return clone


# ---------------------------------------------------------------------------
# Graph-neighbor expansion (bounded, additive-only)
# ---------------------------------------------------------------------------


def _expand_neighbors(memory_instance: Any, seed_ids: list[str], existing: set[str], cfg: dict) -> tuple[list[Any], bool]:
    """Materialize up to ``recall_shaping_neighbors_max`` graph neighbors.

    Returns ``(neighbor_docs, degraded)``. Neighbors are sorted by
    ``(hop asc, edge.confidence desc)`` (pipeline parity), capped, fetched
    via the framework's own ``Memory.get_document_by_id`` (sync,
    memory.py:337), and NEVER displace native results. Any failure degrades
    (no neighbors, degraded marker) instead of breaking recall (C1).
    """
    cap = _cfg_int(cfg, "recall_shaping_neighbors_max", 3)
    if cap <= 0 or not seed_ids:
        return [], False
    try:
        from usr.plugins.neuro_core.helpers.graph_store import GraphStore

        subdir = getattr(memory_instance, "memory_subdir", None)
        if not isinstance(subdir, str) or not subdir:
            subdir = "default"
        graph_store = GraphStore(subdir)
        hops = _cfg_int(cfg, "graph_max_hops", 2)
        triples = graph_store.neighbors(from_id=list(seed_ids), hops=hops)
        triples.sort(key=lambda t: (t[1], -float(getattr(t[2], "confidence", 0.0))))
        docs: list[Any] = []
        for target_id, _hop, _edge in triples:
            if len(docs) >= cap:
                break
            if target_id in existing:
                continue
            doc = memory_instance.get_document_by_id(target_id)
            if doc is not None:
                docs.append(doc)
                existing.add(target_id)
        return docs, False
    except Exception as exc:
        log.warning("neuro_core shaping: graph expansion degraded: %s", exc)
        return [], True


# ---------------------------------------------------------------------------
# Core shaping
# ---------------------------------------------------------------------------


def _shape(memory_instance: Any, docs: list[Any], native_scores: dict[str, float] | None) -> list[Any] | None:
    """Shape a native result list. Returns the new list, or None to leave the
    native result untouched (gate off, empty/non-list input, or total failure).
    """
    if not shaping_enabled():
        return None
    if not isinstance(docs, (list, tuple)) or not docs:
        return None

    cfg = _plugin_config()
    try:
        from usr.plugins.neuro_core.helpers.scores import ScoreStore

        subdir = getattr(memory_instance, "memory_subdir", None)
        score_store = ScoreStore(subdir if isinstance(subdir, str) and subdir else "default")
    except Exception as exc:
        log.warning("neuro_core shaping: ScoreStore unavailable, degrading: %s", exc)
        score_store = None

    degraded_any = score_store is None
    entries: list[tuple[int, Any, dict, bool]] = []
    seed_ids: list[str] = []
    for idx, doc in enumerate(docs):
        try:
            from usr.plugins.neuro_core.helpers.retrieval import _doc_id

            doc_id = _doc_id(doc)
            seed_ids.append(doc_id)
            native_sem = None
            if native_scores is not None:
                raw = native_scores.get(doc_id)
                if raw is not None:
                    from plugins._memory.helpers.memory import Memory

                    native_sem = Memory._cosine_normalizer(float(raw))
            factors, degraded = _factor_record(doc, native_sem, score_store, cfg)
            degraded_any = degraded_any or degraded
            entries.append((idx, doc, factors, degraded))
        except Exception as exc:
            # Per-document failure: keep the document in native position with
            # a neutral factor record and the degraded marker (KI-008).
            log.warning("neuro_core shaping: factor computation failed for one doc: %s", exc)
            factors = {
                "similarity": 0.5, "importance": 0.5, "confidence": 0.7,
                "recency": 0.5, "validation_status": "unvalidated",
                "validation_factor": _cfg_float(cfg, "validation_factor_unvalidated", 0.8),
                "shaped_score": 0.5,
            }
            entries.append((idx, doc, factors, True))
            degraded_any = True
            try:
                from usr.plugins.neuro_core.helpers.retrieval import _doc_id as _di
                seed_ids.append(_di(doc))
            except Exception:
                seed_ids.append("")

    # Stable re-rank: shaped score descending; equal scores keep native order.
    ranked = sorted(entries, key=lambda e: (-e[2]["shaped_score"], e[0]))
    delivered = [
        _shallow_copy(doc, factors, degraded, neighbor=False)
        for (_idx, doc, factors, degraded) in ranked
    ]

    # Bounded additive neighbor expansion (never displaces natives).
    neighbor_ids: list[str] = []
    existing = set(seed_ids)
    neighbor_docs, neighbors_degraded = _expand_neighbors(memory_instance, seed_ids, existing, cfg)
    degraded_any = degraded_any or neighbors_degraded
    if neighbor_docs:
        scored: list[tuple[float, int, Any]] = []
        for n_idx, ndoc in enumerate(neighbor_docs):
            try:
                factors, degraded = _factor_record(ndoc, 0.5, score_store, cfg)
                degraded_any = degraded_any or degraded
            except Exception:
                factors, degraded = {
                    "similarity": 0.5, "importance": 0.5, "confidence": 0.7,
                    "recency": 0.5, "validation_status": "unvalidated",
                    "validation_factor": 0.8, "shaped_score": 0.5,
                }, True
            scored.append((factors["shaped_score"], n_idx, (ndoc, factors, degraded)))
        scored.sort(key=lambda t: (-t[0], t[1]))
        for _s, _i, (ndoc, factors, degraded) in scored:
            delivered.append(_shallow_copy(ndoc, factors, degraded, neighbor=True))
            try:
                from usr.plugins.neuro_core.helpers.retrieval import _doc_id as _di
                neighbor_ids.append(_di(ndoc))
            except Exception:
                neighbor_ids.append("")

    # Telemetry (bounded; never memory/query text). Zero writes when off is
    # guaranteed because this code is only reached with the gate on.
    try:
        subdir = getattr(memory_instance, "memory_subdir", None) or "default"
        shaped_scores = [e[2]["shaped_score"] for e in entries]
        record_shaping_event(str(subdir), {
            "kind": "recall_shaped",
            "gate": True,
            "native_count": len(docs),
            "delivered_count": len(delivered),
            "delivered_ids": [i for i in seed_ids if i],
            "neighbor_ids_delivered": [i for i in neighbor_ids if i],
            "shaped_score_min": min(shaped_scores) if shaped_scores else None,
            "shaped_score_max": max(shaped_scores) if shaped_scores else None,
            "degraded": degraded_any,
        })
    except Exception as exc:
        log.warning("neuro_core shaping: telemetry failed (non-fatal): %s", exc)

    return delivered


def shape_plain(memory_instance: Any, result: Any) -> list[Any] | None:
    """Shape a plain ``search_similarity_threshold`` result (list[Document])."""
    try:
        if not isinstance(result, (list, tuple)):
            return None
        return _shape(memory_instance, list(result), None)
    except Exception as exc:
        log.warning("neuro_core shaping: plain-path failure, native result preserved: %s", exc)
        return None


def shape_with_scores(memory_instance: Any, result: Any) -> list[tuple[Any, float]] | None:
    """Shape a ``_with_scores`` result (list[tuple[Document, float]]).

    The tuple's native relevance score is preserved VERBATIM (C3); the
    shaped score lives only in ``neuro_factors.shaped_score``.
    """
    try:
        if not isinstance(result, (list, tuple)):
            return None
        pairs = [p for p in result if isinstance(p, tuple) and len(p) == 2]
        if not pairs:
            return None
        from usr.plugins.neuro_core.helpers.retrieval import _doc_id

        native_scores: dict[str, float] = {}
        for doc, score in pairs:
            try:
                native_scores[_doc_id(doc)] = float(score)
            except (TypeError, ValueError):
                pass
        # C3 hardening: if any document lacks a resolvable, unique id, skip
        # shaping entirely (native result preserved) rather than risk
        # mis-attaching native scores to the wrong document.
        if len(native_scores) != len(pairs) or None in native_scores:
            return None
        shaped_docs = _shape(memory_instance, [p[0] for p in pairs], native_scores)
        if shaped_docs is None:
            return None
        # Re-attach the ORIGINAL native score per delivered doc (verbatim).
        out: list[tuple[Any, float]] = []
        for doc in shaped_docs:
            doc_id = _doc_id(doc)
            out.append((doc, native_scores.get(doc_id, 0.0)))
        return out
    except Exception as exc:
        log.warning("neuro_core shaping: with-scores failure, native result preserved: %s", exc)
        return None
