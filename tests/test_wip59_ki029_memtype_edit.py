'''Tests for WI-P59-KI029-MEMTYPE-EDIT — user-editable memory_type in the
graph-panel inspector (KI-029), per ADR-NC1-004:

  - helpers/metadata.py normalize_memory_types is the SINGLE normalization
    authority (C1): lenient reads derive the set WITHOUT mutation; strict
    mode validates a full-set-replace payload (loud ValueError, no partial
    write).
  - api/memory_edit.py: GET exposes the normalized types block; POST
    accepts a full-set-replace `types` payload; Memory ID immutable; no
    sidecar write (KI-009 untouched); re-reads current state immediately
    before mutation (C5 last-writer-wins posture).
  - tools/memory_score.py stays behaviorally unchanged (C3): when its
    scalar _FAISS_FIELDS rewrite makes the collection inconsistent, reads
    TOLERATE and flag `inconsistent` (C2) without mutating anything; the
    next handler edit repairs the invariant via full-set-replace.
  - graph-panel.html: safe-mode two-step type editing via a separate
    typeDraft state (editForm score/content pin unchanged); Add-Type
    affordance; removal limited to non-primary types; type-unknown
    fallback preserved; custom chips visually distinct (dashed).
'''

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from usr.plugins.neuro_core.api import memory_edit as me_mod
from usr.plugins.neuro_core.helpers import metadata as md_mod
from usr.plugins.neuro_core.helpers import scores as scores_mod

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"
ENUM_VALUES = [
    "fact",
    "concept",
    "task",
    "event",
    "decision",
    "skill",
    "preference",
    "note",
]


# ---------------------------------------------------------------------
# Fakes (test_wip52/test_wip53 conventions)
# ---------------------------------------------------------------------


class FakeDoc:
    def __init__(self, doc_id: str, metadata: dict):
        self.page_content = f"content of {doc_id}"
        self.metadata = metadata


class FakeDB:
    def __init__(self, docs: dict[str, FakeDoc]):
        self.docs = docs

    async def aget_by_ids(self, ids: list[str]) -> list[FakeDoc]:
        return [self.docs[i] for i in ids if i in self.docs]


class FakeMemory:
    instances: dict[str, "FakeMemory"] = {}

    def __init__(self, db: FakeDB):
        self.db = db
        self.update_calls = 0
        self.updated_docs: list = []

    @classmethod
    async def get_by_subdir(cls, subdir: str) -> "FakeMemory":
        return cls.instances[subdir]

    async def update_documents(self, docs: list) -> None:
        self.update_calls += 1
        self.updated_docs.extend(docs)


def _install_fake_memory(
    monkeypatch: pytest.MonkeyPatch, doc: FakeDoc | None
) -> FakeMemory:
    fake = FakeMemory(FakeDB({doc.metadata["id"]: doc} if doc else {}))
    FakeMemory.instances = {"default": fake}
    monkeypatch.setattr(me_mod, "Memory", FakeMemory)
    return fake


def _patch_scores_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tag: str
) -> None:
    monkeypatch.setattr(
        scores_mod,
        "_scores_path",
        lambda subdir: str(tmp_path / f"scores_{subdir}_{tag}.json"),
    )


def _handler() -> Any:
    return me_mod.MemoryEditApi(app=None, thread_lock=None)


def _req(method: str = "GET", args: dict | None = None) -> Any:
    return type("R", (), {"method": method, "args": (args or {})})()


def _panel_src() -> str:
    return PANEL.read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# (a) C1 — normalization authority
# ---------------------------------------------------------------------


def test_enum_values_unchanged() -> None:
    """The 8 enum values' membership and meaning are untouched (C3/binding)."""
    assert sorted(md_mod.VALID_MEMORY_TYPES) == sorted(ENUM_VALUES)


def test_lenient_read_derives_scalar_only_without_mutation() -> None:
    meta = {"id": "m1", "memory_type": "fact"}
    before = dict(meta)
    norm = md_mod.normalize_memory_types(meta)
    assert norm.primary == "fact"
    assert norm.types == ["fact"]
    assert norm.additional == []
    assert norm.inconsistent is False
    assert meta == before, "lenient read must not mutate metadata"


def test_lenient_read_type_unknown() -> None:
    norm = md_mod.normalize_memory_types({"id": "m2"})
    assert norm.primary is None
    assert norm.types == []
    norm2 = md_mod.normalize_memory_types({"memory_type": 42})
    assert norm2.primary is None and norm2.types == []


def test_lenient_read_collection_with_primary() -> None:
    meta = {"memory_type": "fact", "memory_types": ["fact", "hypothesis"]}
    norm = md_mod.normalize_memory_types(meta)
    assert norm.primary == "fact"
    assert norm.types == ["fact", "hypothesis"]
    assert norm.additional == ["hypothesis"]
    assert norm.inconsistent is False


def test_lenient_read_c2_inconsistent_flagged_no_mutation() -> None:
    """C2: collection exists but scalar was rewritten outside it (e.g. by
    tools/memory_score.py scalar _FAISS_FIELDS write). Read TOLERATES:
    flags inconsistent, surfaces both, mutates nothing."""
    meta = {"memory_type": "note", "memory_types": ["fact", "hypothesis"]}
    before = dict(meta)
    norm = md_mod.normalize_memory_types(meta)
    assert norm.inconsistent is True
    assert norm.primary == "note"
    assert "note" in norm.types and "fact" in norm.types
    assert meta == before


# ---------------------------------------------------------------------
# (b) strict payload validation — loud rejection battery
# ---------------------------------------------------------------------


def test_strict_accepts_valid_payload() -> None:
    norm = md_mod.normalize_memory_types(
        {"primary": "fact", "additional": ["hypothesis", "running-hyp_1"]},
        strict=True,
    )
    assert norm.primary == "fact"
    assert norm.types == ["fact", "hypothesis", "running-hyp_1"]
    assert norm.inconsistent is False


@pytest.mark.parametrize(
    "payload,fragment",
    [
        ({}, "primary"),
        ({"primary": "hypothesis"}, "primary"),  # custom token as primary
        ({"primary": "FACT"}, "primary"),  # case-sensitive enum lock
        ({"primary": "fact", "additional": ["Bad Token"]}, "additional"),
        ({"primary": "fact", "additional": ["-bad"]}, "additional"),
        ({"primary": "fact", "additional": ["a" * 41]}, "additional"),
        ({"primary": "fact", "additional": ["fact"]}, "duplicates the primary"),
        ({"primary": "fact", "additional": ["note"]}, "collides"),
        ({"primary": "fact", "additional": ["x", "x"]}, "duplicate"),
        ({"primary": "fact", "additional": ["fact"] * 8}, ""),
    ],
)
def test_strict_rejection_battery(payload: dict, fragment: str) -> None:
    # >7 cap case: build payload of 8 distinct non-enum tokens.
    if not fragment:
        payload = {
            "primary": "fact",
            "additional": [f"t{i:02d}" for i in range(8)],
        }
        with pytest.raises(ValueError, match="at most"):
            md_mod.normalize_memory_types(payload, strict=True)
        return
    with pytest.raises(ValueError, match=fragment):
        md_mod.normalize_memory_types(payload, strict=True)


def test_strict_cap_boundary_7_additional_ok() -> None:
    payload = {
        "primary": "fact",
        "additional": [f"t{i:02d}" for i in range(7)],
    }
    norm = md_mod.normalize_memory_types(payload, strict=True)
    assert len(norm.additional) == 7


# ---------------------------------------------------------------------
# (c) handler GET — normalized types block (C1 read authority)
# ---------------------------------------------------------------------


def test_api_requires_auth_and_methods() -> None:
    assert me_mod.MemoryEditApi.requires_auth() is True
    assert me_mod.MemoryEditApi.get_methods() == ["GET", "POST"]


@pytest.mark.asyncio
async def test_get_types_scalar_only_legacy_derivation_no_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "types-legacy")
    doc = FakeDoc("mem-1", {"id": "mem-1", "memory_type": "fact"})
    _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {}, _req(args={"memory_subdir": "default", "id": "mem-1"})
    )
    assert out["success"] is True
    assert out["types"]["memory_type"] == "fact"
    assert out["types"]["memory_types"] == ["fact"]
    assert out["types"]["additional"] == []
    assert out["types"]["inconsistent"] is False
    # C1: read must not mutate persisted metadata (no FAISS rewrite).
    assert doc.metadata["memory_type"] == "fact"
    assert "memory_types" not in doc.metadata


@pytest.mark.asyncio
async def test_get_types_inconsistent_surfaces_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "types-inc")
    doc = FakeDoc(
        "mem-1",
        {"id": "mem-1", "memory_type": "note", "memory_types": ["fact"]},
    )
    _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {}, _req(args={"memory_subdir": "default", "id": "mem-1"})
    )
    assert out["types"]["inconsistent"] is True
    assert out["types"]["memory_type"] == "note"
    assert "fact" in out["types"]["memory_types"]


# ---------------------------------------------------------------------
# (d) handler POST — full-set-replace, invariant, id immutable, no sidecar
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_types_full_set_replace_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "types-rt")
    doc = FakeDoc("mem-1", {"id": "mem-1", "memory_type": "note"})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            "types": {"primary": "fact", "additional": ["hypothesis"]},
        },
        _req("POST"),
    )
    assert out["success"] is True
    assert out["types_changed"] is True
    assert out["types"]["memory_types"] == ["fact", "hypothesis"]
    # Invariant written: primary ∈ collection, scalar synced to primary.
    assert doc.metadata["memory_type"] == "fact"
    assert doc.metadata["memory_types"] == ["fact", "hypothesis"]
    assert fake.update_calls == 1
    # Memory ID immutable.
    assert doc.metadata["id"] == "mem-1"
    assert doc.page_content == "content of mem-1"  # content untouched
    # KI-009: no sidecar write by a types-only edit.
    assert scores_mod.ScoreStore("default").get_optional("mem-1") is None


@pytest.mark.asyncio
async def test_post_types_repair_invariant_from_inconsistent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "types-repair")
    doc = FakeDoc(
        "mem-1",
        {"id": "mem-1", "memory_type": "note", "memory_types": ["fact"]},
    )
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            # Repair via a VALID full set: enum tokens cannot appear in
            # `additional` (enum-collision rejection), so the stray "fact"
            # from the inconsistent collection is replaced by the new set.
            "types": {"primary": "note", "additional": ["hypothesis"]},
        },
        _req("POST"),
    )
    assert out["success"] is True
    assert doc.metadata["memory_type"] == "note"
    assert doc.metadata["memory_types"] == ["note", "hypothesis"]
    assert fake.update_calls == 1
    # Re-read: invariant holds now.
    norm = md_mod.normalize_memory_types(doc.metadata)
    assert norm.inconsistent is False


@pytest.mark.asyncio
async def test_post_types_loud_rejection_no_partial_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "types-rej")
    doc = FakeDoc("mem-1", {"id": "mem-1", "memory_type": "fact"})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            "types": {"primary": "fact", "additional": ["Bad Token"]},
        },
        _req("POST"),
    )
    assert out["success"] is False
    assert "additional" in out["error"]
    # No partial write: metadata unchanged, no FAISS update issued.
    assert doc.metadata == {"id": "mem-1", "memory_type": "fact"}
    assert fake.update_calls == 0


@pytest.mark.asyncio
async def test_post_nothing_to_update_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "types-empty")
    doc = FakeDoc("mem-1", {"id": "mem-1"})
    _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {"memory_subdir": "default", "id": "mem-1"}, _req("POST")
    )
    assert out["success"] is False
    assert "nothing to update" in out["error"]


# ---------------------------------------------------------------------
# (e) C3 — tools/memory_score.py unchanged; C2 tolerance end-to-end
# ---------------------------------------------------------------------


def test_memory_score_module_unwired_to_types_collection() -> None:
    """C3: memory_score must not import or write the additive collection —
    its scalar _FAISS_FIELDS behavior is unchanged, which is exactly the
    source of the C2-tolerated inconsistent state."""
    src = (PLUGIN_ROOT / "tools" / "memory_score.py").read_text(encoding="utf-8")
    assert "memory_types" not in src
    assert "normalize_memory_types" not in src


@pytest.mark.asyncio
async def test_c2_end_to_end_scalar_rewrite_then_read_then_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pinned C2 sequence: (1) doc has invariant-conforming set; (2) a
    memory_score-style scalar rewrite makes it inconsistent; (3) reads
    tolerate + flag without mutation; (4) the next handler edit repairs
    the invariant via full-set-replace."""
    _patch_scores_store(monkeypatch, tmp_path, "c2-e2e")
    doc = FakeDoc(
        "mem-1",
        {
            "id": "mem-1",
            "memory_type": "task",
            "memory_types": ["task", "hypothesis"],
        },
    )
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()

    # Scalar rewrite (what tools/memory_score.py's _FAISS_FIELDS write does
    # to the scalar field, without touching the collection).
    doc.metadata["memory_type"] = "note"

    # (3) tolerant read flags inconsistency, mutates nothing.
    out = await h.process(
        {}, _req(args={"memory_subdir": "default", "id": "mem-1"})
    )
    assert out["types"]["inconsistent"] is True
    assert doc.metadata == {
        "id": "mem-1",
        "memory_type": "note",
        "memory_types": ["task", "hypothesis"],
    }

    # (4) handler edit repairs the invariant.
    out2 = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            "types": {"primary": "task", "additional": ["hypothesis"]},
        },
        _req("POST"),
    )
    assert out2["success"] is True
    norm = md_mod.normalize_memory_types(doc.metadata)
    assert norm.inconsistent is False
    assert fake.update_calls == 1


# ---------------------------------------------------------------------
# (f) UI pins — graph-panel.html
# ---------------------------------------------------------------------


def test_panel_type_draft_state_additive_to_editform_pin() -> None:
    """WI-P53/KI-042 pin preserved: editForm stays the four content/score
    fields; type editing lives in the separate typeDraft state."""
    src = _panel_src()
    keys = set(re.findall(r'x-model="editForm\.(\w+)"', src))
    assert keys == {"content", "importance", "confidence", "stability"}
    assert "typeDraft: { primary: '', additional: [] }" in src


def test_panel_primary_select_enum_locked() -> None:
    src = _panel_src()
    m = re.search(r'x-model="typeDraft.primary"', src)
    assert m, "primary select missing"
    for v in ENUM_VALUES:
        assert f"'{v}'" in src


def test_panel_add_type_affordance_and_removal() -> None:
    src = _panel_src()
    assert "toggleTypeAdd()" in src and "addType()" in src
    # Removal limited to non-primary: the method early-returns on primary.
    m = re.search(r"removeType\(t\) \{([^}]*)", src)
    assert m and "this.typeDraft.primary" in m.group(1)


def test_panel_custom_grammar_client_side() -> None:
    src = _panel_src()
    assert "[a-z0-9][a-z0-9_-]{0,39}" in src


def test_panel_type_unknown_fallback_preserved() -> None:
    src = _panel_src()
    assert "'type unknown'" in src


def test_panel_custom_chips_distinct_no_new_ligatures() -> None:
    src = _panel_src()
    assert "nc-details__chip--custom" in src
    # No new material-symbols ligature spans were added by this work item.
    import subprocess

    head = subprocess.run(
        ["git", "show", "HEAD:" + str(PANEL.relative_to(PLUGIN_ROOT))],
        cwd=PLUGIN_ROOT,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0:
        pytest.skip("git HEAD unavailable for ligature pin")
    lig = re.compile(r'class="material-symbols-outlined"[^>]*>([^<]+)<')
    assert len(lig.findall(src)) == len(lig.findall(head.stdout))


def test_panel_types_write_only_via_confirmed_save() -> None:
    """Safe-mode: the types payload is assembled inside saveEdits() (which
    runs only after the two-step Confirm), never in the add/remove handlers."""
    src = _panel_src()
    m = re.search(r"async saveEdits\(\) \{([\s\S]*?)relLabel\(rel\)", src)
    assert m, "saveEdits not found"
    assert "body.types = { primary" in m.group(1)
    # add/remove handlers must not fetch or POST.
    for fn in ("toggleTypeAdd() {", "addType() {", "removeType(t) {"):
        i = src.index(fn)
        chunk = src[i : i + 600]
        assert "fetch(" not in chunk


def test_panel_xdata_parses_under_node() -> None:
    import shutil

    js = _panel_src()
    start = js.index('x-data="') + len('x-data="')
    end = js.index('" x-init=', start)
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        pytest.skip("node runtime unavailable; parse check requires it")
    p = Path("/tmp/p59_test_xdata.js")
    p.write_text("(" + js[start:end] + ")", encoding="utf-8")
    r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    p.unlink(missing_ok=True)
