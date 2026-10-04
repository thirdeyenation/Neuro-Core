'''Tests for WI-P53-KI030-INSPECTOR-EDIT — safe-mode editing of memory
Contents and scores (Importance/Confidence/Stability) in the graph-panel
Details plate, backed by the new api/memory_edit.py handler (KI-030,
contents/scores component only).

Pinned groups:
  (a) api/memory_edit.py handler — GET returns authoritative content plus
      sidecar scores (null when no sidecar entry exists); POST applies
      content via the STANDARD metadata path (Memory.get_by_subdir →
      db.aget_by_ids → mutate page_content → Memory.update_documents) and
      scores via ScoreStore.set() into the EXISTING scores.json sidecar
      ONLY (KI-009/WI-P12 single-write discipline — no FAISS metadata
      score write ever). Score validation REJECTS out-of-range/wrong-type
      values BEFORE ScoreStore.set (which would silently clamp); booleans
      rejected; unknown score keys rejected; nothing-to-update rejected;
      Memory ID immutable; auth required. Memory is faked per the
      test_wip52/test_execute.py FakeMemory convention; ScoreStore paths
      are patched to tmp_path.
  (b) graph-panel.html pins — safe-mode structure: read-only display
      preserved by default (WI-P24/WI-P52 pins intact), explicit Edit
      button, edit form hidden until Edit (x-show=editOpen), Save requires
      an explicit Confirm step, Cancel discards, no ScoreStore reference
      in the panel, no new material-symbols ligature spans, no
      validation/dispute-status or memory_type editing surface (KI-034 /
      KI-029 deferred), Memory ID not editable.
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


# ---------------------------------------------------------------------
# Fakes (test_wip52_ki031_memnames.py conventions)
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


# ---------------------------------------------------------------------
# (a) handler tests
# ---------------------------------------------------------------------


def test_api_requires_auth_and_methods() -> None:
    assert me_mod.MemoryEditApi.requires_auth() is True
    assert me_mod.MemoryEditApi.get_methods() == ["GET", "POST"]


@pytest.mark.asyncio
async def test_get_returns_content_and_sidecar_scores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "get")
    doc = FakeDoc("mem-1", {"id": "mem-1"})
    _install_fake_memory(monkeypatch, doc)
    h = _handler()

    # No sidecar entry yet: scores null, source 'none'.
    out = await h.process(
        {}, _req(args={"memory_subdir": "default", "id": "mem-1"})
    )
    assert out == {
        "success": True,
        "memory_subdir": "default",
        "memory_id": "mem-1",
        "content": "content of mem-1",
        "scores": None,
        "scores_source": "none",
    }

    # After a sidecar write, GET returns the authoritative sidecar values.
    scores_mod.ScoreStore("default").set(
        "mem-1", importance=0.7, confidence=0.6, stability=0.5
    )
    out2 = await h.process(
        {}, _req(args={"memory_subdir": "default", "id": "mem-1"})
    )
    assert out2["success"] is True
    assert out2["scores"] == {
        "importance": 0.7,
        "confidence": 0.6,
        "stability": 0.5,
    }
    assert out2["scores_source"] == "sidecar"


@pytest.mark.asyncio
async def test_get_missing_params_and_unknown_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = FakeDoc("mem-1", {"id": "mem-1"})
    _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process({}, _req(args={"id": "mem-1"}))
    assert out == {"success": False, "error": "`memory_subdir` is required"}
    out2 = await h.process({}, _req(args={"memory_subdir": "default"}))
    assert out2 == {"success": False, "error": "`id` is required"}
    out3 = await h.process(
        {}, _req(args={"memory_subdir": "default", "id": "ghost"})
    )
    assert out3 == {
        "success": False,
        "error": "memory id not found: ghost",
    }


@pytest.mark.asyncio
async def test_post_content_edit_standard_path_id_immutable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "content")
    doc = FakeDoc("mem-1", {"id": "mem-1"})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()

    out = await h.process(
        {"memory_subdir": "default", "id": "mem-1", "content": "New text"},
        _req("POST"),
    )
    assert out["success"] is True
    assert out["content_changed"] is True
    assert out["scores_changed"] is False
    assert fake.update_calls == 1
    assert doc.page_content == "New text"
    # Memory ID immutable: metadata untouched by a content edit.
    assert doc.metadata == {"id": "mem-1"}
    # No sidecar file was created by a content-only edit.
    assert not list(tmp_path.glob("scores_default_content.json")) or (
        scores_mod.ScoreStore("default").get_optional("mem-1") is None
    )


@pytest.mark.asyncio
async def test_post_scores_sidecar_only_never_faiss_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "scores")
    doc = FakeDoc("mem-1", {"id": "mem-1"})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()

    out = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            "scores": {"importance": 0.9, "confidence": 0.4},
        },
        _req("POST"),
    )
    assert out["success"] is True
    assert out["scores_changed"] is True
    assert out["content_changed"] is False
    # KI-009/WI-P12 single-write discipline: sidecar written, FAISS
    # metadata NEVER touched (update_documents not called).
    assert fake.update_calls == 0
    rec = scores_mod.ScoreStore("default").get_optional("mem-1")
    assert rec is not None and rec.importance == 0.9 and rec.confidence == 0.4
    # Stability untouched when not provided (no silent zeroing).
    assert rec.stability == 0.5  # MemoryScores default


@pytest.mark.asyncio
async def test_post_combined_content_and_scores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "both")
    doc = FakeDoc("mem-1", {"id": "mem-1"})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            "content": "Both",
            "scores": {"stability": 0.3},
        },
        _req("POST"),
    )
    assert out["success"] is True
    assert out["content_changed"] and out["scores_changed"]
    assert fake.update_calls == 1
    assert doc.page_content == "Both"
    rec = scores_mod.ScoreStore("default").get_optional("mem-1")
    assert rec is not None and rec.stability == 0.3


@pytest.mark.asyncio
async def test_post_score_validation_rejects_before_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "postval")
    doc = FakeDoc("mem-1", {"id": "mem-1"})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()

    # Out-of-range must be REJECTED, not silently clamped by ScoreStore.
    for bad in (1.5, -0.1, 2):
        out = await h.process(
            {
                "memory_subdir": "default",
                "id": "mem-1",
                "scores": {"importance": bad},
            },
            _req("POST"),
        )
        assert out == {
            "success": False,
            "error": "`importance` must be a number between 0.0 and 1.0",
        }
    # Wrong type (string, bool, None, list) rejected with the same string.
    for bad in ("0.5", True, None, [0.5]):
        out = await h.process(
            {
                "memory_subdir": "default",
                "id": "mem-1",
                "scores": {"confidence": bad},
            },
            _req("POST"),
        )
        assert out == {
            "success": False,
            "error": "`confidence` must be a number between 0.0 and 1.0",
        }
    # Boundary values 0.0 and 1.0 are valid.
    ok = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            "scores": {"importance": 0.0, "stability": 1.0},
        },
        _req("POST"),
    )
    assert ok["success"] is True
    # Nothing was written on any rejection (only the boundary write above).
    rec = scores_mod.ScoreStore("default").get_optional("mem-1")
    assert rec is not None and rec.importance == 0.0 and rec.stability == 1.0
    assert rec.confidence == scores_mod.MemoryScores().confidence  # default untouched
    assert fake.update_calls == 0


@pytest.mark.asyncio
async def test_post_scores_shape_errors() -> None:
    h = _handler()
    err = (
        "`scores` must be an object with optional keys "
        "`importance`, `confidence`, `stability`"
    )
    for raw in ("nope", {}, {"weight": 0.5}):
        out = await h.process(
            {"memory_subdir": "default", "id": "mem-1", "scores": raw},
            _req("POST"),
        )
        assert out == {"success": False, "error": err}


@pytest.mark.asyncio
async def test_post_content_validation_and_nothing_to_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = FakeDoc("mem-1", {"id": "mem-1"})
    _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {"memory_subdir": "default", "id": "mem-1", "content": 42},
        _req("POST"),
    )
    assert out == {"success": False, "error": "`content` must be a string"}
    out2 = await h.process(
        {"memory_subdir": "default", "id": "mem-1", "content": "   "},
        _req("POST"),
    )
    assert out2 == {"success": False, "error": "`content` must not be empty"}
    out3 = await h.process(
        {"memory_subdir": "default", "id": "mem-1"}, _req("POST")
    )
    assert out3 == {
        "success": False,
        "error": "nothing to update: provide `content` and/or `scores`",
    }


@pytest.mark.asyncio
async def test_post_unknown_id_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "ghost")
    _install_fake_memory(monkeypatch, None)
    h = _handler()
    out = await h.process(
        {
            "memory_subdir": "default",
            "id": "ghost",
            "content": "X",
            "scores": {"importance": 0.5},
        },
        _req("POST"),
    )
    assert out == {"success": False, "error": "memory id not found: ghost"}
    assert scores_mod.ScoreStore("default").get_optional("ghost") is None


# ---------------------------------------------------------------------
# (b) panel pins — safe-mode structure
# ---------------------------------------------------------------------


def _panel_src() -> str:
    return PANEL.read_text(encoding="utf-8")


def _xdata_js() -> str:
    src = _panel_src()
    m = re.search(r'x-data="\{([\s\S]*)\}" x-init=', src)
    assert m, "x-data block not found"
    return m.group(1)


def test_panel_edit_state_declared() -> None:
    js = _xdata_js()
    assert "editOpen: false" in js
    assert "editConfirm: false" in js
    assert "editSaving: false" in js
    assert "editError: null" in js
    assert "editForm: { content: '', importance: '', confidence: '', stability: '' }" in js


def test_panel_edit_is_explicit_not_automatic() -> None:
    js = _xdata_js()
    # openEdit guards: no node or already open -> no-op (read-only default).
    assert "openEdit() { if (!this.inspectNode || this.editOpen) return;" in js
    # The only opener is the explicit Edit button in the details plate.
    src = _panel_src()
    assert '@click="openEdit()"' in src


def test_panel_edit_form_hidden_until_edit() -> None:
    src = _panel_src()
    # The edit form is x-show gated on editOpen (hidden by default).
    assert re.search(r'x-show="editOpen"', src)


def test_panel_save_requires_confirm_and_cancel_discards() -> None:
    js = _xdata_js()
    # Two-step Save: first click only arms the confirm flag.
    assert "if (!this.editConfirm) { this.editConfirm = true; return; }" in js
    # Cancel resets all edit state without any write (no fetch in cancelEdit).
    m = re.search(r"cancelEdit\(\) \{([\s\S]*?)\},", js)
    assert m, "cancelEdit not found"
    body = m.group(1)
    assert "fetch" not in body
    assert "this.editOpen = false" in body and "this.editConfirm = false" in body


def test_panel_edit_prefill_uses_authoritative_api() -> None:
    js = _xdata_js()
    # Prefill fetches the memory_edit GET endpoint (sidecar-authoritative).
    assert "fetch('/api/plugins/neuro_core/memory_edit?' + params.toString()" in js
    # Sidecar scores win over the stale metadata mirror.
    assert "sc && sc.importance != null ? Number(sc.importance)" in js


def test_panel_no_scorestore_reference() -> None:
    src = _panel_src()
    assert "ScoreStore" not in src, "panel must not reference ScoreStore directly"


def test_panel_no_validation_or_memory_type_editing() -> None:
    """KI-034 (validation/dispute) and KI-029 (memory_type) are deferred;
    the edit form must not expose them. Memory ID is immutable."""
    src = _panel_src()
    # The edit form exposes EXACTLY these four editable fields — no
    # validation/dispute-status (KI-034), no memory_type (KI-029), no id.
    keys = set(re.findall(r'x-model="editForm\.(\w+)"', src))
    assert keys == {"content", "importance", "confidence", "stability"}
    # Memory ID row remains display-only (no x-model bound to it).
    id_row = re.search(
        r'<div class="nc-details__id">([\s\S]*?)Memory ID</span>([\s\S]*?)</div>', src
    )
    assert id_row and "x-model" not in id_row.group(0)


def test_panel_preserves_wip24_read_only_pins() -> None:
    """The read-only display surface is preserved: content paragraph and
    score rows still render. WI-P58-KI041: score rows are now sidecar-first
    (scVal/scScore with metaVal fallback) and Stability renders an explicit
    'not stored' label when the sidecar carries no value — display remains
    read-only w.r.t. FAISS metadata (KI-009)."""
    src = _panel_src()
    assert '<p class="nc-details__content" x-text="inspectNode.content' in src
    assert re.search(r"scVal\(inspectNode,\s*'confidence'\)", src)
    assert re.search(r"scVal\(inspectNode,\s*'importance'\)", src)
    m = re.search(r"Stability[\s\S]{0,600}?not stored", src, re.I)
    assert m, "stability must still render an explicit 'not stored' label"


def test_panel_no_new_ligature_spans() -> None:
    """No new material-symbols ligature spans vs git HEAD (AGENTS.md rule);
    the edit UI is text-based like the WI-P52 name editor."""
    import subprocess

    head = subprocess.run(
        [
            "git",
            "-C",
            str(PLUGIN_ROOT),
            "show",
            "HEAD:webui/right-canvas-panels/graph-panel.html",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    before = head.count("material-symbols-outlined")
    after = _panel_src().count("material-symbols-outlined")
    assert after == before, "no new material-symbols spans allowed"


def test_panel_client_range_mirror_and_server_authoritative() -> None:
    js = _xdata_js()
    # Client-side range mirror (0.0-1.0) before submit...
    assert "n < 0 || n > 1" in js
    # ...and the POST goes to the server-authoritative memory_edit endpoint.
    assert "fetch('/api/plugins/neuro_core/memory_edit', { method: 'POST'" in js
