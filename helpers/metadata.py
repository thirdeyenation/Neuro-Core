"""Neuro Core metadata validation and seeding heuristics.

This module defines:

- ``MemoryType`` / ``ValidationStatus`` enums (the typed memory categories
  and validation states documented in NEURO_CORE_SPEC.md §4).
- ``VALID_MEMORY_TYPES`` / ``VALID_VALIDATION_STATUSES`` frozensets used for
  insert-time validation and for the ``_10_neuro_metadata`` insert hook.
- ``validate_neuro_metadata()`` — clamps scores to ``[0.0, 1.0]`` and
  coerces invalid enum values to safe fallbacks (``"note"`` for
  ``memory_type``, ``"unvalidated"`` for ``validation_status``).
- ``apply_seeding()`` and the per-field ``seed_*`` helpers — lazy seeding
  heuristics that turn a `memory_subdir` ``area`` field, a memory
  ``source`` field, or a ``consolidation_action`` field into initial
  importance / confidence / stability values when those fields are
  absent from the document metadata.

The 8-value ``MemoryType`` enum resolves Flag 1 from
``ASSESSMENT_SUMMARY.md``: the 13-value enum in NEURO_CORE_SPEC.md §6.1
is superseded by the 8 values in §4.3, and that is the authoritative set
for v1.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enums (resolved Flag 1: 8 values, not 13)
# ---------------------------------------------------------------------------


class MemoryType(str, Enum):
    """Typed memory categories for Neuro Core (v1 — 8 values)."""

    FACT = "fact"
    CONCEPT = "concept"
    TASK = "task"
    EVENT = "event"
    DECISION = "decision"
    SKILL = "skill"
    PREFERENCE = "preference"
    NOTE = "note"


class ValidationStatus(str, Enum):
    """Validation states for a memory document (v1 — 4 values)."""

    UNVALIDATED = "unvalidated"
    VALIDATED = "validated"
    DISPUTED = "disputed"
    DEPRECATED = "deprecated"


# Frozen sets of the underlying string values. These are the authoritative
# membership checks used by ``validate_neuro_metadata`` and by tests.
VALID_MEMORY_TYPES: frozenset[str] = frozenset(m.value for m in MemoryType)
VALID_VALIDATION_STATUSES: frozenset[str] = frozenset(v.value for v in ValidationStatus)


# ---------------------------------------------------------------------------
# Insert-time validation
# ---------------------------------------------------------------------------


def validate_neuro_metadata(metadata: dict) -> dict:
    """Validate and normalize Neuro Core metadata fields at insert time.

    The function mutates and returns ``metadata`` in place:

    - If ``memory_type`` is present and not in ``VALID_MEMORY_TYPES``,
      coerce it to ``"note"``.
    - If ``importance`` / ``confidence`` / ``stability`` are present, clamp
      them to the closed interval ``[0.0, 1.0]``.
    - If ``validation_status`` is present and not in
      ``VALID_VALIDATION_STATUSES``, coerce it to ``"unvalidated"``.

    Missing fields are left untouched — they will be filled in lazily by
    ``apply_seeding()`` (or fall back to the ``MemoryObject`` defaults at
    read time).

    Args:
        metadata: The document metadata dict to normalize in place.

    Returns:
        The same ``metadata`` dict, now normalized.
    """
    if "memory_type" in metadata:
        if metadata["memory_type"] not in VALID_MEMORY_TYPES:
            metadata["memory_type"] = MemoryType.NOTE.value

    for score_key in ("importance", "confidence", "stability"):
        if score_key in metadata:
            try:
                metadata[score_key] = max(0.0, min(1.0, float(metadata[score_key])))
            except (TypeError, ValueError):
                # Non-numeric value: drop it so the downstream default applies.
                metadata.pop(score_key, None)

    if "validation_status" in metadata:
        if metadata["validation_status"] not in VALID_VALIDATION_STATUSES:
            metadata["validation_status"] = ValidationStatus.UNVALIDATED.value

    return metadata


# ---------------------------------------------------------------------------
# Seeding heuristics (Section 7.2)
# ---------------------------------------------------------------------------
#
# Lazy seeding: only applied when the corresponding score field is absent
# from the metadata dict. Existing values are preserved.
#
#   area (memory_subdir):  solutions   -> importance 0.8
#                          main        -> importance 0.5
#                          fragments   -> importance 0.3
#
#   source:                knowledge_* -> confidence 1.0
#                          llm / agent -> confidence 0.7
#
#   consolidation_action:  replace       -> stability 0.9
#                          merge         -> stability 0.7
#                          keep_separate -> stability 0.5


_KNOWLEDGE_SOURCES: frozenset[str] = frozenset(
    {"knowledge", "knowledge_file", "knowledge_import", "external", "human", "imported"}
)
_LLM_SOURCES: frozenset[str] = frozenset(
    {"agent", "llm", "llm_generated", "system", "consolidation"}
)


def seed_importance_from_area(area: Any) -> Optional[float]:
    """Map a `memory_subdir` ``area`` value to a default ``importance``.

    Returns ``None`` when the area is unknown so the caller can fall back
    to the ``MemoryObject`` default (``0.5``).
    """
    if not area:
        return None
    area_str = str(area).lower().strip()
    if area_str == "solutions":
        return 0.8
    if area_str == "main":
        return 0.5
    if area_str == "fragments":
        return 0.3
    return None


def seed_confidence_from_source(source: Any) -> Optional[float]:
    """Map a memory ``source`` value to a default ``confidence``.

    Knowledge-sourced memories (imported files, human edits, external
    data) are seeded with ``1.0``; LLM-generated memories with ``0.7``.
    Returns ``None`` for unknown sources.
    """
    if not source:
        return None
    source_str = str(source).lower().strip()
    if source_str in _KNOWLEDGE_SOURCES:
        return 1.0
    if source_str in _LLM_SOURCES:
        return 0.7
    return None


def seed_stability_from_action(action: Any) -> Optional[float]:
    """Map a ``consolidation_action`` value to a default ``stability``.

    Returns ``None`` when the action is unknown.
    """
    if not action:
        return None
    action_str = str(action).lower().strip()
    if action_str == "replace":
        return 0.9
    if action_str == "merge":
        return 0.7
    if action_str == "keep_separate":
        return 0.5
    return None


def apply_seeding(metadata: dict) -> dict:
    """Fill in missing importance / confidence / stability from heuristics.

    Existing values are preserved (the function only writes a key when it
    is absent). The function is idempotent and safe to call on every
    insert.
    """
    if "importance" not in metadata:
        seeded = seed_importance_from_area(metadata.get("area"))
        if seeded is not None:
            metadata["importance"] = seeded

    if "confidence" not in metadata:
        seeded = seed_confidence_from_source(metadata.get("source"))
        if seeded is not None:
            metadata["confidence"] = seeded

    if "stability" not in metadata:
        seeded = seed_stability_from_action(metadata.get("consolidation_action"))
        if seeded is not None:
            metadata["stability"] = seeded

    return metadata


# ---------------------------------------------------------------------------
# apply_defaults — safe migration helper
# ---------------------------------------------------------------------------
#
# Lazy seeding used by ``execute.py`` when migrating pre-existing memories
# into the Neuro Core schema. Unlike ``apply_seeding()`` (which uses
# heuristics based on ``area`` / ``source`` / ``consolidation_action``),
# ``apply_defaults()`` uses **safe fallbacks**:
#
#   - ``memory_type``       -> ``"note"`` (most permissive category)
#   - ``importance``        -> ``0.5``    (neutral)
#   - ``confidence``        -> ``0.7``    (slightly above neutral)
#   - ``stability``         -> ``0.5``    (neutral)
#   - ``validation_status`` -> ``"unvalidated"``
#   - ``task_status``       -> only set if the memory is already typed as
#                              ``"task"``; otherwise left absent
#
# Existing values are NEVER overwritten. The function is idempotent and
# safe to call on every migration pass.


def apply_defaults(metadata: dict) -> dict:
    """Seed missing Neuro Core fields with safe fallbacks (in place).

    Args:
        metadata: The document metadata dict to normalize.

    Returns:
        The same ``metadata`` dict, with missing fields filled in.
    """
    if "memory_type" not in metadata:
        metadata["memory_type"] = MemoryType.NOTE.value

    if "importance" not in metadata:
        metadata["importance"] = 0.5

    if "confidence" not in metadata:
        metadata["confidence"] = 0.7

    if "stability" not in metadata:
        metadata["stability"] = 0.5

    if "validation_status" not in metadata:
        metadata["validation_status"] = ValidationStatus.UNVALIDATED.value

    return metadata
