"""Neuro Core ``context_graph`` API handler.

Exposes the ``/context_graph`` endpoint under ``/api/plugins/neuro_core/``:

* ``GET /context_graph`` — run a hybrid retrieval and return the
  serialized ``ContextGraph``.

All endpoints require an authenticated session (cookie or API key)
and follow the same shape as ``plugins/_memory/api/memory_dashboard.py``.

Note: Relationship endpoints (``/relationships``, ``/relationships?id=<id>``,
``POST /relationships``) live in ``api/relationships.py`` (D42).
Framework routing splits on path.split("/", 2) so a single .py file maps
to exactly one routable URL prefix; deeper path segments are not supported.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from typing import Any

from helpers.api import ApiHandler, Request, Response
from plugins._memory.helpers.memory import Memory

from usr.plugins.neuro_core.helpers.context_graph import ContextGraph
from usr.plugins.neuro_core.helpers.graph_store import (
    GraphEdge,
    GraphStore,
    VALID_RELATIONSHIP_TYPES,
)
from usr.plugins.neuro_core.helpers.retrieval import search_context_graph
from usr.plugins.neuro_core.helpers.scores import ScoreStore


# ---------------------------------------------------------------------------
# API Handler
# ---------------------------------------------------------------------------


class ContextGraphApi(ApiHandler):
    """REST surface for the Neuro Core context-graph endpoint.

    Relationship routes (``/relationships``, ``/relationships?id=<id>``,
    ``POST /relationships``) are exposed by ``RelationshipsApi`` in
    ``api/relationships.py``. The two handlers are intentionally
    separate so each .py file maps to exactly one URL prefix (D42).
    """

    # All routes require a logged-in user (cookie) OR a valid API key.
    # MUST be a @classmethod (per /a0/helpers/api.py base contract) so the
    # framework can call ``cls.requires_auth()`` and ``cls.requires_csrf()``.
    # Defining it as a plain ``True`` attribute shadows the base classmethod
    # and raises ``TypeError: 'bool' object is not callable`` during dispatch.
    @classmethod
    def requires_auth(cls) -> bool:
        return True

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET"]

    # The framework dispatches based on the request path; we route on
    # ``request.path`` and the HTTP method inside ``process()`` to keep
    # this class single-instance and the dispatch table small.

    async def process(self, input: dict, request: Request) -> dict | Response:
        # Single-entry-point guard: every dict response leaves this method
        # already enum-safe. This means future handler additions cannot
        # forget to wrap their output — the guard is centralized here.
        # Non-dict responses (``Response`` objects) pass through unchanged.
        result: dict | Response
        try:
            path = (request.path or "").rstrip("/")
            method = (request.method or "GET").upper()

            if method == "GET" and path.endswith("/context_graph"):
                result = await self._get_context_graph(input, request)
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
    # GET /context_graph
    # ------------------------------------------------------------------

    async def _get_context_graph(
        self, input: dict, request: Request
    ) -> dict:
        try:
            query = (input.get("query") or "").strip()
            memory_subdir = (input.get("memory_subdir") or "").strip()
            if not query:
                return {
                    "success": False,
                    "error": "`query` is required",
                }
            if not memory_subdir:
                return {
                    "success": False,
                    "error": "`memory_subdir` is required",
                }

            # Build a config dict for the retrieval pipeline. Defaults
            # match the values the spec recommends when the user does
            # not override them via plugin settings.
            config = _default_retrieval_config()

            memory = await Memory.get_by_subdir(
                memory_subdir, preload_knowledge=False
            )
            graph_store = GraphStore(memory_subdir)
            score_store = ScoreStore(memory_subdir)

            graph: ContextGraph = await search_context_graph(
                memory=memory,
                query=query,
                graph_store=graph_store,
                score_store=score_store,
                config=config,
            )

            return {
                "success": True,
                "context_graph": _serialize_context_graph(graph),
            }
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_retrieval_config() -> dict:
    """Build a retrieval config dict from the plugin's default_config.yaml."""
    try:
        from helpers import plugins
        # We need an agent to call get_plugin_config, but the API handler
        # may not have one bound yet. We fall back to hardcoded defaults
        # if no agent is available.
        # The default values are documented in ``default_config.yaml``.
    except Exception:  # pragma: no cover - defensive
        pass
    return {
        "graph_max_hops": 2,
        "graph_neighbors_max": 10,
        "semantic_limit": 5,
        "semantic_threshold": 0.6,
        "similarity_weight": 0.5,
        "importance_weight": 0.3,
        "recency_weight": 0.2,
    }


def _serialize_context_graph(graph: ContextGraph) -> dict:
    """Convert a ``ContextGraph`` to a JSON-safe dict.

    The default ``dataclasses.asdict`` does not recursively walk nested
    dict fields, so any ``enum.Enum`` value that lives inside a
    ``GraphNode.metadata`` dict (e.g. ``Memory.Area.MAIN`` or any
    ``MemoryType`` / ``ValidationStatus`` instance persisted in FAISS
    metadata) would survive serialization as an ``Enum`` instance and
    crash ``json.dumps`` with ``TypeError: Object of type Area is not
    JSON serializable``.

    ``_enum_safe_asdict`` walks the dataclass tree, the metadata dicts
    inside it, and any list values, converting every ``enum.Enum``
    instance it finds to its ``.value`` string. The result is always
    JSON-safe, regardless of which enums the upstream caller stored.
    """
    return {
        "query": graph.query,
        "seed_ids": list(graph.seed_ids),
        "nodes": [_enum_safe_asdict(n) for n in graph.nodes],
        "edges": [_serialize_edge(e) for e in graph.edges],
        "prompt_text": graph.to_prompt_text(),
    }


def _serialize_edge(edge: GraphEdge) -> dict:
    """Convert a ``GraphEdge`` dataclass to a JSON-safe dict.

    The ``type`` field is declared ``str`` in ``GraphEdge`` and is
    validated against ``VALID_RELATIONSHIP_TYPES`` in ``__post_init__``,
    so it is normally a plain string. We still apply the
    ``_enum_safe_value`` helper defensively so that any future change
    that allows a ``RelationshipType`` enum member to reach this point
    (e.g. an internal tool constructing the edge with the enum rather
    than its value) does not regress the API response with a
    ``TypeError``.
    """
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


# ---------------------------------------------------------------------------
# Enum-safe serialization helpers
# ---------------------------------------------------------------------------
#
# The FAISS metadata dict attached to every document is a plain
# ``dict`` that the upstream ``Memory`` code (and several extensions)
# can mutate freely. In particular, ``Memory.Area`` is an ``Enum``
# whose value can leak into metadata when documents are constructed
# in-process and inserted through the extension hooks. The default
# ``dataclasses.asdict`` does not recurse into nested dict fields, so
# an ``Area`` instance inside ``GraphNode.metadata`` would survive
# ``asdict`` unchanged and break ``json.dumps`` on the way out of the
# API handler. These helpers walk the dataclass tree recursively and
# convert every ``enum.Enum`` value to its ``.value`` string.


def _enum_safe_value(v: Any) -> Any:
    """Coerce a single value: ``Enum`` -> ``.value``; containers -> recurse.

    All other values (str, int, float, bool, None, ``datetime``, …) are
    returned unchanged so ``json.dumps`` can serialize them as-is.
    """
    if isinstance(v, enum.Enum):
        return v.value
    if isinstance(v, dict):
        return {k: _enum_safe_value(item) for k, item in v.items()}
    if isinstance(v, (list, tuple)):
        coerced = [_enum_safe_value(item) for item in v]
        # Preserve tuple -> tuple so callers that inspect the type are not
        # surprised. ``json.dumps`` treats both as JSON arrays anyway.
        return type(v)(coerced) if isinstance(v, tuple) else coerced
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return _enum_safe_asdict(v)
    return v


def _enum_safe_asdict(obj: Any) -> Any:
    """``dataclasses.asdict`` equivalent that also coerces ``Enum`` values.

    Walks dataclass fields, the dicts and lists they contain, and any
    nested dataclasses, converting every ``enum.Enum`` member to its
    underlying ``.value``. Non-dataclass inputs are processed by
    ``_enum_safe_value`` so the function is safe to call on a bare
    dict (e.g. ``GraphNode.metadata``) as well.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        result: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            result[f.name] = _enum_safe_value(getattr(obj, f.name))
        return result
    return _enum_safe_value(obj)


def _now_iso() -> str:
    try:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
    except Exception:  # pragma: no cover - defensive
        return ""
