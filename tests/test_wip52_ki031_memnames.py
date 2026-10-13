'''Tests for WI-P52-KI031-MEMNAMES — Memory Name aliases + custom Cluster
names (KI-031).

Pinned groups:
  (a) GraphStore reserved `_cluster_names` key — additive-only persistence
      inside the EXISTING relationships.json sidecar (ADR-NC1-002 boundary
      5): round-trip, adjacency isolation, legacy-file load, preservation
      across adjacency writes, targeted clear semantics.
  (b) api/memory_names.py handler — GET name lookup, GET cluster_names,
      POST set/change/clear for both name kinds, exactly-one-name per
      request, 120-char limit, auth requirement. Memory is faked per the
      test_execute.py FakeMemory convention; the framework Memory ID is
      pinned immutable (only the metadata key changes).
  (c) sanitizer non-interference — the real on-disk _05_neuro_sanitizer.py
      hook strips only neuro_* keys and leaves the plain `memory_name` key
      untouched (test_wip45 convention: hook loaded from its file).
  (d) graph-panel.html pins — name map feeding the dormant KI-018-BM
      from_name/to_name edge hooks, Details-panel Memory Name row under
      the immutable Memory ID, cluster legend custom-name control keyed on
      the lexicographically smallest member ID, names loaded before every
      render, 120-char client mirror, and preservation of the existing
      WI-P31 legacy pin substrings.
'''

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

from usr.plugins.neuro_core.api import memory_names as mn_mod
from usr.plugins.neuro_core.helpers.graph_store import (
    RESERVED_CLUSTER_NAMES_KEY,
    GraphEdge,
    GraphStore,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"


# ---------------------------------------------------------------------
# (a) GraphStore reserved-key persistence
# ---------------------------------------------------------------------


def _patch_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tag: str
) -> None:
    import usr.plugins.neuro_core.helpers.graph_store as gsm

    monkeypatch.setattr(
        gsm,
        "_relationships_path",
        lambda subdir: str(tmp_path / f"rel_{subdir}_{tag}.json"),
    )


def test_reserved_key_round_trip_and_adjacency_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_store(monkeypatch, tmp_path, "rt")
    gs = GraphStore("p52rt")
    gs.add_edge(GraphEdge("a", "b", "related_to"))
    assert gs.set_cluster_name("a", "Alpha Cluster") == 1
    assert gs.get_cluster_names() == {"a": "Alpha Cluster"}

    # On-disk shape: reserved key sits beside the adjacency buckets.
    raw = json.loads(Path(gs._path).read_text(encoding="utf-8"))
    assert raw[RESERVED_CLUSTER_NAMES_KEY] == {"a": "Alpha Cluster"}
    assert raw["a"][0]["to_id"] == "b"

    # Adjacency isolation: reserved key never returned as adjacency.
    fresh = GraphStore("p52rt")
    assert fresh.get_edges("a")[0].to_id == "b"
    assert fresh.get_cluster_names() == {"a": "Alpha Cluster"}

    # Same-name re-set is a no-op (targeted-write discipline).
    assert gs.set_cluster_name("a", "Alpha Cluster") == 0


def test_reserved_key_preserved_across_adjacency_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_store(monkeypatch, tmp_path, "pres")
    gs = GraphStore("p52pres")
    gs.add_edge(GraphEdge("a", "b", "related_to"))
    gs.set_cluster_name("b", "Beta")

    gs2 = GraphStore("p52pres")
    gs2.add_edge(GraphEdge("b", "c", "depends_on"))
    gs2.remove_edge("a", "b", "related_to")

    gs3 = GraphStore("p52pres")
    assert gs3.get_cluster_names() == {"b": "Beta"}
    assert [e.to_id for e in gs3.get_edges("b")] == ["c"]


def test_legacy_file_without_reserved_key_loads_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_store(monkeypatch, tmp_path, "legacy")
    path = tmp_path / "rel_p52leg_legacy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {"a": [{"to_id": "b", "type": "related_to", "weight": 1.0}]}
    path.write_text(json.dumps(legacy), encoding="utf-8")

    gs = GraphStore("p52leg")
    assert gs.get_cluster_names() == {}
    assert gs.get_edges("a")[0].to_id == "b"
    # First adjacency write must NOT inject an empty reserved key.
    gs.add_edge(GraphEdge("b", "c", "related_to"))
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert RESERVED_CLUSTER_NAMES_KEY not in on_disk


def test_set_cluster_name_clear_and_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_store(monkeypatch, tmp_path, "clr")
    gs = GraphStore("p52clr")
    gs.set_cluster_name("a", "Named")
    assert gs.set_cluster_name("a", None) == 1
    assert gs.get_cluster_names() == {}
    # Clearing an absent key is a no-op.
    assert gs.set_cluster_name("zz", None) == 0
    with pytest.raises(ValueError):
        gs.set_cluster_name("   ", "x")
    with pytest.raises(ValueError):
        gs.set_cluster_name("a", 123)


# ---------------------------------------------------------------------
# (b) API handler
# ---------------------------------------------------------------------


class FakeDoc:
    def __init__(self, doc_id: str, metadata: dict | None = None):
        self.metadata = dict(metadata or {"id": doc_id})


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

    @classmethod
    async def get_by_subdir(cls, subdir: str) -> "FakeMemory":
        return cls.instances[subdir]

    async def update_documents(self, docs: list) -> None:
        self.update_calls += 1


def _install_fake_memory(
    monkeypatch: pytest.MonkeyPatch, doc: FakeDoc | None
) -> FakeMemory:
    fake = FakeMemory(FakeDB({doc.metadata["id"]: doc} if doc else {}))
    FakeMemory.instances = {"default": fake}
    monkeypatch.setattr(mn_mod, "Memory", FakeMemory)
    return fake


def _handler() -> Any:
    return mn_mod.MemoryNamesApi(app=None, thread_lock=None)


def _req(method: str = "GET", args: dict | None = None) -> Any:
    return type("R", (), {"method": method, "args": (args or {})})()


def test_api_requires_auth_and_methods() -> None:
    assert mn_mod.MemoryNamesApi.requires_auth() is True
    assert mn_mod.MemoryNamesApi.get_methods() == ["GET", "POST"]


@pytest.mark.asyncio
async def test_get_memory_name_found_and_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = FakeDoc("mem-1", {"id": "mem-1", "memory_name": "Grocery List"})
    _install_fake_memory(monkeypatch, doc)
    h = _handler()
    out = await h.process(
        {}, _req(args={"memory_subdir": "default", "id": "mem-1"})
    )
    assert out == {
        "success": True,
        "memory_subdir": "default",
        "memory_id": "mem-1",
        "memory_name": "Grocery List",
    }
    out2 = await h.process(
        {}, _req(args={"memory_subdir": "default", "id": "nope"})
    )
    assert out2["success"] is True and out2["memory_name"] is None


@pytest.mark.asyncio
async def test_post_set_change_and_clear_memory_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = FakeDoc("mem-1", {"id": "mem-1"})
    fake = _install_fake_memory(monkeypatch, doc)
    h = _handler()

    out = await h.process(
        {"memory_subdir": "default", "id": "mem-1", "name": "Groceries"},
        _req("POST"),
    )
    assert out["success"] is True and out["memory_name"] == "Groceries"
    assert fake.update_calls == 1
    # Standard path: ONLY the metadata key changed; Memory ID immutable.
    assert doc.metadata == {"id": "mem-1", "memory_name": "Groceries"}

    await h.process(
        {"memory_subdir": "default", "id": "mem-1", "name": "Shopping"},
        _req("POST"),
    )
    assert doc.metadata["memory_name"] == "Shopping"

    out = await h.process(
        {"memory_subdir": "default", "id": "mem-1", "name": ""}, _req("POST")
    )
    assert out["success"] is True
    assert doc.metadata == {"id": "mem-1"}  # cleared; id untouched


@pytest.mark.asyncio
async def test_post_memory_name_unknown_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_memory(monkeypatch, None)
    h = _handler()
    out = await h.process(
        {"memory_subdir": "default", "id": "ghost", "name": "X"},
        _req("POST"),
    )
    assert out["success"] is False and "not found" in out["error"].lower()


@pytest.mark.asyncio
async def test_post_exactly_one_name_per_request() -> None:
    h = _handler()
    both = await h.process(
        {"memory_subdir": "default", "id": "m", "cluster_key": "k", "name": "x"},
        _req("POST"),
    )
    assert both["success"] is False and "exactly one" in both["error"]
    neither = await h.process(
        {"memory_subdir": "default", "name": "x"}, _req("POST")
    )
    assert neither["success"] is False


@pytest.mark.asyncio
async def test_post_name_validation_limit() -> None:
    h = _handler()
    out = await h.process(
        {"memory_subdir": "default", "id": "m", "name": "x" * 121},
        _req("POST"),
    )
    assert out["success"] is False and "120" in out["error"]


@pytest.mark.asyncio
async def test_cluster_name_round_trip_via_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_store(monkeypatch, tmp_path, "api")
    h = _handler()
    out = await h.process(
        {"memory_subdir": "default", "cluster_key": "m1", "name": "Cluster A"},
        _req("POST"),
    )
    assert out["success"] is True and out["cluster_name"] == "Cluster A"

    got = await h.process(
        {}, _req(args={"memory_subdir": "default", "cluster_names": "1"})
    )
    assert got["success"] is True
    assert got["cluster_names"] == {"m1": "Cluster A"}

    await h.process(
        {"memory_subdir": "default", "cluster_key": "m1", "name": ""},
        _req("POST"),
    )
    got2 = await h.process(
        {}, _req(args={"memory_subdir": "default", "cluster_names": "1"})
    )
    assert got2["cluster_names"] == {}


@pytest.mark.asyncio
async def test_get_requires_id_or_clusters_flag() -> None:
    h = _handler()
    out = await h.process({}, _req(args={"memory_subdir": "default"}))
    assert out["success"] is False


# ---------------------------------------------------------------------
# (c) sanitizer non-interference (real on-disk hook)
# ---------------------------------------------------------------------


def test_sanitizer_leaves_memory_name_key_untouched() -> None:
    sanitizer_path = (
        PLUGIN_ROOT
        / "extensions/python/_functions/plugins/_memory/helpers/memory/"
        / "Memory/update_documents/start/_05_neuro_sanitizer.py"
    )
    spec = importlib.util.spec_from_file_location(
        "p52_sanitizer", str(sanitizer_path)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    doc = FakeDoc(
        "mem-1",
        {"id": "mem-1", "memory_name": "Keep Me", "neuro_shaped": "strip me"},
    )
    # Mirrors the framework start-hook dispatch data shape (test_wip45).
    mod.NeuroUpdateSanitizer().execute(
        agent=None,
        data={"args": ([doc],), "kwargs": {}, "result": None, "exception": None},
    )
    assert doc.metadata.get("memory_name") == "Keep Me"
    assert "neuro_shaped" not in doc.metadata
    assert doc.metadata["id"] == "mem-1"


# ---------------------------------------------------------------------
# (d) graph-panel.html pins
# ---------------------------------------------------------------------


def _panel_src() -> str:
    return PANEL.read_text(encoding="utf-8")


def _xdata_js() -> str:
    src = _panel_src()
    m = re.search(r'x-data="\{([\s\S]*)\}" x-init=', src)
    assert m, "x-data block not found"
    return m.group(1)


def test_panel_name_map_feeds_dormant_edge_hooks() -> None:
    js = _xdata_js()
    # renderGraph builds a name map from plain metadata.memory_name ...
    assert "this._nameMap = nameMap;" in js
    assert js.find("const nameMap = {}") != -1
    # ...and the dormant KI-018-BM hooks consume it (backend payload wins).
    assert (
        "from_name: e.from_name || (this._nameMap && this._nameMap[e.from_id]) || ''"
        in js
    )
    assert (
        "to_name: e.to_name || (this._nameMap && this._nameMap[e.to_id]) || ''"
        in js
    )
    src = _panel_src()
    assert 'x-show="inspectEdge.from_name"' in src  # P31 dormant hooks
    assert 'x-show="inspectEdge.to_name"' in src


def test_panel_details_memory_name_row_and_editor() -> None:
    src = _panel_src()
    assert "Memory Name" in src
    assert "nodeDisplayName(inspectNode)" in src
    assert '@click="openNameEditor()"' in src
    assert '@click="saveMemoryName()"' in src
    assert 'x-text="inspectNode.id"' in src  # Memory ID stays visible
    js = _xdata_js()
    # POST body: exactly one name per request; empty clears (name: null).
    assert (
        "body = { memory_subdir: this.sub || 'default', id: id, name: nm || null };"
        in js
    )
    assert "/api/plugins/neuro_core/memory_names" in js
    # Client mirrors the 120-char server limit.
    assert "nm.length > 120" in js


def test_panel_cluster_name_legend_pins_preserved() -> None:
    src = _panel_src()
    # Existing P31 pin substrings remain verbatim.
    assert "realClusters.slice(0, 6)" in src
    assert "'+' + (realClusters.length - 6) + ' more clusters…'" in src
    assert 'x-show="realClusters.length > 0"' in src
    # New: custom name display + rename control wired to the endpoint.
    assert "c.customName" in src
    assert '@click="openClusterEditor(c.key, c.customName)"' in src
    assert '@click="saveClusterName(c.key)"' in src
    js = _xdata_js()
    # Stable persistence key = lexicographically smallest member ID.
    assert "sort()[0]" in js
    assert "cluster_key: cKey" in js
    assert "customName: this._clusterNames[cKey]" in js


def test_panel_cluster_names_loaded_before_render() -> None:
    js = _xdata_js()
    # search() and applyAdvancedFilters() both refresh names before render.
    assert js.count("await this.loadClusterNames();") >= 2
    assert "this._clusterNames = data.cluster_names || {};" in js
    assert "(cKey && this._clusterNames && this._clusterNames[cKey])" in js
