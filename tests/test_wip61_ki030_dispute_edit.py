'''Tests for WI-P61-KI030-DISPUTE-EDIT — user-editable validation_status
in the graph-panel inspector, per the ARC-approved design (C1-C10):

  - api/memory_edit.py: USER_ALLOWED_TRANSITIONS is enforced server-side
    at the SINGLE validation point in _apply_edit (C1); loud rejection,
    no partial write, no sidecar write for status (KI-009 untouched).
  - ``deprecated`` is terminal (C2): every transition from it is
    rejected; unknown stored values are surfaced via GET with a flag and
    rejected on edit — never silently mapped or invented.
  - NO suppression mechanism (C3): a user-cleared disputed memory may be
    re-disputed by the next sweep pass; the UI helper text states this.
  - The ->validated action is labeled a user attestation (C4).
  - Status writes go through the same governed FAISS metadata path as
    the types payload, with WI-P60 rev3 deep-copy staging (C5).
  - Each successful user transition writes exactly one activity_ledger
    event with kind ``validation_status_user_edit`` (C6); rejected
    transitions write no entry.
  - KI-045 (C7): the type dropdown pre-fills the actual current primary
    via a display-only option when it is outside the 8-value enum.
  - Safe-mode: the status edit participates in the two-step Save/Confirm
    flow (C10); Cancel discards the draft.
'''

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from usr.plugins.neuro_core.api import memory_edit as me_mod
from usr.plugins.neuro_core.helpers import scores as scores_mod

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"


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
        # Persistence fidelity: the staged copy REPLACES the stored doc
        # (matching real Memory.update_documents semantics), so sequential
        # transitions observe the previously persisted state.
        for d in docs:
            self.db.docs[d.metadata["id"]] = d


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


def _fresh_ledger(monkeypatch: pytest.MonkeyPatch) -> me_mod.ActivityLedger:
    ledger = me_mod.ActivityLedger()
    monkeypatch.setattr(me_mod, "ACTIVITY_LEDGER", ledger)
    return ledger


def _handler() -> Any:
    return me_mod.MemoryEditApi(app=None, thread_lock=None)


def _req(method: str = "GET", args: dict | None = None) -> Any:
    return type("R", (), {"method": method, "args": (args or {})})()


def _panel_src() -> str:
    return PANEL.read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# (a) GET — current status, unknown flag, allowed targets (C2)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "current", "unknown", "allowed"),
    [
        (None, "unvalidated", False, ["disputed", "validated"]),
        ("unvalidated", "unvalidated", False, ["disputed", "validated"]),
        ("validated", "validated", False, ["disputed", "unvalidated"]),
        ("disputed", "disputed", False, ["unvalidated", "validated"]),
        ("deprecated", "deprecated", False, []),
        ("bogus-value", "bogus-value", True, []),
    ],
)
async def test_get_status_block_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stored: str | None,
    current: str,
    unknown: bool,
    allowed: list[str],
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, f"get-{stored}")
    meta = {"id": "mem-1"}
    if stored is not None:
        meta["validation_status"] = stored
    doc = FakeDoc("mem-1", meta)
    _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {}, _req(args={"memory_subdir": "default", "id": "mem-1"})
    )
    assert out["success"] is True
    assert out["validation_status"] == {
        "current": current,
        "unknown": unknown,
        "allowed_targets": allowed,
    }
    # C2: a read never mutates persisted metadata.
    assert doc.metadata == meta


# ---------------------------------------------------------------------
# (b) POST — the full transition matrix (C1)
# ---------------------------------------------------------------------


_ALLOWED_EDGES = [
    ("unvalidated", "disputed"),
    ("unvalidated", "validated"),
    ("validated", "disputed"),
    ("validated", "unvalidated"),
    ("disputed", "unvalidated"),
    ("disputed", "validated"),
]

_REJECTED_EDGES = [
    # same-state (no-op transitions are rejected, not silently accepted)
    ("unvalidated", "unvalidated"),
    ("validated", "validated"),
    ("disputed", "disputed"),
    ("deprecated", "deprecated"),
    # deprecated is terminal (C2)
    ("deprecated", "unvalidated"),
    ("deprecated", "validated"),
    ("deprecated", "disputed"),
    # non-matrix edges
    ("unvalidated", "deprecated"),
    ("validated", "deprecated"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("frm", "to"), _ALLOWED_EDGES)
async def test_post_allowed_transition_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    frm: str,
    to: str,
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, f"ok-{frm}-{to}")
    ledger = _fresh_ledger(monkeypatch)
    doc = FakeDoc("mem-1", {"id": "mem-1", "validation_status": frm})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            "validation_status": to,
        },
        _req("POST"),
    )
    assert out["success"] is True, out
    assert out["validation_status_changed"] is True
    assert out["validation_status"] == to
    assert out["ledger_recorded"] is True
    # C5: the staged deep copy carries the write; the fetched doc is not
    # mutated in place.
    assert fake.update_calls == 1
    staged = fake.updated_docs[0]
    assert staged.metadata["validation_status"] == to
    assert staged.metadata["id"] == "mem-1"
    assert doc.metadata == {"id": "mem-1", "validation_status": frm}
    # C6: exactly one ledger event, distinguishing user edit from sweep.
    events = ledger.all()
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "validation_status_user_edit"
    assert ev.targets == ("mem-1",)
    assert ev.outcome == to
    assert ev.evidence["from"] == frm
    assert ev.evidence["to"] == to
    assert ev.evidence["writer"] == "user_edit"
    # KI-009: no sidecar write for a status-only edit.
    assert scores_mod.ScoreStore("default").get_optional("mem-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("frm", "to"), _REJECTED_EDGES)
async def test_post_rejected_transition_loud_no_write_no_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    frm: str,
    to: str,
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, f"rej-{frm}-{to}")
    ledger = _fresh_ledger(monkeypatch)
    doc = FakeDoc("mem-1", {"id": "mem-1", "validation_status": frm})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            "validation_status": to,
        },
        _req("POST"),
    )
    assert out["success"] is False
    assert "transition not permitted" in out["error"]
    assert frm in out["error"] and to in out["error"]
    # No partial write: no FAISS update, metadata unchanged.
    assert fake.update_calls == 0
    assert doc.metadata == {"id": "mem-1", "validation_status": frm}
    # C6: rejected transitions write no ledger entry.
    assert len(ledger.all()) == 0


@pytest.mark.asyncio
async def test_post_unknown_current_value_rejected_never_mapped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C2: an unknown STORED value is rejected on edit — never silently
    mapped onto a vocabulary value or invented into one."""
    _patch_scores_store(monkeypatch, tmp_path, "rej-unknown")
    _fresh_ledger(monkeypatch)
    doc = FakeDoc("mem-1", {"id": "mem-1", "validation_status": "weird"})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()
    for target in ("unvalidated", "validated", "disputed", "deprecated"):
        out = await h.process(
            {
                "memory_subdir": "default",
                "id": "mem-1",
                "validation_status": target,
            },
            _req("POST"),
        )
        assert out["success"] is False
        assert "transition not permitted" in out["error"]
    assert fake.update_calls == 0
    assert doc.metadata["validation_status"] == "weird"


@pytest.mark.asyncio
async def test_post_status_shape_and_vocabulary_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "rej-shape")
    _fresh_ledger(monkeypatch)
    doc = FakeDoc("mem-1", {"id": "mem-1"})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {"memory_subdir": "default", "id": "mem-1", "validation_status": 42},
        _req("POST"),
    )
    assert out == {
        "success": False,
        "error": "`validation_status` must be a string",
    }
    out2 = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            "validation_status": "approved",
        },
        _req("POST"),
    )
    assert out2["success"] is False
    assert "must be one of" in out2["error"]
    assert fake.update_calls == 0
    assert len(me_mod.ACTIVITY_LEDGER.all()) == 0


# ---------------------------------------------------------------------
# (c) combined writes, staging, and ledger isolation
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_combined_content_and_status_single_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "combo")
    _fresh_ledger(monkeypatch)
    doc = FakeDoc("mem-1", {"id": "mem-1", "validation_status": "unvalidated"})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            "content": "Revised",
            "validation_status": "validated",
        },
        _req("POST"),
    )
    assert out["success"] is True
    assert out["content_changed"] is True
    assert out["validation_status_changed"] is True
    # One governed FAISS metadata write for both components (C5).
    assert fake.update_calls == 1
    staged = fake.updated_docs[0]
    assert staged.page_content == "Revised"
    assert staged.metadata["validation_status"] == "validated"
    assert doc.page_content == "content of mem-1"
    assert doc.metadata["validation_status"] == "unvalidated"


@pytest.mark.asyncio
async def test_post_status_with_types_same_governed_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C5: status rides the SAME governed FAISS metadata path as the
    types payload — one staged write, no sidecar involvement."""
    _patch_scores_store(monkeypatch, tmp_path, "combo-types")
    _fresh_ledger(monkeypatch)
    doc = FakeDoc("mem-1", {"id": "mem-1", "memory_type": "note"})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            "types": {"primary": "fact", "additional": []},
            "validation_status": "disputed",
        },
        _req("POST"),
    )
    assert out["success"] is True
    assert out["types_changed"] is True
    assert out["validation_status_changed"] is True
    assert fake.update_calls == 1
    staged = fake.updated_docs[0]
    assert staged.metadata["memory_type"] == "fact"
    assert staged.metadata["memory_types"] == ["fact"]
    assert staged.metadata["validation_status"] == "disputed"
    assert scores_mod.ScoreStore("default").get_optional("mem-1") is None


@pytest.mark.asyncio
async def test_ledger_events_distinct_across_repeated_transitions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "ledger-multi")
    ledger = _fresh_ledger(monkeypatch)
    doc = FakeDoc("mem-1", {"id": "mem-1", "validation_status": "unvalidated"})
    _install_fake_memory(monkeypatch, doc)
    h = _handler()
    for target in ("disputed", "unvalidated", "validated"):
        out = await h.process(
            {
                "memory_subdir": "default",
                "id": "mem-1",
                "validation_status": target,
            },
            _req("POST"),
        )
        assert out["success"] is True, out
    events = ledger.all()
    assert len(events) == 3
    assert len({ev.event_id for ev in events}) == 3
    assert [ev.outcome for ev in events] == ["disputed", "unvalidated", "validated"]


# ---------------------------------------------------------------------
# (d) matrix constant shape
# ---------------------------------------------------------------------


def test_user_allowed_transitions_matrix_shape() -> None:
    assert set(me_mod.USER_ALLOWED_TRANSITIONS) == {
        "unvalidated",
        "validated",
        "disputed",
        "deprecated",
    }
    assert me_mod.USER_ALLOWED_TRANSITIONS["deprecated"] == ()
    for frm, allowed in me_mod.USER_ALLOWED_TRANSITIONS.items():
        for to in allowed:
            assert to in me_mod.USER_ALLOWED_TRANSITIONS
            assert frm != to


# ---------------------------------------------------------------------
# (e) UI pins — graph-panel.html (C3, C4, C7/KI-045, C10, KI-046)
# ---------------------------------------------------------------------


def test_panel_status_state_additive_to_editform_pin() -> None:
    """editForm stays the four content/score fields (WI-P53 pin); status
    editing lives in the separate statusDraft state (C10 safe-mode)."""
    src = _panel_src()
    keys = set(re.findall(r'x-model="editForm\.(\w+)"', src))
    assert keys == {"content", "importance", "confidence", "stability"}
    assert "statusDraft: ''" in src and "statusLoaded: null" in src
    assert 'x-model="statusDraft"' in src


def test_panel_status_select_server_scoped_and_attestation_label() -> None:
    """C1/C4: the select offers only the GET-returned allowed targets and
    labels ->validated as a user attestation."""
    src = _panel_src()
    assert "statusLoaded.allowed_targets" in src
    assert "attest: I have reviewed this memory" in src
    assert "keep current" in src


def test_panel_redispute_semantics_stated() -> None:
    """C3: NO suppression mechanism — the helper text states explicitly
    that a cleared dispute may be re-disputed by the next sweep pass."""
    src = _panel_src()
    assert "re-dispute" in src
    assert "not permanent" in src


def test_panel_deprecated_terminal_and_unknown_warning() -> None:
    src = _panel_src()
    assert "Deprecated is terminal" in src
    assert "unknown stored value" in src


def test_panel_unsaved_draft_notice() -> None:
    """KI-046c: an explicit unsaved-draft-changes notice exists."""
    src = _panel_src()
    assert "editDraftDirty" in src
    assert "Unsaved draft changes" in src


def test_panel_removal_mechanics_helper_text() -> None:
    """KI-046a: removal mechanics are stated — chips are clickable in the
    draft, the primary has no removal control."""
    src = _panel_src()
    assert "Removal is draft-only" in src
    assert "click a type chip" in src
    assert "no removal control" in src
    # The draft chips carry the click-to-remove affordance.
    m = re.search(
        r'x-for="t in typeDraft\.additional"[\s\S]{0,400}?@click="removeType\(t\)"',
        src,
    )
    assert m, "draft chips must carry the click-to-remove affordance"


def test_panel_primary_vs_custom_chip_distinction() -> None:
    """KI-046b: the primary type is designated in the details plate and
    custom chips carry a distinguishing tooltip."""
    src = _panel_src()
    assert "Primary type (scalar memory_type" in src
    assert "Custom (user-defined) type" in src
    assert "Implemented enum type" in src


def test_panel_ki045_extra_current_primary_option() -> None:
    """C7/KI-045: a display-only option carries the actual current primary
    when it is outside the 8-value enum; type-unknown nodes keep the
    blank-draft behavior (the extra option requires a known primary)."""
    src = _panel_src()
    assert "(current)" in src
    m = re.search(
        r"indexOf\(typeLoadedTypes\.primary\) === -1",
        src,
    )
    assert m, "extra option must be gated on primary being outside the enum"
    # The option is display-only: it does not alter the enum-locked select
    # options themselves (the 8 enum options remain the x-for source).
    assert src.count("x-for=") >= 1


def test_panel_status_edit_inside_confirmed_save_flow() -> None:
    """C10: the status payload is assembled inside saveEdits() (which runs
    only after the two-step Confirm), never in a draft handler."""
    src = _panel_src()
    m = re.search(r"async saveEdits\(\) \{([\s\S]*?)relLabel\(rel\)", src)
    assert m, "saveEdits body not found"
    assert "body.validation_status" in m.group(1)
    # Cancel discards the status draft.
    c = re.search(r"cancelEdit\(\) \{([\s\S]*?)\},", src)
    assert c and "this.statusDraft = ''" in c.group(1)


def test_panel_no_new_ligature_spans() -> None:
    """No new material-symbols ligature spans vs git HEAD (AGENTS.md
    rule); the status UI is text-based like the existing edit UI."""
    import subprocess

    src = _panel_src()
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
