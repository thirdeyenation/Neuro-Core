"""Neuro Core — periodic contradiction-detection job.

Sweeps ``fact`` memories in every subdir and compares them pairwise
with the lexical heuristic in ``helpers/lifecycle.py``
(``run_contradiction_detection``), which returns the counters
``{"checked": int, "disputed": int}`` plus an additive ``disputes``
list (WI-P60, KI-034): one entry per detection with ``memory_id``
(the disputed, older memory), ``disputed_id`` (the newer opposing
memory), ``detected_at`` and ``basis``. The job persists each disputed
status to FAISS document metadata (``validation_status = "disputed"``)
via the best-effort ``_persist_disputes`` write-back, enforcing the
transition governance ruled in WI-P60 (only unvalidated/validated ->
disputed is written; terminal states are skipped; disputed -> disputed
is a no-op). Until the boundary-6 amendment decides a durable audit
home, the per-detection structural log lines emitted by the lifecycle
helper are the reconstructable dispute audit trail.

The extension is throttled to ``contradiction_interval_hours``
(default ``168`` = one week) and never raises — errors are caught,
logged and swallowed so the framework scheduler stays healthy.

**v1 safety note:** the LLM-assisted contradiction detection path is
**not implemented in v0.1.0** and no LLM calls are ever made from this
job, regardless of the ``contradiction_llm_enabled`` config key
(``False`` default). The ``contradiction_llm_enabled`` key changes only
what gets logged: when false, a heuristic-only note is logged; when
true, a "not yet implemented — heuristic fallback" note is logged.
The heuristic sweep itself runs in both states (WI-P41 fix for KI-032).

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
import copy
import logging
import time
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

# WI-P60 (KI-034) — Q1 ruling: explicit domain->FAISS vocabulary mapping at
# the persistence boundary. helpers/metadata.py ValidationStatus
# (unvalidated | validated | disputed | deprecated) is canonical here;
# the memory_lifecycle.py domain vocabulary (unreviewed | validated |
# disputed | superseded) maps onto it: unreviewed is a domain-only alias
# of unvalidated, superseded maps to deprecated. No value outside this
# mapping is ever invented; unknown values are skipped defensively.
_STATUS_VOCAB_MAP: dict[str, str] = {
    "unreviewed": "unvalidated",
    "unvalidated": "unvalidated",
    "validated": "validated",
    "disputed": "disputed",
    "superseded": "deprecated",
    "deprecated": "deprecated",
}


# Boot-grace guard (WI-2026-09-04-PHASE0-PATCH-ARCH, D-NC1-015 remediation).
# ---------------------------------------------------------------------------
# At framework boot, a fresh process means fresh throttle state, so the first
# job_loop tick fires immediately — potentially while the framework is still
# initializing (watchdog registration in particular, see the boot-deadlock
# diagnosis). A heavy first run also floods filesystem events during that
# window. This guard defers every tick until the process has been up for at
# least ``_BOOT_GRACE_SECONDS`` and — critically — a skipped tick records NO
# throttle state, so the first post-grace tick runs the real job immediately.
_BOOT_GRACE_SECONDS: float = 300.0
_IMPORT_MONOTONIC: float = time.monotonic()


def _process_uptime_seconds() -> float:
    """Return the current process uptime in seconds.

    Primary source: ``/proc/self/stat`` field 22 (``starttime``, in
    clock ticks after boot) combined with ``/proc/stat``'s ``btime``
    (boot epoch) — this yields true process age regardless of when this
    module happened to be imported by the framework's dynamic loader.

    Fallback (non-Linux or /proc unavailable): seconds since this
    module was imported, via ``time.monotonic()``. That under-estimates
    process uptime, which only ever *extends* the grace period — the
    fail-safe direction.
    """
    try:
        with open("/proc/self/stat", "rb") as f:
            raw = f.read()
        # Field 2 (comm) may contain spaces and parentheses; anchor on the
        # LAST ')' — everything after it starts at field 3 (state).
        tail = raw[raw.rindex(b")") + 1:].split()
        starttime_ticks = float(tail[19])  # field 22 = starttime, in CLK_TCK
        btime = None
        with open("/proc/stat", "rb") as f:
            for line in f:
                if line.startswith(b"btime"):
                    btime = float(line.split()[1])
                    break
        if btime is None:
            raise ValueError("btime not found in /proc/stat")
        hz = 100.0  # CLK_TCK is 100 on standard Linux kernels
        return max(0.0, time.time() - btime - starttime_ticks / hz)
    except Exception:
        return time.monotonic() - _IMPORT_MONOTONIC


def _boot_grace_active() -> bool:
    """True while the process is younger than ``_BOOT_GRACE_SECONDS``.

    Fail-safe: on any error computing uptime, treat the process as
    still inside the grace period (skipping a tick is always safe; a
    spurious 'expired' verdict is not).
    """
    try:
        return _process_uptime_seconds() < _BOOT_GRACE_SECONDS
    except Exception:  # pragma: no cover - defensive
        return True


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


def _all_docs_from_db(db: Any) -> List[Any]:
    """Return the Documents held by a MyFaiss-like db, defensively.

    WI-P60 revision 2 (VAL integration-FAIL fix, defect 1): the real
    host contract — ``plugins/_memory/helpers/memory.py``
    ``MyFaiss.get_all_docs`` — returns ``self.docstore._dict``, a DICT
    of ``docstore_id -> Document``. Iterating it directly yields string
    keys with no ``.metadata``, so zero documents are ever matched.
    This helper iterates ``.values()`` for the dict contract and still
    accepts a list-shaped return defensively (older/alternative
    contracts), so both shapes work.
    """
    try:
        raw = db.get_all_docs()
    except Exception:  # pragma: no cover - defensive
        return []
    if isinstance(raw, dict):
        return [d for d in raw.values() if d is not None]
    return [d for d in (raw or []) if d is not None]


def _normalize_memory_handle(mem: Any, subdir: str) -> Any:
    """Normalize a resolved memory handle to a Memory-wrapper shape.

    WI-P60 revision 2 (VAL integration-FAIL fix, defect 2): the
    production warm cache holds RAW ``MyFaiss`` objects in
    ``Memory.index`` (``plugins/_memory/helpers/memory.py`` —
    ``index: dict[str, MyFaiss]``), which have NO ``.db`` attribute and
    no ``update_documents``; the warm path (production norm) previously
    failed with a swallowed AttributeError and silently no-op'd.

    A resolved ``Memory`` wrapper (has ``.db`` and an async
    ``update_documents``) is returned unchanged. A raw MyFaiss-like
    object (has ``get_all_docs``/``docstore``) is wrapped in the real
    ``Memory`` class — its ``__init__`` is a trivial
    ``(db, memory_subdir)`` assignment (verified by source read), so
    wrapping is side-effect-free and the real host
    ``update_documents`` mechanism (adelete + aadd_documents +
    ``_save_db``) is used for both cache states.

    Returns ``None`` when the handle cannot be normalized (caller must
    treat ``None`` as "skip this subdir").
    """
    if mem is None:
        return None
    if hasattr(mem, "db") and callable(getattr(mem, "update_documents", None)):
        return mem
    if callable(getattr(mem, "get_all_docs", None)) or hasattr(mem, "docstore"):
        try:
            from plugins._memory.helpers.memory import Memory as _Memory  # type: ignore
        except Exception:  # pragma: no cover - defensive
            _logger.warning(
                "contradiction_detection: cannot wrap raw memory handle for "
                "subdir %r: host Memory class unavailable",
                subdir,
            )
            return None
        try:
            return _Memory(db=mem, memory_subdir=subdir)
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning(
                "contradiction_detection: cannot wrap raw memory handle for "
                "subdir %r: %s", subdir, exc,
            )
            return None
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
        # ---- 0. Boot-grace guard (D-NC1-015 remediation) ----------------------
        # Skip every tick until the process has been up for at least
        # ``_BOOT_GRACE_SECONDS``. A skipped tick must record NO
        # throttle state (no ``_STATE['last_run']`` update), so the
        # first post-grace tick runs the real job immediately. This
        # guard sits before config resolution and before any throttle
        # check/update for exactly that reason.
        if _boot_grace_active():
            return

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
        # LLM-assisted contradiction detection is **not implemented in
        # v0.1.0** and this job never constructs or passes an LLM
        # object (v1 safety guarantee, retained). The ``contradiction_llm_enabled``
        # key therefore changes only what gets logged here — the
        # heuristic sweep below runs in both states (WI-P41 fix for
        # KI-032: previously the false-state branch early-returned and
        # the scheduled sweep performed no check at all).
        if not config.get("contradiction_llm_enabled", False):
            PrintStyle().info(
                "[neuro_core] ContradictionDetectionJob: heuristic-only "
                "mode (contradiction_llm_enabled=False) — running heuristic sweep"
            )
        else:
            PrintStyle().info(
                "[neuro_core] ContradictionDetectionJob: LLM-assisted NLI is "
                "not yet implemented in v0.1.0 (contradiction_llm_enabled=True) — "
                "falling back to the heuristic sweep; no LLM calls are made"
            )

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
                # WI-P41 fix (KI-032): the verified signature
                # (helpers/lifecycle.py) accepts ``facts=`` — an iterable
                # of ``(memory_id, content, metadata)`` triples — not
                # ``docs=``. With ``memory=None`` the function runs the
                # O(n^2) lexical heuristic entirely on these triples
                # (no LLM, no FAISS access).
                facts = [
                    (str(d["id"]), d.get("page_content", ""), dict(d.get("metadata") or {}))
                    for d in docs
                ]
                result = run_contradiction_detection(subdir, config, None, facts=facts)
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
        # WI-P60 rev 2 (VAL integration-FAIL fix, defect 2 on the read
        # side): the production warm cache holds RAW MyFaiss objects in
        # Memory.index (no .db attribute); normalize the handle so the
        # sweep enumerates facts in both warm-cache and cold-cache
        # states instead of silently yielding nothing.
        mem = _normalize_memory_handle(mem, subdir)
        if mem is None:
            return iter(())
        try:
            # WI-P60 rev 2 (VAL integration-FAIL fix, defect 1): the real
            # host contract — MyFaiss.get_all_docs — returns a DICT
            # (docstore_id -> Document); list-iterating it yields string
            # keys with no .metadata, so zero facts were ever enumerated
            # on the real path. Iterate via the defensive helper
            # (handles both dict and list contracts).
            docs = _all_docs_from_db(mem.db)  # type: ignore[arg-type]
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

        WI-P60 (KI-034): consumes the lifecycle's ``disputes`` list —
        entries carry ``memory_id`` (the disputed, older memory),
        ``disputed_id``, ``detected_at`` and ``basis``.

        Transition governance (WI-P60 Q4 ruling, enforced here at the
        persistence boundary):

        - only ``unvalidated``/``unreviewed`` -> ``disputed`` and
          ``validated`` -> ``disputed`` are written;
        - documents already in a terminal state (``deprecated`` FAISS /
          ``superseded`` domain) are skipped — no transition out of a
          terminal state is ever written;
        - ``disputed`` -> ``disputed`` is a no-op;
        - any other/unknown current value is skipped defensively (no
          vocabulary value is invented).

        Failure semantics (WI-P60 rev 3, VAL defect fix — previously the
        docstring promised a retry the implementation could not honor):
        the write payload is built from deep copies, so the in-memory
        document metadata — and therefore the state the next pass sees —
        is never mutated before a successful write. A failing
        ``update_documents`` is logged and swallowed (the scheduler
        never crashes); because the on-disk status still shows the
        original value, the next sweep pass classifies the transition as
        still needed and retries the persist.
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
            mem = None
        # WI-P60 rev 2 (VAL integration-FAIL fix, defect 2): the
        # production warm cache holds RAW MyFaiss objects in
        # Memory.index (no .db, no update_documents); normalize the
        # handle so the real host update_documents mechanism is used in
        # both warm-cache (production norm) and cold-cache states.
        mem = _normalize_memory_handle(mem, subdir)
        if mem is None:
            return
        # Disputed memory ids from the lifecycle payload (keyed by
        # ``memory_id``; ``id`` accepted for backward compatibility).
        disputed_targets = {
            str(d.get("memory_id") or d.get("id"))
            for d in disputes
            if d.get("memory_id") or d.get("id")
        }
        if not disputed_targets:
            return
        try:
            # WI-P60 rev 2 (VAL integration-FAIL fix, defect 1): iterate
            # via the defensive helper — the real host contract returns a
            # DICT (docstore_id -> Document), not a list.
            docs = _all_docs_from_db(mem.db)  # type: ignore[arg-type]
        except Exception:  # pragma: no cover - defensive
            return
        modified = []
        for d in docs:
            md = getattr(d, "metadata", None)
            if not isinstance(md, dict):
                continue
            did = md.get("id")
            if not did or str(did) not in disputed_targets:
                continue
            # Q4: map the current status through the explicit Q1
            # vocabulary mapping and apply the transition rules.
            current = _STATUS_VOCAB_MAP.get(
                str(md.get("validation_status", "unvalidated")).lower(), ""
            )
            if current == "deprecated":
                # Terminal state (FAISS ``deprecated`` / domain
                # ``superseded``): never transitioned by the sweep.
                continue
            if current == "disputed":
                # disputed -> disputed is a no-op.
                continue
            if current not in ("unvalidated", "validated"):
                # Unknown/unmapped value: skip defensively — never
                # invent a vocabulary value.
                _logger.warning(
                    "contradiction_detection: skipping dispute persist for %r "
                    "in subdir %r: unmapped validation_status %r",
                    did, subdir, md.get("validation_status"),
                )
                continue
            modified.append((d, current))
        if not modified:
            return
        # WI-P60 rev 3 (VAL defect fix — mutation-before-write): the
        # write payload is built from DEEP COPIES. The real host
        # ``Memory.update_documents`` re-adds the exact Document objects
        # it is given (adelete + aadd_documents), so mutating the
        # originals before the write would leave the in-memory state as
        # ``disputed`` even when the write fails — the next pass would
        # then see disputed->disputed and no-op, silently losing the
        # dispute. With staged copies the originals (and the state the
        # next pass reads) keep their pre-write status until the write
        # actually succeeds, so a failed write is retried on the next
        # pass.
        payload = []
        for d, _prev in modified:
            staged = copy.deepcopy(d)
            staged.metadata["validation_status"] = "disputed"
            payload.append(staged)

        def _log_persisted() -> None:
            for d, prev in modified:
                _logger.info(
                    "contradiction_detection: dispute persisted "
                    "memory_id=%s subdir=%s validation_status=disputed "
                    "previous_status=%s",
                    d.metadata.get("id"), subdir, prev,
                )

        def _log_persist_failed(exc: BaseException) -> None:
            _logger.warning(
                "contradiction_detection: persist failed for subdir %r: %s "
                "(on-disk status unchanged; the next pass will retry)",
                subdir, exc,
            )

        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running event loop (typical job-loop context under
                # Python 3.13): run the coroutine to completion. The
                # "dispute persisted" log line is emitted only after the
                # write is confirmed successful.
                asyncio.run(mem.update_documents(payload))
                _log_persisted()
            else:
                # Fire-and-forget coroutine; we cannot await in this
                # context. The done-callback logs success/failure after
                # the task completes and consumes the exception so the
                # loop never reports an unretrieved task exception.
                task = loop.create_task(mem.update_documents(payload))  # type: ignore[attr-defined]

                def _on_write_done(t: "asyncio.Task") -> None:
                    exc = t.exception()
                    if exc is not None:
                        _log_persist_failed(exc)
                    else:
                        _log_persisted()

                task.add_done_callback(_on_write_done)
        except Exception as exc:  # pragma: no cover - defensive
            _log_persist_failed(exc)


def _resolve_agent():  # pragma: no cover - runtime helper
    try:
        from agent import Agent  # type: ignore
        return Agent.get_single()  # type: ignore[attr-defined]
    except Exception:
        return None
