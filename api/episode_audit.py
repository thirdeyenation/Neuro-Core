"""Neuro Core ``episode_audit`` API handler.

Exposes the ``/episode_audit`` endpoint under ``/api/plugins/neuro_core/``:

* ``GET /episode_audit`` — list all episodes in a memory subdir with
  metadata, memory counts, date ranges, and reflection status.
* ``GET /episode_audit?id=<episode_id>`` — get detailed audit data for a
  specific episode including all memories and their scores.

All endpoints require an authenticated session (cookie or API key)
and follow the same shape as ``plugins/_memory/api/memory_dashboard.py``.

This endpoint was created to address Gap 5 from the WebUI Polish Relay 3
diagnostic review (DIAGNOSTIC_RELAYS.md, D2). It provides the backend
support for the episode audit view in the WebUI.
"""

from __future__ import annotations

import dataclasses
import enum
from datetime import datetime, timezone
from typing import Any

from helpers.api import ApiHandler, Request, Response
from plugins._memory.helpers.memory import Memory

from usr.plugins.neuro_core.helpers.scores import ScoreStore


# ---------------------------------------------------------------------------
# API Handler
# ---------------------------------------------------------------------------


class EpisodeAuditApi(ApiHandler):
    """REST surface for Neuro Core episode audit views.

    Provides:
    - Episode listing with summary metadata
    - Episode detail with all memories and scores
    - Reflection status tracking
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

            if method == "GET" and path.endswith("/episode_audit"):
                episode_id = (
                    input.get("id") or request.args.get("id") or ""
                ).strip()
                if episode_id:
                    result = await self._get_episode_detail(input, request, episode_id)
                else:
                    result = await self._list_episodes(input, request)
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
    # GET /episode_audit — list all episodes
    # ------------------------------------------------------------------

    async def _list_episodes(
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

            memory = await Memory.get_by_subdir(
                memory_subdir, preload_knowledge=False
            )
            score_store = ScoreStore(memory_subdir)

            # Get all documents from FAISS index
            all_docs = _get_all_documents(memory)

            # Group by episode_id
            episodes: dict[str, dict] = {}
            for doc in all_docs:
                meta = doc.metadata or {}
                episode_id = meta.get("episode_id")
                if not episode_id:
                    continue

                if episode_id not in episodes:
                    episodes[episode_id] = {
                        "episode_id": episode_id,
                        "memory_count": 0,
                        "memory_types": set(),
                        "first_timestamp": None,
                        "last_timestamp": None,
                        "has_reflection": False,
                        "reflection_id": None,
                        "avg_importance": 0.0,
                        "importance_sum": 0.0,
                    }

                ep = episodes[episode_id]
                ep["memory_count"] += 1

                mem_type = meta.get("memory_type")
                if mem_type:
                    ep["memory_types"].add(mem_type)

                # Track timestamps
                ts = meta.get("timestamp") or meta.get("created_at")
                if ts:
                    try:
                        if isinstance(ts, (int, float)):
                            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                        else:
                            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                        if ep["first_timestamp"] is None or dt < ep["first_timestamp"]:
                            ep["first_timestamp"] = dt
                        if ep["last_timestamp"] is None or dt > ep["last_timestamp"]:
                            ep["last_timestamp"] = dt
                    except Exception:
                        pass

                # Check for reflection
                if mem_type == "episode" and meta.get("is_reflection") == True:
                    ep["has_reflection"] = True
                    ep["reflection_id"] = getattr(doc, "id", None) or meta.get("id")

                # Accumulate importance
                doc_id = getattr(doc, "id", None) or meta.get("id") or meta.get("memory_id")
                if doc_id:
                    scores = score_store.get(doc_id)
                    ep["importance_sum"] += scores.importance

            # Finalize episode summaries
            episode_list = []
            for ep in episodes.values():
                if ep["memory_count"] > 0:
                    ep["avg_importance"] = ep["importance_sum"] / ep["memory_count"]
                ep["memory_types"] = sorted(list(ep["memory_types"]))
                ep["first_timestamp"] = (
                    ep["first_timestamp"].isoformat() if ep["first_timestamp"] else None
                )
                ep["last_timestamp"] = (
                    ep["last_timestamp"].isoformat() if ep["last_timestamp"] else None
                )
                del ep["importance_sum"]
                episode_list.append(ep)

            # Sort by first_timestamp descending (newest first)
            episode_list.sort(
                key=lambda e: e["first_timestamp"] or "",
                reverse=True,
            )

            return {
                "success": True,
                "memory_subdir": memory_subdir,
                "episode_count": len(episode_list),
                "episodes": episode_list,
            }
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # GET /episode_audit?id=<episode_id> — episode detail
    # ------------------------------------------------------------------

    async def _get_episode_detail(
        self, input: dict, request: Request, episode_id: str
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

            memory = await Memory.get_by_subdir(
                memory_subdir, preload_knowledge=False
            )
            score_store = ScoreStore(memory_subdir)

            all_docs = _get_all_documents(memory)

            # Filter docs by episode_id
            episode_docs = []
            for doc in all_docs:
                meta = doc.metadata or {}
                if meta.get("episode_id") == episode_id:
                    doc_id = getattr(doc, "id", None) or meta.get("id") or meta.get("memory_id")
                    scores = score_store.get(doc_id) if doc_id else None
                    episode_docs.append({
                        "id": doc_id,
                        "content": getattr(doc, "page_content", "") or meta.get("content", ""),
                        "metadata": meta,
                        "scores": {
                            "importance": scores.importance if scores else 0.0,
                            "confidence": scores.confidence if scores else 0.0,
                            "stability": scores.stability if scores else 0.0,
                            "access_count": scores.access_count if scores else 0,
                            "last_accessed_at": (
                                scores.last_accessed_at.isoformat()
                                if scores and hasattr(scores.last_accessed_at, "isoformat")
                                else str(scores.last_accessed_at) if scores else None
                            ),
                        } if scores else None,
                    })

            # Sort by timestamp ascending (chronological)
            episode_docs.sort(
                key=lambda d: d["metadata"].get("timestamp") or d["metadata"].get("created_at") or "",
            )

            # Find reflection if present
            reflection = None
            for doc in episode_docs:
                if doc["metadata"].get("memory_type") == "episode" and doc["metadata"].get("is_reflection") == True:
                    reflection = doc
                    break

            return {
                "success": True,
                "memory_subdir": memory_subdir,
                "episode_id": episode_id,
                "memory_count": len(episode_docs),
                "has_reflection": reflection is not None,
                "reflection": reflection,
                "memories": episode_docs,
            }
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_all_documents(memory: Any) -> list:
    """Get all documents from a Memory instance, with fallback."""
    try:
        if hasattr(memory.db, "get_all_documents"):
            return memory.db.get_all_documents()
    except Exception:
        pass
    try:
        if hasattr(memory, "docstore"):
            return list(memory.docstore._dict.values())
    except Exception:
        pass
    return []


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
