"""Neuro Core ``ContextGraph`` — structured retrieval return type.

A ``ContextGraph`` is what the agent sees when a query triggers graph-aware
retrieval. Instead of a flat list of documents, the agent gets:

- ``nodes``: the documents that participated in the answer, each tagged
  with the hop distance from the original semantic seed.
- ``edges``: the typed relationships (``GraphEdge``) that connect those
  nodes.
- ``seed_ids``: the document ids that started the search (the semantic
  hits from ``Memory.search_similarity_threshold``).
- ``query``: the original user query, preserved for traceability.

The ``to_prompt_text()`` serializer renders this as LLM-readable text with
hop grouping so the model can reason about distance and provenance.

``GraphEdge`` is re-exported here so callers can import both the node and
edge types from a single module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from usr.plugins.neuro_core.helpers.graph_store import GraphEdge


# Re-export for convenience so callers can write
# ``from usr.plugins.neuro_core.helpers.context_graph import GraphEdge``.
__all__ = ["GraphNode", "GraphEdge", "ContextGraph"]


# ---------------------------------------------------------------------------
# GraphNode — a single document in a ContextGraph
# ---------------------------------------------------------------------------


@dataclass
class GraphNode:
    """A document participating in a ``ContextGraph``.

    Attributes:
        doc_id: The memory document id (matches ``Document.metadata['id']``).
        content: The textual content (``page_content``) of the document.
        metadata: The document's full metadata dict (read-only by contract).
        score: The final ranking score after importance-weighted re-ranking.
            In ``[0.0, 1.0]`` but not strictly clamped (re-rank weights can
            sum above 1.0 in pathological cases).
        hop: The graph distance from the nearest seed. ``0`` = seed (a
            direct semantic hit), ``1`` = one edge away, ``2`` = two
            edges, etc.
    """

    doc_id: str
    content: str
    metadata: dict = field(default_factory=dict)
    score: float = 0.0
    hop: int = 0


# ---------------------------------------------------------------------------
# ContextGraph — the top-level retrieval result
# ---------------------------------------------------------------------------


@dataclass
class ContextGraph:
    """Structured retrieval result returned by ``search_context_graph``.

    Attributes:
        nodes: All documents in the answer, including seeds. May be empty
            if the search produced no hits.
        edges: All typed relationships (``GraphEdge``) between the
            returned nodes. Edges that touch nodes not in ``nodes`` are
            still included if they touch at least one node in the graph.
        query: The original user query string.
        seed_ids: The document ids that started the graph expansion —
            the raw semantic hits before any graph traversal.
    """

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    query: str = ""
    seed_ids: list[str] = field(default_factory=list)

    # ---- Helpers ----------------------------------------------------------

    def get_node(self, doc_id: str) -> GraphNode | None:
        """Return the node for ``doc_id`` or ``None`` if not present."""
        for n in self.nodes:
            if n.doc_id == doc_id:
                return n
        return None

    def edges_for(self, doc_id: str) -> list[GraphEdge]:
        """Return all edges that touch ``doc_id`` (in either direction)."""
        return [e for e in self.edges if e.from_id == doc_id or e.to_id == doc_id]

    # ---- Serialization ----------------------------------------------------

    def to_prompt_text(self) -> str:
        """Render the graph as LLM-readable text.

        Layout:
            1. A header line with the query and counts.
            2. Nodes grouped by hop distance, in hop-ascending order.
               Within each hop group, nodes are ordered by descending
               score.
            3. An "Edges" section listing every relationship among the
               returned nodes.

        The format is deliberately plain-text so it slots into the
        agent's standard ``<context>...</context>`` injection without
        any parsing.
        """
        if not self.nodes and not self.edges:
            return (
                f"# ContextGraph\n"
                f"query: {self.query!r}\n"
                f"(no nodes or edges found)\n"
            )

        # Bucket nodes by hop, then sort each bucket by descending score.
        by_hop: dict[int, list[GraphNode]] = {}
        for n in self.nodes:
            by_hop.setdefault(n.hop, []).append(n)
        for hop in by_hop:
            by_hop[hop].sort(key=lambda node: node.score, reverse=True)

        # Collect node ids so we only mention edges that actually touch
        # the returned graph.
        node_ids = {n.doc_id for n in self.nodes}
        visible_edges = [
            e for e in self.edges
            if e.from_id in node_ids or e.to_id in node_ids
        ]

        lines: list[str] = []
        lines.append("# ContextGraph")
        lines.append(
            f"query: {self.query!r} | nodes: {len(self.nodes)} | "
            f"edges: {len(visible_edges)} | "
            f"seeds: {len(self.seed_ids)}"
        )
        lines.append("")

        # --- Nodes by hop --------------------------------------------------
        lines.append("## Nodes")
        for hop in sorted(by_hop):
            label = "seed (semantic hit)" if hop == 0 else f"hop {hop}"
            lines.append(f"### {label} [{len(by_hop[hop])}]")
            for n in by_hop[hop]:
                meta_preview = _format_meta_preview(n.metadata)
                content_preview = _truncate(n.content or "", 280)
                lines.append(
                    f"- [{n.doc_id}] score={n.score:.3f} | {meta_preview}"
                )
                lines.append(f"    {content_preview}")
        lines.append("")

        # --- Edges ----------------------------------------------------------
        if visible_edges:
            lines.append("## Edges")
            for e in visible_edges:
                lines.append(
                    f"- {e.from_id} --{e.type}--> {e.to_id} "
                    f"(confidence={e.confidence:.2f}, source={e.source})"
                )
        else:
            lines.append("## Edges")
            lines.append("(no typed relationships among the returned nodes)")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal formatting helpers
# ---------------------------------------------------------------------------


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def _format_meta_preview(meta: dict | None) -> str:
    """Compact one-line summary of the most relevant metadata fields."""
    if not meta:
        return "(no metadata)"
    keys_of_interest = (
        "memory_type",
        "importance",
        "confidence",
        "validation_status",
        "task_status",
    )
    parts: list[str] = []
    for k in keys_of_interest:
        if k in meta:
            parts.append(f"{k}={meta[k]}")
    return ", ".join(parts) if parts else "(no typed fields)"
