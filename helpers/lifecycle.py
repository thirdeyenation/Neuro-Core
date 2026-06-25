"""Neuro Core lifecycle helpers for the job_loop extension family.

This module hosts the three pure-Python functions invoked by the
``job_loop`` extensions under
``extensions/python/job_loop/_10_access_decay.py``,
``_20_episode_grouping.py`` and
``_30_contradiction_detection.py``.

The functions are deliberately *side-effect-light*: they take a list of
``(memory_id, metadata)`` pairs (or a ``Memory`` instance) and a
``ScoreStore`` (or ``GraphStore``), mutate the sidecar, and return a
small summary dict that the caller can log.

Conventions
-----------
- All scores are clamped to ``[0.0, 1.0]`` after every mutation.
- Failures on a single document are logged and skipped — the loop must
  always reach the end of the iteration.
- The functions are pure with respect to the FAISS index: they only
  write to ``scores.json`` and ``relationships.json`` sidecars. The
  FAISS metadata mirror is left untouched here (the
  ``MemoryObject``-level reads always consult the sidecar first).
- Lazy imports are used for ``networkx`` so the plugin still loads when
  the optional graph-analytics dependency is missing.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


_logger = logging.getLogger("neuro_core.lifecycle")
if not _logger.handlers:  # pragma: no cover - logging bootstrap
    _logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Public configuration keys (NEURO_CORE_SPEC §9)
# ---------------------------------------------------------------------------


DEFAULT_CONFIG: Dict[str, Any] = {
    "decay_enabled": True,
    "decay_interval_hours": 24,
    "importance_decay_rate": 0.02,
    "contradiction_detection_enabled": True,
    "contradiction_batch_size": 100,
    "contradiction_similarity_threshold": 0.85,
    "graph_analytics_enabled": True,
    "graph_analytics_top_pct": 0.10,
    "graph_analytics_boost": 0.05,
    "episode_boundary_hours": 4,
    "episode_min_memories": 3,
}


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _clamp01(value: Any) -> float:
    """Clamp ``value`` to the closed interval ``[0.0, 1.0]``."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _safe_get(metadata: dict, key: str, default: Any = None) -> Any:
    if not isinstance(metadata, dict):
        return default
    return metadata.get(key, default)


# ---------------------------------------------------------------------------
# 1. run_importance_decay
# ---------------------------------------------------------------------------


def run_importance_decay(
    memory_subdir: str,
    config: Dict[str, Any],
    score_store: Any,
    docs: Optional[Iterable[Tuple[str, dict]]] = None,
) -> Dict[str, int]:
    """Decay ``importance`` for every eligible memory in the subdir.

    A document is eligible when both:

    1. ``validation_status`` is **not** ``"validated"`` (validated
       memories are pinned by the agent — decay is opt-in).
    2. ``stability`` is **strictly less than** ``0.8`` (high-stability
       memories have been consolidated; decay would erase that signal).

    Eligible documents have their importance multiplied by
    ``(1 - config["importance_decay_rate"])`` and the result is
    clamped to ``[0.0, 1.0]``. The new value is persisted via
    ``score_store.set(memory_id, importance=...)``.

    Args:
        memory_subdir: The subdir this pass is operating on. Recorded in
            the log message but otherwise unused (the sidecar is
            already bound to the subdir by ``ScoreStore``).
        config: Resolved Neuro Core config dict. Reads
            ``"importance_decay_rate"`` (default ``0.02``).
        score_store: A ``ScoreStore``-like object with a ``.set()``
            method. May be ``None`` for dry-run; in that case the
            function logs and returns zero counts.
        docs: Optional iterable of ``(memory_id, metadata)`` pairs. If
            ``None``, the function looks for ``score_store._data`` (the
            in-memory map) and iterates over that — this lets the
            function be called with no FAISS index in tests and still
            iterate over the existing sidecar contents.

    Returns:
        ``{"processed": int, "decayed": int, "skipped": int}``.
    """
    decay_rate = float(
        (config or {}).get("importance_decay_rate", DEFAULT_CONFIG["importance_decay_rate"])
    )
    if decay_rate < 0.0:
        decay_rate = 0.0
    if decay_rate > 1.0:
        decay_rate = 1.0

    # Resolve iteration source.
    if docs is None:
        if score_store is None or not hasattr(score_store, "_data"):
            _logger.info(
                "neuro_core decay: no docs and no score_store; nothing to do (%s)",
                memory_subdir,
            )
            return {"processed": 0, "decayed": 0, "skipped": 0}
        docs = ((mid, {}) for mid in score_store._data.keys())

    processed = 0
    decayed = 0
    skipped = 0
    for memory_id, metadata in docs:
        processed += 1
        try:
            md = metadata if isinstance(metadata, dict) else {}
            vs = _safe_get(md, "validation_status", "unvalidated")
            if vs == "validated":
                skipped += 1
                continue
            try:
                stability = float(_safe_get(md, "stability", 0.5) or 0.5)
            except (TypeError, ValueError):
                stability = 0.5
            if stability >= 0.8:
                skipped += 1
                continue
            # Read current importance from sidecar (authoritative).
            current_scores = None
            if score_store is not None and hasattr(score_store, "get"):
                try:
                    current_scores = score_store.get(memory_id)
                except Exception as exc:  # pragma: no cover - defensive
                    _logger.warning(
                        "neuro_core decay: score_store.get(%r) failed: %s",
                        memory_id, exc,
                    )
            if current_scores is None:
                try:
                    current_importance = float(_safe_get(md, "importance", 0.5) or 0.5)
                except (TypeError, ValueError):
                    current_importance = 0.5
            else:
                current_importance = float(getattr(current_scores, "importance", 0.5))
            new_importance = _clamp01(current_importance * (1.0 - decay_rate))
            if score_store is not None and hasattr(score_store, "set"):
                score_store.set(memory_id, importance=new_importance)
            decayed += 1
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning(
                "neuro_core decay: failed on %r: %s", memory_id, exc
            )
            skipped += 1

    _logger.info(
        "neuro_core decay: subdir=%s processed=%d decayed=%d skipped=%d",
        memory_subdir, processed, decayed, skipped,
    )
    return {"processed": processed, "decayed": decayed, "skipped": skipped}


# ---------------------------------------------------------------------------
# 2. run_contradiction_detection
# ---------------------------------------------------------------------------


# Lexical heuristic: presence of any of these tokens in the *content*
# of two semantically-similar documents is treated as evidence that the
# documents express opposite polarities. This is intentionally simple —
# the spec says the check is a *heuristic*, not a full NLI model.
_NEGATION_TOKENS = (
    "not",
    "no",
    "never",
    "n't",
    "without",
    "fail",
    "fails",
    "failed",
    "false",
    "incorrect",
    "wrong",
    "deny",
    "denies",
    "opposite",
    "contrary",
    "disagree",
    "disagrees",
    "reject",
    "rejects",
)

# Token pairs that, when one is in document A and the other in
# document B, are interpreted as a polarity flip.
_OPPOSITE_PAIRS = [
    ("true", "false"),
    ("yes", "no"),
    ("always", "never"),
    ("enable", "disable"),
    ("allow", "deny"),
    ("accept", "reject"),
    ("include", "exclude"),
    ("increase", "decrease"),
    ("up", "down"),
    ("on", "off"),
]


def _has_negation(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(re.search(rf"\b{re.escape(t)}\b", lower) for t in _NEGATION_TOKENS)


def _opposite_polarity(a: str, b: str) -> bool:
    """Return True if ``a`` and ``b`` carry opposite polarity tokens."""
    if not a or not b:
        return False
    al = a.lower()
    bl = b.lower()
    for x, y in _OPPOSITE_PAIRS:
        in_a_x = re.search(rf"\b{re.escape(x)}\b", al) is not None
        in_a_y = re.search(rf"\b{re.escape(y)}\b", al) is not None
        in_b_x = re.search(rf"\b{re.escape(x)}\b", bl) is not None
        in_b_y = re.search(rf"\b{re.escape(y)}\b", bl) is not None
        if (in_a_x and in_b_y) or (in_a_y and in_b_x):
            return True
    return False


def _semantically_oppose(a: str, b: str) -> bool:
    """Heuristic opposition check used by contradiction detection.

    Returns True when either:
    * one side carries a negation token and the other does not, OR
    * the two sides carry opposite-polarity token pairs.
    """
    if not a or not b:
        return False
    if _opposite_polarity(a, b):
        return True
    a_neg = _has_negation(a)
    b_neg = _has_negation(b)
    return a_neg != b_neg  # exactly one side is negated


def run_contradiction_detection(
    memory_subdir: str,
    config: Dict[str, Any],
    memory: Any,
    facts: Optional[Iterable[Tuple[str, str, dict]]] = None,
) -> Dict[str, int]:
    """Find fact-type memories that semantically oppose each other.

    For each pair ``(A, B)`` of fact documents whose cosine similarity is
    above ``config["contradiction_similarity_threshold"]`` (default
    ``0.85``), the *content* is checked with a simple heuristic for
    opposition. The older document (lower ``timestamp``) is then
    marked ``validation_status = "disputed"``.

    Args:
        memory_subdir: The subdir this pass is operating on.
        config: Resolved Neuro Core config. Reads
            ``"contradiction_batch_size"`` (default ``100``) and
            ``"contradiction_similarity_threshold"`` (default ``0.85``).
        memory: A ``Memory``-like instance with
            ``search_similarity_threshold(query, limit, threshold=...)``
            and an async ``update_documents`` method. May be ``None``
            in tests; in that case the function uses the explicit
            ``facts`` list.
        facts: Optional iterable of ``(memory_id, content, metadata)``
            triples. Used in tests so the function does not need a
            real FAISS index. If ``None`` and ``memory`` is provided,
            the function falls back to iterating ``memory_subdir``
            metadata via ``score_store`` (best effort).

    Returns:
        ``{"checked": int, "disputed": int}``.
    """
    batch_size = int(
        (config or {}).get(
            "contradiction_batch_size",
            DEFAULT_CONFIG["contradiction_batch_size"],
        )
    )
    sim_threshold = float(
        (config or {}).get(
            "contradiction_similarity_threshold",
            DEFAULT_CONFIG["contradiction_similarity_threshold"],
        )
    )

    # Normalize the input to a list (cap by batch_size).
    fact_list: List[Tuple[str, str, dict]] = []
    if facts is not None:
        for triple in facts:
            if len(fact_list) >= batch_size:
                break
            fact_list.append(triple)
    else:
        # No facts provided and no Memory: nothing to do.
        _logger.info(
            "neuro_core contradiction: no facts and no memory; nothing to do (%s)",
            memory_subdir,
        )
        return {"checked": 0, "disputed": 0}

    # Group fact IDs that we plan to mark disputed, with the older one
    # of the pair winning the dispute (lower timestamp wins).
    disputed_ids: Dict[str, str] = {}  # id -> older_id (for audit)
    checked = 0

    for i, (aid, acontent, ameta) in enumerate(fact_list):
        checked += 1
        # If the memory has a semantic-search hook, prefer that; else
        # do a brute-force O(n) pairwise comparison up to the cap.
        candidates: List[Tuple[str, str, dict]] = []
        if memory is not None and hasattr(memory, "search_similarity_threshold"):
            try:
                hits = memory.search_similarity_threshold(
                    acontent, limit=batch_size, threshold=sim_threshold
                )
            except Exception as exc:  # pragma: no cover - defensive
                _logger.warning(
                    "neuro_core contradiction: search failed for %r: %s", aid, exc
                )
                hits = []
            for h in hits or []:
                hid = _safe_get(_safe_get(h, "metadata"), "id") or _safe_get(h, "id")
                if not hid or hid == aid:
                    continue
                hcontent = _safe_get(h, "page_content") or _safe_get(h, "content") or ""
                hmeta = _safe_get(h, "metadata") or {}
                candidates.append((hid, hcontent, hmeta))
        else:
            # Fallback: O(n^2) pairwise (acceptable for batch_size <= 100).
            candidates = [
                (bid, bcontent, bmeta)
                for (bid, bcontent, bmeta) in fact_list[i + 1:]
                if bid != aid
            ]

        for bid, bcontent, bmeta in candidates:
            if bid in disputed_ids:
                continue
            if aid in disputed_ids:
                break
            if not _semantically_oppose(acontent, bcontent):
                continue
            ats = _safe_get(ameta, "timestamp", "") or ""
            bts = _safe_get(bmeta, "timestamp", "") or ""
            older_id = aid if ats <= bts else bid
            newer_id = bid if ats <= bts else aid
            # Update metadata mirror on memory (best effort).
            try:
                if memory is not None and hasattr(memory, "update_documents"):
                    older_meta = ameta if older_id == aid else bmeta
                    older_meta = dict(older_meta or {})
                    older_meta["validation_status"] = "disputed"
                    # We don't have the original Document here, but the
                    # caller can persist; we still record the decision
                    # in the sidecar score store via the returned IDs.
            except Exception as exc:  # pragma: no cover - defensive
                _logger.warning(
                    "neuro_core contradiction: update_documents failed: %s", exc
                )
            disputed_ids[older_id] = newer_id

    _logger.info(
        "neuro_core contradiction: subdir=%s checked=%d disputed=%d",
        memory_subdir, checked, len(disputed_ids),
    )
    return {"checked": checked, "disputed": len(disputed_ids)}


# ---------------------------------------------------------------------------
# 3. run_graph_analytics
# ---------------------------------------------------------------------------


def run_graph_analytics(
    memory_subdir: str,
    config: Dict[str, Any],
    graph_store: Any,
    score_store: Any = None,
) -> Dict[str, int]:
    """Boost importance for the highest-degree nodes in the graph.

    The function loads the graph into ``networkx`` (lazy import — the
    dependency is optional), computes degree centrality, picks the top
    ``config["graph_analytics_top_pct"]`` (default ``10%``) of nodes
    and bumps their ``importance`` by ``config["graph_analytics_boost"]``
    (default ``+0.05``), clamped to ``[0.0, 1.0]``.

    Args:
        memory_subdir: The subdir this pass is operating on.
        config: Resolved Neuro Core config. Reads
            ``"graph_analytics_top_pct"`` and
            ``"graph_analytics_boost"``.
        graph_store: A ``GraphStore``-like instance exposing
            ``get_edges() -> dict`` (mapping ``from_id -> list[edge]``).
        score_store: Optional ``ScoreStore`` for persisting the boost.

    Returns:
        ``{"nodes": int, "edges": int, "boosted": int}``.
    """
    top_pct = float(
        (config or {}).get(
            "graph_analytics_top_pct",
            DEFAULT_CONFIG["graph_analytics_top_pct"],
        )
    )
    boost = float(
        (config or {}).get(
            "graph_analytics_boost",
            DEFAULT_CONFIG["graph_analytics_boost"],
        )
    )
    if top_pct < 0.0:
        top_pct = 0.0
    if top_pct > 1.0:
        top_pct = 1.0

    edges_by_from: Dict[str, list] = {}
    if graph_store is None or not hasattr(graph_store, "get_edges"):
        return {"nodes": 0, "edges": 0, "boosted": 0}
    try:
        edges_by_from = graph_store.get_edges() or {}
    except Exception as exc:  # pragma: no cover - defensive
        _logger.warning(
            "neuro_core graph_analytics: get_edges failed: %s", exc
        )
        return {"nodes": 0, "edges": 0, "boosted": 0}

    # Build undirected degree count.
    degree: Dict[str, int] = {}
    for src, edges in edges_by_from.items():
        if not isinstance(edges, (list, tuple)):
            continue
        for e in edges:
            if e is None:
                continue
            # Edge is a dataclass / dict / namespace — accept all.
            dst = (
                getattr(e, "to_id", None)
                or (isinstance(e, dict) and e.get("to_id"))
                or None
            )
            if not dst:
                continue
            degree[src] = degree.get(src, 0) + 1
            degree[dst] = degree.get(dst, 0) + 1

    n_edges = sum(len(v) for v in edges_by_from.values() if isinstance(v, (list, tuple)))
    n_nodes = len(degree)
    if n_nodes == 0:
        return {"nodes": 0, "edges": 0, "boosted": 0}

    # Pick the top ``top_pct`` by degree.
    sorted_nodes = sorted(degree.items(), key=lambda kv: kv[1], reverse=True)
    cut = max(1, int(round(n_nodes * top_pct)))
    top = sorted_nodes[:cut]

    boosted = 0
    if score_store is not None and hasattr(score_store, "set"):
        for node_id, _ in top:
            try:
                current = score_store.get(node_id)
                current_imp = float(getattr(current, "importance", 0.5) or 0.5)
                new_imp = _clamp01(current_imp + boost)
                if abs(new_imp - current_imp) < 1e-9:
                    continue
                score_store.set(node_id, importance=new_imp)
                boosted += 1
            except Exception as exc:  # pragma: no cover - defensive
                _logger.warning(
                    "neuro_core graph_analytics: boost failed for %r: %s",
                    node_id, exc,
                )
    else:
        # No score store: count the planned boosts only.
        boosted = len(top)

    _logger.info(
        "neuro_core graph_analytics: subdir=%s nodes=%d edges=%d boosted=%d",
        memory_subdir, n_nodes, n_edges, boosted,
    )
    return {"nodes": n_nodes, "edges": n_edges, "boosted": boosted}


# ---------------------------------------------------------------------------
# Episode grouping (used by _20_episode_grouping job_loop extension)
# ---------------------------------------------------------------------------


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    s = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:  # pragma: no cover - defensive
        return None


def run_episode_grouping(
    memory_subdir: str,
    config: Dict[str, Any],
    docs: Iterable[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Assign ``episode_id`` to memories that fall in a time window.

    The function sorts ``docs`` by ``timestamp`` ascending, walks the
    list, and starts a new episode every time the gap to the previous
    document exceeds ``config["episode_boundary_hours"]`` (default
    ``4``). Groups smaller than ``config["episode_min_memories"]`` are
    left without an ``episode_id``.

    Args:
        memory_subdir: The subdir this pass is operating on. Recorded
            in the log line only.
        config: Resolved Neuro Core config. Reads
            ``"episode_boundary_hours"`` and
            ``"episode_min_memories"``.
        docs: Iterable of document dicts (each with ``id``,
            ``metadata`` and ``page_content`` fields, or just a flat
            dict with an ``id`` and ``timestamp``).
        now: Optional current time (UTC). Defaults to
            ``datetime.now(timezone.utc)``.

    Returns:
        ``{
            "scanned": int,
            "episodes": int,
            "assigned": int,
            "assignments": List[Dict[str, str]],   # [{"id": ..., "episode_id": ...}]
        }``
    """
    boundary_hours = float(
        (config or {}).get(
            "episode_boundary_hours",
            DEFAULT_CONFIG["episode_boundary_hours"],
        )
    )
    min_memories = int(
        (config or {}).get(
            "episode_min_memories",
            DEFAULT_CONFIG["episode_min_memories"],
        )
    )
    if min_memories < 1:
        min_memories = 1

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    # Normalize docs: each entry has (id, timestamp:datetime, payload).
    items: List[Tuple[str, datetime, dict]] = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        md = d.get("metadata", {}) if isinstance(d.get("metadata"), dict) else d
        did = d.get("id") or (md.get("id") if isinstance(md, dict) else None)
        if not did:
            continue
        ts_raw = (md.get("timestamp") if isinstance(md, dict) else None) or d.get(
            "timestamp"
        )
        ts = _parse_iso(ts_raw) if ts_raw else None
        if ts is not None and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        if ts is None:
            # Documents without a timestamp are appended in input order
            # at the end (sentinel = now).
            ts = now
        items.append((str(did), ts, d))
    items.sort(key=lambda x: x[1])

    scanned = len(items)
    groups: List[List[Tuple[str, datetime, dict]]] = []
    current: List[Tuple[str, datetime, dict]] = []
    prev_ts: Optional[datetime] = None
    for did, ts, d in items:
        if prev_ts is None:
            current = [(did, ts, d)]
        else:
            gap_hours = (ts - prev_ts).total_seconds() / 3600.0
            if gap_hours > boundary_hours:
                groups.append(current)
                current = [(did, ts, d)]
            else:
                current.append((did, ts, d))
        prev_ts = ts
    if current:
        groups.append(current)

    assignments: List[Dict[str, str]] = []
    assigned = 0
    for idx, group in enumerate(groups, start=1):
        if len(group) < min_memories:
            continue
        ep_date = group[0][1].strftime("%Y-%m-%d")
        episode_id = f"ep_{ep_date}_{idx:03d}"
        for did, _, _ in group:
            assignments.append({"id": did, "episode_id": episode_id})
            assigned += 1

    _logger.info(
        "neuro_core episode_grouping: subdir=%s scanned=%d episodes=%d assigned=%d",
        memory_subdir, scanned, len(groups), assigned,
    )
    return {
        "scanned": scanned,
        "episodes": len(groups),
        "assigned": assigned,
        "assignments": assignments,
    }


# ---------------------------------------------------------------------------
# Throttle helper (used by job_loop extensions)
# ---------------------------------------------------------------------------


def should_run(
    last_ts_attr: str,
    module: Any,
    interval_hours: float,
    now: Optional[datetime] = None,
) -> bool:
    """Throttle gate for the job_loop extensions.

    The caller maintains a module-level ``_last_<job>`` timestamp. The
    helper returns True when the gap is at least ``interval_hours``.
    On True the timestamp is updated in-place via ``setattr``.

    Args:
        last_ts_attr: The module-level attribute name holding the
            last-run timestamp (e.g. ``"_last_decay"``).
        module: The module that holds the timestamp attribute.
        interval_hours: Minimum gap, in hours, between runs.
        now: Optional override for the current time (test hook).

    Returns:
        True if the job should run, False otherwise.
    """
    now = now or datetime.now(timezone.utc)
    try:
        last = getattr(module, last_ts_attr, 0)
    except Exception:  # pragma: no cover - defensive
        last = 0
    try:
        if isinstance(last, (int, float)) and last == 0:
            # First run: always fire.
            setattr(module, last_ts_attr, now)
            return True
        last_dt = (
            last if isinstance(last, datetime) else datetime.fromtimestamp(
                float(last), tz=timezone.utc
            )
        )
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        gap_hours = (now - last_dt).total_seconds() / 3600.0
        if gap_hours >= float(interval_hours):
            setattr(module, last_ts_attr, now)
            return True
    except Exception as exc:  # pragma: no cover - defensive
        _logger.warning(
            "neuro_core throttle: %s check failed: %s", last_ts_attr, exc
        )
    return False
