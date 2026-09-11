"""Neuro Core ``relationships`` API handler.

Exposes relationship endpoints under ``/api/plugins/neuro_core/``:

* ``GET  /relationships?id=<memory_id>`` — list all edges for a memory ID.
* ``GET  /relationships`` — list all edges in the subdir.
* ``POST /relationships`` — add a new graph edge.
* ``DELETE /relationships`` — remove one graph edge, identified by the
  query params ``?memory_subdir=<sub>&from_id=<id>&to_id=<id>&rel_type=<type>``
  (query strings, NOT path segments — see the routing note above).

All endpoints require an authenticated session (cookie or API key).
Memory ID is passed as query param ``?id=<memory_id>`` — NOT as a path segment.
Framework routing splits on path.split("/", 2); path parameters must use query strings.
"""

from __future__ import annotations

import dataclasses
import enum
from datetime import datetime, timezone
from typing import Any

from helpers.api import ApiHandler, Request, Response
from usr.plugins.neuro_core.helpers.graph_store import (
    GraphEdge,
    GraphStore,
    VALID_RELATIONSHIP_TYPES,
)


class RelationshipsApi(ApiHandler):
    """REST surface for Neuro Core graph relationships."""

    @classmethod
    def requires_auth(cls) -> bool:
        return True

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET", "POST", "DELETE"]

    async def process(self, input: dict, request: Request) -> dict | Response:
        result: dict | Response
        try:
            method = (request.method or "GET").upper()
            if method == "GET" and (input.get("id") or request.args.get("id")):
                result = await self._get_relationships(input, request)
            elif method == "GET":
                result = await self._list_all_relationships(input, request)
            elif method == "POST":
                result = await self._post_relationship(input, request)
            elif method == "DELETE":
                result = await self._delete_relationship(input, request)
            else:
                result = {
                    "success": False,
                    "error": f"Unknown route: {method}",
                }
        except Exception as e:  # pragma: no cover - defensive top-level
            result = {"success": False, "error": str(e)}

        if isinstance(result, dict):
            return _enum_safe_value(result)
        return result

    async def _get_relationships(self, input: dict, request: Request) -> dict:
        try:
            memory_subdir = (input.get("memory_subdir") or request.args.get("memory_subdir") or "").strip()
            if not memory_subdir:
                return {"success": False, "error": "`memory_subdir` is required"}
            memory_id = (input.get("id") or request.args.get("id") or "").strip()
            if not memory_id:
                return {"success": False, "error": "`id` query param is required"}

            store = GraphStore(memory_subdir)
            outbound = [_serialize_edge(e) for e in store.get_edges(memory_id)]
            inbound_raw = store.neighbors(from_id=memory_id, hops=1)
            inbound: list[dict] = []
            for nl in inbound_raw:
                for _target, _hop, edge in nl:
                    if edge.to_id == memory_id:
                        inbound.append(_serialize_edge(edge))

            seen: set[tuple[str, str, str]] = set()
            deduped: list[dict] = []
            for e in outbound + inbound:
                key = (e["from_id"], e["to_id"], e["type"])
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(e)

            return _enum_safe_value({
                "success": True,
                "memory_id": memory_id,
                "memory_subdir": memory_subdir,
                "edges": deduped,
            })
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}

    async def _post_relationship(self, input: dict, request: Request) -> dict:
        try:
            memory_subdir = (input.get("memory_subdir") or "").strip()
            from_id = (input.get("from_id") or "").strip()
            to_id = (input.get("to_id") or "").strip()
            rel_type = (input.get("rel_type") or "").strip()
            weight = input.get("weight", 1.0)

            if not memory_subdir:
                return {"success": False, "error": "`memory_subdir` is required"}
            if not from_id or not to_id:
                return {"success": False, "error": "`from_id` and `to_id` are required"}
            if from_id == to_id:
                return {"success": False, "error": "self-referential edges are not allowed"}
            if rel_type not in VALID_RELATIONSHIP_TYPES:
                return {
                    "success": False,
                    "error": (
                        f"unknown rel_type '{rel_type}'. "
                        f"Valid: {sorted(VALID_RELATIONSHIP_TYPES)}"
                    ),
                }

            try:
                w = float(weight)
            except (TypeError, ValueError):
                w = 1.0
            w = max(0.0, min(1.0, w))

            store = GraphStore(memory_subdir)
            edge = GraphEdge(
                from_id=from_id,
                to_id=to_id,
                type=rel_type,
                weight=w,
                confidence=w,
                source="api",
                created_at=_now_iso(),
            )
            store.add_edge(edge)
            return {
                "success": True,
                "status": "ok",
                "from_id": from_id,
                "to_id": to_id,
                "rel_type": rel_type,
                "weight": w,
            }
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}

    async def _list_all_relationships(self, input: dict, request: Request) -> dict:
        try:
            memory_subdir = (
                input.get("memory_subdir")
                or request.args.get("memory_subdir")
                or ""
            ).strip()
            if not memory_subdir:
                return {"success": False, "error": "`memory_subdir` is required"}
            store = GraphStore(memory_subdir)
            # Full adjacency map: {source_id: [GraphEdge, ...]} — the public
            # no-arg form of get_edges() (helpers/graph_store.py). Do not
            # access private store internals here.
            adjacency = store.get_edges()
            flat: list[dict] = []
            for edges in adjacency.values():
                for e in edges:
                    flat.append(_serialize_edge(e))
            return _enum_safe_value({
                "success": True,
                "memory_subdir": memory_subdir,
                "edges": flat,
                "count": len(flat),
            })
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}


    async def _delete_relationship(self, input: dict, request: Request) -> dict:
        """DELETE: remove one edge identified by the (from_id, to_id,
        rel_type) triple, passed as query strings (not path segments —
        framework routing splits on path.split("/", 2)).

        Failure contract (ARC condition C2): every validation and store
        failure returns a structured {success: false, error: ...} — nothing
        is swallowed. A symmetric reverse-removal failure for related_to
        edges (D24 mirror) is reported in the response and does not fail the
        request. A 0-removal (no matching edge) returns structured
        not-found with no store write (ARC condition C3).
        """
        memory_subdir = (
            input.get("memory_subdir")
            or request.args.get("memory_subdir")
            or ""
        ).strip()
        from_id = (input.get("from_id") or request.args.get("from_id") or "").strip()
        to_id = (input.get("to_id") or request.args.get("to_id") or "").strip()
        rel_type = (input.get("rel_type") or request.args.get("rel_type") or "").strip()

        if not memory_subdir:
            return {"success": False, "error": "`memory_subdir` is required"}
        if not from_id or not to_id:
            return {"success": False, "error": "`from_id` and `to_id` are required"}
        if from_id == to_id:
            return {"success": False, "error": "self-referential edges are not allowed"}
        if rel_type not in VALID_RELATIONSHIP_TYPES:
            return {
                "success": False,
                "error": (
                    f"unknown rel_type '{rel_type}'. "
                    f"Valid: {sorted(VALID_RELATIONSHIP_TYPES)}"
                ),
            }

        store = GraphStore(memory_subdir)
        try:
            removed = store.remove_edge(from_id, to_id, rel_type)
        except Exception as e:
            # C2: store failures are surfaced, never swallowed.
            return {"success": False, "error": f"store removal failed: {e}"}

        if removed == 0:
            # C3: structured not-found; the store was not modified.
            return {
                "success": False,
                "error": (
                    "edge not found: no edge matching "
                    f"({from_id!r}, {to_id!r}, {rel_type!r}) in "
                    f"memory_subdir '{memory_subdir}'"
                ),
                "removed": 0,
            }

        reverse_removed: int | None = None
        reverse_error: str | None = None
        if rel_type == "related_to":
            # D24 mirror: symmetric reverse-direction pass is non-fatal
            # best-effort; its outcome is surfaced, never swallowed.
            try:
                reverse_removed = store.remove_edge(to_id, from_id, rel_type)
            except Exception as e:
                reverse_error = str(e)

        response: dict[str, Any] = {
            "success": True,
            "status": "ok",
            "from_id": from_id,
            "to_id": to_id,
            "rel_type": rel_type,
            "removed": 1,
            "reverse_removed": reverse_removed,
        }
        if reverse_error is not None:
            response["reverse_error"] = reverse_error
        return response


# ---------------------------------------------------------------------------
# Serialization helpers (duplicated from context_graph.py — not imported
# cross-file; private helpers are not a public API surface)
# ---------------------------------------------------------------------------

def _enum_safe_value(v: Any) -> Any:
    if isinstance(v, enum.Enum):
        return v.value
    if isinstance(v, dict):
        return {k: _enum_safe_value(item) for k, item in v.items()}
    if isinstance(v, (list, tuple)):
        coerced = [_enum_safe_value(item) for item in v]
        return type(v)(coerced) if isinstance(v, tuple) else coerced
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return _enum_safe_asdict(v)
    return v


def _enum_safe_asdict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        result: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            result[f.name] = _enum_safe_value(getattr(obj, f.name))
        return result
    return _enum_safe_value(obj)


def _serialize_edge(edge: GraphEdge) -> dict:
    edge_type = _enum_safe_value(getattr(edge, "type", ""))
    return {
        "from_id": edge.from_id,
        "to_id": edge.to_id,
        "type": edge_type,
        "weight": float(getattr(edge, "weight", 0.0) or 0.0),
        "confidence": float(getattr(edge, "confidence", 0.0) or 0.0),
        "source": getattr(edge, "source", "") or "",
        "created_at": getattr(edge, "created_at", "") or "",
    }


def _now_iso() -> str:
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:  # pragma: no cover - defensive
        return ""
