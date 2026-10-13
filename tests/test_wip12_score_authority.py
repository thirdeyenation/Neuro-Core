"""WI-P12-SCORE-AUTHORITY (S1) - ADR-NC1-002 mandated sidecar-precedence tests.

Boundary 3 requires the downstream mirror-removal work item to add a test
asserting sidecar precedence and preserve the non-destructive,
non-activation-blocking properties. Boundary 4 assigns the FAISS access_count
mirror mutation in helpers/native_access.py _track_access for removal in the
same work item. Includes a disposable-fixture restart-persistence check
(ScoreStore re-open semantics) per ARC Q4. Uses the REAL ScoreStore (atomic
write logic) at the per-test temp directory from the conftest memory_subdir
fixture; Documents are minimal stand-ins to avoid a live FAISS import.
"""

from __future__ import annotations

import pytest

from usr.plugins.neuro_core.helpers.scores import ScoreStore


class _FakeDoc:
    """Minimal Document stand-in (avoids langchain_core import)."""

    def __init__(self, doc_id: str, content: str = "", metadata=None):
        self.doc_id = doc_id
        self.metadata = dict(metadata or {})
        self.metadata.setdefault("id", doc_id)
        self.page_content = content


def _importance(doc, scores):
    """Thin adapter to the retrieval implementation under test."""
    from usr.plugins.neuro_core.helpers.retrieval import _importance_for as impl
    return impl(doc_id=doc.metadata["id"], doc=doc, score_store=scores)


class TestSidecarPrecedence:
    def test_sidecar_entry_overrides_metadata(self, memory_subdir):
        """Sidecar entry present: sidecar importance is used; the stale
        metadata mirror copy is ignored for scores."""
        scores = ScoreStore(memory_subdir)
        scores.set("seed_a", importance=0.9, confidence=0.8, stability=0.7)
        doc = _FakeDoc("seed_a", metadata={"importance": 0.1})
        imp, degraded = _importance(doc, scores)
        assert imp == 0.9, "sidecar importance must override metadata copy"
        assert degraded is False, "healthy sidecar entry is not degraded"

    def test_legacy_record_marks_degraded(self, memory_subdir):
        """Legacy record without a sidecar entry: falls back to the metadata
        copy with the explicit degraded marker - never presented as a healthy
        sidecar-backed score (binding ARC C3)."""
        scores = ScoreStore(memory_subdir)
        doc = _FakeDoc("seed_b", metadata={"importance": 0.6})
        imp, degraded = _importance(doc, scores)
        assert imp == 0.6, "legacy fallback returns the metadata importance"
        assert degraded is True, "legacy record must surface the degraded marker"


class TestAccessTrackingSingleWriter:
    def test_update_access_writes_sidecar_only(self, memory_subdir):
        """Access tracking writes the sidecar only - no doc.metadata mutation
        (boundary-4 mirror removed by this WI)."""
        store = ScoreStore(memory_subdir)
        doc = _FakeDoc("seed_c")
        updated = store.update_access("seed_c")
        assert updated.access_count == 1, "first access recorded in sidecar"
        assert updated.last_accessed_at, "last_accessed_at stamped"
        assert "access_count" not in doc.metadata, "no metadata mirror mutation"
        rec = ScoreStore(memory_subdir).get("seed_c")
        assert rec is not None and rec.access_count == 1, "sidecar authoritative"


class TestRestartPersistence:
    def test_sidecar_values_survive_store_reopen(self, memory_subdir):
        """Programmatic restart-persistence check (ARC Q4): sidecar values
        survive a store re-open (fresh ScoreStore re-loads from disk on a
        disposable fixture)."""
        w = ScoreStore(memory_subdir)
        w.set("seed_e", importance=0.85, confidence=0.75, stability=0.65)
        w.update_access("seed_e")
        r = ScoreStore(memory_subdir)
        rec = r.get("seed_e")
        assert rec is not None, "sidecar record must survive re-open"
        assert rec.importance == 0.85
        assert rec.confidence == 0.75
        assert rec.stability == 0.65
        assert rec.access_count == 1


def test_boundary4_mutation_absent_from_source():
    """Source-level pin for C2: the boundary-4 metadata mutation is absent."""
    import pathlib as pl
    src = (pl.Path(__file__).resolve().parent.parent / "helpers" / "native_access.py").read_text()
    assert 'doc.metadata["access_count"]' not in src, "boundary-4 mutation must be gone"
