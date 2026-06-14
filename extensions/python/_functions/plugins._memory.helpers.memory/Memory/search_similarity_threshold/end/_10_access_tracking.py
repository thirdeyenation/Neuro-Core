"""Neuro Core access-tracking end-hook for ``Memory.search_similarity_threshold``.

When the agent runs a similarity search, every returned document is
considered "accessed". We persist that access to the sidecar
``scores.json`` via ``ScoreStore.update_access`` so the in-memory
metadata mirror stays consistent without rewriting the FAISS index.

The hook contract (per the framework's ``_functions`` extensibility):
    - The file is named ``_NN_<name>.py``.
    - The class inside has the same qualified name as the parent
      function (``Memory.search_similarity_threshold``), and inherits
      from the framework's ``Extensible`` wrapper.
    - The class has an ``end()`` coroutine method that receives the
      return value of the wrapped method and returns a possibly-
      modified version of it.

The framework invokes ``end()`` after the original method completes.
We do NOT mutate the returned documents in-place — framework callers
hold shared references to the returned Document objects, and
mutating their metadata breaks Agent Zero's own context retrieval.
Access tracking is persisted exclusively to the sidecar
``scores.json`` via ``ScoreStore.update_access(memory_id)``.

Robustness rules (per the spec):
    - Skip documents that have no ``id`` in metadata.
    - Wrap ``ScoreStore`` operations in try/except so a sidecar
      failure (missing dir, permission, etc.) never crashes the
      search.
    - Never modify the return type, shape, or per-document
      metadata dicts of the original response. Access tracking is
      persisted to the sidecar only.

Stability contract (v2):
    - Plugin-local imports (``from usr.plugins.neuro_core...``) are
      moved inside the method that uses them. Module-level imports
      of plugin-local code are NOT permitted in hook files because
      the framework's dotted-path extension loader may not have
      initialised the ``usr.plugins.*`` package at import time.
    - The body is wrapped in a broad ``except Exception`` that
      logs a warning and returns the original response. If this
      hook throws, it would break ALL
      ``Memory.search_similarity_threshold()`` calls system-wide
      (not just Neuro Core operations), so failure is non-fatal.
    - The method never re-raises under any circumstances; the
      original response is always returned (possibly with the
      access-tracking side-effect skipped).
"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime, timezone
from typing import Any, List

from helpers.print_style import PrintStyle


_logger = logging.getLogger("neuro_core.access_tracking")
if not _logger.handlers:  # pragma: no cover - logging bootstrap
    _logger.setLevel(logging.INFO)


def _now_iso() -> str:
    """UTC ISO 8601 timestamp with second precision."""
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:  # pragma: no cover - defensive
        return ""


def _coerce_memory_subdir(memory) -> str:
    """Best-effort extraction of ``memory_subdir`` from a Memory instance."""
    try:
        if memory is None:
            return "default"
        sub = getattr(memory, "memory_subdir", None)
        if isinstance(sub, str) and sub:
            return sub
    except Exception:  # pragma: no cover - defensive
        pass
    return "default"


def _coerce_id(doc: Any) -> str | None:
    """Extract the document ID from a Document-like object."""
    if doc is None:
        return None
    md = getattr(doc, "metadata", None)
    if not isinstance(md, dict):
        return None
    did = md.get("id")
    if isinstance(did, str) and did:
        return did
    return None


def _resolve_score_store():  # pragma: no cover - lazy import
    """Lazy import of ``ScoreStore`` to avoid module-load time failures."""
    try:
        from usr.plugins.neuro_core.helpers.scores import ScoreStore
        return ScoreStore
    except Exception as exc:  # pragma: no cover - defensive
        _logger.warning(
            "neuro_core access_tracking: ScoreStore import failed: %s",
            exc,
        )
        return None


def _bump_in_metadata(doc: Any, now_iso: str) -> int:
    """Increment ``access_count`` in doc.metadata; return new count."""
    md = getattr(doc, "metadata", None)
    if not isinstance(md, dict):
        return 0
    try:
        current = int(md.get("access_count", 0) or 0)
    except (TypeError, ValueError):
        current = 0
    current += 1
    md["access_count"] = current
    if now_iso:
        md["last_accessed_at"] = now_iso
    return current


class Memory_search_similarity_threshold:
    """End-hook for ``Memory.search_similarity_threshold``."""

    async def end(
        self,
        response: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Persist access tracking after a similarity search completes.

        ``response`` is the value returned by the original method.
        The framework passes it as the first positional arg; we accept
        it positionally and via ``response=`` for robustness across
        framework versions.

        The body is wrapped in a broad ``except Exception`` that
        logs a warning and returns the original response. The
        access-tracking side-effect is best-effort and must never
        break the search call that triggered it.
        """
        # All plugin-local imports live inside end() because the
        # framework's dotted-path extension loader does not guarantee
        # the ``usr.plugins.*`` package is initialised at module-import
        # time. Importing at the top of this file would also pull in
        # the ``usr.plugins.neuro_core.helpers.scores`` chain (which
        # depends on helpers.plugins, agent, FAISS, ...) even when
        # the hook is loaded but never invoked.
        try:
            from usr.plugins.neuro_core.helpers.scores import ScoreStore  # noqa: F401
            return await self._end_impl(response, *args, **kwargs)
        except Exception as e:
            # Never re-raise — access tracking must not break the search.
            PrintStyle().warning(
                f"[neuro_core] access_tracking hook non-fatal: {e}"
            )
            return response

    async def _end_impl(
        self,
        response: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Inner implementation of the end-hook.

        Split out so the outer ``end()`` method can wrap it in a
        try/except and still return the original ``response``
        unchanged on any failure. This preserves the framework's
        return-type contract (same shape, same order) even when
        access tracking itself is broken.
        """
        # --- 1. Unpack arguments (the framework passes both the
        # return value and the original args; tolerate either shape).
        original_args: tuple = args
        if response is None and args:
            response = args[0]
            original_args = args[1:]

        # Pull the Memory instance from the call args so we know
        # which subdir's sidecar to write to.
        memory = None
        try:
            if original_args:
                memory = original_args[0]
            elif "memory" in kwargs:
                memory = kwargs.get("memory")
            elif "self" in kwargs:
                memory = kwargs.get("self")
        except Exception:  # pragma: no cover - defensive
            memory = None

        if memory is None:
            # Try to recover the Memory from the first non-None arg of
            # the original method's bound call. This is best-effort
            # and silently degrades to metadata-only tracking.
            for a in original_args:
                if a is not None and getattr(a, "db", None) is not None:
                    memory = a
                    break

        # --- 2. Normalise the response shape. The wrapped method may
        # return either a list of documents or a dict such as
        # ``{"documents": [...], "distances": [...]}``. Support both.
        docs: List[Any] = []
        if response is None:
            return response  # Nothing to do.
        if isinstance(response, dict):
            for key in ("documents", "docs", "results"):
                if key in response and isinstance(response[key], list):
                    docs = list(response[key])
                    break
            if not docs and "matches" in response and isinstance(
                response["matches"], list
            ):
                docs = list(response["matches"])
        elif isinstance(response, (list, tuple)):
            docs = list(response)
        else:
            # Single document or unknown shape: track it as a one-item list.
            docs = [response]

        if not docs:
            return response

        # --- 3. Collect IDs to track. We do NOT mutate doc.metadata
        # in-place here: framework callers hold shared references to
        # the returned Document objects, and mutating their metadata
        # breaks Agent Zero's own context retrieval. Access tracking is
        # persisted exclusively to the scores.json sidecar via
        # ScoreStore.update_access() below.
        ids_to_track: list[str] = []
        for doc in docs:
            try:
                did = _coerce_id(doc)
                if not did:
                    continue
                ids_to_track.append(did)
            except Exception as exc:  # pragma: no cover - defensive
                _logger.warning(
                    "neuro_core access_tracking: id collection failed for %r: %s",
                    doc,
                    exc,
                )

        # --- 4. Persist to the sidecar. Failures must NOT crash the search.
        if not ids_to_track:
            return response

        ScoreStore = _resolve_score_store()
        if ScoreStore is None:
            return response

        subdir = _coerce_memory_subdir(memory)
        try:
            store = ScoreStore(subdir)
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning(
                "neuro_core access_tracking: could not open ScoreStore(%r): %s",
                subdir,
                exc,
            )
            return response

        for mid in ids_to_track:
            try:
                store.update_access(mid)
            except Exception as exc:  # pragma: no cover - defensive
                _logger.warning(
                    "neuro_core access_tracking: update_access(%r) failed: %s\n%s",
                    mid,
                    exc,
                    traceback.format_exc(),
                )

        return response
