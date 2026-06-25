"""
Neuro Core metadata validator for the start of ``Memory.insert_documents``.

Hook path:
    extensions/python/_functions/plugins._memory.helpers.memory/
    Memory/insert_documents/start/_10_neuro_metadata.py

The start-hook contract (see ``helpers/extension.py``) is:

- ``data["args"]``     = positional args of the wrapped call; for a bound
                          ``Memory.insert_documents(docs)`` invocation that is
                          ``(self, docs)`` where ``docs`` is a list of
                          ``Document`` instances.
- ``data["kwargs"]``   = keyword args (usually empty for this call).
- ``data["result"]``   = the wrapped function's return value; starts as the
                          internal ``_UNSET`` sentinel and is set by either an
                          extension short-circuit or the wrapped function.

Behavior:
    Iterate the incoming ``Document`` objects, normalise every metadata
    dict through ``validate_neuro_metadata`` and ``apply_seeding``, then
    yield control to the original implementation (i.e. do not set
    ``data["result"]`` — the framework will call the wrapped function
    with the (possibly mutated) ``docs`` list).

The hook is intentionally a no-op for documents that already carry a
``memory_type`` field; validation is idempotent and only ever narrows
the value, so re-inserts and dashboard edits do not flip valid types.

Stability contract (v2):
    - Plugin-local imports (``from usr.plugins.neuro_core...``) are
      moved inside the method that uses them. Module-level imports
      of plugin-local code are NOT permitted in hook files because
      the framework's dotted-path extension loader may not have
      initialised the ``usr.plugins.*`` package at import time.
    - The body is wrapped in a broad ``except Exception`` that
      logs a warning and returns. If this hook throws, it would
      break ALL ``Memory.insert_documents()`` calls system-wide
      (not just Neuro Core operations), so failure is non-fatal.
    - The method never re-raises under any circumstances.
"""

from __future__ import annotations

from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle


class NeuroInsertMetadata(Extension):
    """Normalise and seed Neuro Core metadata on every inserted document."""

    def execute(self, **kwargs: Any) -> None:
        """Hook entry point — NEVER re-raises.

        The wrapper logs a warning on any failure and returns
        normally. An unhandled exception here would propagate to
        ``Memory.insert_documents()`` and break ALL memory insert
        calls in the system, not just Neuro Core operations.
        """
        # All plugin-local imports live inside execute() because the
        # framework's dotted-path extension loader does not guarantee
        # the ``usr.plugins.*`` package is initialised at module-import
        # time. Importing at the top of this file would also pull in
        # the ``usr.plugins.neuro_core.helpers.metadata`` chain
        # (which depends on helpers.plugins, agent, FAISS, ...)
        # even when the hook is loaded but never invoked.
        try:
            from usr.plugins.neuro_core.helpers.metadata import (
                apply_seeding,
                validate_neuro_metadata,
            )

            data: dict = kwargs.get("data") or {}
            args: tuple = tuple(data.get("args") or ())

            # Bound-method call: (self, docs). Defensive: tolerate 1-arg form too.
            if len(args) < 2:
                return
            docs = args[1]
            if docs is None:
                return
            if not hasattr(docs, "__iter__"):
                return

            normalised = 0
            for doc in docs:
                metadata = getattr(doc, "metadata", None)
                if not isinstance(metadata, dict):
                    continue
                new_meta = dict(metadata)
                validate_neuro_metadata(new_meta)
                apply_seeding(new_meta)
                doc.metadata = new_meta
                normalised += 1

            # No data["result"] = no short-circuit; the original function runs
            # with the mutated docs list and writes to FAISS as usual.
            if normalised:
                # Intentionally silent in production; debug-only breadcrumb.
                pass
        except Exception as e:
            # Never re-raise — memory insert must not be blocked by this hook.
            PrintStyle().warning(
                f"[neuro_core] metadata hook non-fatal: {e}"
            )
