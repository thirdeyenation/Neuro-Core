"""Pins for WI-P63-KI048-CONTRADICTION-REMEDIATION (KI-048 remediation, S1).

Approved design: steward-design-decision.yaml revision 4
(approved-with-conditions). This module pins the ratified semantics:

    1. Q2 fact-eligibility predicate (condition 2): the scheduled sweep
       builds facts ONLY from documents whose normalized memory-type set
       (``helpers.metadata.normalize_memory_types``, lenient read mode,
       ``strict=False``, never mutating metadata) contains ``"fact"``.
       Pinned shapes: scalar-only legacy record; collection with a
       non-fact primary but ``"fact"`` in the collection; collection
       with ``"fact"`` as primary; inconsistent-flagged record (scalar
       not a collection member); untyped record EXCLUDED (intended
       consequence — no dispute is ever produced for it).
    2. Q3 threshold gating on the embeddings channel (condition 3):
       with an embeddings mapping, a pair is compared with the lexical
       heuristic ONLY when its cosine similarity is >=
       ``contradiction_similarity_threshold``; a below-threshold pair is
       NOT disputed despite lexical opposition; a missing vector is a
       per-pair fail-safe (no comparison, no dispute); an empty mapping
       degrades to zero detections with no errors.
    3. Q4 backward compatibility (condition 4): ``embeddings=None`` (the
       default) preserves the legacy ungated channel verbatim; existing
       3-tuple callers pass unchanged.
    4. Q1 process-all semantics (condition 1): ALL eligible facts are
       processed per pass — more than ``contradiction_batch_size``
       facts are all checked on the fallback path.
    5. The seam (condition 4 test-seam rules): the job's
       embedding-computation step (``_compute_embeddings``) is the
       single failure point; monkeypatching ONLY the seam to raise or
       return empty yields zero detections and no errors; the REAL seam
       degrades to an empty mapping under the suite runtime (litellm
       absent), pinning the production degradation direction.

All data is synthetic in-memory fixtures; no plugin data files, no
FAISS index and no LLM are touched.
"""
from __future__ import annotations

import asyncio
import importlib.util
import math
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from usr.plugins.neuro_core.helpers import lifecycle


_PLUGIN = Path("/a0/usr/plugins/neuro_core")
_JOB = _PLUGIN / "extensions" / "python" / "job_loop" / "_30_contradiction_detection.py"

_FAKE_AGENT = object()


def _load_job_module():
    spec = importlib.util.spec_from_file_location(
        "_neuro_core_test_wip63_contradiction_remediation", str(_JOB)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc(memory_id: str, content: str, metadata: dict | None = None) -> dict:
    """A doc in the shape ``_iter_docs`` actually yields."""
    md = {"id": memory_id, "timestamp": "2026-01-01T00:00:00+00:00"}
    if metadata:
        md.update(metadata)
    return {"id": memory_id, "metadata": md, "page_content": content}


# ---------------------------------------------------------------------------
# 1. Q2 fact-eligibility predicate (through the job's facts construction)
# ---------------------------------------------------------------------------


def _capture_facts(monkeypatch, job_module, docs):
    """Run one sweep with the lifecycle fn mocked; return the facts and
    embeddings kwargs it was invoked with."""
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_read_config",
        staticmethod(lambda: {
            "contradiction_detection_enabled": True,
            "contradiction_interval_hours": 168,
            "contradiction_llm_enabled": False,
        }),
    )
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_subdirs",
        staticmethod(lambda: ["test_subdir"]),
    )
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_iter_docs",
        staticmethod(lambda subdir: list(docs)),
    )
    monkeypatch.setattr(job_module, "_boot_grace_active", lambda: False)
    # The seam is monkeypatched here ONLY to isolate the Q2 predicate
    # under test from embedding-mechanism behavior (the suite runtime
    # cannot import the mechanism); the real heuristic is NOT mocked.
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_compute_embeddings",
        staticmethod(lambda docs: {}),
    )
    mock_fn = MagicMock(return_value={"checked": 0, "disputed": 0, "disputes": []})
    monkeypatch.setattr(lifecycle, "run_contradiction_detection", mock_fn)

    ext = job_module.ContradictionDetectionJob(agent=_FAKE_AGENT)
    asyncio.run(ext.execute())
    mock_fn.assert_called_once()
    _args, kwargs = mock_fn.call_args
    return kwargs.get("facts"), kwargs.get("embeddings")


@pytest.fixture()
def job_module():
    return _load_job_module()


def test_q2_scalar_only_fact_record_eligible(monkeypatch, job_module) -> None:
    """A legacy scalar-only record (memory_type='fact', no collection)
    is eligible."""
    docs = [_doc("m1", "The sync completed successfully.",
                 {"memory_type": "fact"})]
    facts, embeddings = _capture_facts(monkeypatch, job_module, docs)
    assert [(f[0], f[1]) for f in facts] == [("m1", "The sync completed successfully.")]
    assert isinstance(embeddings, dict)


def test_q2_collection_nonfact_primary_with_fact_eligible(
    monkeypatch, job_module
) -> None:
    """A collection-carrying document whose PRIMARY is not 'fact' but
    whose collection contains 'fact' is eligible (the normalized set is
    authoritative, per ADR-NC1-004)."""
    docs = [_doc("m1", "The sync completed successfully.",
                 {"memory_type": "note", "memory_types": ["note", "fact"]})]
    facts, _embeddings = _capture_facts(monkeypatch, job_module, docs)
    assert [f[0] for f in facts] == ["m1"]


def test_q2_collection_fact_primary_eligible(monkeypatch, job_module) -> None:
    """A collection-carrying document with 'fact' as primary is eligible."""
    docs = [_doc("m1", "The sync completed successfully.",
                 {"memory_type": "fact", "memory_types": ["fact", "note"]})]
    facts, _embeddings = _capture_facts(monkeypatch, job_module, docs)
    assert [f[0] for f in facts] == ["m1"]


def test_q2_inconsistent_flagged_record_eligible(monkeypatch, job_module) -> None:
    """An inconsistent-flagged record (scalar 'fact' not a member of the
    collection) is still eligible: the lenient read path surfaces the
    scalar primary in the normalized set without mutating anything."""
    docs = [_doc("m1", "The sync completed successfully.",
                 {"memory_type": "fact", "memory_types": ["note"]})]
    facts, _embeddings = _capture_facts(monkeypatch, job_module, docs)
    assert [f[0] for f in facts] == ["m1"]


def test_q2_untyped_record_excluded(monkeypatch, job_module) -> None:
    """An untyped record (neither memory_type nor memory_types) is NOT
    eligible — the intended consequence: the sweep never disputes a
    memory whose type was never declared a fact."""
    docs = [_doc("m1", "The sync completed successfully.")]
    facts, _embeddings = _capture_facts(monkeypatch, job_module, docs)
    assert facts == []


def test_q2_untyped_opposing_pair_never_disputed_end_to_end(
    monkeypatch, job_module
) -> None:
    """End-to-end with the REAL heuristic: two untyped, lexically
    opposing memories produce zero detections and no persisted dispute."""
    docs = [
        _doc("m1", "The sync completed successfully."),
        _doc("m2", "The sync failed."),
    ]
    messages: list[str] = []

    class _CaptureStyle:
        def __getattr__(self, name):
            def _sink(msg=""):
                messages.append(str(msg))
            return _sink

    monkeypatch.setattr(job_module, "_boot_grace_active", lambda: False)
    monkeypatch.setattr(job_module, "PrintStyle", _CaptureStyle)
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_read_config",
        staticmethod(lambda: {
            "contradiction_detection_enabled": True,
            "contradiction_interval_hours": 168,
            "contradiction_llm_enabled": False,
        }),
    )
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_subdirs",
        staticmethod(lambda: ["test_subdir"]),
    )
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_iter_docs",
        staticmethod(lambda subdir: list(docs)),
    )
    persist = MagicMock()
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_persist_disputes", persist
    )

    ext = job_module.ContradictionDetectionJob(agent=_FAKE_AGENT)
    asyncio.run(ext.execute())

    summary = [m for m in messages if "disputed=" in m and "subdirs=" in m]
    assert summary
    assert "checked=0" in summary[-1]
    assert "disputed=0" in summary[-1]
    persist.assert_called_once()
    assert persist.call_args.args[1] == []


def test_q2_predicate_never_mutates_metadata(monkeypatch, job_module) -> None:
    """The eligibility predicate is read-only: the documents' metadata
    dicts are byte-identical after the sweep."""
    docs = [
        _doc("m1", "The sync completed successfully.", {"memory_type": "fact"}),
        _doc("m2", "The sync failed."),
    ]
    snapshots = [dict(d["metadata"]) for d in docs]
    _capture_facts(monkeypatch, job_module, docs)
    assert [dict(d["metadata"]) for d in docs] == snapshots


# ---------------------------------------------------------------------------
# 2. Q3 threshold gating on the embeddings channel (lifecycle level)
# ---------------------------------------------------------------------------


def _pair_facts():
    return [
        ("a", "please enable the plugin", {"timestamp": "2026-01-01T00:00:00+00:00"}),
        ("b", "please disable the plugin", {"timestamp": "2026-01-02T00:00:00+00:00"}),
    ]


def test_q3_below_threshold_pair_not_disputed_despite_lexical_opposition() -> None:
    """With an embeddings mapping, a lexically opposing pair whose cosine
    similarity is below the threshold is NOT disputed."""
    result = lifecycle.run_contradiction_detection(
        "default",
        {"contradiction_similarity_threshold": 0.85},
        memory=None,
        facts=_pair_facts(),
        embeddings={"a": [1.0, 0.0], "b": [0.6, 0.8]},  # cosine 0.6 < 0.85
    )
    assert result["checked"] == 2
    assert result["disputed"] == 0
    assert result["disputes"] == []


def test_q3_at_or_above_threshold_pair_evaluated_and_disputed() -> None:
    """With an embeddings mapping, a lexically opposing pair whose cosine
    similarity is at/above the threshold IS evaluated by the heuristic
    and disputed (identical vectors → cosine 1.0 >= 0.85)."""
    result = lifecycle.run_contradiction_detection(
        "default",
        {"contradiction_similarity_threshold": 0.85},
        memory=None,
        facts=_pair_facts(),
        embeddings={"a": [1.0, 0.0], "b": [1.0, 0.0]},  # cosine 1.0
    )
    assert result["checked"] == 2
    assert result["disputed"] == 1
    assert result["disputes"][0]["memory_id"] == "a"
    assert result["disputes"][0]["disputed_id"] == "b"


def test_q3_missing_vector_is_per_pair_fail_safe() -> None:
    """A pair with a missing vector on either side is never compared and
    never disputed — even when the other pair's vectors would gate in."""
    facts = _pair_facts() + [
        ("c", "faiss is the vector store", {"timestamp": "2026-01-03T00:00:00+00:00"}),
        ("d", "agent zero uses faiss", {"timestamp": "2026-01-04T00:00:00+00:00"}),
    ]
    # Only 'a' has a vector: every pair involving 'a' is fail-safe
    # skipped; the c/d pair has no vectors either → zero disputes.
    result = lifecycle.run_contradiction_detection(
        "default",
        {"contradiction_similarity_threshold": 0.85},
        memory=None,
        facts=facts,
        embeddings={"a": [1.0, 0.0]},
    )
    assert result["checked"] == 4
    assert result["disputed"] == 0
    assert result["disputes"] == []


def test_q3_empty_mapping_degrades_to_zero_detections() -> None:
    """An EMPTY embeddings mapping (the production failure value) yields
    zero detections and no errors — never a mass false-positive mode."""
    result = lifecycle.run_contradiction_detection(
        "default",
        {"contradiction_similarity_threshold": 0.85},
        memory=None,
        facts=_pair_facts(),
        embeddings={},
    )
    assert result == {"checked": 2, "disputed": 0, "disputes": []}


def test_q3_none_preserves_legacy_ungated_channel() -> None:
    """``embeddings=None`` (the default) preserves the legacy ungated
    channel verbatim: the lexically opposing pair is disputed with no
    vectors present at all."""
    result = lifecycle.run_contradiction_detection(
        "default",
        {"contradiction_similarity_threshold": 0.85},
        memory=None,
        facts=_pair_facts(),
        embeddings=None,
    )
    assert result["checked"] == 2
    assert result["disputed"] == 1


def test_q4_backward_compatible_call_without_embeddings_kwarg() -> None:
    """Q4: existing 3-tuple callers pass unchanged — the embeddings
    parameter is optional with default None (the legacy channel)."""
    result = lifecycle.run_contradiction_detection(
        "default",
        {"contradiction_similarity_threshold": 0.85},
        memory=None,
        facts=_pair_facts(),
    )
    assert result["checked"] == 2
    assert result["disputed"] == 1


# ---------------------------------------------------------------------------
# 3. Q1 process-all semantics (lifecycle level)
# ---------------------------------------------------------------------------


def test_q1_process_all_facts_beyond_batch_size() -> None:
    """Condition 1: ALL eligible facts are processed per pass — with 120
    facts and contradiction_batch_size=100 (the default), checked == 120
    on the fallback path (no first-N truncation)."""
    facts = [
        (f"m{i}", f"fact number {i} is online", {"timestamp": "2026-01-01T00:00:00+00:00"})
        for i in range(120)
    ]
    result = lifecycle.run_contradiction_detection(
        "default",
        {"contradiction_similarity_threshold": 0.85, "contradiction_batch_size": 100},
        memory=None,
        facts=facts,
    )
    assert result["checked"] == 120
    assert result["disputed"] == 0


# ---------------------------------------------------------------------------
# 4. The seam (job level) — single failure point, empty-mapping contract
# ---------------------------------------------------------------------------


def _sweep_env(monkeypatch, job_module, docs):
    """Wire a sweep over ``docs`` with the REAL lifecycle heuristic and a
    mocked persistence boundary; return (persist mock, messages)."""
    messages: list[str] = []

    class _CaptureStyle:
        def __getattr__(self, name):
            def _sink(msg=""):
                messages.append(str(msg))
            return _sink

    monkeypatch.setattr(job_module, "_boot_grace_active", lambda: False)
    monkeypatch.setattr(job_module, "PrintStyle", _CaptureStyle)
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_read_config",
        staticmethod(lambda: {
            "contradiction_detection_enabled": True,
            "contradiction_interval_hours": 168,
            "contradiction_llm_enabled": False,
        }),
    )
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_subdirs",
        staticmethod(lambda: ["test_subdir"]),
    )
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_iter_docs",
        staticmethod(lambda subdir: list(docs)),
    )
    persist = MagicMock()
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_persist_disputes", persist
    )
    return persist, messages


def _typed_pair_docs():
    return [
        _doc("m1", "The sync completed successfully.", {"memory_type": "fact"}),
        _doc("m2", "The sync failed.", {"memory_type": "fact"}),
    ]


def test_seam_raising_yields_zero_detections_and_no_error(
    monkeypatch, job_module
) -> None:
    """Condition 4 rule 4: monkeypatching ONLY the seam to RAISE yields
    zero detections, no persisted dispute, and no propagated error. The
    raised exception is contained by the per-subdir guard (the seam's
    own internal fail-safe is bypassed by the monkeypatch), so the
    lifecycle function is never invoked for that subdir and the
    scheduler stays healthy."""
    persist, messages = _sweep_env(monkeypatch, job_module, _typed_pair_docs())

    def _boom(docs):
        raise RuntimeError("simulated mechanism failure")

    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_compute_embeddings", _boom
    )

    ext = job_module.ContradictionDetectionJob(agent=_FAKE_AGENT)
    asyncio.run(ext.execute())  # must not raise

    summary = [m for m in messages if "disputed=" in m and "subdirs=" in m]
    assert summary
    assert "checked=0" in summary[-1]
    assert "disputed=0" in summary[-1]
    # The per-subdir guard contains the raised exception and skips the
    # subdir entirely — the lifecycle function is never invoked, so the
    # persistence boundary is never reached (zero persistence calls is
    # the correct degradation; WI-P63 condition 4 / D-NC1-137).
    assert persist.call_count == 0


def test_seam_empty_mapping_yields_zero_detections(monkeypatch, job_module) -> None:
    """Condition 4 rule 4: monkeypatching ONLY the seam to return the
    EMPTY mapping yields zero detections and no errors — the ratified
    production degradation direction."""
    persist, messages = _sweep_env(monkeypatch, job_module, _typed_pair_docs())
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_compute_embeddings",
        staticmethod(lambda docs: {}),
    )

    ext = job_module.ContradictionDetectionJob(agent=_FAKE_AGENT)
    asyncio.run(ext.execute())

    summary = [m for m in messages if "disputed=" in m and "subdirs=" in m]
    assert summary
    assert "checked=2" in summary[-1]
    assert "disputed=0" in summary[-1]
    persist.assert_called_once()
    assert persist.call_args.args[1] == []


def test_real_seam_degrades_under_suite_runtime(monkeypatch, job_module) -> None:
    """Condition 4 rule 4 (real-seam variant): with NO seam monkeypatch,
    the REAL seam executes under the suite runtime — where the embedding
    mechanism cannot import (litellm absent) — and degrades to the empty
    mapping: zero detections, no errors, no persisted dispute. This pins
    the production degradation direction under the only runtime
    run_suite.sh permits."""
    persist, messages = _sweep_env(monkeypatch, job_module, _typed_pair_docs())

    ext = job_module.ContradictionDetectionJob(agent=_FAKE_AGENT)
    asyncio.run(ext.execute())  # must not raise

    summary = [m for m in messages if "disputed=" in m and "subdirs=" in m]
    assert summary
    assert "checked=2" in summary[-1]
    assert "disputed=0" in summary[-1]
    persist.assert_called_once()
    assert persist.call_args.args[1] == []


def test_seam_gates_in_above_threshold_pair_end_to_end(
    monkeypatch, job_module
) -> None:
    """The seam returning a constructed above-threshold mapping gates the
    lexically opposing pair IN: the real heuristic then disputes it and
    the dispute reaches the persistence boundary."""
    persist, messages = _sweep_env(monkeypatch, job_module, _typed_pair_docs())
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_compute_embeddings",
        staticmethod(lambda docs: {"m1": [1.0, 0.0], "m2": [1.0, 0.0]}),
    )

    ext = job_module.ContradictionDetectionJob(agent=_FAKE_AGENT)
    asyncio.run(ext.execute())

    summary = [m for m in messages if "disputed=" in m and "subdirs=" in m]
    assert summary
    assert "checked=2" in summary[-1]
    assert "disputed=1" in summary[-1]
    persist.assert_called_once()
    disputes = persist.call_args.args[1]
    assert len(disputes) == 1
    assert disputes[0]["memory_id"] == "m1"
    assert disputes[0]["disputed_id"] == "m2"


def test_production_binding_always_passes_a_mapping(monkeypatch, job_module) -> None:
    """Production binding (condition 3): the job ALWAYS passes an
    embeddings mapping to the lifecycle function — never None. Pinned
    via the seam's production failure value (the EMPTY mapping): even
    on mechanism failure the lifecycle call receives a dict."""
    docs = _typed_pair_docs()
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_read_config",
        staticmethod(lambda: {
            "contradiction_detection_enabled": True,
            "contradiction_interval_hours": 168,
            "contradiction_llm_enabled": False,
        }),
    )
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_subdirs",
        staticmethod(lambda: ["test_subdir"]),
    )
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_iter_docs",
        staticmethod(lambda subdir: list(docs)),
    )
    monkeypatch.setattr(job_module, "_boot_grace_active", lambda: False)
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_compute_embeddings",
        staticmethod(lambda docs: {}),
    )
    mock_fn = MagicMock(return_value={"checked": 0, "disputed": 0, "disputes": []})
    monkeypatch.setattr(lifecycle, "run_contradiction_detection", mock_fn)

    ext = job_module.ContradictionDetectionJob(agent=_FAKE_AGENT)
    asyncio.run(ext.execute())

    mock_fn.assert_called_once()
    _args, kwargs = mock_fn.call_args
    assert isinstance(kwargs.get("embeddings"), dict)
    assert kwargs["embeddings"] == {}
