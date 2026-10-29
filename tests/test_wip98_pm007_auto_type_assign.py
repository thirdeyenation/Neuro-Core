'''Tests for WI-P98-PM007-AUTO-TYPE-ASSIGN — capture-time auto-assignment
of ``memory_type`` (PM-007), implemented per D-NC1-179 and the binding
ARC conditions C1-C10 in steward-design-decision.yaml rev 1:

  - helpers/metadata.py: pure, total rules-based classifier
    (``classify_memory_type``) and the absent-only applier
    (``apply_auto_memory_type``) with ``memory_type_origin`` provenance
    (C3, C6).
  - Both ``_10_neuro_metadata`` insert hooks invoke the applier AFTER
    ``validate_neuro_metadata`` (C2), absent-only; present types are
    never overwritten; invalid types coerced by validation carry NO
    origin key; the collection-only edge case is skipped (C4).
  - ``api/memory_edit.py`` annotates ``memory_type_origin: manual`` on
    the types edit path ONLY (C5); WI-P59-KI029 guarantees unchanged.
  - No backfill of legacy records (C7): the applier is invoked only on
    the insert path; existing records are untouched.
  - Docs honesty (C10): classification is documented as heuristic
    rules-based, NOT semantic understanding; no performance,
    concurrency, or security claims.
  - The PM-011 drift guard (MEMORY_TYPE_ENUM) keeps passing.
'''

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from usr.plugins.neuro_core.helpers import metadata as md_mod
from usr.plugins.neuro_core.helpers.metadata import (
    MEMORY_TYPE_ORIGIN_AUTO,
    MEMORY_TYPE_ORIGIN_MANUAL,
    VALID_MEMORY_TYPES,
    apply_auto_memory_type,
    classify_memory_type,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOK_BASE = (
    PLUGIN_ROOT
    / "extensions"
    / "python"
    / "_functions"
    / "plugins"
    / "_memory"
    / "helpers"
    / "memory"
    / "Memory"
)
HOOK_TEXT = HOOK_BASE / "insert_text" / "start" / "_10_neuro_metadata.py"
HOOK_DOCS = HOOK_BASE / "insert_documents" / "start" / "_10_neuro_metadata.py"


def _load_hook(rel: Path, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, str(rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Doc:
    """Minimal Document stand-in: mutable metadata dict + page_content."""

    def __init__(self, metadata: dict, page_content: str = ""):
        self.metadata = metadata
        self.page_content = page_content


# ---------------------------------------------------------------------
# (a) classifier unit properties (C6: pure, total, conservative fallback)
# ---------------------------------------------------------------------


def test_classifier_is_total_never_raises() -> None:
    """C6: any input is tolerated; output is always a valid enum value."""
    for text in (None, "", 42, b"bytes", ["weird"], {"x": 1}, object()):
        assert classify_memory_type(text) in VALID_MEMORY_TYPES
    # Metadata garbage is tolerated too.
    assert classify_memory_type("x", metadata="not-a-dict") in VALID_MEMORY_TYPES
    assert classify_memory_type("x", metadata=None) in VALID_MEMORY_TYPES


def test_classifier_is_pure() -> None:
    """C6: no mutation of inputs, deterministic across calls."""
    meta = {"area": "main", "source": "agent_zero"}
    before = dict(meta)
    r1 = classify_memory_type("We decided to use SQLite", meta)
    r2 = classify_memory_type("We decided to use SQLite", meta)
    assert r1 == r2 == "decision"
    assert meta == before, "classifier must not mutate metadata"


def test_classifier_cue_battery() -> None:
    assert classify_memory_type("We decided to use SQLite") == "decision"
    assert classify_memory_type("I prefer dark mode") == "preference"
    assert classify_memory_type("TODO: fix the login bug") == "task"
    assert classify_memory_type("The meeting happened yesterday") == "event"
    assert classify_memory_type(
        "How to reset the index: step-by-step procedure"
    ) == "skill"
    assert classify_memory_type("A shard refers to a scoped unit") == "concept"
    assert classify_memory_type("The store is located in /tmp") == "fact"


def test_classifier_conservative_note_fallback() -> None:
    """No confident cue -> conservative 'note' fallback (design ruling)."""
    assert classify_memory_type("asdf qwerty") == "note"
    assert classify_memory_type("") == "note"
    assert classify_memory_type(None) == "note"


def test_classifier_metadata_area_cue_solutions_skill() -> None:
    meta = {"area": "solutions"}
    assert classify_memory_type("anything at all", meta) == "skill"


# ---------------------------------------------------------------------
# (b) applier: absent-only semantics (C2), collection-only edge (C4)
# ---------------------------------------------------------------------


def test_applier_absent_type_classifies_with_auto_origin() -> None:
    meta: dict[str, Any] = {"id": "m1", "area": "main"}
    apply_auto_memory_type(meta, "I prefer tea")
    assert meta["memory_type"] == "preference"
    assert meta["memory_type_origin"] == MEMORY_TYPE_ORIGIN_AUTO


def test_applier_present_type_never_overwritten_no_origin() -> None:
    """C2: a present scalar (valid) is untouched and carries no origin."""
    meta = {"id": "m2", "memory_type": "fact"}
    apply_auto_memory_type(meta, "I prefer tea")
    assert meta["memory_type"] == "fact"
    assert "memory_type_origin" not in meta


def test_applier_coerced_note_carries_no_origin() -> None:
    """C2: a record whose invalid type was coerced to 'note' by
    validate_neuro_metadata carries a scalar memory_type — the applier
    must not touch it and must not write an origin key."""
    meta = {"id": "m3", "memory_type": "note"}  # post-coercion state
    apply_auto_memory_type(meta, "We decided to use SQLite")
    assert meta["memory_type"] == "note"
    assert "memory_type_origin" not in meta


def test_applier_collection_only_edge_case_skipped() -> None:
    """C4: memory_types present WITHOUT the scalar primary — explicitly
    specified behavior: classification is SKIPPED (no scalar write, no
    origin key); the read path derives type info from the collection."""
    meta = {"id": "m4", "memory_types": ["fact", "hypothesis"]}
    apply_auto_memory_type(meta, "I prefer tea")
    assert "memory_type" not in meta
    assert "memory_type_origin" not in meta
    assert meta["memory_types"] == ["fact", "hypothesis"]


def test_applier_never_raises_on_garbage() -> None:
    apply_auto_memory_type(None, "x")  # type: ignore[arg-type]
    apply_auto_memory_type("not-a-dict", "x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# (c) hook-path integration: both hooks classify absent types (C2/C4)
# ---------------------------------------------------------------------


def _run_text_hook(metadata: dict, text: str) -> None:
    mod = _load_hook(HOOK_TEXT, f"_p98_text_hook_{id(metadata)}")
    ext = mod.NeuroInsertTextMetadata(agent=None)
    ext.execute(data={"args": (object(), text, metadata), "kwargs": {}})


def _run_docs_hook(doc: _Doc) -> None:
    mod = _load_hook(HOOK_DOCS, f"_p98_docs_hook_{id(doc)}")
    ext = mod.NeuroInsertMetadata(agent=None)
    ext.execute(data={"args": (object(), [doc]), "kwargs": {}})


def test_hook_insert_text_classifies_absent_type() -> None:
    meta: dict[str, Any] = {"id": "h1", "area": "main"}
    _run_text_hook(meta, "TODO: fix the login bug")
    assert meta["memory_type"] == "task"
    assert meta["memory_type_origin"] == MEMORY_TYPE_ORIGIN_AUTO


def test_hook_insert_documents_classifies_absent_type() -> None:
    doc = _Doc({"id": "h2", "area": "main"}, "I prefer dark mode")
    _run_docs_hook(doc)
    assert doc.metadata["memory_type"] == "preference"
    assert doc.metadata["memory_type_origin"] == MEMORY_TYPE_ORIGIN_AUTO


def test_hook_insert_documents_present_type_not_overwritten() -> None:
    doc = _Doc({"id": "h3", "memory_type": "fact"}, "I prefer dark mode")
    _run_docs_hook(doc)
    assert doc.metadata["memory_type"] == "fact"
    assert "memory_type_origin" not in doc.metadata


def test_hook_insert_documents_collection_only_skipped() -> None:
    """C4 on the docs hook path: collection-only record stays scalar-free."""
    doc = _Doc(
        {"id": "h4", "memory_types": ["fact"]}, "I prefer dark mode"
    )
    _run_docs_hook(doc)
    assert "memory_type" not in doc.metadata
    assert "memory_type_origin" not in doc.metadata


def test_hook_failure_injection_never_breaks_host_call() -> None:
    """C6: the classifier runs inside the existing broad except wrapper —
    an applier failure is logged and swallowed; the hook never re-raises."""
    mod = _load_hook(HOOK_TEXT, "_p98_text_hook_fail")
    ext = mod.NeuroInsertTextMetadata(agent=None)

    import usr.plugins.neuro_core.helpers.metadata as real_md

    original = real_md.apply_auto_memory_type

    def _boom(*a, **k):
        raise RuntimeError("injected hook failure")

    try:
        real_md.apply_auto_memory_type = _boom  # type: ignore[assignment]
        # Must NOT raise despite the injected failure.
        ext.execute(
            data={"args": (object(), "text", {"id": "h5"}), "kwargs": {}}
        )
    finally:
        real_md.apply_auto_memory_type = original  # type: ignore[assignment]


# ---------------------------------------------------------------------
# (d) manual edit path origin annotation (C5)
# ---------------------------------------------------------------------


def test_memory_edit_annotates_manual_origin() -> None:
    """C5: the types edit path writes memory_type_origin: manual."""
    from usr.plugins.neuro_core.api import memory_edit as me_mod

    src = Path(me_mod.__file__).read_text(encoding="utf-8")
    assert 'meta["memory_type_origin"] = MEMORY_TYPE_ORIGIN_MANUAL' in src
    assert me_mod.MEMORY_TYPE_ORIGIN_MANUAL == "manual"


async def test_memory_edit_types_edit_sets_manual_origin_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C5 end-to-end: a types POST through the real handler stages the
    manual origin on the document passed to update_documents. Call shape
    and fakes mirror test_wip59_ki029_memtype_edit.py (WI-P59 guarantees
    otherwise unchanged)."""
    from usr.plugins.neuro_core.api import memory_edit as me_mod
    from usr.plugins.neuro_core.helpers import scores as scores_mod

    class FakeDoc:
        def __init__(self, doc_id: str, metadata: dict):
            self.page_content = f"content of {doc_id}"
            self.metadata = metadata

    class FakeDB:
        def __init__(self, docs: dict):
            self.docs = docs

        async def aget_by_ids(self, ids):
            return [self.docs[i] for i in ids if i in self.docs]

    class FakeMemory:
        instances: dict = {}

        def __init__(self, db):
            self.db = db
            self.update_calls = 0
            self.updated_docs = []

        @classmethod
        async def get_by_subdir(cls, subdir):
            return cls.instances[subdir]

        async def update_documents(self, docs):
            self.update_calls += 1
            self.updated_docs.extend(docs)

    doc = FakeDoc("mem-1", {"id": "mem-1", "memory_type": "note"})
    fake = FakeMemory(FakeDB({"mem-1": doc}))
    FakeMemory.instances = {"default": fake}
    monkeypatch.setattr(me_mod, "Memory", FakeMemory)
    monkeypatch.setattr(
        scores_mod,
        "_scores_path",
        lambda subdir: str(tmp_path / f"scores_{subdir}_p98.json"),
    )

    h = me_mod.MemoryEditApi(app=None, thread_lock=None)
    out = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            "types": {"primary": "fact", "additional": []},
        },
        type("R", (), {"method": "POST", "args": {}})(),
    )
    assert out["success"] is True
    assert out["types_changed"] is True
    staged = fake.updated_docs[0]
    assert staged.metadata["memory_type"] == "fact"
    assert staged.metadata["memory_type_origin"] == MEMORY_TYPE_ORIGIN_MANUAL
    assert fake.update_calls == 1
    # Memory ID immutable; fetched doc not mutated (WI-P60 rev3 staging).
    assert staged.metadata["id"] == "mem-1"
    assert doc.metadata == {"id": "mem-1", "memory_type": "note"}


# ---------------------------------------------------------------------
# (e) no backfill (C7) + origin persistence across restart (C9)
# ---------------------------------------------------------------------


def test_no_backfill_of_legacy_records() -> None:
    """C7: the applier is insert-path-only; no migration/backfill code is
    added anywhere (grep guard over the plugin tree)."""
    import subprocess

    hits = (
        subprocess.run(
            [
                "grep",
                "-rl",
                "apply_auto_memory_type",
                str(PLUGIN_ROOT),
                "--include=*.py",
            ],
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .splitlines()
    )
    allowed = {
        str(PLUGIN_ROOT / "helpers" / "metadata.py"),
        str(HOOK_TEXT),
        str(HOOK_DOCS),
        str(PLUGIN_ROOT / "tests" / "test_wip98_pm007_auto_type_assign.py"),
    }
    unexpected = [h for h in hits if h not in allowed]
    assert not unexpected, f"unexpected applier call-sites (backfill?): {unexpected}"


try:
    import faiss  # noqa: F401

    _HOST_FAISS_AVAILABLE = True
except Exception:
    _HOST_FAISS_AVAILABLE = False

_host_faiss = pytest.mark.skipif(
    not _HOST_FAISS_AVAILABLE,
    reason="host FAISS stack unavailable in this runtime; executed under the "
    "framework runtime as in the WI-P60 precedent",
)


@_host_faiss
def test_origin_persistence_across_restart(monkeypatch) -> None:
    """C9: an auto-assigned type + origin survive a save/reload restart
    simulation through the REAL host FAISS persistence path (pattern
    from test_wip60_dispute_persistence.py, including its faiss-
    availability gating and host-assigned-ID lookup)."""
    import asyncio
    import os
    import sys
    import tempfile

    # Drop the conftest stubs so the real host stack at /a0 is imported
    # fresh (monkeypatch restores them afterwards).
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

    import importlib

    mm = importlib.import_module("plugins._memory.helpers.memory")
    assert hasattr(mm.Memory, "get_by_subdir"), "real host Memory class required"

    tmp = tempfile.mkdtemp(prefix="nc1-wip98-restart-")
    mm.abs_db_dir = lambda subdir: os.path.join(tmp, "memory", subdir)
    mm.get_knowledge_subdirs_by_memory_subdir = lambda *a, **k: []

    from langchain_core.documents import Document

    Memory = mm.Memory
    subdir = "wip98restart"
    mem = asyncio.run(Memory.get_by_subdir(subdir, preload_knowledge=False))

    meta: dict[str, Any] = {"id": "p98-restart-1", "area": "main"}
    apply_auto_memory_type(meta, "I prefer tea over coffee")
    assert meta["memory_type"] == "preference"
    assert meta["memory_type_origin"] == MEMORY_TYPE_ORIGIN_AUTO

    docs = [
        Document(page_content="I prefer tea over coffee", metadata=dict(meta)),
    ]
    asyncio.run(mem.insert_documents(docs))

    # The host Memory reassigns its own UUID at insert (WI-P60 pattern):
    # capture the host-assigned id from the store, not the supplied one.
    host_id = list(mem.db.get_all_docs().values())[0].metadata.get("id")
    assert host_id, "host-assigned id missing after insert"

    # Restart simulation: clear the warm cache and reload from disk
    # (real save_local/load_local).
    Memory.index.clear()
    mem2 = asyncio.run(Memory.get_by_subdir(subdir, preload_knowledge=False))
    reloaded = [
        d.metadata
        for d in mem2.db.get_all_docs().values()
        if d.metadata.get("id") == host_id
    ]
    assert reloaded, f"memory {host_id} not found after restart"
    lm = reloaded[0]
    assert lm.get("memory_type") == "preference"
    assert lm.get("memory_type_origin") == MEMORY_TYPE_ORIGIN_AUTO


# ---------------------------------------------------------------------
# (f) drift guard still passing (PM-011) + docs honesty (C10)
# ---------------------------------------------------------------------


def test_pm011_drift_guard_still_passes() -> None:
    """The panel MEMORY_TYPE_ENUM equals the backend MemoryType enum."""
    from usr.plugins.neuro_core.helpers.metadata import MemoryType

    panel = (
        PLUGIN_ROOT
        / "webui"
        / "right-canvas-panels"
        / "graph-panel.html"
    ).read_text(encoding="utf-8")
    import re

    m = re.search(r"MEMORY_TYPE_ENUM:\s*(\[[^\]]*\])", panel)
    assert m, "MEMORY_TYPE_ENUM constant must exist in graph-panel.html"
    panel_values = re.findall(r'["\']([a-z]+)["\']', m.group(1))
    backend_values = [t.value for t in MemoryType]
    assert sorted(panel_values) == sorted(backend_values)


def test_docs_honesty_rules_based_not_semantic() -> None:
    """C10: docs describe classification as heuristic rules-based, not
    semantic understanding, and claim no performance/concurrency/security
    properties."""
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    data_model = (PLUGIN_ROOT / "docs" / "data-model.md").read_text(
        encoding="utf-8"
    )
    for src in (readme, data_model):
        assert "rules-based" in src
        assert "semantic understanding" in src
    for banned in (
        "semantically understands",
        "guarantees correct classification",
        "thread-safe classification",
        "secure classification",
    ):
        assert banned not in readme + data_model, banned
