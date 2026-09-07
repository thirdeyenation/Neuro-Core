"""Neuro Core cascade cleanup for the end of ``Memory.delete_documents_by_ids``.

Hook path:
    extensions/python/_functions/plugins._memory.helpers.memory/
    Memory/delete_documents_by_ids/end/_10_graph_cascade.py

The end-hook contract (see ``helpers/extension.py``) is:

- ``data["args"]``     = positional args of the wrapped call; for a bound
                          ``Memory.delete_documents_by_ids(ids)`` invocation
                          that is ``(self, ids)``.
- ``data["kwargs"]``   = keyword args (usually empty for this call).
- ``data["result"]``   = the wrapped function's return value — for
                          ``delete_documents_by_ids`` this is the list of
                          ``Document`` objects that were actually removed
                          from the FAISS index.

Behavior:
    For every document that was removed, drop the matching edges from
    ``relationships.json`` and the matching entry from ``scores.json``.
    Both sidecars are kept in sync with the FAISS index so a cascade
    delete in the memory layer is reflected across the whole Neuro Core
    sidecar surface.

    Failures inside the cascade are swallowed: the FAISS delete has
    already succeeded and the agent should still see that delete as
    successful. The exception is logged via ``PrintStyle.warning`` for
    debug-only visibility.

Stability contract (v2):
    - Plugin-local imports (``from usr.plugins.neuro_core...``) are
      moved inside the method that uses them. Module-level imports
      of plugin-local code are NOT permitted in hook files because
      the framework's dotted-path extension loader may not have
      initialised the ``usr.plugins.*`` package at import time.
    - The body is wrapped in a broad ``except Exception`` that
      logs a warning and returns. If this hook throws, the FAISS
      delete has already succeeded and the agent should still see
      the delete as successful.
    - The method never re-raises under any circumstances.
"""

from __future__ import annotations

from typing import Any, List, Optional

from helpers.extension import Extension
from helpers.print_style import PrintStyle


def _resolve_memory_subdir(memory_instance: Any) -> Optional[str]:
    """Return the ``memory_subdir`` for a ``Memory`` instance.

    Tries several attribute names because the core ``Memory`` class has
    changed its internal layout across Agent Zero versions. We do not
    import ``Memory`` here to keep the import surface small and to avoid
    breaking the plugin when the framework is updated.
    """
    for attr in (
        "subdir",
        "_subdir",
        "memory_subdir",
        "_memory_subdir",
        "_db_dir",
        "db_dir",
    ):
        value = getattr(memory_instance, attr, None)
        if isinstance(value, str) and value:
            return value

    # The FAISS vector store may carry the directory on the docstore.
    db = getattr(memory_instance, "db", None)
    if db is not None:
        for attr in ("_directory", "directory", "index_path"):
            value = getattr(db, attr, None)
            if isinstance(value, str) and value:
                return value

    return None


class NeuroDeleteCascade(Extension):
    """Cascade-delete edges and scores for every removed memory."""

    def execute(self, **kwargs: Any) -> None:
        """Hook entry point — NEVER re-raises.

        The wrapper logs a warning on any failure and returns
        normally. The FAISS delete has already succeeded by the time
        this end-hook runs; an unhandled exception here would
        propagate to the caller and could cause the agent to see the
        delete as failed even though it actually committed.
        """
        # All plugin-local imports live inside execute() because the
        # framework's dotted-path extension loader does not guarantee
        # the ``usr.plugins.*`` package is initialised at module-import
        # time. Importing at the top of this file would also pull in
        # the ``usr.plugins.neuro_core.helpers.{graph_store,scores}``
        # chain (which depends on helpers.plugins, agent, FAISS, ...)
        # even when the hook is loaded but never invoked.
        try:
            data: dict = kwargs.get("data") or {}
            args: tuple = tuple(data.get("args") or ())
            result = data.get("result")

            if len(args) < 2:
                return
            memory_instance = args[0]
            subdir = _resolve_memory_subdir(memory_instance)
            if not subdir:
                return

            # `delete_documents_by_ids` returns the list of removed Documents.
            removed_docs: List[Any] = []
            if isinstance(result, list):
                removed_docs = result
            elif isinstance(result, tuple):
                removed_docs = list(result)
            else:
                # Defensive: not the shape we expect, nothing to cascade.
                return

            if not removed_docs:
                return

            from usr.plugins.neuro_core.helpers.graph_store import GraphStore
            from usr.plugins.neuro_core.helpers.scores import ScoreStore

            try:
                graph = GraphStore(subdir)
                scores = ScoreStore(subdir)
            except Exception as exc:  # pragma: no cover - defensive
                PrintStyle().warning(
                    f"[neuro_core] cascade init failed for {subdir}: {exc}"
                )
                return

            for doc in removed_docs:
                doc_id = None
                metadata = getattr(doc, "metadata", None)
                if isinstance(metadata, dict):
                    doc_id = metadata.get("id") or metadata.get("memory_id")
                if not doc_id:
                    doc_id = getattr(doc, "id", None)
                if not doc_id:
                    continue
                try:
                    graph.remove_edges_for_id(str(doc_id))
                except Exception as exc:  # pragma: no cover - defensive
                    PrintStyle().warning(
                        f"[neuro_core] graph cascade failed for {doc_id}: {exc}"
                    )
                try:
                    scores.forget(str(doc_id))
                except Exception as exc:  # pragma: no cover - defensive
                    PrintStyle().warning(
                        f"[neuro_core] score cascade failed for {doc_id}: {exc}"
                    )
        except Exception as e:
            # Never re-raise — the FAISS delete has already succeeded.
            PrintStyle().warning(
                f"[neuro_core] cascade hook non-fatal: {e}"
            )
