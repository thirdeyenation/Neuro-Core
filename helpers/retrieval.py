"""Neuro Core hybrid retrieval — semantic seed + graph expansion + re-rank.

``search_context_graph()`` is the primary retrieval entry point for the
Neuro Core plugin. It implements the four-step pipeline described in
``NEURO_CORE_SPEC.md §8``:

  1. **Semantic seed**  
     ``Memory.search_similarity_threshold`` returns the initial hit
     documents — these are the *seeds* (hop 0).
  2. **Graph expansion**  
     For each seed, ``GraphStore.neighbors`` walks the
     ``relationships.json`` adjacency up to ``config.graph_max_hops``,
     capped at ``config.graph_neighbors_max`` total nodes so the result
     stays bounded for prompt context windows.
  3. **Importance-weighted re-ranking**  
     Each candidate node is rescored as
     ``similarity_w * sem + importance_w * imp + recency_w * rec``,
     with weights from config (defaults ``0.5 / 0.3 / 0.2``).
  4. **Assembly**  
     Returns a ``ContextGraph`` with nodes, edges, the original query,
     and the seed ids (for transparency in the LLM prompt).

This module deliberately does not do the recall-time side effects
(``access_count`` / ``last_accessed_at`` updates). Those are wired in
via a separate ``_functions`` extension on
``Memory.search_similarity_threshold/end`` so that unit tests for the
retrieval algorithm stay free of side effects.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

from usr.plugins.neuro_core.helpers.context_graph import (
    ContextGraph,
    GraphEdge,
    GraphNode,
)

if TYPE_CHECKING:  # pragma: no cover - import-only for type hints
    from plugins._memory.helpers.memory import Memory
    from usr.plugins.neuro_core.helpers.graph_store import GraphStore
    from usr.plugins.neuro_core.helpers.scores import ScoreStore


# ---------------------------------------------------------------------------
# Config accessor helpers
# ---------------------------------------------------------------------------


def _cfg(config: Any, key: str, default: float | int) -> float | int:
    """Read a config value with a default.

    The ``config`` argument is a plain dict (or a ``PluginConfig``-style
    object that supports ``.get``). We try attribute, ``get``, and dict
    lookup in that order so the function is flexible about its input.
    """
    if config is None:
        return default
    if hasattr(config, key) and not callable(getattr(config, key, None)):
        return getattr(config, key)
    if hasattr(config, "get"):
        try:
            v = config.get(key)
        except Exception:
            v = None
        if v is not None:
            return v
    if isinstance(config, dict):
        v = config.get(key)
        if v is not None:
            return v
    return default


# ---------------------------------------------------------------------------
# Recency scoring
# ---------------------------------------------------------------------------


def _recency_score(iso_ts: str | None) -> float:
    """Return a recency score in ``[0.0, 1.0]``.

    Uses an exponential decay with a half-life of 7 days. A timestamp
    from "now" returns ``1.0``; a week old returns ``0.5``; a month old
    returns ~``0.15``. Missing or unparseable timestamps return ``0.5``
    (neutral).
    """
    if not iso_ts:
        return 0.5
    try:
        # Python's fromisoformat handles "Z" only since 3.11, so normalize.
        ts = iso_ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return 0.5
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    half_life_seconds = 7 * 24 * 3600.0
    # 2^(-age/half_life)
    return math.pow(0.5, age_seconds / half_life_seconds)


# ---------------------------------------------------------------------------
# Node helpers
# ---------------------------------------------------------------------------


def _doc_id(doc: Any) -> str:
    """Extract a stable id from a Document-like object."""
    meta = getattr(doc, "metadata", None) or {}
    return (
        meta.get("id")
        or meta.get("doc_id")
        or getattr(doc, "id", None)
        or str(getattr(doc, "page_content", ""))[:32]
    )


def _importance_for(doc_id: str, doc: Any, score_store: Any) -> float:
    """Read the importance score from the sidecar; fall back to metadata."""
    if score_store is not None:
        try:
            rec = score_store.get(doc_id)
            if rec is not None:
                return float(rec.importance)
        except Exception:
            pass
    meta = getattr(doc, "metadata", None) or {}
    imp = meta.get("importance")
    if isinstance(imp, (int, float)):
        return float(imp)
    return 0.5


def _semantic_for(doc: Any, default: float = 1.0) -> float:
    """Extract a semantic score if the doc carries one; else default."""
    meta = getattr(doc, "metadata", None) or {}
    for key in ("semantic_score", "similarity", "score"):
        v = meta.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return default


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def search_context_graph(
    memory: "Memory",
    query: str,
    graph_store: "GraphStore | None",
    score_store: "ScoreStore | None",
    config: Any = None,
) -> ContextGraph:
    """Run the four-step hybrid retrieval pipeline.

    Args:
        memory: An initialized ``Memory`` wrapper (e.g. from
            ``await Memory.get(agent)``).
        query: The user query string.
        graph_store: The ``GraphStore`` for the active ``memory_subdir``.
            May be ``None`` — graph expansion is then skipped but the
            semantic seeds are still returned.
        score_store: The ``ScoreStore`` for the active ``memory_subdir``.
            May be ``None`` — importance falls back to metadata.
        config: A ``PluginConfig``-style or plain-dict config object.
            Recognized keys: ``graph_max_hops``, ``graph_neighbors_max``,
            ``similarity_weight``, ``importance_weight``, ``recency_weight``,
            ``semantic_limit``, ``semantic_threshold``.

    Returns:
        A ``ContextGraph`` (possibly with an empty ``nodes`` list if the
        semantic search returned nothing).
    """
    max_hops = int(_cfg(config, "graph_max_hops", 2))
    max_nodes = int(_cfg(config, "graph_neighbors_max", 40))
    sim_w = float(_cfg(config, "similarity_weight", 0.5))
    imp_w = float(_cfg(config, "importance_weight", 0.3))
    rec_w = float(_cfg(config, "recency_weight", 0.2))
    sem_limit = int(_cfg(config, "semantic_limit", 10))
    sem_threshold = float(_cfg(config, "semantic_threshold", 0.5))

    # --- Step 1: semantic seed ------------------------------------------
    seed_docs: list[Any] = []
    try:
        seed_docs = await memory.search_similarity_threshold(
            query=query,
            limit=sem_limit,
            threshold=sem_threshold,
        )
    except Exception:
        # Memory failures should never crash retrieval; fall back to empty.
        seed_docs = []

    seed_ids: list[str] = [_doc_id(d) for d in seed_docs]

    # Build seed nodes (hop 0)
    nodes_by_id: dict[str, GraphNode] = {}
    for d in seed_docs:
        did = _doc_id(d)
        if did in nodes_by_id:
            continue
        sem = _semantic_for(d)
        imp = _importance_for(did, d, score_store)
        meta = getattr(d, "metadata", None) or {}
        rec = _recency_score(
            meta.get("last_accessed_at") or meta.get("timestamp")
        )
        score = sim_w * sem + imp_w * imp + rec_w * rec
        nodes_by_id[did] = GraphNode(
            doc_id=did,
            content=getattr(d, "page_content", "") or "",
            metadata=dict(meta),
            score=score,
            hop=0,
        )

    edges_by_key: dict[tuple[str, str, str], GraphEdge] = {}

    # --- Step 2: graph expansion ----------------------------------------
    if graph_store is not None and seed_ids and max_hops > 0:
        # BFS through the neighbor frontier, capped at ``max_nodes``
        # *additional* graph neighbors beyond the seed set (seeds are
        # always retained regardless of capacity). This matches the
        # ``graph_neighbors_max`` config semantics: "how many neighbors
        # may I add to my seed set?", not "what is the total graph size?".
        remaining_capacity = max_nodes
        if remaining_capacity > 0:
            neighbor_lists = graph_store.neighbors(
                from_id=seed_ids,
                hops=max_hops,
            )
            # Flatten into (target_id, hop, edge) tuples.
            flat: list[tuple[str, int, GraphEdge]] = []
            for nl in neighbor_lists:
                for target_id, hop, edge in nl:
                    flat.append((target_id, hop, edge))

            # Sort by hop asc, then by edge confidence desc, so the best
            # neighbors win the capacity battle.
            flat.sort(key=lambda t: (t[1], -float(t[2].confidence)))

            for target_id, hop, edge in flat:
                # Register the edge (dedup by from/to/type).
                key = (edge.from_id, edge.to_id, edge.type)
                if key not in edges_by_key:
                    edges_by_key[key] = edge

                if target_id in nodes_by_id:
                    continue
                if remaining_capacity <= 0:
                    # Still record the edge (we already did) but skip
                    # adding the node — the graph is already full.
                    continue

                # Resolve the neighbor's Document. We may not have the
                # actual content if the seed came back without it; try
                # to fetch it from the FAISS store.
                content = ""
                meta: dict = {}
                try:
                    fetched = memory.db.get_by_ids([target_id])
                    if fetched:
                        content = getattr(fetched[0], "page_content", "") or ""
                        meta = dict(getattr(fetched[0], "metadata", None) or {})
                except Exception:
                    pass

                imp = _importance_for(target_id, None, score_store)
                rec = _recency_score(
                    meta.get("last_accessed_at") or meta.get("timestamp")
                )
                # No semantic score for a graph-only neighbor: assume
                # a moderate baseline so the re-rank weights still
                # differentiate. The importance and recency terms
                # dominate the score in that case.
                sem_baseline = 0.5
                score = sim_w * sem_baseline + imp_w * imp + rec_w * rec

                nodes_by_id[target_id] = GraphNode(
                    doc_id=target_id,
                    content=content,
                    metadata=meta,
                    score=score,
                    hop=hop,
                )
                remaining_capacity -= 1

    # --- Step 3 (re-rank is folded into Step 1+2) -----------------------
    # Re-rank by descending score so the prompt serializer renders the
    # most relevant nodes first within each hop bucket.
    sorted_nodes = sorted(
        nodes_by_id.values(), key=lambda n: (-n.score, n.doc_id)
    )

    return ContextGraph(
        nodes=sorted_nodes,
        edges=list(edges_by_key.values()),
        query=query,
        seed_ids=seed_ids,
    )
