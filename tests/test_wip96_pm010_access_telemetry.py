'''Tests for WI-P96-PM010-ACCESS-TELEMETRY — read-only inspector display of
the score sidecar's per-memory access_count / last_accessed_at, with honest
sidecar-degradation (neuro_degraded) fallback semantics.

Pinned groups:
  (a) API: GET /memory_edit serializes the already-read sidecar record's
      access_count / last_accessed_at as an additive top-level
      ``access_telemetry`` object (present only when a sidecar record
      exists; null otherwise — never fabricated). POST write validation
      (_SCORE_FIELDS) is untouched: telemetry fields are display-only and
      are NOT accepted as editable scores. The display path stays
      read-only (KI-009: no sidecar or FAISS writes from GET).
  (b) Panel: graph-panel.html declares the telemetry state, adopts it from
      the SAME authoritative GET read (sidecar-gated, no new fetch), and
      renders two read-only rows. Sidecar values win; 'not stored' when
      absent; when the sidecar entry is absent/unreadable (neuro_degraded
      semantics), last_accessed falls back to the node's metadata copy
      ONLY with an explicit best-effort fallback label — never presented
      as canonical sidecar data. access_count has no metadata source and
      is never fabricated. No edit-surface expansion; no ScoreStore or
      POST reference in the panel. Fakes follow the test_wip58 (itself
      test_wip53) conventions.
'''

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from usr.plugins.neuro_core.api import memory_edit as me_mod
from usr.plugins.neuro_core.helpers import scores as scores_mod

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"


# ---------------------------------------------------------------------
# Fakes (test_wip58_ki041_score_display.py conventions)
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

    @classmethod
    async def get_by_subdir(cls, subdir: str) -> "FakeMemory":
        return cls.instances[subdir]


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
# (a) API — GET payload carries additive read-only access_telemetry
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_serializes_access_telemetry_when_sidecar_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "tel")
    doc = FakeDoc("mem_tel", {"id": "mem_tel"})
    _install_fake_memory(monkeypatch, doc)
    store = scores_mod.ScoreStore("default")
    store.set(
        "mem_tel",
        importance=0.7,
        confidence=0.6,
        stability=0.5,
        access_count=3,
        last_accessed_at="2026-06-05T12:34:56+00:00",
    )
    res = await _handler().process(
        {}, _req("GET", {"memory_subdir": "default", "id": "mem_tel"})
    )
    assert res["success"] is True
    assert res["scores_source"] == "sidecar"
    tel = res["access_telemetry"]
    assert tel is not None
    assert tel["access_count"] == 3
    assert tel["last_accessed_at"] == "2026-06-05T12:34:56+00:00"


@pytest.mark.asyncio
async def test_get_access_telemetry_null_when_no_sidecar_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "notel")
    doc = FakeDoc("mem_notel", {"id": "mem_notel"})
    _install_fake_memory(monkeypatch, doc)
    res = await _handler().process(
        {}, _req("GET", {"memory_subdir": "default", "id": "mem_notel"})
    )
    assert res["success"] is True
    assert res["scores_source"] == "none"
    assert res["access_telemetry"] is None


@pytest.mark.asyncio
async def test_get_is_read_only_no_sidecar_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "ro")
    doc = FakeDoc("mem_ro", {"id": "mem_ro"})
    _install_fake_memory(monkeypatch, doc)
    await _handler().process(
        {}, _req("GET", {"memory_subdir": "default", "id": "mem_ro"})
    )
    # No sidecar file may be created by the GET display path.
    assert not list(tmp_path.glob("scores_*")), "GET must not write sidecars"


@pytest.mark.asyncio
async def test_post_rejects_telemetry_fields_as_scores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_scores_store(monkeypatch, tmp_path, "post")
    doc = FakeDoc("mem_post", {"id": "mem_post"})
    _install_fake_memory(monkeypatch, doc)
    res = await _handler().process(
        {
            "memory_subdir": "default",
            "id": "mem_post",
            "scores": {"access_count": 5},
        },
        _req("POST"),
    )
    assert res["success"] is False
    # The existing validator's message names only the allowed score keys —
    # telemetry fields are display-only and are never accepted as scores.
    assert "importance" in str(res.get("error", ""))


# ---------------------------------------------------------------------
# (b) Panel — state, sidecar-gated adoption, rows, degraded labeling
# ---------------------------------------------------------------------


def test_panel_declares_telemetry_state() -> None:
    js = _xdata_js()
    assert "inspectAccessTelemetry: null" in js


def test_panel_loader_adopts_telemetry_sidecar_gated() -> None:
    js = _xdata_js()
    # Adopted from the SAME GET read, only when scores_source === 'sidecar'.
    assert "data.access_telemetry" in js
    assert "scores_source === 'sidecar' && data.access_telemetry" in js
    # Cleared on failure — never fabricated.
    assert "self.inspectAccessTelemetry = null" in js


def test_panel_renders_readonly_telemetry_rows() -> None:
    src = _panel_src()
    assert "Access count" in src
    assert "Last accessed" in src
    assert "accessCountDisplay()" in src
    assert "lastAccessedDisplay()" in src
    # 'not stored' when absent — never a fabricated value.
    assert src.count("not stored") >= 3  # stability + the two new rows


def test_panel_degraded_fallback_is_labeled_best_effort() -> None:
    src = _panel_src()
    assert "lastAccessedIsFallback()" in src
    assert "best-effort fallback" in src
    assert "neuro_degraded" in src
    # The fallback hint must NOT be presented as canonical sidecar data.
    assert "not canonical sidecar data" in src


def test_panel_access_count_has_no_metadata_fallback() -> None:
    js = _xdata_js()
    m = re.search(r"accessCountDisplay\(\)\s*\{([^}]*)\}", js)
    assert m, "accessCountDisplay helper not found"
    assert "metaVal" not in m.group(1), (
        "access_count must never fall back to metadata — no honest source exists"
    )


def test_panel_no_edit_surface_expansion_for_telemetry() -> None:
    src = _panel_src()
    # Telemetry rows are display-only: no input/select bound to them.
    assert 'x-model="inspectAccessTelemetry' not in src
    assert "editForm.access_count" not in src
    assert "editForm.last_accessed" not in src


def test_panel_display_path_no_scorestore_and_no_post() -> None:
    src = _panel_src()
    assert "ScoreStore" not in src
    # The telemetry adoption lives in the GET loader — no POST may be
    # involved in fetching it (other endpoints may legitimately POST).
    tel_lines = [ln for ln in src.splitlines() if "data.access_telemetry" in ln]
    assert tel_lines, "telemetry adoption line not found"
    for ln in tel_lines:
        assert "POST" not in ln


def test_panel_xdata_parses_under_node() -> None:
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        pytest.skip("node runtime unavailable; parse check requires it")
    p = Path("/tmp/p96_test_xdata.js")
    p.write_text("({" + _xdata_js() + "})", encoding="utf-8")
    r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    p.unlink(missing_ok=True)
