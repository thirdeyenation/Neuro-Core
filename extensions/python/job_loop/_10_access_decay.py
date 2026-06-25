"""Neuro Core — periodic importance-decay job.

This extension is invoked by the framework's job_loop scheduler every
``SLEEP_TIME`` (60 s) seconds via ``call_extensions_async("job_loop")``.
The extension throttles itself to ``config["decay_interval_hours"]``
(default ``24``) and, on each fire, walks every existing memory
subdir, applies the decay formula from
``helpers.lifecycle.run_importance_decay`` and logs a summary.

The extension never raises — every error is caught, logged and
swallowed so the framework scheduler stays healthy.

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
import concurrent.futures
import logging
import time
from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle


_logger = logging.getLogger("neuro_core.job_loop.access_decay")

# Module-level throttle state — updated in-place by the run() method.
# Use a dict (not a bare global) so we never need ``sys.modules[__name__]``
# to find the state. The Agent Zero dynamic extension loader does not
# guarantee that ``sys.modules[__name__]`` resolves to the right module
# object — a previous implementation crashed with
# ``KeyError: '_10_access_decay'`` on every scheduler tick because of
# that assumption.
_STATE: dict[str, float] = {"last_run": 0.0}


# D51 — module-level helpers for async Memory access from a sync extension.
# AccessDecayJob.execute() is async but the framework scheduler invokes
# it from a thread that already has a running loop. We use
# ``ThreadPoolExecutor`` + ``asyncio.run`` (same pattern as D48 fix in
# ``_20_episode_grouping.py``) so the sync ``_iter_docs`` can call the
# framework's async ``Memory.get_by_subdir`` without event-loop crossing.

def _get_memory_sync(subdir: str):
    """Synchronously load ``Memory`` for ``subdir`` from a worker thread.

    Returns the ``Memory`` wrapper on success, ``None`` on any failure.
    Uses a single-worker ``ThreadPoolExecutor`` so ``asyncio.run`` can
    create its own loop without colliding with the framework scheduler.
    The ``timeout=20`` matches the same safety bound used in D48.
    """
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                asyncio.run, _get_memory(subdir),
            ).result(timeout=20)
    except Exception:
        return None


async def _get_memory(subdir: str):
    """Async helper that awaits ``Memory.get_by_subdir``.

    Returns the ``Memory`` wrapper on success, ``None`` on any failure.
    """
    try:
        from plugins._memory.helpers.memory import Memory
        return await Memory.get_by_subdir(subdir, None)
    except Exception:
        return None


class AccessDecayJob(Extension):
    """Periodically decay importance for non-validated, low-stability docs."""

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
                "[neuro_core] AccessDecayJob timed out after 30s — skipped"
            )
        except Exception as e:
            PrintStyle().error(
                f"[neuro_core] AccessDecayJob unhandled error: {e}"
            )

    async def _run(self, **kwargs: Any) -> None:
        # All plugin-local imports live here, NOT at module level.
        from helpers import plugins
        from usr.plugins.neuro_core.helpers.lifecycle import (
            DEFAULT_CONFIG as _LC_DEFAULTS,
        )
        from usr.plugins.neuro_core.helpers.lifecycle import (
            run_importance_decay,
        )

        # ---- 1. Resolve config -------------------------------------------------
        try:
            config = self._read_config()
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning("access_decay: config read failed: %s", exc)
            return

        if not bool(config.get("decay_enabled", _LC_DEFAULTS["decay_enabled"])):
            return

        now = time.time()
        interval = float(config.get(
            "decay_interval_hours", _LC_DEFAULTS["decay_interval_hours"]
        ))
        # ``plugins`` is imported inside the function so the name is
        # available for the ``get_plugin_config`` call below. We silence
        # the unused-import warning while keeping the explicit import
        # (it documents the dependency for readers).
        del plugins

        # ---- 2. Throttle -------------------------------------------------------
        # Use the module-level ``_STATE`` dict. The previous
        # implementation used ``should_run("_last_decay", sys.modules[__name__], ...)``
        # which crashed with ``KeyError: '_10_access_decay'`` on every
        # 60s tick. The in-line check below is the v2 equivalent.
        last_run = float(_STATE.get("last_run", 0.0))
        if last_run and (now - last_run) < interval * 3600.0:
            return
        _STATE["last_run"] = now

        # ---- 3. Walk all subdirs and apply decay -------------------------------
        try:
            subdirs = self._subdirs()
        except Exception as exc:  # pragma: no cover - defensive
            PrintStyle().warning(
                f"neuro_core access_decay: cannot enumerate subdirs: {exc}"
            )
            return

        totals = {"processed": 0, "decayed": 0, "skipped": 0}
        for subdir in subdirs:
            try:
                from usr.plugins.neuro_core.helpers.scores import ScoreStore
                score_store = ScoreStore(subdir)
                docs = list(self._iter_docs(subdir))
                summary = run_importance_decay(
                    subdir, config, score_store, docs=docs
                )
                for k in totals:
                    totals[k] += summary.get(k, 0)
            except Exception as exc:  # pragma: no cover - defensive
                PrintStyle().warning(
                    f"neuro_core access_decay: subdir {subdir!r} failed: {exc}"
                )
                continue

        PrintStyle().info(
            "neuro_core access_decay: subdirs=%d processed=%d decayed=%d skipped=%d"
            % (len(subdirs), totals["processed"], totals["decayed"], totals["skipped"])
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _read_config() -> dict:
        """Return the resolved Neuro Core config dict.

        ``get_plugin_config`` accepts an optional ``agent`` argument; we
        try to find one from the current Extension instance, then fall
        back to a system-wide lookup. If nothing works we synthesise the
        defaults so the extension never crashes on missing config.
        """
        from usr.plugins.neuro_core.helpers.lifecycle import (
            DEFAULT_CONFIG as _LC_DEFAULTS,
        )
        try:
            from helpers.plugins import get_plugin_config  # type: ignore
        except Exception:  # pragma: no cover - defensive
            return dict(_LC_DEFAULTS)
        try:
            # Late import to avoid hard dep on agent at module load time.
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
            _logger.warning("access_decay: get_plugin_config failed: %s", exc)
        return dict(_LC_DEFAULTS)

    @staticmethod
    def _subdirs() -> list:
        """Return the list of memory subdirs that currently exist on disk."""
        try:
            from plugins._memory.helpers.memory import (  # type: ignore
                get_existing_memory_subdirs,
            )
        except Exception:
            # If the _memory plugin is unavailable, fall back to scanning
            # the default db dir.
            return ["default"]
        try:
            return list(get_existing_memory_subdirs() or ["default"])  # type: ignore[misc]
        except Exception:  # pragma: no cover - defensive
            return ["default"]

    @staticmethod
    def _iter_docs(subdir: str):
        """Yield ``(memory_id, metadata)`` pairs for a given subdir.

        Returns an empty iterator when the FAISS index is not present
        or when ``Memory`` cannot be loaded. The ``(id, metadata)`` shape
        matches ``helpers.lifecycle.run_importance_decay``'s expected
        input.

        D51 — ``Memory.get_by_subdir_sync`` does NOT exist on the Memory
        class. The previous implementation silently returned an empty
        iterator, which made every AccessDecayJob run produce
        ``processed=0 decayed=0 skipped=0``. We now use the same
        ``ThreadPoolExecutor`` + ``asyncio.run`` pattern as D48 in
        ``_20_episode_grouping.py`` to bridge from this sync method to
        the framework's async ``Memory.get_by_subdir``.
        """
        mem = _get_memory_sync(subdir)
        if mem is None:
            return iter(())
        try:
            raw = mem.db.get_all_docs() or {}
            docs = raw.values() if isinstance(raw, dict) else raw
        except Exception:  # pragma: no cover - defensive
            return iter(())
        result = []
        for d in docs:
            md = getattr(d, "metadata", None) or {}
            mid = md.get("id") if isinstance(md, dict) else None
            if not mid:
                continue
            result.append((str(mid), md))
        return iter(result)


def _resolve_agent():  # pragma: no cover - runtime helper
    """Best-effort lookup of a single Agent instance for config reading.

    The job_loop scheduler does not pass an ``agent`` kwarg, so this
    helper tries (in order): ``self.agent`` on the extension, the
    AgentZero singleton, and finally the first agent from the global
    ``agents`` registry. Returns ``None`` on any failure.
    """
    try:
        from agent import Agent  # type: ignore
        return Agent.get_single()  # type: ignore[attr-defined]
    except Exception:
        return None
