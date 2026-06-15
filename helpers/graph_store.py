"""Neuro Core graph sidecar (relationships.json).

This module implements the on-disk adjacency store that records typed
relationships between memory documents. The store is keyed by
``memory_subdir`` (one ``relationships.json`` per FAISS index) and is
loaded into memory on first access.

Concurrency:
    All reads and writes go through a per-subdir ``threading.RLock`` so
    concurrent agents do not corrupt the file. The lock is acquired
    with an explicit ``timeout=5.0`` deadline; a bare
    ``with self._lock:`` can deadlock if the calling thread already
    holds the lock and an exception prevents release. The
    ``_locked(timeout=...)`` helper below centralises the
    acquire/try/finally/release pattern so every critical section
    has consistent deadlock protection.

Persistence:
    Writes are atomic — the new JSON is written to a temp file in the same
    directory and renamed with ``os.replace`` so partial writes never leave
    a corrupt file on disk.

Schema (adjacency list keyed by source memory ID):

    {
        "from_memory_id_1": [
            {
                "to_id": "memory_id_2",
                "type": "supports",
                "confidence": 0.85,
                "source": "agent",
                "created_at": "2026-05-31T10:00:00Z"
            },
            ...
        ],
        ...
    }
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator, Optional, Union


# ---------------------------------------------------------------------------
# Lock timeout configuration
# ---------------------------------------------------------------------------

# All GraphStore RLocks are acquired with this deadline. If a thread cannot
# acquire the lock within 5 seconds we raise TimeoutError rather than
# blocking indefinitely, which prevents deadlock when a caller already
# holds the lock and an exception prevents release.
_LOCK_TIMEOUT_SECONDS = 5.0


# ---------------------------------------------------------------------------
# RelationshipType enum (8 values per NEURO_CORE_SPEC.md §5.1)
# ---------------------------------------------------------------------------


class RelationshipType(str, Enum):
    """Typed graph relationships between memory documents (v1 — 8 values)."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    DEPENDS_ON = "depends_on"
    DERIVED_FROM = "derived_from"
    RELATED_TO = "related_to"
    PRECEDES = "precedes"
    FOLLOWS = "follows"
    PART_OF = "part_of"


VALID_RELATIONSHIP_TYPES: frozenset[str] = frozenset(r.value for r in RelationshipType)


# ---------------------------------------------------------------------------
# GraphEdge dataclass
# ---------------------------------------------------------------------------


@dataclass
class GraphEdge:
    """A single directed edge in the memory relationship graph."""

    from_id: str
    to_id: str
    type: str
    confidence: float = 0.7
    source: str = "agent"
    weight: float = 1.0
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        # Clamp confidence to [0.0, 1.0] and validate the type token.
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if self.type not in VALID_RELATIONSHIP_TYPES:
            raise ValueError(
                f"Invalid relationship type {self.type!r}; must be one of "
                f"{sorted(VALID_RELATIONSHIP_TYPES)}"
            )
        if not self.from_id or not self.to_id:
            raise ValueError("GraphEdge requires non-empty from_id and to_id")
        if self.from_id == self.to_id:
            raise ValueError("GraphEdge cannot self-loop (from_id == to_id)")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, from_id: str, raw: dict) -> "GraphEdge":
        return cls(
            from_id=from_id,
            to_id=str(raw["to_id"]),
            type=str(raw["type"]),
            confidence=float(raw.get("confidence", 0.7)),
            source=str(raw.get("source", "agent")),
            weight=float(raw.get("weight", 1.0)),
            created_at=str(raw.get("created_at", "")),
        )


# ---------------------------------------------------------------------------
# GraphStore — adjacency list with atomic JSON persistence
# ---------------------------------------------------------------------------


# Per-subdir lock registry. We use a module-level dict so the lock survives
# across multiple GraphStore instances (one per active memory_subdir).
_LOCKS: dict[str, "threading.RLock"] = {}
_LOCKS_GUARD = threading.Lock()


def _get_lock(memory_subdir: str) -> "threading.RLock":
    if memory_subdir not in _LOCKS:
        with _LOCKS_GUARD:
            if memory_subdir not in _LOCKS:
                _LOCKS[memory_subdir] = threading.RLock()
    return _LOCKS[memory_subdir]


def _relationships_path(memory_subdir: str) -> str:
    """Return the on-disk path to ``relationships.json`` for a subdir."""
    # Lazy import: abs_db_dir lives inside the _memory plugin and we want
    # to keep this module importable even if the framework rewires it later.
    from plugins._memory.helpers.memory import Memory, abs_db_dir

    return os.path.join(abs_db_dir(memory_subdir), "relationships.json")


class GraphStore:
    """Adjacency-list store for memory-to-memory relationships.

    One instance is bound to a single ``memory_subdir``; the instance
    caches the in-memory adjacency dict and re-reads it on first access.
    """

    def __init__(self, memory_subdir: str) -> None:
        self.memory_subdir = memory_subdir
        self._lock = _get_lock(memory_subdir)
        self._path = _relationships_path(memory_subdir)
        self._adj: dict[str, list[dict]] = {}
        self._loaded = False

    # ---- Lock helper ------------------------------------------------------

    @contextlib.contextmanager
    def _locked(
        self, timeout: float = _LOCK_TIMEOUT_SECONDS
    ) -> Iterator[None]:
        """Acquire ``self._lock`` with an explicit timeout deadline.

        All critical sections in this class MUST use this helper
        instead of ``with self._lock:``. A bare ``with`` can deadlock
        if the caller already holds the lock and an exception
        prevents release (the finally clause is reached, but a
        subsequent re-acquire in the same call stack blocks
        forever). The timeout acquire raises ``TimeoutError``
        after 5 seconds so the caller can fail fast and the
        scheduler can keep ticking.
        """
        if not self._lock.acquire(timeout=timeout):
            raise TimeoutError(
                f"[neuro_core] GraphStore lock timed out after {timeout}s"
            )
        try:
            yield
        finally:
            self._lock.release()

    # ---- I/O --------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._locked():
            if self._loaded:
                return
            self._adj = self._read_file()
            self._loaded = True

    def _read_file(self) -> dict[str, list[dict]]:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable file: start fresh, do not crash.
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _atomic_write(self, data: dict[str, list[dict]]) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(self._path) or ".",
            prefix=".relationships.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def save(self) -> None:
        """Persist the current adjacency dict to disk atomically."""
        with self._locked():
            self._atomic_write(self._adj)

    def load(self) -> dict[str, list[dict]]:
        """Re-read the on-disk file (discarding the in-memory cache)."""
        with self._locked():
            self._adj = self._read_file()
            self._loaded = True
            return dict(self._adj)

    # ---- Edge operations --------------------------------------------------

    def add_edge(self, edge: GraphEdge) -> None:
        """Add or update an edge in the adjacency list.

        D25 deduplication: if an edge with the same (from_id, to_id, type)
        already exists, the existing entry is updated in place (weight,
        confidence, created_at, source) rather than appending a duplicate.
        Different (to_id, type) combinations are kept as separate edges.
        """
        if not isinstance(edge, GraphEdge):
            raise TypeError("add_edge() requires a GraphEdge instance")
        self._ensure_loaded()
        with self._locked():
            data = self._adj
            bucket = data.setdefault(edge.from_id, [])
            edge_dict = edge.to_dict()
            # D25: deduplicate by (to_id, type) — update in place if found
            existing = [
                e for e in bucket
                if e.get("to_id") == edge.to_id and e.get("type") == edge.type
            ]
            if existing:
                existing[0].update(edge_dict)
            else:
                bucket.append(edge_dict)
            self._atomic_write(data)

    def remove_edges_for_id(self, memory_id: str) -> int:
        """Cascade-delete every edge touching ``memory_id``.

        Removes any entry where ``memory_id`` appears as either ``from_id``
        (the adjacency key) or as ``to_id`` inside another bucket. Returns
        the total number of edges removed.

        Used by the ``_10_graph_cascade`` extension when a memory is
        deleted via ``Memory.delete_documents_by_ids``.
        """
        if not memory_id:
            return 0
        self._ensure_loaded()
        with self._locked():
            removed = 0

            # 1. Drop the bucket whose key is memory_id (outgoing edges).
            if memory_id in self._adj:
                removed += len(self._adj.pop(memory_id, []))

            # 2. Walk every other bucket and drop edges where to_id == memory_id.
            for from_id, edges in list(self._adj.items()):
                keep: list[dict] = []
                for raw in edges:
                    if raw.get("to_id") == memory_id:
                        removed += 1
                    else:
                        keep.append(raw)
                if len(keep) != len(edges):
                    if keep:
                        self._adj[from_id] = keep
                    else:
                        self._adj.pop(from_id, None)

            if removed:
                self._atomic_write(self._adj)
            return removed

    def get_edges(
        self, from_id: Optional[str] = None
    ) -> Union[list[GraphEdge], dict[str, list[GraphEdge]]]:
        """Return edges from the adjacency store.

        With ``from_id`` (the historical signature) — return a list of
        ``GraphEdge`` for that single source node.

        With no ``from_id`` — return the full adjacency map:
        ``{source_id: [GraphEdge, ...]}``. This is what the analytics
        layer (``run_graph_analytics``) and any "give me everything"
        caller needs.
        """
        self._ensure_loaded()
        with self._locked():
            if from_id is None:
                return {
                    src: [GraphEdge.from_dict(src, raw) for raw in raw_list]
                    for src, raw_list in self._adj.items()
                }
            raw_list = list(self._adj.get(from_id, []))
        return [GraphEdge.from_dict(from_id, raw) for raw in raw_list]

    def neighbors(
        self,
        from_id: str | list[str],
        max_hops: Optional[int] = None,
        rel_type: Optional[str] = None,
        hops: Optional[int] = None,
    ) -> list[tuple[str, int, GraphEdge]]:
        """BFS from ``from_id`` (or each id in ``from_id``) up to ``max_hops``.

        Returns a list of ``(neighbor_id, hop, triggering_edge)`` tuples.
        ``rel_type`` filters by ``RelationshipType`` value when provided.
        When ``from_id`` is a ``list``, BFS is run from each seed and the
        results are merged (deduped via the visited set across seeds).

        ``hops`` is accepted as a backward-compatible alias for ``max_hops``
        (used by earlier callers and the context_graph API). If both are
        provided, ``max_hops`` wins.
        """
        effective_hops = max_hops if max_hops is not None else (hops if hops is not None else 1)
        if effective_hops < 0:
            return []
        if effective_hops == 0:
            return []

        # Normalize to a list of seeds so the BFS body can iterate.
        seed_ids: list[str] = [from_id] if isinstance(from_id, str) else list(from_id)
        if not seed_ids:
            return []

        self._ensure_loaded()

        visited: set[str] = set(seed_ids)
        frontier: list[tuple[str, int, GraphEdge]] = []
        out: list[tuple[str, int, GraphEdge]] = []

        with self._locked():
            snapshot = {
                fid: [GraphEdge.from_dict(fid, raw) for raw in edges]
                for fid, edges in self._adj.items()
            }

        for hop in range(1, effective_hops + 1):
            # ``frontier`` entries are ``(node_id, hop, edge)`` 3-tuples
            # (see the ``next_frontier.append`` below) — so the unpack
            # MUST match that arity exactly. The previous ``(e, _)``
            # unpacking caused ``too many values to unpack (expected 2)``.
            current_ids = (
                [node_id for (node_id, _, _) in frontier]
                if frontier
                else list(seed_ids)
            )
            if hop == 1:
                current_ids = list(seed_ids)
            next_frontier: list[tuple[str, int, GraphEdge]] = []
            for node in current_ids:
                for edge in snapshot.get(node, []):
                    if rel_type is not None and edge.type != rel_type:
                        continue
                    if edge.to_id in visited:
                        continue
                    visited.add(edge.to_id)
                    entry = (edge.to_id, hop, edge)
                    out.append(entry)
                    next_frontier.append((edge.to_id, hop, edge))
            frontier = next_frontier
            if not frontier:
                break

        return out

    # ---- Introspection ----------------------------------------------------

    def all_edges(self) -> list[GraphEdge]:
        """Return every edge in the store (debug / graph analytics)."""
        self._ensure_loaded()
        with self._locked():
            return [
                GraphEdge.from_dict(from_id, raw)
                for from_id, edges in self._adj.items()
                for raw in edges
            ]

    def __len__(self) -> int:
        self._ensure_loaded()
        with self._locked():
            return sum(len(v) for v in self._adj.values())
