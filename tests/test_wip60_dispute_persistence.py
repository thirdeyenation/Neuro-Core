"""Tests for WI-P60-KI034-DISPUTE-PERSIST (KI-034 remediation, S1).

Approved design (steward-design-decision.yaml, approved-with-conditions):

    1. ``run_contradiction_detection`` returns an additive ``disputes``
       list (``memory_id``, ``disputed_id``, ``detected_at``, ``basis``)
       alongside the unchanged ``checked``/``disputed`` counters, and
       logs each detection structurally (Q5).
    2. The ``_30`` extension's ``_persist_disputes`` consumes that list
       and writes ``validation_status = "disputed"`` to FAISS document
       metadata (storage authority A1, Q2) — enforcing the Q1 vocabulary
       mapping and the Q4 transition governance at the persistence
       boundary:
         - only ``unvalidated``/``unreviewed`` -> ``disputed`` and
           ``validated`` -> ``disputed`` are written;
         - terminal states (``deprecated`` FAISS / ``superseded``
           domain) are skipped;
         - ``disputed`` -> ``disputed`` is a no-op;
         - unknown values are skipped defensively (nothing invented).
    3. Persistence is best-effort and exception-safe: a failing
       ``update_documents`` is logged and swallowed.

All data is synthetic in-memory fixtures; no plugin data files, no
FAISS index and no LLM are touched.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
import types
from pathlib import Path

import pytest

from usr.plugins.neuro_core.helpers import lifecycle


_PLUGIN = Path("/a0/usr/plugins/neuro_core")
_JOB = _PLUGIN / "extensions" / "python" / "job_loop" / "_30_contradiction_detection.py"


def _load_job_module():
    spec = importlib.util.spec_from_file_location(
        "_neuro_core_test_wip60_dispute_persist", str(_JOB)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fact(
    memory_id: str,
    content: str,
    *,
    timestamp: str = "2026-01-01T00:00:00+00:00",
    extra: dict | None = None,
):
    meta = {"timestamp": timestamp}
    if extra:
        meta.update(extra)
    return (memory_id, content, meta)


def _lifecycle_run(facts):
    return lifecycle.run_contradiction_detection(
        "default",
        {"contradiction_similarity_threshold": 0.85},
        memory=None,
        facts=facts,
    )


# ---------------------------------------------------------------------------
# 1. Lifecycle disputes contract (additive, counters unchanged)
# ---------------------------------------------------------------------------


class TestLifecycleDisputesContract:
    def test_disputes_list_additive_with_older_wins(self) -> None:
        """A contradicting pair yields one dispute entry: the older
        memory is ``memory_id``, the newer is ``disputed_id``; the
        counters are unchanged."""
        facts = [
            _fact("old", "please enable the plugin",
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("new", "please disable the plugin",
                  timestamp="2026-06-01T00:00:00+00:00"),
        ]
        result = _lifecycle_run(facts)
        assert result["checked"] == 2
        assert result["disputed"] == 1
        assert len(result["disputes"]) == 1
        entry = result["disputes"][0]
        assert entry["memory_id"] == "old"
        assert entry["disputed_id"] == "new"
        assert entry["detected_at"]
        assert entry["basis"]

    def test_agreeing_facts_empty_disputes(self) -> None:
        facts = [
            _fact("a", "the server is enabled and running"),
            _fact("b", "the server is enabled and running"),
        ]
        result = _lifecycle_run(facts)
        assert result["checked"] == 2
        assert result["disputed"] == 0
        assert result["disputes"] == []

    def test_no_facts_returns_empty_disputes(self) -> None:
        result = lifecycle.run_contradiction_detection(
            "default", {}, memory=None, facts=None
        )
        assert result == {"checked": 0, "disputed": 0, "disputes": []}

    def test_structural_log_line_per_detection(self, caplog) -> None:
        """Q5: each detection logs memory_id, disputed_id, detected_at
        and basis as a structured log line."""
        facts = [
            _fact("old", "please enable the plugin",
                  timestamp="2026-01-01T00:00:00+00:00"),
            _fact("new", "please disable the plugin",
                  timestamp="2026-06-01T00:00:00+00:00"),
        ]
        with caplog.at_level(logging.INFO, logger="neuro_core.lifecycle"):
            _lifecycle_run(facts)
        dispute_lines = [
            r for r in caplog.records if "dispute detected" in r.getMessage()
        ]
        assert len(dispute_lines) == 1
        msg = dispute_lines[0].getMessage()
        assert "memory_id=old" in msg
        assert "disputed_id=new" in msg
        assert "detected_at=" in msg
        assert "basis=" in msg


# ---------------------------------------------------------------------------
# 2. _persist_disputes transition governance (Q4) and vocabulary (Q1)
# ---------------------------------------------------------------------------


def _dispute(memory_id: str, disputed_id: str = "newer") -> dict:
    return {
        "memory_id": memory_id,
        "disputed_id": disputed_id,
        "detected_at": "2026-10-05T18:00:00+00:00",
        "basis": "test basis",
    }


class _FakeDoc:
    """Stand-in for a FAISS-backed document with dict metadata."""

    def __init__(self, memory_id: str, validation_status: str | None):
        self.metadata = {"id": memory_id}
        if validation_status is not None:
            self.metadata["validation_status"] = validation_status


class _FakeMemoryHandle:
    """Stand-in for the resolved ``Memory`` handle used by
    ``_persist_disputes``: exposes ``db.get_all_docs()`` and an async
    ``update_documents`` that records what it was called with.

    WI-P60 rev 2 (VAL integration-FAIL fix, defect 3 — test fidelity):
    ``get_all_docs`` mirrors the REAL host contract
    (``MyFaiss.get_all_docs`` returns ``self.docstore._dict``): a DICT
    of ``docstore_id -> Document``. The previous stub returned a list,
    structurally masking the dict-iteration defect on the real host
    path.
    """

    def __init__(self, docs):
        # Real host contract: dict of docstore_id -> Document.
        self.db = types.SimpleNamespace(
            get_all_docs=lambda: {f"ds_{i}": d for i, d in enumerate(docs)}
        )
        self.updated = []

        async def _update_documents(docs_to_update):
            self.updated.append(list(docs_to_update))

        self.update_documents = _update_documents


def _patch_memory_env(monkeypatch, job_module, docs) -> _FakeMemoryHandle:
    """Patch the plugin-local Memory import and memory resolution so
    ``_persist_disputes`` runs against an in-memory fake handle."""
    handle = _FakeMemoryHandle(docs)
    fake_mod = types.ModuleType("plugins._memory.helpers.memory")
    fake_mod.Memory = object
    monkeypatch.setitem(sys.modules, "plugins._memory.helpers.memory", fake_mod)
    monkeypatch.setattr(
        job_module, "_get_memory_sync", lambda MemoryCls, subdir: handle
    )
    return handle


@pytest.fixture()
def job_module():
    return _load_job_module()


class TestPersistDisputesGovernance:
    def test_unvalidated_to_disputed_persisted(self, monkeypatch, job_module) -> None:
        handle = _patch_memory_env(
            monkeypatch, job_module, [_FakeDoc("m1", "unvalidated")]
        )
        job_module.ContradictionDetectionJob._persist_disputes(
            "default", [_dispute("m1")]
        )
        assert len(handle.updated) == 1
        assert handle.updated[0][0].metadata["validation_status"] == "disputed"

    def test_unreviewed_domain_alias_maps_to_unvalidated(
        self, monkeypatch, job_module
    ) -> None:
        """Q1: domain-only ``unreviewed`` is the alias of
        ``unvalidated`` and the transition to ``disputed`` is legal."""
        handle = _patch_memory_env(
            monkeypatch, job_module, [_FakeDoc("m1", "unreviewed")]
        )
        job_module.ContradictionDetectionJob._persist_disputes(
            "default", [_dispute("m1")]
        )
        assert len(handle.updated) == 1
        assert handle.updated[0][0].metadata["validation_status"] == "disputed"

    def test_validated_to_disputed_persisted(self, monkeypatch, job_module) -> None:
        handle = _patch_memory_env(
            monkeypatch, job_module, [_FakeDoc("m1", "validated")]
        )
        job_module.ContradictionDetectionJob._persist_disputes(
            "default", [_dispute("m1")]
        )
        assert len(handle.updated) == 1
        assert handle.updated[0][0].metadata["validation_status"] == "disputed"

    @pytest.mark.parametrize("terminal", ["deprecated", "superseded"])
    def test_terminal_states_skipped(
        self, monkeypatch, job_module, terminal
    ) -> None:
        """Q4: no transition out of a terminal state is ever written."""
        handle = _patch_memory_env(
            monkeypatch, job_module, [_FakeDoc("m1", terminal)]
        )
        job_module.ContradictionDetectionJob._persist_disputes(
            "default", [_dispute("m1")]
        )
        assert handle.updated == []

    def test_disputed_to_disputed_noop(self, monkeypatch, job_module) -> None:
        handle = _patch_memory_env(
            monkeypatch, job_module, [_FakeDoc("m1", "disputed")]
        )
        job_module.ContradictionDetectionJob._persist_disputes(
            "default", [_dispute("m1")]
        )
        assert handle.updated == []

    def test_unknown_status_skipped_no_value_invented(
        self, monkeypatch, job_module, caplog
    ) -> None:
        handle = _patch_memory_env(
            monkeypatch, job_module, [_FakeDoc("m1", "some-future-value")]
        )
        with caplog.at_level(logging.WARNING, logger=job_module._logger.name):
            job_module.ContradictionDetectionJob._persist_disputes(
                "default", [_dispute("m1")]
            )
        assert handle.updated == []
        assert any(
            "unmapped validation_status" in r.getMessage() for r in caplog.records
        )

    def test_structural_persist_log_line(
        self, monkeypatch, job_module, caplog
    ) -> None:
        handle = _patch_memory_env(
            monkeypatch, job_module, [_FakeDoc("m1", "unvalidated")]
        )
        with caplog.at_level(logging.INFO, logger=job_module._logger.name):
            job_module.ContradictionDetectionJob._persist_disputes(
                "default", [_dispute("m1")]
            )
        persist_lines = [
            r for r in caplog.records if "dispute persisted" in r.getMessage()
        ]
        assert len(persist_lines) == 1
        msg = persist_lines[0].getMessage()
        assert "memory_id=m1" in msg
        assert "validation_status=disputed" in msg
        assert "previous_status=unvalidated" in msg

    def test_empty_disputes_is_full_noop(self, monkeypatch, job_module) -> None:
        handle = _patch_memory_env(monkeypatch, job_module, [])
        job_module.ContradictionDetectionJob._persist_disputes("default", [])
        assert handle.updated == []

    def test_update_documents_failure_swallowed(
        self, monkeypatch, job_module, caplog
    ) -> None:
        """Exception safety: a failing ``update_documents`` is logged and
        swallowed — the scheduler stays healthy."""
        handle = _patch_memory_env(
            monkeypatch, job_module, [_FakeDoc("m1", "unvalidated")]
        )

        async def _boom(docs):
            raise RuntimeError("simulated persistence failure")

        handle.update_documents = _boom
        with caplog.at_level(logging.WARNING, logger=job_module._logger.name):
            job_module.ContradictionDetectionJob._persist_disputes(
                "default", [_dispute("m1")]
            )
        assert any("persist failed" in r.getMessage() for r in caplog.records)

    def test_failed_write_retried_on_next_pass(
        self, monkeypatch, job_module, caplog
    ) -> None:
        """WI-P60 rev 3 (VAL discriminating failure-injection experiment):
        a failing ``update_documents`` must NOT mutate the in-memory
        document metadata before the write — otherwise the next pass
        sees disputed->disputed, no-ops, and the dispute is silently
        lost. With the staged-copy fix: after a failed pass the original
        keeps its pre-write status, the next pass re-attempts the write,
        and the dispute lands.
        """
        doc = _FakeDoc("m1", "unvalidated")
        store = {"ds_0": doc}
        handle = _patch_memory_env(monkeypatch, job_module, [doc])
        # Make the fake store stateful: get_all_docs reflects current
        # store contents; a working update_documents applies the payload.
        handle.db.get_all_docs = lambda: dict(store)

        calls = {"n": 0}

        async def _update(docs_to_update):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated host write failure")
            for d in docs_to_update:
                store[f"ds_new_{d.metadata['id']}"] = d

        handle.update_documents = _update

        # Pass 1: write fails.
        with caplog.at_level(logging.WARNING, logger=job_module._logger.name):
            job_module.ContradictionDetectionJob._persist_disputes(
                "default", [_dispute("m1")]
            )
        assert calls["n"] == 1
        # The original in-memory metadata was never mutated pre-write.
        assert doc.metadata["validation_status"] == "unvalidated"
        assert any("persist failed" in r.getMessage() for r in caplog.records)

        # Pass 2 (the documented retry): the transition is still needed,
        # so update_documents is invoked again and the dispute lands.
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=job_module._logger.name):
            job_module.ContradictionDetectionJob._persist_disputes(
                "default", [_dispute("m1")]
            )
        assert calls["n"] == 2
        written = [d for d in store.values() if d.metadata.get("id") == "m1"]
        assert written and written[-1].metadata["validation_status"] == "disputed"
        assert any(
            "dispute persisted" in r.getMessage() for r in caplog.records
        )

    def test_failed_write_originals_unmutated_noop_next_pass_guard(
        self, monkeypatch, job_module
    ) -> None:
        """Complement to the retry test: after a FAILED write the
        in-memory document must still carry its pre-write status (the
        rev-2 defect mutated it to 'disputed' before the write)."""
        doc = _FakeDoc("m1", "validated")
        handle = _patch_memory_env(monkeypatch, job_module, [doc])

        async def _boom(docs):
            raise RuntimeError("simulated persistence failure")

        handle.update_documents = _boom
        job_module.ContradictionDetectionJob._persist_disputes(
            "default", [_dispute("m1")]
        )
        assert doc.metadata["validation_status"] == "validated"


# ---------------------------------------------------------------------------
# 3. End-to-end: real heuristic through the sweep persists the dispute
# ---------------------------------------------------------------------------


def test_sweep_persists_dispute_end_to_end(monkeypatch, job_module) -> None:
    """Integration pin: the real lifecycle heuristic runs through the
    scheduled sweep; the disputes list reaches _persist_disputes and the
    disputed status is written to the (fake) FAISS metadata."""
    docs = [
        {"id": "m1",
         "metadata": {"id": "m1",
                      "timestamp": "2026-01-01T00:00:00+00:00",
                      "validation_status": "unvalidated",
                      # WI-P63 (KI-048) condition 6: explicit fact typing —
                      # the sweep's eligibility predicate (condition 2)
                      # requires a normalized 'fact' type; D-NC1-137/138.
                      "memory_type": "fact"},
         "page_content": "The sync completed successfully."},
        {"id": "m2",
         "metadata": {"id": "m2",
                      "timestamp": "2026-06-01T00:00:00+00:00",
                      "validation_status": "unvalidated",
                      # WI-P63 (KI-048) condition 6: explicit fact typing.
                      "memory_type": "fact"},
         "page_content": "The sync failed."},
    ]
    handle = _patch_memory_env(monkeypatch, job_module, [
        _FakeDoc("m1", "unvalidated"), _FakeDoc("m2", "unvalidated")
    ])
    monkeypatch.setattr(job_module, "_boot_grace_active", lambda: False)
    # WI-P63 (KI-048) condition 6: monkeypatch ONLY the embedding seam
    # (the suite runtime cannot import the embedding mechanism); the
    # returned vectors gate the lexically opposing pair IN (cosine 1.0
    # >= threshold) so the real heuristic still produces the dispute
    # this test pins. D-NC1-137/138.
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_compute_embeddings",
        staticmethod(lambda docs: {"m1": [1.0, 0.0], "m2": [1.0, 0.0]}),
    )
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

    ext = job_module.ContradictionDetectionJob(agent=object())
    asyncio.run(ext.execute())

    # The older memory (m1) is disputed and persisted to FAISS metadata.
    assert len(handle.updated) == 1
    updated = handle.updated[0]
    assert len(updated) == 1
    assert updated[0].metadata["id"] == "m1"
    assert updated[0].metadata["validation_status"] == "disputed"


# ---------------------------------------------------------------------------
# 4. WI-P60 rev 2 — host-contract fidelity (VAL integration-FAIL fixes)
# ---------------------------------------------------------------------------


class TestAllDocsFromDbContract:
    """Defect 1 fix: the real host MyFaiss.get_all_docs() returns a
    DICT (docstore_id -> Document); the helper must handle the dict
    contract and still accept a defensive list contract."""

    def test_dict_contract_yields_documents(self, job_module) -> None:
        d1, d2 = _FakeDoc("m1", "unvalidated"), _FakeDoc("m2", "validated")
        db = types.SimpleNamespace(get_all_docs=lambda: {"k1": d1, "k2": d2})
        assert job_module._all_docs_from_db(db) == [d1, d2]

    def test_list_contract_still_accepted(self, job_module) -> None:
        d1 = _FakeDoc("m1", "unvalidated")
        db = types.SimpleNamespace(get_all_docs=lambda: [d1])
        assert job_module._all_docs_from_db(db) == [d1]

    def test_get_all_docs_failure_returns_empty(self, job_module) -> None:
        def _boom():
            raise RuntimeError("simulated db failure")

        db = types.SimpleNamespace(get_all_docs=_boom)
        assert job_module._all_docs_from_db(db) == []


class TestNormalizeMemoryHandle:
    """Defect 2 fix: the production warm cache holds RAW MyFaiss
    objects (no .db, no update_documents); the handle must be
    normalized to the Memory-wrapper shape in both cache states."""

    def test_wrapper_passthrough(self, job_module) -> None:
        handle = _FakeMemoryHandle([])
        assert job_module._normalize_memory_handle(handle, "default") is handle

    def test_raw_handle_wrapped_in_real_memory(
        self, job_module, monkeypatch
    ) -> None:
        raw = types.SimpleNamespace(get_all_docs=lambda: {})
        wrapped = []

        class _FakeWrapper:
            def __init__(self, db, memory_subdir):
                wrapped.append((db, memory_subdir))

        fake_mod = types.ModuleType("plugins._memory.helpers.memory")
        fake_mod.Memory = _FakeWrapper
        monkeypatch.setitem(sys.modules, "plugins._memory.helpers.memory", fake_mod)
        result = job_module._normalize_memory_handle(raw, "sub_x")
        assert isinstance(result, _FakeWrapper)
        assert wrapped == [(raw, "sub_x")]

    def test_raw_handle_unwrappable_returns_none(
        self, job_module, monkeypatch
    ) -> None:
        raw = types.SimpleNamespace(get_all_docs=lambda: {})
        fake_mod = types.ModuleType("plugins._memory.helpers.memory")
        fake_mod.Memory = object  # __init__ signature mismatch -> wrap fails
        monkeypatch.setitem(sys.modules, "plugins._memory.helpers.memory", fake_mod)
        assert job_module._normalize_memory_handle(raw, "sub_x") is None

    def test_none_handle_returns_none(self, job_module) -> None:
        assert job_module._normalize_memory_handle(None, "default") is None


def test_persist_disputes_raw_warm_cache_handle(monkeypatch, job_module) -> None:
    """Defect 2 end-to-end pin: a RAW warm-cache handle (MyFaiss-like:
    has get_all_docs, NO .db, NO update_documents — the production
    Memory.index shape) is wrapped and the dispute is persisted through
    the wrapper's update_documents mechanism."""
    doc = _FakeDoc("m1", "unvalidated")
    raw = types.SimpleNamespace(get_all_docs=lambda: {"ds_0": doc})
    recorded = []

    class _FakeWrapper:
        def __init__(self, db, memory_subdir):
            self.db = db
            self.memory_subdir = memory_subdir

        async def update_documents(self, docs):
            recorded.append(list(docs))

    fake_mod = types.ModuleType("plugins._memory.helpers.memory")
    fake_mod.Memory = _FakeWrapper
    monkeypatch.setitem(sys.modules, "plugins._memory.helpers.memory", fake_mod)
    monkeypatch.setattr(
        job_module, "_get_memory_sync", lambda MemoryCls, subdir: raw
    )

    job_module.ContradictionDetectionJob._persist_disputes(
        "default", [_dispute("m1")]
    )
    assert len(recorded) == 1
    # WI-P60 rev 3 (mutation-before-write fix): the payload is a staged
    # deep copy carrying the disputed status; the original in-memory
    # document keeps its pre-write status so a failed write is retried
    # on the next pass.
    assert recorded[0][0].metadata["id"] == "m1"
    assert recorded[0][0].metadata["validation_status"] == "disputed"
    assert doc.metadata["validation_status"] == "unvalidated"


def test_iter_docs_dict_contract_yields_facts(monkeypatch, job_module) -> None:
    """Defect 1 pin for the sweep read side: _iter_docs must enumerate
    facts when get_all_docs() returns the real host DICT contract —
    previously it list-iterated string keys and yielded nothing."""
    d1 = _FakeDoc("m1", "unvalidated")
    d1.metadata["timestamp"] = "2026-01-01T00:00:00+00:00"
    d1.page_content = "The sync completed successfully."
    raw = types.SimpleNamespace(get_all_docs=lambda: {"ds_0": d1})

    class _FakeWrapper:
        def __init__(self, db, memory_subdir):
            self.db = db
            self.memory_subdir = memory_subdir

    fake_mod = types.ModuleType("plugins._memory.helpers.memory")
    fake_mod.Memory = _FakeWrapper
    monkeypatch.setitem(sys.modules, "plugins._memory.helpers.memory", fake_mod)
    monkeypatch.setattr(
        job_module, "_get_memory_sync", lambda MemoryCls, subdir: raw
    )

    docs = list(job_module.ContradictionDetectionJob._iter_docs("default"))
    assert len(docs) == 1
    assert docs[0]["id"] == "m1"
    assert docs[0]["page_content"] == "The sync completed successfully."
    assert docs[0]["metadata"]["timestamp"] == "2026-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# 5. Host-faithful tests (real MyFaiss + real Memory wrapper).
#
# These execute only where the host FAISS stack is importable (framework
# runtime /opt/venv-a0, as in VAL's integration harness); the canonical
# suite runtime (/opt/venv) has no faiss, so they skip there. Fixtures
# are disposable /tmp dirs with abs_db_dir patched — no live plugin data
# files or live FAISS index are ever touched.
# ---------------------------------------------------------------------------

try:
    import faiss  # noqa: F401

    _HOST_FAISS_AVAILABLE = True
except Exception:
    _HOST_FAISS_AVAILABLE = False

_host_faiss = pytest.mark.skipif(
    not _HOST_FAISS_AVAILABLE,
    reason="host FAISS stack unavailable in this runtime; executed under the "
    "framework runtime as in VAL's integration harness",
)


def _host_fixture(monkeypatch):
    """Patch abs_db_dir to a disposable /tmp fixture dir (VAL's proven
    approach) and disable knowledge preloading. Returns the real host
    module.

    The NC1 suite conftest stubs ``plugins._memory.helpers.memory``
    whenever it is not already importable; under the framework runtime
    the REAL host stack exists, so the stub is swapped out for the real
    module for the duration of each host-faithful test (monkeypatch
    restores the stub afterwards).
    """
    import importlib
    import tempfile

    # The conftest stubs the whole ``plugins._memory`` package chain,
    # the framework ``helpers`` package and its submodules, and
    # ``agent``; drop them all (monkeypatch restores them afterwards)
    # so the real host stack at /a0 is imported fresh, as in VAL's
    # standalone probe harness.
    for name in list(sys.modules):
        if (
            name == "helpers"
            or name.startswith("helpers.")
            or name == "plugins"
            or name.startswith("plugins.")
            or name == "agent"
        ):
            monkeypatch.delitem(sys.modules, name)
    if "/a0" not in sys.path:
        sys.path.insert(0, "/a0")
    mm = importlib.import_module("plugins._memory.helpers.memory")
    assert hasattr(mm.Memory, "get_by_subdir"), "real host Memory class required"

    tmp = tempfile.mkdtemp(prefix="nc1-wip60-rev2-")
    mm.abs_db_dir = lambda subdir: os.path.join(tmp, "memory", subdir)
    mm.get_knowledge_subdirs_by_memory_subdir = lambda *a, **k: []
    return tmp, mm


@_host_faiss
def test_host_warm_cache_persist_dispute(monkeypatch) -> None:
    """Host-faithful (VAL defect 1+2 reproduction): with the production
    warm cache holding a RAW MyFaiss in Memory.index, _persist_disputes
    writes validation_status=disputed through the real host
    update_documents mechanism."""
    from langchain_core.documents import Document

    _tmp, mm = _host_fixture(monkeypatch)
    Memory = mm.Memory

    subdir = "wip60rev2warm"
    mem = asyncio.run(Memory.get_by_subdir(subdir, preload_knowledge=False))
    assert type(Memory.index.get(subdir)).__name__ == "MyFaiss"  # raw, warm

    docs = [
        Document(page_content="User lives in Berlin", metadata={
            "id": "m_old", "memory_type": "fact",
            "timestamp": "2026-10-05T10:00:00+00:00",
            "validation_status": "unvalidated"}),
        Document(page_content="User lives in Paris", metadata={
            "id": "m_new", "memory_type": "fact",
            "timestamp": "2026-10-05T11:00:00+00:00",
            "validation_status": "unvalidated"}),
    ]
    asyncio.run(mem.insert_documents(docs))
    actual = {
        d.metadata.get("id"): d.metadata.get("validation_status")
        for d in mem.db.get_all_docs().values()
    }
    # Use the actual host-generated ids (insert_documents assigns ids).
    ids = list(actual.keys())
    old_id, new_id = ids[0], ids[1]

    job = _load_job_module()
    job.ContradictionDetectionJob._persist_disputes(
        subdir,
        [{"memory_id": old_id, "disputed_id": new_id,
          "detected_at": "2026-10-05T12:00:00+00:00", "basis": "test"}],
    )
    after = {
        d.metadata.get("id"): d.metadata.get("validation_status")
        for d in mem.db.get_all_docs().values()
    }
    assert after[old_id] == "disputed"
    assert after[new_id] == "unvalidated"


@_host_faiss
def test_host_cold_cache_persist_restart_survival(monkeypatch) -> None:
    """Host-faithful end-to-end: cold-cache persist through the real
    host path, then a reload-from-disk restart simulation — the
    disputed status survives and is visible via recall_shaping's
    _validation_factor read path."""
    from langchain_core.documents import Document

    _tmp, mm = _host_fixture(monkeypatch)
    Memory = mm.Memory

    subdir = "wip60rev2cold"
    mem = asyncio.run(Memory.get_by_subdir(subdir, preload_knowledge=False))
    docs = [
        Document(page_content="User likes tea", metadata={
            "id": "x1", "memory_type": "fact",
            "timestamp": "2026-10-05T10:00:00+00:00",
            "validation_status": "unvalidated"}),
    ]
    asyncio.run(mem.insert_documents(docs))
    doc_id = list(mem.db.get_all_docs().values())[0].metadata.get("id")

    # Cold cache: clear the warm cache so the extension resolves fresh.
    Memory.index.clear()
    job = _load_job_module()
    job.ContradictionDetectionJob._persist_disputes(
        subdir,
        [{"memory_id": doc_id, "disputed_id": "none", "detected_at": "t",
          "basis": "test"}],
    )

    # Restart simulation: reload from disk (real save_local/load_local).
    Memory.index.clear()
    mem2 = asyncio.run(Memory.get_by_subdir(subdir, preload_knowledge=False))
    after = {
        d.metadata.get("id"): d.metadata.get("validation_status")
        for d in mem2.db.get_all_docs().values()
    }
    assert after[doc_id] == "disputed"

    # Read-path visibility via recall_shaping (read-only use).
    from usr.plugins.neuro_core.helpers.recall_shaping import _validation_factor

    for d in mem2.db.get_all_docs().values():
        factor, status = _validation_factor(d.metadata, {})
        assert status == "disputed"
        assert factor == 0.6
