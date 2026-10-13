"""Neuro Core Phase-1 recall-shaping end-hook for
``Memory.search_similarity_threshold`` (WI-P43, ADR-NC1-003, D-NC1-106).

Dispatch order is INTENTIONAL: ``_05_recall_shaping`` runs BEFORE
``_10_access_tracking`` so the access-tracking handler sees the FINAL
delivered set (including graph-neighbor expansions), and so its own
counts match what the host actually receives. Handlers are processed in
lexicographic filename order (helpers/extension.py discovery).

Safety envelope (binding, per the approved ADR):
- C1: never re-raises and never sets ``data['exception']``; any shaping
  failure leaves ``data['result']`` EXACTLY as the native search returned it.
- C2: gated on ``recall_shaping_enabled`` (default false). With the gate
  off this handler leaves ``data['result']`` untouched — byte-identical
  baseline (pinned by test).
- C3: the plain path's return shape (``list[Document]``) is preserved
  verbatim; the native score is never written into document metadata —
  shaping data lives only in the fresh metadata copies under
  ``neuro_shaped`` / ``neuro_factors`` (see helpers/recall_shaping.py).
- C4: handler filename ``_05_recall_shaping.py`` and class name
  ``NeuroRecallShaping`` are collision-unique across the plugin and the
  framework extension folders (framework merge hazard; pinned by test).
- C5: score authority is the ratified sidecar (scores.json), read-side
  only — helpers/recall_shaping.py consumes it; nothing here writes scores.
"""

from __future__ import annotations

from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle


class NeuroRecallShaping(Extension):
    """Shape the delivered recall of the plain similarity-threshold search."""

    def execute(self, **kwargs: Any) -> None:
        # Never re-raise — the host search must not break (C1).
        try:
            data: dict = kwargs.get("data") or {}
            result = data.get("result")
            if result is None:
                return
            args: tuple = tuple(data.get("args") or ())
            memory_instance = args[0] if args else None
            from usr.plugins.neuro_core.helpers.recall_shaping import shape_plain

            shaped = shape_plain(memory_instance, result)
            if shaped is not None:
                # Only ever replaced with a same-shape list of fresh copies.
                data["result"] = shaped
        except Exception as e:
            # Never re-raise — the search has already succeeded (C1).
            PrintStyle().warning(
                f"[neuro_core] recall shaping hook non-fatal: {e}"
            )