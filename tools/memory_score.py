"""Neuro Core `memory_score` tool.

Updates the mutable score layer and typed metadata fields for a single
memory document. Follows the same Tool pattern as
``plugins/_memory/tools/memory_save.py``:

    from helpers.tool import Tool, Response
    from plugins._memory.helpers.memory import Memory

Persistence model:
    - ``memory_type``, ``validation_status``, ``task_status`` are written
      into the FAISS document metadata via ``Memory.update_documents()``
      (in-place update + ``_save_db()``).
    - ``importance``, ``confidence``, ``stability`` are written into the
      ``scores.json`` sidecar via ``ScoreStore.set()`` so they do NOT
      trigger a full FAISS index rewrite.

Validation order (per spec):
    1. Retrieve the document by ``id`` via ``Memory``; return a clear
       error string if not found. Do not raise.
    2. Apply ``validate_neuro_metadata()`` to the incoming fields dict.
    3. Enforce the ``task_status`` guard in code (not just the prompt):
       reject if the document is not of type ``"task"``.
    4. Update the metadata fields and the score sidecar.
    5. Return a confirmation string listing the ``id`` and every field
       that was changed with its new value.
"""

from __future__ import annotations

from typing import Any

from helpers.tool import Tool, Response
from plugins._memory.helpers.memory import Memory

from usr.plugins.neuro_core.helpers.metadata import (
    MemoryType,
    validate_neuro_metadata,
)
from usr.plugins.neuro_core.helpers.scores import ScoreStore


# Fields that are stored in FAISS document metadata.
_FAISS_FIELDS = ("memory_type", "validation_status", "task_status")

# Fields that are stored in the scores.json sidecar.
_SCORECAR_FIELDS = ("importance", "confidence", "stability")


class MemoryScore(Tool):
    """Update the score, validation status, and task status of one memory."""

    async def execute(self, id: str = "", **kwargs: Any) -> Response:
        # --- 1. Validate the id arg and look up the document -----------
        if not id or not isinstance(id, str):
            return Response(
                message="Error: `id` is required and must be a non-empty string.",
                break_loop=False,
            )

        try:
            db = await Memory.get(self.agent)
        except Exception as exc:  # pragma: no cover - defensive
            return Response(
                message=f"Error: could not initialize Memory backend: {exc}",
                break_loop=False,
            )

        try:
            docs = db.db.get_by_ids([id])
        except Exception as exc:  # pragma: no cover - defensive
            return Response(
                message=f"Error: failed to look up memory '{id}': {exc}",
                break_loop=False,
            )

        if not docs:
            return Response(
                message=(
                    f"Error: no memory found with id '{id}'. Use "
                    f"memory_load to find valid ids."
                ),
                break_loop=False,
            )

        doc = docs[0]
        meta = dict(doc.metadata or {})

        # --- 2. Build the incoming-fields dict and validate it ---------
        incoming: dict[str, Any] = {}
        for field_name in _FAISS_FIELDS + _SCORECAR_FIELDS:
            if field_name in kwargs and kwargs[field_name] is not None:
                incoming[field_name] = kwargs[field_name]

        if not incoming:
            return Response(
                message=(
                    f"Error: no updatable fields supplied for memory '{id}'. "
                    f"Provide at least one of: "
                    f"{', '.join(_FAISS_FIELDS + _SCORECAR_FIELDS)}."
                ),
                break_loop=False,
            )

        # Mutates `incoming` in place: clamps scores, coerces invalid
        # enum values to safe fallbacks ("note", "unvalidated").
        validate_neuro_metadata(incoming)

        # --- 3. Enforce the task_status guard in code ------------------
        if "task_status" in incoming:
            # Use the (possibly just-updated) memory_type for the check.
            effective_type = incoming.get("memory_type", meta.get("memory_type"))
            if effective_type != MemoryType.TASK.value:
                return Response(
                    message=(
                        f"Error: task_status is only valid for memories of "
                        f"type 'task' (memory '{id}' is currently typed as "
                        f"'{effective_type}'). Update memory_type to 'task' "
                        f"first, or omit task_status."
                    ),
                    break_loop=False,
                )

        # --- 4. Track changes and apply --------------------------------
        faiss_changes: dict[str, Any] = {}
        for field_name in _FAISS_FIELDS:
            if field_name in incoming and incoming[field_name] != meta.get(field_name):
                faiss_changes[field_name] = incoming[field_name]

        score_changes: dict[str, float] = {}
        for field_name in _SCORECAR_FIELDS:
            if field_name in incoming:
                score_changes[field_name] = float(incoming[field_name])

        if not faiss_changes and not score_changes:
            return Response(
                message=f"No changes to memory '{id}' (values were unchanged).",
                break_loop=False,
            )

        # Persist FAISS metadata fields (in-place update).
        if faiss_changes:
            for k, v in faiss_changes.items():
                meta[k] = v
            doc.metadata = meta
            try:
                await db.update_documents([doc])
            except Exception as exc:  # pragma: no cover - defensive
                return Response(
                    message=(
                        f"Error: failed to persist metadata changes for "
                        f"memory '{id}': {exc}"
                    ),
                    break_loop=False,
                )

        # Persist score fields to the sidecar (no FAISS rewrite).
        if score_changes:
            try:
                store = ScoreStore(db.memory_subdir)
                store.set(memory_id=id, **score_changes)
                doc.metadata = meta  # link meta to doc.metadata so updates flow through
                for k in score_changes:
                    meta[k] = score_changes[k]
                await db.update_documents([doc])
            except Exception as exc:  # pragma: no cover - defensive
                return Response(
                    message=(
                        f"Error: failed to persist score changes for "
                        f"memory '{id}': {exc}"
                    ),
                    break_loop=False,
                )

        # --- 5. Build the confirmation string --------------------------
        all_changes = {**faiss_changes, **score_changes}
        lines = [f"Updated memory '{id}':"]
        for field_name in _FAISS_FIELDS + _SCORECAR_FIELDS:
            if field_name in all_changes:
                lines.append(f"  - {field_name}: {all_changes[field_name]}")
        return Response(message="\n".join(lines), break_loop=False)
