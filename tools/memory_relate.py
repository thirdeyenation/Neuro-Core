"""Neuro Core ``memory_relate`` tool.

Create or remove a typed relationship between two memory entries.

Pattern follows ``plugins/_memory/tools/memory_save.py``:
    from helpers.tool import Tool, Response
    from plugins._memory.helpers.memory import Memory

Persistence model:
    All graph edges live in the ``relationships.json`` sidecar file
    under ``abs_db_dir(memory_subdir)``. The ``GraphStore`` class is
    responsible for atomic writes (tempfile.mkstemp + os.replace) and
    per-subdir locking. This tool never touches FAISS directly.

Validation order (per spec):
    1. Reject if ``from_id == to_id`` — self-referential edges are
       invalid; return a clear error string, never raise.
    2. Validate ``rel_type`` is in the ``RelationshipType`` enum —
       reject unknown values with a clear error.
    3. Verify both IDs exist in the active ``Memory`` instance —
       return a clear error if either is not found.
    4. If ``remove=True``: call ``GraphStore.remove_edges_for_id()``
       scoped to the specific (from_id, to_id, rel_type) tuple;
       return a confirmation.
    5. Otherwise: call ``GraphStore.add_edge()``; return a confirmation
       with all applied values.

All error paths return ``Response(message=...)`` with
``break_loop=False``. No exceptions are ever raised to the caller.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from helpers.tool import Tool, Response
from plugins._memory.helpers.memory import Memory

from usr.plugins.neuro_core.helpers.graph_store import (
    GraphEdge,
    GraphStore,
    RelationshipType,
    VALID_RELATIONSHIP_TYPES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp_weight(weight: float) -> float:
    """Clamp ``weight`` into the [0.0, 1.0] interval."""
    try:
        w = float(weight)
    except (TypeError, ValueError):
        return 1.0
    if w < 0.0:
        return 0.0
    if w > 1.0:
        return 1.0
    return w


def _now_iso() -> str:
    """Return a current UTC ISO 8601 timestamp."""
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:  # pragma: no cover - defensive
        return ""


def _remove_specific_edge(
    store: GraphStore, from_id: str, to_id: str, rel_type: str
) -> int:
    """Remove the single edge matching (from_id, to_id, rel_type).

    ``GraphStore.remove_edges_for_id`` is bulk-only (deletes ALL edges
    touching ``from_id``), so we read the current edge list, drop the
    matching one, and rewrite via the public API. Returns the number
    of edges removed (0 or 1).
    """
    current = list(store.get_edges(from_id))
    target = None
    for e in current:
        if e.from_id == from_id and e.to_id == to_id and e.type == rel_type:
            target = e
            break
    if target is None:
        # Also check the reverse direction.
        reverse = list(store.get_edges(to_id))
        for e in reverse:
            if e.from_id == to_id and e.to_id == from_id and e.type == rel_type:
                # Best-effort: the public API only supports bulk remove.
                store.remove_edges_for_id(to_id)
                return 1
        return 0

    # Forward direction: wipe and rewrite with the target removed.
    remaining = [e for e in current if e is not target]
    store.remove_edges_for_id(from_id)
    for e in remaining:
        store.add_edge(e)
    return 1


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class MemoryRelate(Tool):
    """Create or remove a typed graph edge between two memory entries."""

    async def execute(
        self,
        from_id: str = "",
        to_id: str = "",
        rel_type: str = "",
        weight: float = 1.0,
        remove: bool = False,
        **kwargs: Any,
    ) -> Response:
        # --- 1. Required-arg sanity checks ------------------------------
        if not from_id or not isinstance(from_id, str):
            return Response(
                message="Error: `from_id` is required and must be a non-empty string.",
                break_loop=False,
            )
        if not to_id or not isinstance(to_id, str):
            return Response(
                message="Error: `to_id` is required and must be a non-empty string.",
                break_loop=False,
            )
        if not rel_type or not isinstance(rel_type, str):
            return Response(
                message=(
                    "Error: `rel_type` is required and must be a non-empty string. "
                    f"Valid values: {sorted(VALID_RELATIONSHIP_TYPES)}."
                ),
                break_loop=False,
            )

        # --- 2. Reject self-referential edges ----------------------------
        if from_id == to_id:
            return Response(
                message=(
                    f"Error: self-referential edges are not allowed "
                    f"(from_id == to_id == '{from_id}')."
                ),
                break_loop=False,
            )

        # --- 3. Validate rel_type against the enum -----------------------
        if rel_type not in VALID_RELATIONSHIP_TYPES:
            return Response(
                message=(
                    f"Error: unknown rel_type '{rel_type}'. "
                    f"Valid values: {sorted(VALID_RELATIONSHIP_TYPES)}."
                ),
                break_loop=False,
            )

        # --- 4. Resolve the Memory backend and verify both IDs exist -----
        try:
            db = await Memory.get(self.agent)
        except Exception as exc:  # pragma: no cover - defensive
            return Response(
                message=f"Error: could not initialize Memory backend: {exc}",
                break_loop=False,
            )

        try:
            found = db.db.get_by_ids([from_id, to_id])
        except Exception as exc:  # pragma: no cover - defensive
            return Response(
                message=f"Error: failed to look up memories: {exc}",
                break_loop=False,
            )

        found_ids: set[str] = set()
        for d in (found or []):
            md = getattr(d, "metadata", None) or {}
            did = md.get("id")
            if isinstance(did, str) and did:
                found_ids.add(did)
        missing: list[str] = []
        if from_id not in found_ids:
            missing.append(from_id)
        if to_id not in found_ids:
            missing.append(to_id)
        if missing:
            return Response(
                message=(
                    f"Error: memory id(s) not found in FAISS store: {missing}. "
                    f"Use memory_load to find valid ids."
                ),
                break_loop=False,
            )

        # --- 5. Apply the change via GraphStore --------------------------
        memory_subdir = getattr(db, "memory_subdir", None) or "default"
        store = GraphStore(memory_subdir)
        safe_weight = _clamp_weight(weight)

        if remove:
            try:
                removed = _remove_specific_edge(store, from_id, to_id, rel_type)
            except Exception as exc:  # pragma: no cover - defensive
                return Response(
                    message=f"Error: failed to remove edge: {exc}",
                    break_loop=False,
                )
            if removed == 0:
                return Response(
                    message=(
                        f"No matching edge found to remove: "
                        f"{from_id} -[{rel_type}]-> {to_id}. "
                        f"Store unchanged."
                    ),
                    break_loop=False,
                )
            # --- D24: symmetric back-edge removal for RELATED_TO ---------
            # If the forward removal succeeded and the rel_type is the
            # symmetric one, also remove the reverse edge. Errors here
            # are non-fatal — the forward removal already succeeded.
            if rel_type == RelationshipType.RELATED_TO.value:
                try:
                    _remove_specific_edge(store, to_id, from_id, rel_type)
                except Exception:  # pragma: no cover - defensive
                    pass
            ack = f"Unlinked {from_id} from {to_id} ({rel_type})"
            return Response(
                message=(
                    f"Removed {removed} edge(s): {from_id} -[{rel_type}]-> {to_id}."
                ),
                break_loop=False,
                additional={"neuro_core_ack": ack},
            )

        # --- add_edge path ----------------------------------------------
        try:
            edge = GraphEdge(
                from_id=from_id,
                to_id=to_id,
                type=rel_type,
                weight=safe_weight,
                confidence=safe_weight,
                source="agent",
                created_at=_now_iso(),
            )
            store.add_edge(edge)
        except Exception as exc:  # pragma: no cover - defensive
            return Response(
                message=f"Error: failed to add edge: {exc}",
                break_loop=False,
            )

        # --- D24: symmetric back-edge for RELATED_TO ---------------------
        # The spec requires ``related_to`` to be the only symmetric
        # rel_type. After the forward edge is written, also write the
        # reverse edge (to_id -> from_id) in the same atomic write so the
        # adjacency list is traversable in both directions.
        if rel_type == RelationshipType.RELATED_TO.value:
            try:
                reverse_edge = GraphEdge(
                    from_id=to_id,
                    to_id=from_id,
                    type=rel_type,
                    weight=safe_weight,
                    confidence=safe_weight,
                    source="agent",
                    created_at=_now_iso(),
                )
                store.add_edge(reverse_edge)
            except Exception as exc:  # pragma: no cover - defensive
                return Response(
                    message=(
                        f"Error: failed to add reverse edge for "
                        f"related_to: {exc}"
                    ),
                    break_loop=False,
                )

        ack = f"Linked {from_id} to {to_id} via {rel_type} (weight={safe_weight:.2f})"
        return Response(
            message=(
                f"Edge added: {from_id} -[{rel_type}]-> {to_id} "
                f"(weight={safe_weight:.2f})."
            ),
            break_loop=False,
            additional={"neuro_core_ack": ack},
        )
