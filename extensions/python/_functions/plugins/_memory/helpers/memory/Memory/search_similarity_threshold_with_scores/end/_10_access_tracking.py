"""Neuro Core access-tracking end-hook for
``Memory.search_similarity_threshold_with_scores`` (KI-021 closure).

Mirrors the plain-search access-tracking contract: every returned
(document, score) pair counts as an access, persisted EXCLUSIVELY to the
sidecar ``scores.json`` via ``ScoreStore.update_access``. Returned objects
are never mutated (framework callers hold shared references).

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


class NeuroWithScoresAccessTracking(Extension):
    """Persist access tracking for with-scores search results (KI-021)."""

    def execute(self, **kwargs: Any) -> None:
        # Never re-raise — the host search must not break (ARC cond 3).
        try:
            data: dict = kwargs.get("data") or {}
            args: tuple = tuple(data.get("args") or ())
            result = data.get("result")
            if not args or result is None:
                return
            memory_instance = args[0]
            # with-scores returns [(doc, score), ...]
            if not isinstance(result, (list, tuple)):
                return
            docs = [pair[0] for pair in result if isinstance(pair, tuple) and pair]
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
                        f"[neuro_core] with-scores access tracking failed for doc: {exc}"
                    )
        except Exception as e:
            # Never re-raise — the search has already succeeded.
            PrintStyle().warning(
                f"[neuro_core] with-scores access_tracking hook non-fatal: {e}"
            )
