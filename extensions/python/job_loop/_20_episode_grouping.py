"""Neuro Core — periodic episode-grouping job.

Walks every memory subdir, sorts documents by ``timestamp``, and
assigns an ``episode_id`` to contiguous groups whose mutual time gap
is shorter than ``config["episode_boundary_hours"]`` and whose size
meets ``config["episode_min_memories"]``.

The episode id format is ``ep_{YYYY-MM-DD}_{idx:03d}`` where the date
is the day of the group's first memory and ``idx`` is the 1-based
group index in the current pass.

The extension is throttled to ``episode_boundary_hours`` (default
``4``) and never raises — errors are caught, logged and swallowed.

Stability contract (v2):
    - All throttle state is held in a module-level ``_STATE`` dict.
      ``sys.modules[__name__]`` lookups are NOT used because Agent
      Zero's dynamic extension loader may not have registered the
      module under ``__name__`` when the scheduler ticks.
    - ``execute()`` wraps ``_run()`` in ``asyncio.wait_for(..., 30.0)``
      and adds a broad outer ``except Exception`` so the framework
      scheduler never sees a crash from this extension.
    - Plugin-local imports (``from usr.plugins.neuro_core...``) are
      moved inside the method that uses them. Module-level imports
      of plugin-local code are not permitted in job_loop extensions
      because the framework's loader may invoke ``execute()`` before
      the plugin package is fully initialised.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from helpers.extension import Extension
from helpers.print_style import PrintStyle


_logger = logging.getLogger("neuro_core.job_loop.episode_grouping")

# Module-level throttle state — updated in-place by the run() method.
# Use a dict (not a bare global) so we never need ``sys.modules[__name__]``
# to find the state. The Agent Zero dynamic extension loader does not
# guarantee that ``sys.modules[__name__]`` resolves to the right module
# object — a previous implementation crashed with
# ``KeyError: '_20_episode_grouping'`` on every scheduler tick because
# of that assumption.
_STATE: dict[str, float] = {"last_run": 0.0}


class EpisodeGroupingJob(Extension):
    """Group temporally-clustered memories into episodes."""

    async def execute(self, **kwargs: Any) -> None:
        """Scheduler entry point — NEVER re-raises.

        Two-layer error guard:
            1. ``asyncio.wait_for(self._run(...), 30.0)`` — caps the
               run at 30 s. A ``TimeoutError`` is logged and the
               method returns normally.
            2. A broad ``except Exception`` wraps the entire body so
               any unhandled error in ``_run`` is also logged and
               swallowed.
        """
        try:
            await asyncio.wait_for(self._run(**kwargs), timeout=30.0)
        except asyncio.TimeoutError:
            PrintStyle().warning(
                "[neuro_core] EpisodeGroupingJob timed out after 30s — skipped"
            )
        except Exception as e:
            PrintStyle().error(
                f"[neuro_core] EpisodeGroupingJob unhandled error: {e}"
            )

    async def _run(self, **kwargs: Any) -> None:
        # All plugin-local imports live here, NOT at module level.
        from usr.plugins.neuro_core.helpers.lifecycle import (
            DEFAULT_CONFIG as _LC_DEFAULTS,
        )

        # ---- 1. Resolve config -------------------------------------------------
        try:
            config = self._read_config()
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning("episode_grouping: config read failed: %s", exc)
            return

        if not bool(config.get("decay_enabled", _LC_DEFAULTS["decay_enabled"])):
            # Episode grouping is grouped under the lifecycle toggle so
            # users can switch the whole subsystem off with one flag.
            return

        interval = float(config.get(
            "episode_boundary_hours", _LC_DEFAULTS["episode_boundary_hours"]
        ))

        # ---- 2. Throttle -------------------------------------------------------
        # Use the module-level ``_STATE`` dict. The previous
        # implementation used ``should_run("_last_episode", sys.modules[__name__], ...)``
        # which crashed with ``KeyError: '_20_episode_grouping'`` on every
        # 60s tick. The in-line check below is the v2 equivalent.
        now = asyncio.get_event_loop().time()
        last_run = float(_STATE.get("last_run", 0.0))
        if last_run and (now - last_run) < interval * 3600.0:
            return
        _STATE["last_run"] = now

        # ---- 3. Walk all subdirs ---------------------------------------------
        try:
            subdirs = self._subdirs()
        except Exception as exc:  # pragma: no cover - defensive
            PrintStyle().warning(
                f"neuro_core episode_grouping: cannot enumerate subdirs: {exc}"
            )
            return

        totals = {"scanned": 0, "episodes": 0, "assigned": 0}
        for subdir in subdirs:
            try:
                from usr.plugins.neuro_core.helpers.lifecycle import (
                    run_episode_grouping,
                )
                docs = list(self._iter_docs(subdir))
                result = run_episode_grouping(subdir, config, docs)
                for k in totals:
                    totals[k] += int(result.get(k, 0))
                # Best-effort: write the assignments back to FAISS metadata
                # when the storage layer is available. The helper itself
                # is purely descriptive — the extension is responsible
                # for persistence.
                self._persist_assignments(subdir, result.get("assignments", []))
            except Exception as exc:  # pragma: no cover - defensive
                PrintStyle().warning(
                    f"neuro_core episode_grouping: subdir {subdir!r} failed: {exc}"
                )
                continue

        PrintStyle().info(
            "neuro_core episode_grouping: subdirs=%d scanned=%d episodes=%d assigned=%d"
            % (len(subdirs), totals["scanned"], totals["episodes"], totals["assigned"])
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _read_config() -> dict:
        try:
            from usr.plugins.neuro_core.helpers.lifecycle import (
                DEFAULT_CONFIG as _LC_DEFAULTS,
            )
        except Exception:  # pragma: no cover - defensive
            from usr.plugins.neuro_core.helpers.lifecycle import (  # type: ignore
                DEFAULT_CONFIG as _LC_DEFAULTS,
            )
            return dict(_LC_DEFAULTS)
        try:
            from helpers.plugins import get_plugin_config  # type: ignore
        except Exception:  # pragma: no cover - defensive
            return dict(_LC_DEFAULTS)
        try:
            try:
                agent = _resolve_agent()
            except Exception:  # pragma: no cover - defensive
                agent = None
            try:
                cfg = get_plugin_config("neuro_core", agent=agent)  # type: ignore[arg-type]
            except TypeError:
                cfg = get_plugin_config("neuro_core")  # type: ignore[call-arg]
            if isinstance(cfg, dict):
                merged = dict(_LC_DEFAULTS)
                merged.update(cfg)
                return merged
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning("episode_grouping: get_plugin_config failed: %s", exc)
        return dict(_LC_DEFAULTS)

    @staticmethod
    def _subdirs() -> List[str]:
        try:
            from plugins._memory.helpers.memory import (  # type: ignore
                get_existing_memory_subdirs,
            )
        except Exception:
            return ["default"]
        try:
            return list(get_existing_memory_subdirs() or ["default"])  # type: ignore[misc]
        except Exception:  # pragma: no cover - defensive
            return ["default"]

    @staticmethod
    def _iter_docs(subdir: str):
        """Yield flat document dicts: ``{id, metadata, page_content}``."""
        try:
            from plugins._memory.helpers.memory import Memory  # type: ignore
        except Exception:  # pragma: no cover - defensive
            return iter(())
        try:
            mem = Memory.get_by_subdir_sync(subdir)  # type: ignore[attr-defined]
        except Exception:
            mem = None
        if mem is None:
            return iter(())
        try:
            docs = list(mem.db.get_all_docs() or [])  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive
            return iter(())
        for d in docs:
            md = getattr(d, "metadata", None) or {}
            mid = md.get("id") if isinstance(md, dict) else None
            if not mid:
                continue
            yield {
                "id": str(mid),
                "metadata": dict(md),
                "page_content": getattr(d, "page_content", ""),
            }

    @staticmethod
    def _persist_assignments(subdir: str, assignments: List[Dict[str, str]]) -> None:
        """Best-effort write-back of ``episode_id`` to FAISS metadata.

        The function maps ``memory_id`` → ``episode_id`` and tries to
        update each document in place via ``Memory.update_documents``.
        Failures are swallowed — the next pass will retry.
        """
        if not assignments:
            return
        try:
            from plugins._memory.helpers.memory import Memory  # type: ignore
        except Exception:  # pragma: no cover - defensive
            return
        try:
            mem = Memory.get_by_subdir_sync(subdir)  # type: ignore[attr-defined]
        except Exception:
            return
        if mem is None:
            return
        # Build a quick id→episode map and only emit updates when needed.
        id_to_ep = {str(a["id"]): a["episode_id"] for a in assignments}
        try:
            docs = list(mem.db.get_all_docs() or [])  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive
            return
        modified = []
        for d in docs:
            md = getattr(d, "metadata", None)
            if not isinstance(md, dict):
                continue
            did = md.get("id")
            if not did:
                continue
            ep = id_to_ep.get(str(did))
            if not ep or md.get("episode_id") == ep:
                continue
            md["episode_id"] = ep
            modified.append(d)
        if not modified:
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():  # pragma: no cover - defensive
                # Fire-and-forget coroutine; we cannot await in this context.
                loop.create_task(mem.update_documents(modified))  # type: ignore[attr-defined]
            else:
                loop.run_until_complete(mem.update_documents(modified))  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning(
                "episode_grouping: persist failed for subdir %r: %s",
                subdir, exc,
            )


def _resolve_agent():  # pragma: no cover - runtime helper
    try:
        from agent import Agent  # type: ignore
        return Agent.get_single()  # type: ignore[attr-defined]
    except Exception:
        return None
