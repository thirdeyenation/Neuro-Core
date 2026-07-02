"""Neuro Core ``memory_reflect`` tool.

Trigger a reflection pass over an episode of related memories and
persist the LLM-synthesised insight as a new ``concept`` memory.

Pattern follows ``plugins/_memory/tools/memory_save.py``:
    from helpers.tool import Tool, Response
    from plugins._memory.helpers.memory import Memory

Pipeline (per spec):
    1. ``collect_episode_memories`` — fetch docs that share
       ``metadata.episode_id == episode_id``.
    2. ``reflect_memories`` — call the agent's LLM with the reflection
       system prompt and the collected memory texts.
    3. ``write_reflection`` — insert the new ``concept`` memory with
       ``importance=0.8``, ``stability=0.9``, ``source="neuro_reflect"``.

All three helpers are defensive — they never raise. The tool itself is
also defensive: every error path returns ``Response(message=...)`` with
``break_loop=False``.
"""

from __future__ import annotations

from typing import Any

from helpers.tool import Tool, Response
from plugins._memory.helpers.memory import Memory

from usr.plugins.neuro_core.helpers.reflection import (
    collect_episode_memories,
    reflect_memories,
    write_reflection,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp_limit(limit: int) -> int:
    """Clamp the limit arg to a sane range (1..100)."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return 20
    if n < 1:
        return 1
    if n > 100:
        return 100
    return n


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class MemoryReflect(Tool):
    """Reflect on an episode of related memories and persist the insight."""

    async def execute(
        self,
        episode_id: str = "",
        limit: int = 20,
        **kwargs: Any,
    ) -> Response:
        # --- 1. Required-arg sanity checks --------------------------------
        if not episode_id or not isinstance(episode_id, str):
            return Response(
                message="Error: `episode_id` is required and must be a non-empty string.",
                break_loop=False,
            )

        # --- 2. Resolve the Memory backend --------------------------------
        try:
            db = await Memory.get(self.agent)
        except Exception as exc:  # pragma: no cover - defensive
            return Response(
                message=f"Error: could not initialize Memory backend: {exc}",
                break_loop=False,
            )

        subdir = getattr(db, "memory_subdir", None) or "default"
        n = _clamp_limit(limit)

        # --- 3. Collect episode memories ----------------------------------
        try:
            docs = await collect_episode_memories(subdir, episode_id, db, limit=n)
            import logging as _logging
            _logging.getLogger(__name__).debug(
                "memory_reflect: collect returned %d docs for "
                "episode_id=%r subdir=%r", len(docs), episode_id, subdir
            )
        except Exception as exc:  # pragma: no cover - defensive
            return Response(
                message=(
                    f"Error: failed to collect memories for episode "
                    f"'{episode_id}': {exc}"
                ),
                break_loop=False,
            )

        if not docs:
            return Response(
                message=f"No memories found for episode_id {episode_id} in subdir {subdir}",
                break_loop=False,
            )

        # --- 4. Call the LLM ----------------------------------------------
        try:
            content = await reflect_memories(docs, self.agent)
        except Exception as exc:  # pragma: no cover - defensive
            return Response(
                message=(
                    f"Error: reflection LLM call failed: {exc}"
                ),
                break_loop=False,
            )

        if not content or not content.strip():
            return Response(
                message="Reflection failed — LLM did not return content",
                break_loop=False,
            )

        # --- 5. Persist the reflection ------------------------------------
        try:
            new_id = await write_reflection(subdir, content, episode_id, db)
        except Exception as exc:  # pragma: no cover - defensive
            return Response(
                message=f"Error: failed to persist reflection: {exc}",
                break_loop=False,
            )

        if not new_id:
            return Response(
                message=(
                    f"Reflection failed — could not persist reflection for "
                    f"episode '{episode_id}'"
                ),
                break_loop=False,
            )

        # --- 6. Success ---------------------------------------------------
        ack = f"Reflected on episode '{episode_id}' ({len(docs)} memories) → new insight memory {new_id}"
        return Response(
            message=(
                f"Reflection written as memory {new_id} "
                f"(episode: {episode_id}, {len(docs)} source memories)"
            ),
            break_loop=False,
            additional={"neuro_core_ack": ack},
        )
