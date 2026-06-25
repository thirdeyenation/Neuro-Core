"""Neuro Core — periodic contradiction-detection job.

Sweeps ``fact`` memories in every subdir, pairs them with semantically
similar but content-opposing candidates, and marks the older of the
two ``validation_status = "disputed"``. The result is appended to the
contradiction log so the agent (or a human reviewer) can adjudicate
the dispute later.

The extension is throttled to ``contradiction_interval_hours``
(default ``168`` = one week) and never raises — errors are caught,
logged and swallowed so the framework scheduler stays healthy.

**v1 safety note:** the LLM-based contradiction detection path is
**disabled by default** via the ``contradiction_llm_enabled`` config
key (``False``). The job still runs every ``contradiction_interval_hours``
to log a "skipped (LLM disabled)" message, but no LLM calls are made
until the path is validated in a controlled setting.

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
from typing import Any, Dict, List

from helpers.extension import Extension
from helpers.print_style import PrintStyle


_logger = logging.getLogger("neuro_core.job_loop.contradiction_detection")

# Module-level throttle state — updated in-place by the run() method.
# Use a dict (not a bare global) so we never need ``sys.modules[__name__]``
# to find the state. The Agent Zero dynamic extension loader does not
# guarantee that ``sys.modules[__name__]`` resolves to the right module
# object — a previous implementation crashed with
# ``KeyError: '_30_contradiction_detection'`` on every scheduler tick
# because of that assumption.
_STATE: dict[str, float] = {"last_run": 0.0}


def _get_memory_sync(MemoryCls, subdir: str):
    """Resolve a ``Memory`` handle for ``subdir`` synchronously.

    D52 helper for the broken ``Memory.get_by_subdir_sync`` references
    that previously lived in ``_iter_docs`` and ``_persist_disputes``.
    Resolution is two-step:

      1. **Warm cache (sync, safe everywhere).** The framework keeps
         a class-level ``Memory.index`` dict mapping ``memory_subdir``
         → ``Memory`` handle. In production the agent has already
         loaded its memory subdir long before the scheduler tick
         fires, so the cache is warm and we return immediately.

      2. **Cold-cache fallback.** If the cache is cold, attempt
         ``asyncio.run`` of the real async ``Memory.get_by_subdir``.
         Two sub-cases:

         a. **No running loop** (the common production case — the
            scheduler tick runs in a ``DeferredTask`` worker thread
            outside the framework's main loop). ``asyncio.run`` works
            directly.

         b. **Running loop present** (e.g. when the job is invoked
            directly from a test harness via
            ``asyncio.run(job.execute(...))``). ``asyncio.run`` would
            raise ``RuntimeError``. We escape via a one-shot
            ``ThreadPoolExecutor`` — the executor runs
            ``asyncio.run`` in a worker thread that has no running
            loop, so the async loader completes successfully.

    Returns the ``Memory`` handle on success, ``None`` on any
    failure (caller must treat ``None`` as "skip this subdir").
    """
    # ---- Step 1: warm cache fast path -----------------------------------
    try:
        index = getattr(MemoryCls, "index", None)
        if isinstance(index, dict):
            cached = index.get(subdir)
            if cached is not None:
                return cached
    except Exception:  # pragma: no cover - defensive
        pass
    # ---- Step 2: cold-cache fallback ------------------------------------
    # Try direct asyncio.run first (fast path — works when no loop is
    # running, which is the production DeferredTask case).
    try:
        mem = asyncio.run(MemoryCls.get_by_subdir(subdir, None))  # type: ignore[arg-type]
        return mem
    except RuntimeError:
        # Running loop detected — asyncio.run cannot nest. Escape via
        # a worker thread that has no running loop of its own.
        pass
    except Exception:  # pragma: no cover - defensive
        return None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            mem = pool.submit(
                asyncio.run,
                MemoryCls.get_by_subdir(subdir, None),  # type: ignore[arg-type]
            ).result(timeout=30)
        return mem
    except Exception:  # pragma: no cover - defensive
        return None


class ContradictionDetectionJob(Extension):
    """Periodically flag contradicting fact memories as ``disputed``."""

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
                "[neuro_core] ContradictionDetectionJob timed out after 30s — skipped"
            )
        except Exception as e:
            PrintStyle().error(
                f"[neuro_core] ContradictionDetectionJob unhandled error: {e}"
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
            _logger.warning("contradiction_detection: config read failed: %s", exc)
            return

        if not bool(config.get(
            "contradiction_detection_enabled",
            _LC_DEFAULTS["contradiction_detection_enabled"],
        )):
            return

        # ---- 1a. v1 LLM safety gate -------------------------------------------
        # The LLM-based contradiction detection path is **disabled by
        # default** in v1. We still log a "skipped" message on each
        # scheduled run so the user can confirm the job is alive, but
        # we never call ``run_contradiction_detection`` with a real
        # LLM object. This ensures the job runs safely (does nothing)
        # until the LLM path is validated in a controlled setting.
        if not config.get("contradiction_llm_enabled", False):
            PrintStyle().info(
                "[neuro_core] ContradictionDetectionJob: LLM path disabled "
                "(contradiction_llm_enabled=False) — skipping LLM scan"
            )
            return

        interval = float(config.get(
            "contradiction_interval_hours",
            _LC_DEFAULTS.get("contradiction_interval_hours", 168),
        ))

        # ---- 2. Throttle -------------------------------------------------------
        # Use the module-level ``_STATE`` dict. The previous
        # implementation used ``should_run("_last_contradiction", sys.modules[__name__], ...)``
        # which crashed with ``KeyError: '_30_contradiction_detection'``
        # on every 60s tick. The in-line check below is the v2
        # equivalent.
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
                f"neuro_core contradiction_detection: cannot enumerate subdirs: {exc}"
            )
            return

        totals = {"checked": 0, "disputed": 0}
        for subdir in subdirs:
            try:
                from usr.plugins.neuro_core.helpers.lifecycle import (
                    run_contradiction_detection,
                )
                docs = list(self._iter_docs(subdir))
                result = run_contradiction_detection(subdir, config, None, docs=docs)
                totals["checked"] += int(result.get("checked", 0))
                totals["disputed"] += int(result.get("disputed", 0))
                # Best-effort: persist disputed statuses to FAISS metadata.
                self._persist_disputes(subdir, result.get("disputes", []))
            except Exception as exc:  # pragma: no cover - defensive
                PrintStyle().warning(
                    f"neuro_core contradiction_detection: subdir {subdir!r} failed: {exc}"
                )
                continue

        PrintStyle().info(
            "neuro_core contradiction_detection: subdirs=%d checked=%d disputed=%d"
            % (len(subdirs), totals["checked"], totals["disputed"])
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
            _logger.warning("contradiction_detection: get_plugin_config failed: %s", exc)
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
        try:
            from plugins._memory.helpers.memory import Memory  # type: ignore
        except Exception:  # pragma: no cover - defensive
            return iter(())
        try:
            mem = _get_memory_sync(Memory, subdir)
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
    def _persist_disputes(subdir: str, disputes: List[Dict[str, str]]) -> None:
        """Best-effort write-back of ``validation_status = "disputed"``.

        Failures are swallowed — the next pass will retry.
        """
        if not disputes:
            return
        try:
            from plugins._memory.helpers.memory import Memory  # type: ignore
        except Exception:  # pragma: no cover - defensive
            return
        try:
            mem = _get_memory_sync(Memory, subdir)
        except Exception:
            return
        if mem is None:
            return
        id_to_status = {str(d["id"]): d.get("status", "disputed") for d in disputes}
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
            new_status = id_to_status.get(str(did))
            if not new_status or md.get("validation_status") == new_status:
                continue
            md["validation_status"] = new_status
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
                "contradiction_detection: persist failed for subdir %r: %s",
                subdir, exc,
            )


def _resolve_agent():  # pragma: no cover - runtime helper
    try:
        from agent import Agent  # type: ignore
        return Agent.get_single()  # type: ignore[attr-defined]
    except Exception:
        return None
