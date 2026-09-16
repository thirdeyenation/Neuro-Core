"""WI-P20 — API/store-side remediation batch (KI-018-BE/BF/BG) pin tests.

Pins the three fixes applied under WI-P20:

* BE — ``api/memory_subdirs.py`` scans the actual project-memory location
  ``<project>/.a0proj/memory/`` (framework ground truth: helpers/projects.py
  ``PROJECT_META_DIR = ".a0proj"`` + plugins/_memory/helpers/memory.py
  project resolution), graceful for projects without project memory.
* BF — ``deleteEdge`` success path re-runs the current view (search when a
  query is active, applyAdvancedFilters otherwise) and refreshes the
  inspected node via refocusInspectNode(), with NO q-overwrite (same
  treatment addEdge received in WI-P19).
* BG — ``GraphStore.neighbors()`` does not omit seed-to-seed edges in
  multi-seed retrieval (the pre-seeded visited set previously swallowed
  them). Single-seed semantics are unchanged. This is the VAL-pattern
  discriminating pin: single-seed returns the edge before and after;
  multi-seed now also returns it.

The extensions shell copy stays the small pointer copy (WI-P5B) — untouched.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from usr.plugins.neuro_core.helpers.graph_store import GraphStore, GraphEdge

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"
SHELL = PLUGIN_ROOT / "extensions" / "webui" / "right-canvas-panels" / "graph-panel.html"


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _body_between(anchor: str, next_anchor: str) -> str:
    src = PANEL.read_text(encoding="utf-8")
    start = src.index(anchor)
    end = src.index(next_anchor, start)
    return src[start:end]


# --------------------------------------------------------------------- BE


def _make_handler(tmp_path: Path):
    from usr.plugins.neuro_core.api import memory_subdirs as mod

    mod._STANDARD_MEMORY_ROOT = str(tmp_path / "memory")
    mod._PROJECTS_ROOT = str(tmp_path / "projects")
    return mod.MemorySubdirsApi(app=None, thread_lock=None)


async def _call(handler) -> dict:
    fake_request = type("R", (), {"method": "GET", "args": {}})()
    result = await handler.process(input={}, request=fake_request)
    assert isinstance(result, dict)
    return result


@pytest.mark.asyncio
async def test_be_project_memory_scanned_at_a0proj_location(tmp_path: Path):
    """BE: a project with <project>/.a0proj/memory/ is discovered as a
    project entry pointing at that location."""
    (tmp_path / "projects" / "nc1" / ".a0proj" / "memory").mkdir(parents=True)

    handler = _make_handler(tmp_path)
    result = await _call(handler)

    assert result["success"] is True
    entries = [s for s in result["subdirs"] if s["type"] == "project"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == "nc1"
    assert entry["path"].endswith("/nc1/.a0proj/memory/")


@pytest.mark.asyncio
async def test_be_legacy_memory_dir_is_not_project_entry(tmp_path: Path):
    """BE: a legacy <project>/memory/ directory is NOT the project-memory
    scan location and must not produce a project entry."""
    (tmp_path / "projects" / "legacy" / "memory").mkdir(parents=True)

    handler = _make_handler(tmp_path)
    result = await _call(handler)

    assert result["success"] is True
    entries = [s for s in result["subdirs"] if s["type"] == "project"]
    assert entries == []


@pytest.mark.asyncio
async def test_be_graceful_without_project_memory(tmp_path: Path):
    """BE: projects without project memory are gracefully omitted (never
    fatal); standard entries still surface."""
    (tmp_path / "projects" / "bare").mkdir(parents=True)
    (tmp_path / "memory" / "default").mkdir(parents=True)

    handler = _make_handler(tmp_path)
    result = await _call(handler)

    assert result["success"] is True
    entries = [s for s in result["subdirs"] if s["type"] == "project"]
    assert entries == []
    standard = [s for s in result["subdirs"] if s["type"] == "standard"]
    assert len(standard) == 1
    assert standard[0]["name"] == "default"


@pytest.mark.asyncio
async def test_be_mixed_projects_split_correctly(tmp_path: Path):
    """BE: with both a project-memory project and a bare project present,
    only the one with .a0proj/memory is returned, and the empty-projects
    case (no projects dir at all) stays graceful."""
    (tmp_path / "projects" / "nc1" / ".a0proj" / "memory").mkdir(parents=True)
    (tmp_path / "projects" / "empty").mkdir(parents=True)

    handler = _make_handler(tmp_path)
    result = await _call(handler)

    project_names = [s["name"] for s in result["subdirs"] if s["type"] == "project"]
    assert project_names == ["nc1"]


# --------------------------------------------------------------------- BF


def test_bf_deleteedge_no_q_overwrite():
    """BF: deleteEdge must not overwrite the user's query with the inspected
    node id (the WI-P19 addEdge defect pattern)."""
    body = _body_between("async deleteEdge(", "async openAddForm() {")
    assert "this.q = this.inspectNode.id" not in body


def test_bf_deleteedge_reruns_current_view_and_refocuses():
    """BF: after a successful delete, deleteEdge re-runs the current search
    (or the advanced-filter view when the graph was loaded filter-first) and
    then re-focuses/re-selects the inspected node via refocusInspectNode()."""
    body = _body_between("async deleteEdge(", "async openAddForm() {")
    assert "if (this.q.trim()) { await this.search(); }" in body
    assert "await this.applyAdvancedFilters();" in body
    assert "this.refocusInspectNode()" in body


def test_bf_deleteedge_keeps_stored_rel_type_verbatim():
    """BF boundary: deleteEdge still sends the stored rel_type verbatim to
    the DELETE API — stored rel_type data is never rewritten."""
    body = _body_between("async deleteEdge(", "async openAddForm() {")
    assert "rel.relType" in body


def test_bf_shell_copy_untouched():
    """The extensions shell copy remains the WI-P5B pointer copy and carries
    no panel logic (no deleteEdge, no x-data scope)."""
    shell = SHELL.read_text(encoding="utf-8")
    assert "deleteEdge" not in shell
    assert "x-data" not in shell


# --------------------------------------------------------------------- BG


class TestNeighborsSeedToSeed:
    def _store(self, monkeypatch, tmp_path: Path) -> None:
        import usr.plugins.neuro_core.helpers.graph_store as gsm

        monkeypatch.setattr(
            gsm,
            "_relationships_path",
            lambda subdir: str(tmp_path / f"rel_{subdir}.json"),
        )

    def test_multi_seed_returns_seed_to_seed_edge(
        self, monkeypatch, tmp_path: Path
    ):
        """BG discriminating pin: with seeds [a, b] and an a->b edge, the
        edge is returned (previously swallowed by the pre-seeded visited
        set)."""
        self._store(monkeypatch, tmp_path)
        gs = GraphStore("wip20a")
        gs.add_edge(GraphEdge("a", "b", "related_to"))

        results = gs.neighbors(from_id=["a", "b"], hops=1)
        triples = [(t, h, e.from_id) for t, h, e in results]
        assert ("b", 1, "a") in triples

    def test_single_seed_semantics_unchanged(
        self, monkeypatch, tmp_path: Path
    ):
        """Single-seed retrieval is unchanged by the BG fix."""
        self._store(monkeypatch, tmp_path)
        gs = GraphStore("wip20b")
        gs.add_edge(GraphEdge("a", "b", "related_to"))
        gs.add_edge(GraphEdge("b", "c", "related_to"))

        results = gs.neighbors(from_id="a", hops=2)
        assert [(t, h) for t, h, _ in results] == [("b", 1), ("c", 2)]

    def test_multi_seed_no_duplicate_expansion(
        self, monkeypatch, tmp_path: Path
    ):
        """Seed-to-seed entries are emitted without re-expanding seeds: a
        hop-2 chain from seed b is returned once, from b's own expansion."""
        self._store(monkeypatch, tmp_path)
        gs = GraphStore("wip20c")
        gs.add_edge(GraphEdge("a", "b", "related_to"))
        gs.add_edge(GraphEdge("b", "c", "related_to"))

        results = gs.neighbors(from_id=["a", "b"], hops=2)
        triples = [(t, h, e.from_id) for t, h, e in results]
        assert ("b", 1, "a") in triples
        assert ("c", 1, "b") in triples
        assert len([t for t, _, _ in triples if t == "c"]) == 1

    def test_rel_type_filter_still_applies(
        self, monkeypatch, tmp_path: Path
    ):
        """rel_type filtering is untouched by the BG fix."""
        self._store(monkeypatch, tmp_path)
        gs = GraphStore("wip20d")
        gs.add_edge(GraphEdge("a", "b", "related_to"))
        gs.add_edge(GraphEdge("a", "d", "supports"))

        results = gs.neighbors(
            from_id=["a", "b"], hops=1, rel_type="supports"
        )
        assert [(t, h) for t, h, _ in results] == [("d", 1)]

    def test_reverse_direction_seed_edge_discovered(
        self, monkeypatch, tmp_path: Path
    ):
        """A directed b->a edge between seeds is likewise returned from the
        seed a side (direction-aware, not rewritten)."""
        self._store(monkeypatch, tmp_path)
        gs = GraphStore("wip20e")
        gs.add_edge(GraphEdge("b", "a", "related_to"))

        results = gs.neighbors(from_id=["a", "b"], hops=1)
        triples = [(t, h, e.from_id) for t, h, e in results]
        assert ("a", 1, "b") in triples
