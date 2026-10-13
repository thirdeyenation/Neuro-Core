"""Neuro Core Phase-1 recall-shaping end-hook for
``Memory.search_similarity_threshold_with_scores`` (WI-P43, ADR-NC1-003,
D-NC1-106).

Dispatch order is INTENTIONAL: ``_05_recall_shaping`` runs BEFORE
``_10_access_tracking`` so access tracking sees the final delivered set.

Safety envelope (binding, per the approved ADR):
- C1: never re-raises and never sets ``data['exception']``; any shaping
  failure leaves ``data['result']`` EXACTLY as the native search returned it.
- C2: gated on ``recall_shaping_enabled`` (default false); gate off leaves
  the native result untouched — byte-identical baseline (pinned by test).
- C3: the with-scores return shape (``list[tuple[Document, float]]``) is
  preserved; the NATIVE relevance score in each tuple is kept VERBATIM —
  the shaped score lives ONLY in ``neuro_factors.shaped_score``. If the
  native result contains any document without a resolvable, unique id,
  shaping is SKIPPED entirely (native result preserved) rather than risk
  mis-attaching native scores.
- C4: handler filename ``_05_recall_shaping.py`` and class name
  ``NeuroWithScoresRecallShaping`` are collision-unique (pinned by test).
- C5: score authority is the ratified sidecar (scores.json), read-side only.
"""

from __future__ import annotations

from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle


class NeuroWithScoresRecallShaping(Extension):
    """Shape the delivered recall of the with-scores similarity-threshold search."""

    def execute(self, **kwargs: Any) -> None:
        # Never re-raise — the host search must not break (C1).
        try:
            data: dict = kwargs.get("data") or {}
            result = data.get("result")
            if result is None:
                return
            args: tuple = tuple(data.get("args") or ())
            memory_instance = args[0] if args else None
            from usr.plugins.neuro_core.helpers.recall_shaping import shape_with_scores

            shaped = shape_with_scores(memory_instance, result)
            if shaped is not None:
                # Same-shape replacement: list of (fresh copy, native score).
                data["result"] = shaped
        except Exception as e:
            # Never re-raise — the search has already succeeded (C1).
            PrintStyle().warning(
                f"[neuro_core] with-scores recall shaping hook non-fatal: {e}"
            )