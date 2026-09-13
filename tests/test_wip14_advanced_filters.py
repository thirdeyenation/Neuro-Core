"""WI-P14-GRAPH-FILTER-DEFECTS — advanced_filters endpoint + panel re-render.

Covers:
* KI-018-AM: GET /advanced_filters must return correct node/edge counts on a
  populated subdir — no filters and each filter type (memory_type,
  validation_status, relationship_type, score thresholds). Root cause fixed
  here: the handler enumerated documents via a nonexistent
  ``memory.db.get_all_documents()``; the framework FAISS db class (MyFaiss,
  /a0/plugins/_memory/helpers/memory.py) exposes ``get_all_docs()`` returning
  ``self.docstore._dict``.
* KI-018-AN: graph-panel.html must re-render on clearAdvancedFilters() and on
  minScore slider input (source-level pins, per WI-P13 harness conventions).

Store fixtures are REAL GraphStore/ScoreStore instances with ``abs_db_dir``
redirected to a tmp directory (the path helpers import it lazily from the
framework module, so patching there redirects both).
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from usr.plugins.neuro_core.helpers.graph_store import GraphEdge, GraphStore
from usr.plugins.neuro_core.helpers.metadata import MemoryType, ValidationStatus
from usr.plugins.neuro_core.helpers.scores import ScoreStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeMyFaissDb:
    """Mimics the real framework MyFaiss interface used by the handler.

    Real class: /a0/plugins/_memory/helpers/memory.py — ``get_all_docs()``
    returns ``self.docstore._dict`` (an id -> Document mapping).
    """

    def __init__(self, docs: list):
        self.docstore = types.SimpleNamespace(
            _dict={f"doc-{i}": d for i, d in enumerate(docs)}
        )

    def get_all_docs(self):
        return self.docstore._dict


def _doc(i: int, memory_type: str, validation_status: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=f"doc-{i}",
        page_content=f"memory {i}",
        metadata={
            "id": f"doc-{i}",
            "memory_type": memory_type,
            "validation_status": validation_status,
        },
    )


@pytest.fixture
def populated_env(tmp_path, monkeypatch):
    """Real GraphStore + ScoreStore in a tmp dir, plus a fake Memory whose
    .db mimics MyFaiss. Returns (api_mod, memory, subdir, request_factory)."""
    from plugins._memory.helpers import memory as fw_memory
    from usr.plugins.neuro_core.api import advanced_filters as api_mod

    # Redirect both stores' on-disk location (lazy import inside the path
    # helpers resolves this attribute at call time).
    monkeypatch.setattr(
        fw_memory, "abs_db_dir", lambda subdir: str(tmp_path)
    )

    docs = [
        _doc(0, MemoryType.NOTE.value, ValidationStatus.VALIDATED.value),
        _doc(1, MemoryType.FACT.value, ValidationStatus.VALIDATED.value),
        _doc(2, MemoryType.NOTE.value, ValidationStatus.UNVALIDATED.value),
    ]

    graph_store = GraphStore("projects/neuro_core")
    graph_store.add_edge(
        GraphEdge(from_id="doc-0", to_id="doc-1", type="related_to")
    )
    graph_store.add_edge(
        GraphEdge(from_id="doc-1", to_id="doc-2", type="supports")
    )

    score_store = ScoreStore("projects/neuro_core")
    score_store.set("doc-0", importance=0.9, confidence=0.8, stability=0.7)
    score_store.set("doc-1", importance=0.3, confidence=0.4, stability=0.2)
    score_store.set("doc-2", importance=0.6, confidence=0.6, stability=0.6)

    memory = types.SimpleNamespace(db=_FakeMyFaissDb(docs))

    async def _fake_get_by_subdir(memory_subdir, **kwargs):
        return memory

    fake_memory_cls = types.SimpleNamespace(get_by_subdir=_fake_get_by_subdir)
    monkeypatch.setattr(api_mod, "Memory", fake_memory_cls)

    def _request():
        req = types.SimpleNamespace()
        req.path = "/api/plugins/neuro_core/advanced_filters"
        req.method = "GET"
        req.args = {}
        return req

    return api_mod, memory, "projects/neuro_core", _request


def _run(api_mod, memory, subdir, request_factory, **params):
    handler = api_mod.AdvancedFiltersApi()
    result = asyncio.new_event_loop().run_until_complete(
        handler._get_filtered_graph(
            input={"memory_subdir": subdir, **params},
            request=request_factory(),
        )
    )
    return json.loads(json.dumps(result))


# ---------------------------------------------------------------------------
# KI-018-AM: node/edge enumeration on a populated subdir
# ---------------------------------------------------------------------------


class TestAdvancedFiltersCounts:
    def test_no_filters_returns_all_nodes_and_edges(self, populated_env):
        api_mod, memory, subdir, req = populated_env
        result = _run(api_mod, memory, subdir, req)
        assert result["success"] is True
        assert result["node_count"] == 3
        assert result["edge_count"] == 2
        assert {n["id"] for n in result["nodes"]} == {
            "doc-0", "doc-1", "doc-2"
        }

    def test_memory_type_filter(self, populated_env):
        api_mod, memory, subdir, req = populated_env
        result = _run(api_mod, memory, subdir, req, memory_type="note")
        assert result["success"] is True
        assert result["node_count"] == 2
        assert {n["id"] for n in result["nodes"]} == {"doc-0", "doc-2"}
        # Only edges fully inside the filtered node set survive; both edges
        # touch doc-1 (excluded), so none survive.
        assert result["edge_count"] == 0

    def test_validation_status_filter(self, populated_env):
        api_mod, memory, subdir, req = populated_env
        result = _run(
            api_mod, memory, subdir, req, validation_status="validated"
        )
        assert result["success"] is True
        assert result["node_count"] == 2
        assert {n["id"] for n in result["nodes"]} == {"doc-0", "doc-1"}

    def test_relationship_type_filter(self, populated_env):
        api_mod, memory, subdir, req = populated_env
        result = _run(api_mod, memory, subdir, req, relationship_type="supports")
        assert result["success"] is True
        assert result["node_count"] == 3  # node filter untouched
        assert result["edge_count"] == 1
        assert result["edges"][0]["type"] == "supports"

    def test_importance_threshold_filter(self, populated_env):
        api_mod, memory, subdir, req = populated_env
        result = _run(api_mod, memory, subdir, req, importance_min=0.5)
        assert result["success"] is True
        # doc-1 has importance 0.3 -> excluded.
        assert {n["id"] for n in result["nodes"]} == {"doc-0", "doc-2"}
        assert result["edge_count"] == 0  # edge doc-1->doc-2 loses doc-1

    def test_confidence_and_stability_thresholds(self, populated_env):
        api_mod, memory, subdir, req = populated_env
        result = _run(
            api_mod, memory, subdir, req,
            confidence_min=0.5, stability_min=0.5,
        )
        assert result["success"] is True
        # doc-1 (0.4/0.2) excluded; doc-0 and doc-2 pass.
        assert {n["id"] for n in result["nodes"]} == {"doc-0", "doc-2"}

    def test_combined_filters(self, populated_env):
        api_mod, memory, subdir, req = populated_env
        result = _run(
            api_mod, memory, subdir, req,
            memory_type="note", validation_status="validated",
        )
        assert result["success"] is True
        assert {n["id"] for n in result["nodes"]} == {"doc-0"}
        assert result["edge_count"] == 0


# ---------------------------------------------------------------------------
# KI-018-AM: enumeration-interface robustness pins
# ---------------------------------------------------------------------------


class TestEnumerationInterface:
    def test_docstore_fallback_when_get_all_docs_missing(
        self, populated_env, monkeypatch
    ):
        """A db without get_all_docs() but with a docstore still enumerates."""
        api_mod, memory, subdir, req = populated_env
        # Replace the db with an object exposing only the docstore fallback
        # path (no get_all_docs method).
        memory.db = types.SimpleNamespace(docstore=memory.db.docstore)
        result = _run(api_mod, memory, subdir, req)
        assert result["success"] is True
        assert result["node_count"] == 3

    def test_db_without_any_interface_yields_zero_nodes(
        self, populated_env
    ):
        """Legacy behavior pin: no enumeration interface -> empty result,
        not a crash (defensive, matches pre-fix fallback semantics)."""
        api_mod, memory, subdir, req = populated_env
        memory.db = types.SimpleNamespace()
        result = _run(api_mod, memory, subdir, req)
        assert result["success"] is True
        assert result["node_count"] == 0
        assert result["edge_count"] == 0


# ---------------------------------------------------------------------------
# KI-018-AN: panel re-render source pins
# ---------------------------------------------------------------------------


PANEL = "/a0/usr/plugins/neuro_core/webui/right-canvas-panels/graph-panel.html"


class TestPanelRerender:
    def _source(self) -> str:
        with open(PANEL, "r", encoding="utf-8") as f:
            return f.read()

    def test_clear_advanced_filters_requeries(self):
        src = self._source()
        # clearAdvancedFilters must re-run the last query type: filters were
        # active -> applyAdvancedFilters(); otherwise -> search().
        assert "hadFilters" in src
        assert "if (hadFilters) { this.applyAdvancedFilters(); } else { this.search(); }" in src

    def test_minscore_slider_triggers_rerender(self):
        src = self._source()
        # The minScore slider must re-render the graph from current data on
        # input (client-side filter — no re-fetch).
        assert 'x-model.number="minScore" @input="$nextTick(() => renderGraph())"' in src

    def test_extensions_shell_copy_untouched_by_these_pins(self):
        """The pins above target the webui copy only; the extensions shell
        copy is pinned byte-identical by test_graphpanel_copies.py — this
        test just guards that the panel file edited here is the webui one."""
        src = self._source()
        assert "clearAdvancedFilters" in src
        assert "renderGraph" in src
