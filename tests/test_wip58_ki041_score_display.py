'''Tests for WI-P58-KI041-SCORE-DISPLAY — the inspector Details-plate score
display round-trip (KI-041).

Defect: scores saved through the safe-mode editor land in the scores.json
sidecar via ScoreStore.set() (KI-009 single-write discipline), but the
Details plate score rows read FAISS metadata only, so saved Importance /
Confidence displayed as n/a and Stability as 'unavailable'.

Pinned groups:
  (a) Round-trip: a safe-mode score edit (POST /memory_edit, Confirm save)
      followed by the authoritative GET /memory_edit read returns the saved
      sidecar values WITHOUT any search re-run — and the display path stays
      read-only w.r.t. FAISS metadata (KI-009: no FAISS metadata score
      writes ever; the GET uses ScoreStore.get_optional semantics, null
      when no sidecar entry, no fabricated defaults).
  (b) graph-panel.html pins — Details plate renders sidecar-first:
      inspectScores state + loadInspectScores() called at node tap and in
      refocusInspectNode (the post-save refresh path); Importance/
      Confidence prefer the sidecar value and fall back to node metadata
      per the WI-P53 GET contract; graceful n/a when neither carries a
      value; Stability is sidecar-only and explicitly 'not stored' when
      absent — never a fabricated or metadata-fallback value. No
      ScoreStore reference in the panel; no edit-surface expansion
      (no memory_type, no validation/dispute-status fields). Fakes follow
      the test_wip53 FakeMemory convention; ScoreStore paths are patched
      to tmp_path.
'''

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from usr.plugins.neuro_core.api import memory_edit as me_mod
from usr.plugins.neuro_core.helpers import scores as scores_mod

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"
# The maintained shell copy pinned by WI-P24 (SHELL constant convention).
SHELL = PLUGIN_ROOT / "extensions" / "webui" / "right-canvas-panels" / "graph-panel.html"


# ---------------------------------------------------------------------
# Fakes (test_wip53_ki030_inspector_edit.py conventions)
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


def _xdata_js() -> str:
    src = _panel_src()
    m = re.search(r'x-data="\{([\s\S]*)\}" x-init=', src)
    assert m, "x-data block not found"
    return m.group(1)


# ---------------------------------------------------------------------
# (a) Round-trip: safe-mode score edit -> authoritative sidecar read
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_edit_roundtrip_display_without_search_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The acceptance round-trip, pinned at the handler boundary: a
    safe-mode score edit (the Confirm save path) followed by the
    authoritative GET returns the saved values without any search re-run.
    The FAISS document metadata must remain untouched (KI-009)."""
    _patch_scores_store(monkeypatch, tmp_path, "rt")
    doc = FakeDoc("mem-1", {"id": "mem-1", "importance": 0.2})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()

    # Node metadata starts stale w.r.t. what the user is about to save.
    before_get = await h.process(
        {}, _req(args={"memory_subdir": "default", "id": "mem-1"})
    )
    assert before_get["scores_source"] == "none"
    assert before_get["scores"] is None

    # Safe-mode Confirm save: second click performs the POST with scores.
    post = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-1",
            "scores": {"importance": 0.9, "confidence": 0.8, "stability": 0.5},
        },
        _req("POST"),
    )
    assert post["success"] is True
    assert post["scores_changed"] is True

    # No search re-run: the display path reads the authoritative GET.
    out = await h.process(
        {}, _req(args={"memory_subdir": "default", "id": "mem-1"})
    )
    assert out["success"] is True
    assert out["scores_source"] == "sidecar"
    assert out["scores"] == {
        "importance": 0.9,
        "confidence": 0.8,
        "stability": 0.5,
    }

    # KI-009: the FAISS document metadata was never written by the score edit.
    assert fake.update_calls == 0
    assert doc.metadata["importance"] == 0.2


@pytest.mark.asyncio
async def test_get_preserves_missing_score_absence_no_fabricated_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ScoreStore.get_optional semantics via the GET: a memory with no
    sidecar entry yields scores None / source 'none' — the display layer
    receives absence, not fabricated defaults."""
    _patch_scores_store(monkeypatch, tmp_path, "abs")
    doc = FakeDoc("mem-2", {"id": "mem-2"})
    _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {}, _req(args={"memory_subdir": "default", "id": "mem-2"})
    )
    assert out["scores"] is None
    assert out["scores_source"] == "none"


@pytest.mark.asyncio
async def test_stability_only_sidecar_edit_carries_full_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ScoreStore.set creates the full record with store defaults for the
    fields not edited (WI-P53 precedent pin: MemoryScores defaults), so a
    stability-only Confirm save yields a full sidecar-backed record whose
    GET carries the saved stability (0.3) plus the store defaults. The
    display layer consequently shows a sidecar-backed Stability value and
    sidecar-backed Importance/Confidence after any score-field save."""
    _patch_scores_store(monkeypatch, tmp_path, "stab")
    doc = FakeDoc("mem-3", {"id": "mem-3"})
    _install_fake_memory(monkeypatch, doc)
    h = _handler()
    post = await h.process(
        {
            "memory_subdir": "default",
            "id": "mem-3",
            "scores": {"stability": 0.3},
        },
        _req("POST"),
    )
    assert post["success"] is True
    out = await h.process(
        {}, _req(args={"memory_subdir": "default", "id": "mem-3"})
    )
    assert out["scores_source"] == "sidecar"
    assert out["scores"] == {
        "importance": 0.5,
        "confidence": 0.7,
        "stability": 0.3,
    }


# ---------------------------------------------------------------------
# (b) graph-panel.html display-path pins
# ---------------------------------------------------------------------


def test_panel_declares_sidecar_display_state() -> None:
    js = _xdata_js()
    assert "inspectScores: null" in js
    assert "inspectScoresSource: 'none'" in js


def test_panel_loads_scores_at_node_tap() -> None:
    src = _panel_src()
    nh = re.search(r"cy\.on\('tap', 'node'([\s\S]*?)\}\);", src)
    assert nh, "node tap handler not found"
    body = nh.group(1)
    # Fresh plate resets stale sidecar state, then loads authoritative
    # scores. The tap callback closes over `self` (panel convention).
    assert "self.inspectScores = null" in body
    assert "self.inspectScoresSource = 'none'" in body
    assert "self.loadInspectScores()" in body


def test_panel_reload_scores_after_save_without_search_rerun() -> None:
    src = _panel_src()
    # refocusInspectNode is a single-line entry in the x-data; anchor from
    # its start to the next named helper (relLabel).
    m = re.search(r"async refocusInspectNode\(\) \{([\s\S]*?)relLabel\(rel\)", src)
    assert m, "refocusInspectNode not found"
    body = m.group(1)
    assert "this.loadInspectScores()" in body, (
        "post-mutation refocus must re-read the authoritative sidecar scores "
        "so the Details plate shows saved values without a search re-run"
    )


def test_panel_score_rows_sidecar_first_with_metadata_fallback() -> None:
    src = _panel_src()
    for field in ("importance", "confidence"):
        assert re.search(
            rf"scScore\(inspectNode,\s*'{field}'\)", src
        ), f"{field} bar must be sidecar-first via scScore"
        assert re.search(
            rf"scVal\(inspectNode,\s*'{field}'\) !== null", src
        ), f"{field} value must be sidecar-first via scVal with graceful n/a"
    # WI-P53 GET contract: sidecar wins, displayed metadata values are the
    # fallback; the metadata fallback helper must remain present.
    js = _xdata_js()
    assert "sc && sc[k] != null" in js, "scVal must prefer the sidecar record"
    assert "metaVal(n, k)" in js, "metadata fallback helper must remain"


def test_panel_stability_sidecar_only_or_not_stored() -> None:
    src = _panel_src()
    # Anchor on the Details-plate stability row via its label span class
    # (a filters 'Stability' label exists elsewhere in the panel).
    m = re.search(
        r'<span class="nc-details__score-label">Stability</span>([\s\S]{0,600}?)</div>',
        src,
    )
    assert m, "stability row not found"
    row = m.group(1)
    assert "scVal(inspectNode, 'stability') !== null" in row, (
        "stability must show the sidecar-backed value when present"
    )
    assert ">not stored<" in row, (
        "stability must be explicitly labeled 'not stored' when absent"
    )
    assert "unavailable" not in row, "legacy 'unavailable' label must be gone"


def test_panel_loader_applies_absence_as_none_not_defaults() -> None:
    src = _panel_src()
    # loadInspectScores is a single-line entry; anchor to the next named
    # helper (relTypeColor).
    m = re.search(r"async loadInspectScores\(\) \{([\s\S]*?)relTypeColor\(rt\)", src)
    assert m, "loadInspectScores not found"
    body = m.group(1)
    # Absence ('none') is applied as null — no fabricated defaults.
    assert "data.scores_source === 'sidecar'" in body
    assert "data.scores" in body
    assert "self.inspectScores = null" in body
    # Stale-response guard: only apply scores to the still-inspected node.
    assert "self.inspectNode.id === id" in body


def test_panel_display_path_no_scorestore_and_no_post() -> None:
    src = _panel_src()
    assert "ScoreStore" not in src, "panel must not reference ScoreStore directly"
    # KI-009: no FAISS metadata write anywhere in the display path — the
    # only POST to memory_edit is the pre-existing safe-mode save in
    # saveEdits(); the new loader must be GET-only.
    m = re.search(r"async loadInspectScores\(\) \{([\s\S]*?)relTypeColor\(rt\)", src)
    assert m, "loadInspectScores not found"
    assert "method: 'POST'" not in m.group(1), (
        "display path must not POST — read-only w.r.t. FAISS metadata"
    )


def test_panel_no_edit_surface_expansion() -> None:
    """Exclusions: no memory_type editing (WI-P59), no validation/dispute
    status editing (WI-P60). The editable-field set is unchanged."""
    src = _panel_src()
    keys = set(re.findall(r'x-model="editForm\.(\w+)"', src))
    assert keys == {"content", "importance", "confidence", "stability"}


def test_panel_xdata_parses_under_node() -> None:
    import shutil

    js = _xdata_js()
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        pytest.skip("node runtime unavailable; parse check requires it")
    p = Path("/tmp/p58_test_xdata.js")
    p.write_text("({" + js + "})", encoding="utf-8")
    r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    p.unlink(missing_ok=True)


def test_shell_copy_remains_byte_identical_to_git_head() -> None:
    assert SHELL.exists(), SHELL
    r = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", str(SHELL.relative_to(PLUGIN_ROOT))],
        cwd=PLUGIN_ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, "extensions/ shell copy must remain byte-identical"
