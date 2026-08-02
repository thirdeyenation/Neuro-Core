"""Neuro Core ``advanced_filters`` API handler.

Exposes the ``/advanced_filters`` endpoint under ``/api/plugins/neuro_core/``:

* ``GET /advanced_filters`` — run a filtered graph query and return matching
  nodes and edges. Supports combined filters for memory_type, validation_status,
  relationship_type, date_range, importance/confidence/stability thresholds,
  and episode_id.

All endpoints require an authenticated session (cookie or API key)
and follow the same shape as ``plugins/_memory/api/memory_dashboard.py``.

This endpoint was created to address Gap 3 from the WebUI Polish Relay 3
diagnostic review (DIAGNOSTIC_RELAYS.md, D3). It provides the backend
support for the advanced filter UI in the graph panel.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from datetime import datetime, timezone
from typing import Any

from helpers.api import ApiHandler, Request, Response
from plugins._memory.helpers.memory import Memory

from usr.plugins.neuro_core.helpers.graph_store import (
    GraphEdge,
    GraphStore,
    VALID_RELATIONSHIP_TYPES,
)
from usr.plugins.neuro_core.helpers.metadata import (
    MemoryType,
    ValidationStatus,
)
from usr.plugins.neuro_core.helpers.scores import ScoreStore


# ---------------------------------------------------------------------------
# API Handler
# ---------------------------------------------------------------------------


class AdvancedFiltersApi(ApiHandler):
    """REST surface for Neuro Core advanced filtered graph queries.

    Supports combined filters:
    - memory_type: list of MemoryType values
    - validation_status: list of ValidationStatus values
    - relationship_type: list of RelationshipType values
    - date_range: {start: ISO8601, end: ISO8601}
    - importance_min, confidence_min, stability_min: float thresholds
    - episode_id: string filter
    - query: semantic search query (optional)
    """

    @classmethod
    def requires_auth(cls) -> bool:
        return True

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET"]

    async def process(self, input: dict, request: Request) -> dict | Response:
        result: dict | Response
        try:
            path = (request.path or "").rstrip("/")
            method = (request.method or "GET").upper()

            if method == "GET" and path.endswith("/advanced_filters"):
                result = await self._get_filtered_graph(input, request)
            else:
                result = {
                    "success": False,
                    "error": f"Unknown route: {method} {path}",
                }
        except Exception as e:  # pragma: no cover - defensive top-level
            result = {"success": False, "error": str(e)}

        if isinstance(result, dict):
            return _enum_safe_value(result)
        return result

    # ------------------------------------------------------------------
    # GET /advanced_filters
    # ------------------------------------------------------------------

    async def _get_filtered_graph(
        self, input: dict, request: Request
    ) -> dict:
        try:
            memory_subdir = (
                input.get("memory_subdir")
                or request.args.get("memory_subdir")
                or ""
            ).strip()
            if not memory_subdir:
                return {
                    "success": False,
                    "error": "`memory_subdir` is required",
                }

            # Parse filter parameters
            memory_types = _parse_enum_list(
                input.get("memory_type") or request.args.get("memory_type"),
                MemoryType,
            )
            validation_statuses = _parse_enum_list(
                input.get("validation_status") or request.args.get("validation_status"),
                ValidationStatus,
            )
            relationship_types = _parse_enum_list(
                input.get("relationship_type") or request.args.get("relationship_type"),
                None,  # RelationshipType enum imported lazily
            )
            date_range = _parse_date_range(
                input.get("date_range") or request.args.get("date_range")
            )
            importance_min = _parse_float(
                input.get("importance_min") or request.args.get("importance_min")
            )
            confidence_min = _parse_float(
                input.get("confidence_min") or request.args.get("confidence_min")
            )
            stability_min = _parse_float(
                input.get("stability_min") or request.args.get("stability_min")
            )
            episode_id = (
                input.get("episode_id") or request.args.get("episode_id") or ""
            ).strip() or None
            query = (
                input.get("query") or request.args.get("query") or ""
            ).strip() or None
            limit = _parse_int(
                input.get("limit") or request.args.get("limit")
            ) or 100

            # Load memory and stores
            memory = await Memory.get_by_subdir(
                memory_subdir, preload_knowledge=False
            )
            graph_store = GraphStore(memory_subdir)
            score_store = ScoreStore(memory_subdir)

            # Get all documents from FAISS index
            all_docs = memory.db.get_all_documents() if hasattr(memory.db, "get_all_documents") else []
            if not all_docs and hasattr(memory, "docstore"):
                # Fallback: iterate docstore
                try:
                    all_docs = list(memory.docstore._dict.values())
                except Exception:
                    all_docs = []

            # Apply filters
            filtered_nodes = []
            for doc in all_docs:
                meta = doc.metadata or {}

                # Memory type filter
                if memory_types is not None:
                    doc_type = meta.get("memory_type")
                    if doc_type not in memory_types:
                        continue

                # Validation status filter
                if validation_statuses is not None:
                    doc_status = meta.get("validation_status")
                    if doc_status not in validation_statuses:
                        continue

                # Date range filter
                if date_range is not None:
                    doc_date = meta.get("timestamp") or meta.get("created_at")
                    if doc_date:
                        try:
                            if isinstance(doc_date, (int, float)):
                                doc_dt = datetime.fromtimestamp(doc_date, tz=timezone.utc)
                            else:
                                doc_dt = datetime.fromisoformat(str(doc_date).replace("Z", "+00:00"))
                            if doc_dt < date_range["start"] or doc_dt > date_range["end"]:
                                continue
                        except Exception:
                            pass

                # Episode ID filter
                if episode_id is not None:
                    if meta.get("episode_id") != episode_id:
                        continue

                # Score thresholds
                doc_id = getattr(doc, "id", None) or meta.get("id") or meta.get("memory_id")
                if doc_id and (importance_min is not None or confidence_min is not None or stability_min is not None):
                    scores = score_store.get(doc_id)
                    if importance_min is not None and scores.importance < importance_min:
                        continue
                    if confidence_min is not None and scores.confidence < confidence_min:
                        continue
                    if stability_min is not None and scores.stability < stability_min:
                        continue

                filtered_nodes.append({
                    "id": doc_id,
                    "content": getattr(doc, "page_content", "") or meta.get("content", ""),
                    "metadata": meta,
                })

            # Apply limit
            filtered_nodes = filtered_nodes[:limit]

            # Get edges between filtered nodes
            filtered_ids = {n["id"] for n in filtered_nodes if n["id"]}
            all_edges = graph_store.get_all_edges() if hasattr(graph_store, "get_all_edges") else []
            filtered_edges = []
            for edge in all_edges:
                if relationship_types is not None and edge.rel_type not in relationship_types:
                    continue
                if edge.from_id in filtered_ids and edge.to_id in filtered_ids:
                    filtered_edges.append(_serialize_edge(edge))

            return {
                "success": True,
                "memory_subdir": memory_subdir,
                "filters_applied": {
                    "memory_type": memory_types,
                    "validation_status": validation_statuses,
                    "relationship_type": relationship_types,
                    "date_range": date_range,
                    "importance_min": importance_min,
                    "confidence_min": confidence_min,
                    "stability_min": stability_min,
                    "episode_id": episode_id,
                    "query": query,
                },
                "node_count": len(filtered_nodes),
                "edge_count": len(filtered_edges),
                "nodes": filtered_nodes,
                "edges": filtered_edges,
            }
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_enum_list(value: Any, enum_cls: Any) -> list[str] | None:
    """Parse a comma-separated string or list into enum values."""
    if value is None:
        return None
    if isinstance(value, list):
        items = value
    else:
        items = [v.strip() for v in str(value).split(",") if v.strip()]
    if not items:
        return None
    if enum_cls is not None:
        valid = {e.value for e in enum_cls}
        return [v for v in items if v in valid]
    return items


def _parse_date_range(value: Any) -> dict | None:
    """Parse date_range parameter into {start, end} dict."""
    if value is None:
        return None
    if isinstance(value, dict):
        start = value.get("start")
        end = value.get("end")
    else:
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
            start = parsed.get("start")
            end = parsed.get("end")
        except Exception:
            return None
    if not start or not end:
        return None
    try:
        start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        return {"start": start_dt, "end": end_dt}
    except Exception:
        return None


def _parse_float(value: Any) -> float | None:
    """Parse a float value, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_int(value: Any) -> int | None:
    """Parse an int value, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _serialize_edge(edge: GraphEdge) -> dict:
    """Serialize a GraphEdge to a JSON-safe dict."""
    return {
        "from_id": edge.from_id,
        "to_id": edge.to_id,
        "type": edge.rel_type,
        "weight": edge.weight,
        "created_at": edge.created_at.isoformat() if hasattr(edge.created_at, "isoformat") else str(edge.created_at),
    }


def _enum_safe_value(value: Any) -> Any:
    """Recursively convert enum values to their string values for JSON safety."""
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _enum_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_enum_safe_value(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _enum_safe_value(dataclasses.asdict(value))
    return value
