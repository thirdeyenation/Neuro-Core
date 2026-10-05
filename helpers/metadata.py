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

D39-B / D54 closure (2026-06-22):
    - This module is the authoritative home for ``MemoryType``,
      ``ValidationStatus``, ``VALID_MEMORY_TYPES``, and
      ``VALID_VALIDATION_STATUSES``. The legacy ``helpers/types.py``
      placeholder was deleted at commit ``da16f66`` and is explicitly
      non-existent — do not recreate it. Imports of the real types
      must point here.
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


# ---------------------------------------------------------------------------
# memory_types collection (WI-P59-KI029-MEMTYPE-EDIT, ADR-NC1-004)
# ---------------------------------------------------------------------------
#
# Durable metadata policy (ADR-NC1-004; WI-P59, S2):
#   - The scalar ``memory_type`` remains the enum-locked primary type.
#   - The additive ``memory_types`` collection (list of strings) carries the
#     FULL authoritative type set including the primary. Invariant:
#     ``memory_type`` is a member of ``memory_types``.
#   - User-defined custom types live ONLY in the collection (the enum is
#     never extended); they match ``MEMORY_TYPE_TOKEN_RE`` (1-40 chars).
#   - Caps (durable policy values, ADR-NC1-004): max 1 primary + 7 additional.
#   - This module is the SINGLE sanctioned normalization authority (C1):
#     read paths derive the normalized set WITHOUT mutating metadata; only
#     the memory_edit handler writes through strict validation.

import re as _re

MEMORY_TYPE_TOKEN_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
MAX_ADDITIONAL_MEMORY_TYPES = 7


class NormalizedMemoryTypes:
    """Immutable result of normalizing the type fields of one document.

    ``types`` is the authoritative set (list, primary first, deduped).
    ``inconsistent`` is True when ``memory_types`` exists but the scalar
    primary is not its member (reachable when tools/memory_score.py rewrites
    the scalar; C2) — read paths TOLERATE this without mutation; the next
    handler edit repairs the invariant via full-set-replace.
    """

    __slots__ = ("types", "primary", "additional", "inconsistent")

    def __init__(self, types, primary, additional, inconsistent):
        self.types = types
        self.primary = primary
        self.additional = additional
        self.inconsistent = inconsistent

    def to_payload(self):
        """UI/API payload: primary scalar plus full normalized set."""
        return {
            "memory_type": self.primary,
            "memory_types": list(self.types),
            "additional": list(self.additional),
            "inconsistent": self.inconsistent,
        }


def normalize_memory_types(metadata: dict, strict: bool = False):
    """Normalize the scalar primary + additive memory_types collection.

    This is the SINGLE normalization authority for the two type fields
    (C1). It NEVER mutates ``metadata`` in either mode (no FAISS or store
    writes anywhere in this function).

    Lenient mode (read paths, ``strict=False``):
    - scalar-only legacy record  -> read-derives ``[memory_type]``.
    - missing both               -> empty set (UI renders 'type unknown').
    - collection present          -> returns the collection as-is plus the
      scalar as primary. When the scalar is NOT a collection member the
      result is flagged ``inconsistent`` (C2 tolerated state; no repair
      on read).

    Strict mode (handler write path, ``strict=True``): validates a
    full-set-replace payload mapping with keys ``primary`` (required, must
    be an enum value) and ``additional`` (optional list; each token must
    match ``MEMORY_TYPE_TOKEN_RE``, max
    ``MAX_ADDITIONAL_MEMORY_TYPES``, no enum-collision, no duplicates).
    Raises ``ValueError`` with the canonical error strings on any
    violation (loud rejection, no partial write). Returns the
    ``NormalizedMemoryTypes`` for the validated set.

    Args:
        metadata: The document metadata dict (read-only here).
        strict: Validate as a write payload instead of reading.

    Returns:
        ``NormalizedMemoryTypes``

    Raises:
        ValueError: In strict mode on any payload violation.
    """
    if strict:
        if not isinstance(metadata, dict):
            raise ValueError("`types` must be an object with `primary` and optional `additional`")
        primary = metadata.get("primary")
        if primary is None or not isinstance(primary, str) or primary not in VALID_MEMORY_TYPES:
            raise ValueError(
                "`types.primary` must be one of the 8 valid memory types: "
                + ", ".join(sorted(VALID_MEMORY_TYPES))
            )
        raw_additional = metadata.get("additional", [])
        if raw_additional is None:
            raw_additional = []
        if not isinstance(raw_additional, list) or any(
            not isinstance(t, str) for t in raw_additional
        ):
            raise ValueError("`types.additional` must be a list of strings")
        additional = []
        seen = {primary}
        for token in raw_additional:
            t = token.strip().lower()
            if not MEMORY_TYPE_TOKEN_RE.match(t):
                raise ValueError(
                    "`types.additional` entries must be 1-40 chars, lowercase, "
                    "no whitespace: ^[a-z0-9][a-z0-9_-]{0,39}$ (got: " + token + ")"
                )
            if t in seen:
                if t == primary:
                    raise ValueError(
                        "`types.additional` entry duplicates the primary type: " + t
                    )
                raise ValueError("`types.additional` contains a duplicate: " + t)
            if t in VALID_MEMORY_TYPES:
                raise ValueError(
                    "`types.additional` entry collides with an enum type: " + t
                )
            seen.add(t)
            additional.append(t)
        if len(additional) > MAX_ADDITIONAL_MEMORY_TYPES:
            raise ValueError(
                "at most " + str(MAX_ADDITIONAL_MEMORY_TYPES)
                + " additional types are allowed (1 primary + "
                + str(MAX_ADDITIONAL_MEMORY_TYPES) + " additional)"
            )
        return NormalizedMemoryTypes(
            [primary] + additional, primary, additional, False
        )

    # ---- lenient read path (no mutation, ever) ----------------------------
    scalar = metadata.get("memory_type")
    collection = metadata.get("memory_types")
    if collection is None:
        if scalar is None:
            return NormalizedMemoryTypes([], None, [], False)
        if isinstance(scalar, str):
            return NormalizedMemoryTypes([scalar], scalar, [], False)
        # Non-string scalar (defensive): treat as type-unknown on read.
        return NormalizedMemoryTypes([], None, [], False)
    if isinstance(collection, str):
        collection = [collection]
    if not isinstance(collection, list):
        return NormalizedMemoryTypes([], None, [], False)
    collection = [t for t in collection if isinstance(t, str)]
    primary = scalar if isinstance(scalar, str) else None
    additional = [t for t in collection if t != primary]
    inconsistent = primary is not None and primary not in collection
    if primary is not None and not inconsistent:
        types = [primary] + additional
    elif primary is not None:
        # C2 tolerated inconsistent state: surface the scalar primary
        # alongside the collection without mutating anything.
        types = [primary] + [t for t in collection if t != primary]
    else:
        types = list(collection)
    return NormalizedMemoryTypes(types, primary, additional, inconsistent)
