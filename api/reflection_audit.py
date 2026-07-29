"""Neuro Core ``reflection_audit`` API handler.

Exposes the ``/reflection_audit`` endpoint under ``/api/plugins/neuro_core/``:

* ``GET /reflection_audit`` — list all reflection memories in a memory subdir
  with metadata, source episode info, and audit details.
* ``GET /reflection_audit?id=<memory_id>`` — get detailed audit data for a
  specific reflection memory.

All endpoints require an authenticated session (cookie or API key)
and follow the same shape as ``plugins/_memory/api/memory_dashboard.py``.

This endpoint was created to address Gap 5 from the WebUI Polish Relay 3
diagnostic review (DIAGNOSTIC_RELAYS.md, D2). It provides the backend
support for the reflection audit view in the WebUI.
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


class ReflectionAuditApi(ApiHandler):
    """REST surface for Neuro Core reflection audit views.

    Provides:
    - Reflection listing with source episode info
    - Reflection detail with full content and metadata
    - Audit trail for reflection lifecycle
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

            if method == "GET" and path.endswith("/reflection_audit"):
                reflection_id = (
                    input.get("id") or request.args.get("id") or ""
                ).strip()
                if reflection_id:
                    result = await self._get_reflection_detail(input, request, reflection_id)
                else:
                    result = await self._list_reflections(input, request)
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
    # GET /reflection_audit — list all reflections
    # ------------------------------------------------------------------

    async def _list_reflections(
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

            all_docs = _get_all_documents(memory)

            # Find all reflection memories
            reflections = []
            for doc in all_docs:
                meta = doc.metadata or {}
                mem_type = meta.get("memory_type")
                is_reflection = meta.get("is_reflection") == True

                # A reflection is an episode-type memory with is_reflection=True
                if mem_type == "episode" and is_reflection:
                    doc_id = getattr(doc, "id", None) or meta.get("id") or meta.get("memory_id")
                    scores = score_store.get(doc_id) if doc_id else None

                    # Get source episode info
                    source_episode_id = meta.get("source_episode_id") or meta.get("episode_id")
                    source_memory_count = meta.get("source_memory_count")

                    reflections.append({
                        "id": doc_id,
                        "content_preview": (
                            (getattr(doc, "page_content", "") or meta.get("content", ""))[:200]
                        ),
                        "metadata": meta,
                        "scores": {
                            "importance": scores.importance if scores else 0.0,
                            "confidence": scores.confidence if scores else 0.0,
                            "stability": scores.stability if scores else 0.0,
                        } if scores else None,
                        "source_episode_id": source_episode_id,
                        "source_memory_count": source_memory_count,
                        "created_at": meta.get("timestamp") or meta.get("created_at"),
                        "validation_status": meta.get("validation_status", "unvalidated"),
                    })

            # Sort by created_at descending (newest first)
            reflections.sort(
                key=lambda r: r["created_at"] or "",
                reverse=True,
            )

            return {
                "success": True,
                "memory_subdir": memory_subdir,
                "reflection_count": len(reflections),
                "reflections": reflections,
            }
        except Exception as e:  # pragma: no cover - defensive
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # GET /reflection_audit?id=<memory_id> — reflection detail
    # ------------------------------------------------------------------

    async def _get_reflection_detail(
        self, input: dict, request: Request, reflection_id: str
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

            # Find the specific reflection
            reflection_doc = None
            for doc in all_docs:
                meta = doc.metadata or {}
                doc_id = getattr(doc, "id", None) or meta.get("id") or meta.get("memory_id")
                if doc_id == reflection_id:
                    if meta.get("memory_type") == "episode" and meta.get("is_reflection") == True:
                        reflection_doc = doc
                        break

            if reflection_doc is None:
                return {
                    "success": False,
                    "error": f"Reflection with id '{reflection_id}' not found",
                }

            meta = reflection_doc.metadata or {}
            doc_id = getattr(reflection_doc, "id", None) or meta.get("id") or meta.get("memory_id")
            scores = score_store.get(doc_id) if doc_id else None

            # Get source episode memories if source_episode_id is known
            source_episode_id = meta.get("source_episode_id") or meta.get("episode_id")
            source_memories = []
            if source_episode_id:
                for doc in all_docs:
                    doc_meta = doc.metadata or {}
                    if doc_meta.get("episode_id") == source_episode_id:
                        if doc_meta.get("is_reflection") != True:  # Exclude the reflection itself
                            source_memories.append({
                                "id": getattr(doc, "id", None) or doc_meta.get("id"),
                                "content_preview": (
                                    (getattr(doc, "page_content", "") or doc_meta.get("content", ""))[:100]
                                ),
                                "memory_type": doc_meta.get("memory_type"),
                                "timestamp": doc_meta.get("timestamp") or doc_meta.get("created_at"),
                            })

            return {
                "success": True,
                "memory_subdir": memory_subdir,
                "reflection": {
                    "id": doc_id,
                    "content": getattr(reflection_doc, "page_content", "") or meta.get("content", ""),
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
                    "source_episode_id": source_episode_id,
                    "source_memory_count": len(source_memories),
                    "created_at": meta.get("timestamp") or meta.get("created_at"),
                    "validation_status": meta.get("validation_status", "unvalidated"),
                },
                "source_memories": source_memories,
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
