"""Neuro Core access-tracking end-hook for ``Memory.search_similarity_threshold``.

Every returned document counts as an access, persisted EXCLUSIVELY to the
sidecar ``scores.json`` via ``ScoreStore.update_access``. Returned objects
are never mutated (framework callers hold shared references).

Contract (grounded in helpers/extension.py): the framework discovers
handler classes via ``load_classes_from_folder(folder, '*', Extension)``
(helpers/extension.py:367), so this class MUST inherit
``helpers.extension.Extension`` and implement ``execute(**kwargs)``.
``data['args']`` holds the wrapped call's positional args (for a bound
``Memory.search_similarity_threshold(query, ...)`` that is
``(self, query, ...)``) and ``data['result']`` holds the returned
document list.

Exception safety (ARC condition 3): the entire body is wrapped; the hook
never re-raises and never sets ``data['exception']`` — a sidecar failure
cannot break the host search.
"""

from __future__ import annotations

from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle


def _subdir(memory_instance: Any) -> str:
    try:
        sub = getattr(memory_instance, "memory_subdir", None)
        if isinstance(sub, str) and sub:
            return sub
    except Exception:
        pass
    return "default"


class NeuroAccessTracking(Extension):
    """Persist access tracking for plain-search results to scores.json."""

    def execute(self, **kwargs: Any) -> None:
        # Never re-raise — the host search must not break (ARC cond 3).
        try:
            data: dict = kwargs.get("data") or {}
            args: tuple = tuple(data.get("args") or ())
            result = data.get("result")
            if not args or result is None:
                return
            memory_instance = args[0]
            if isinstance(result, dict):
                for key in ("documents", "docs", "results", "matches"):
                    if isinstance(result.get(key), list):
                        result = result[key]
                        break
            if not isinstance(result, (list, tuple)):
                return
            docs = list(result)
            if not docs:
                return
            from usr.plugins.neuro_core.helpers.scores import ScoreStore

            store = ScoreStore(_subdir(memory_instance))
            for doc in docs:
                try:
                    md = getattr(doc, "metadata", None)
                    doc_id = None
                    if isinstance(md, dict):
                        doc_id = md.get("id")
                    doc_id = doc_id or getattr(doc, "id", None)
                    if doc_id:
                        store.update_access(str(doc_id))
                except Exception as exc:
                    PrintStyle().warning(
                        f"[neuro_core] access tracking failed for doc: {exc}"
                    )
        except Exception as e:
            # Never re-raise — the search has already succeeded.
            PrintStyle().warning(
                f"[neuro_core] access_tracking hook non-fatal: {e}"
            )
