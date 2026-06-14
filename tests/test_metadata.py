"""Tests for Neuro Core metadata validation and seeding.

Covers:
- 8-value ``MemoryType`` enum and ``VALID_MEMORY_TYPES`` set (Flag 1).
- 4-value ``ValidationStatus`` enum.
- ``validate_neuro_metadata`` enforces the enums and clamps score fields
  to the closed interval ``[0.0, 1.0]``.
- Fallback behavior: unknown memory_type → "note"; unknown
  validation_status → "unvalidated"; non-numeric scores are dropped.
- ``apply_seeding`` populates importance / confidence / stability from
  the documented heuristics (solutions/main/fragments area,
  knowledge/llm source, replace/merge/keep_separate action).
- ``apply_seeding`` is idempotent and never overwrites explicit values.
"""

from __future__ import annotations

import pytest

from usr.plugins.neuro_core.helpers.metadata import (
    MemoryType,
    VALID_MEMORY_TYPES,
    VALID_VALIDATION_STATUSES,
    ValidationStatus,
    apply_seeding,
    seed_confidence_from_source,
    seed_importance_from_area,
    seed_stability_from_action,
    validate_neuro_metadata,
)


# ---------------------------------------------------------------------------
# Enum + set fixtures
# ---------------------------------------------------------------------------


class TestMemoryTypeEnum:
    def test_eight_values(self):
        # Resolved Flag 1: 8, not 13.
        assert len(MemoryType) == 8
        assert len(VALID_MEMORY_TYPES) == 8

    def test_canonical_values(self):
        expected = {
            "fact",
            "concept",
            "task",
            "event",
            "decision",
            "skill",
            "preference",
            "note",
        }
        assert VALID_MEMORY_TYPES == expected
        assert {m.value for m in MemoryType} == expected

    def test_str_enum_membership(self):
        assert MemoryType.FACT == "fact"
        assert MemoryType("decision") is MemoryType.DECISION
        # Anything outside the 8 set is rejected by the enum constructor.
        with pytest.raises(ValueError):
            MemoryType("rumor")


class TestValidationStatusEnum:
    def test_four_values(self):
        assert len(ValidationStatus) == 4
        assert len(VALID_VALIDATION_STATUSES) == 4

    def test_canonical_values(self):
        assert VALID_VALIDATION_STATUSES == {
            "unvalidated",
            "validated",
            "disputed",
            "deprecated",
        }


# ---------------------------------------------------------------------------
# validate_neuro_metadata()
# ---------------------------------------------------------------------------


class TestValidateNeuroMetadata:
    def test_valid_passthrough(self):
        meta = {
            "memory_type": "fact",
            "validation_status": "validated",
            "importance": 0.7,
            "confidence": 0.9,
            "stability": 0.5,
        }
        assert validate_neuro_metadata(meta) is meta
        assert meta == {
            "memory_type": "fact",
            "validation_status": "validated",
            "importance": 0.7,
            "confidence": 0.9,
            "stability": 0.5,
        }

    def test_unknown_memory_type_falls_back_to_note(self):
        meta = {"memory_type": "rumor"}
        validate_neuro_metadata(meta)
        assert meta["memory_type"] == "note"

    def test_unknown_validation_status_falls_back_to_unvalidated(self):
        meta = {"validation_status": "probably_true"}
        validate_neuro_metadata(meta)
        assert meta["validation_status"] == "unvalidated"

    def test_missing_fields_untouched(self):
        meta = {}
        validate_neuro_metadata(meta)
        assert meta == {}

    def test_score_clamp_high(self):
        meta = {"importance": 1.7, "confidence": 99.0, "stability": 2.5}
        validate_neuro_metadata(meta)
        assert meta["importance"] == 1.0
        assert meta["confidence"] == 1.0
        assert meta["stability"] == 1.0

    def test_score_clamp_low(self):
        meta = {"importance": -0.4, "confidence": -2.0, "stability": -10}
        validate_neuro_metadata(meta)
        assert meta["importance"] == 0.0
        assert meta["confidence"] == 0.0
        assert meta["stability"] == 0.0

    def test_score_clamp_string_dropped(self):
        meta = {"importance": "very high", "confidence": 0.5}
        validate_neuro_metadata(meta)
        assert "importance" not in meta
        assert meta["confidence"] == 0.5

    def test_score_none_value_dropped(self):
        meta = {"stability": None, "confidence": 0.5}
        validate_neuro_metadata(meta)
        assert "stability" not in meta
        assert meta["confidence"] == 0.5

    def test_in_place_mutation(self):
        meta = {"memory_type": "x"}
        same = validate_neuro_metadata(meta)
        assert same is meta
        assert meta["memory_type"] == "note"

    def test_all_eight_types_accepted(self):
        for t in VALID_MEMORY_TYPES:
            meta = {"memory_type": t}
            validate_neuro_metadata(meta)
            assert meta["memory_type"] == t

    def test_all_four_statuses_accepted(self):
        for s in VALID_VALIDATION_STATUSES:
            meta = {"validation_status": s}
            validate_neuro_metadata(meta)
            assert meta["validation_status"] == s


# ---------------------------------------------------------------------------
# Seeding heuristics
# ---------------------------------------------------------------------------


class TestSeedImportanceFromArea:
    def test_solutions(self):
        assert seed_importance_from_area("solutions") == 0.8

    def test_main(self):
        assert seed_importance_from_area("main") == 0.5

    def test_fragments(self):
        assert seed_importance_from_area("fragments") == 0.3

    def test_case_insensitive(self):
        assert seed_importance_from_area("SOLUTIONS") == 0.8
        assert seed_importance_from_area("  Main  ") == 0.5

    def test_unknown_area_returns_none(self):
        assert seed_importance_from_area("drafts") is None
        assert seed_importance_from_area(None) is None
        assert seed_importance_from_area("") is None


class TestSeedConfidenceFromSource:
    def test_knowledge_sources(self):
        for src in ("knowledge", "knowledge_file", "knowledge_import",
                    "external", "human", "imported"):
            assert seed_confidence_from_source(src) == 1.0, src

    def test_llm_sources(self):
        for src in ("agent", "llm", "llm_generated", "system",
                    "consolidation"):
            assert seed_confidence_from_source(src) == 0.7, src

    def test_case_insensitive(self):
        assert seed_confidence_from_source("KNOWLEDGE") == 1.0
        assert seed_confidence_from_source("Agent") == 0.7

    def test_unknown_source_returns_none(self):
        assert seed_confidence_from_source("user") is None
        assert seed_confidence_from_source(None) is None
        assert seed_confidence_from_source("") is None


class TestSeedStabilityFromAction:
    def test_replace(self):
        assert seed_stability_from_action("replace") == 0.9

    def test_merge(self):
        assert seed_stability_from_action("merge") == 0.7

    def test_keep_separate(self):
        assert seed_stability_from_action("keep_separate") == 0.5

    def test_case_insensitive(self):
        assert seed_stability_from_action("REPLACE") == 0.9
        assert seed_stability_from_action(" Keep_Separate ") == 0.5

    def test_unknown_action_returns_none(self):
        assert seed_stability_from_action("split") is None
        assert seed_stability_from_action(None) is None
        assert seed_stability_from_action("") is None


class TestApplySeeding:
    def test_populates_missing_fields(self):
        meta = {"area": "solutions", "source": "agent",
               "consolidation_action": "replace"}
        apply_seeding(meta)
        assert meta["importance"] == 0.8
        assert meta["confidence"] == 0.7
        assert meta["stability"] == 0.9

    def test_preserves_explicit_values(self):
        meta = {"area": "solutions", "source": "agent",
               "consolidation_action": "replace",
               "importance": 0.99,
               "confidence": 0.42,
               "stability": 0.0}
        apply_seeding(meta)
        assert meta["importance"] == 0.99
        assert meta["confidence"] == 0.42
        assert meta["stability"] == 0.0

    def test_idempotent(self):
        meta = {"area": "main", "source": "knowledge_file"}
        apply_seeding(meta)
        snapshot = dict(meta)
        apply_seeding(meta)
        assert meta == snapshot

    def test_no_seeding_for_unknown_signals(self):
        meta = {"area": "drafts", "source": "user",
               "consolidation_action": "split"}
        apply_seeding(meta)
        assert "importance" not in meta
        assert "confidence" not in meta
        assert "stability" not in meta

    def test_partial_seeding(self):
        # Only area is recognised; source and action are not.
        meta = {"area": "fragments", "source": "user",
               "consolidation_action": "split"}
        apply_seeding(meta)
        assert meta["importance"] == 0.3
        assert "confidence" not in meta
        assert "stability" not in meta

    def test_knowledge_source_overrides_llm_default(self):
        # Even with no explicit confidence, knowledge_file should yield 1.0.
        meta = {"source": "knowledge_file"}
        apply_seeding(meta)
        assert meta["confidence"] == 1.0

    def test_empty_metadata_stays_empty(self):
        meta: dict = {}
        apply_seeding(meta)
        assert meta == {}
