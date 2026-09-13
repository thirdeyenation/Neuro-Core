"""WI-P15-AP-AO-REMEDIATION — KI-018-AP docs bullet + KI-018-AO date-range fix.

Covers:
* KI-018-AP: docs/api.md advanced_filters Notes bullet must state the
  implemented match-nothing semantics (PRD-banked wording from
  WI-P14-GRAPH-FILTER-DEFECTS narrative-report.yaml).
* KI-018-AO: the documented param contract (docs/api.md: single ``date_range``
  param, JSON {"start": ISO, "end": ISO}) is enforced end-to-end at handler
  level — params parsed, inclusive bounds applied, invalid/missing handled per
  documented semantics — and the graph panel sends the documented shape (not
  the undocumented date_range_start/date_range_end params).

Store fixtures follow the WI-P14 harness conventions: REAL GraphStore/
ScoreStore instances with ``abs_db_dir`` redirected to a tmp directory.
"""

from __future__ import annotations

import asyncio
import json
import types
from datetime import datetime, timezone

import pytest

from usr.plugins.neuro_core.helpers.graph_store import GraphEdge, GraphStore
from usr.plugins.neuro_core.helpers.metadata import MemoryType, ValidationStatus
from usr.plugins.neuro_core.helpers.scores import ScoreStore


# ---------------------------------------------------------------------------
# Fixtures (WI-P14 harness conventions)
# ---------------------------------------------------------------------------


class _FakeMyFaissDb:
    """Mimics the real framework MyFaiss interface used by the handler."""

    def __init__(self, docs: list):
        self.docstore = types.SimpleNamespace(
            _dict={f"doc-{i}": d for i, d in enumerate(docs)}
        )

    def get_all_docs(self):
        return self.docstore._dict


def _doc(i: int, timestamp) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=f"doc-{i}",
        page_content=f"memory {i}",
        metadata={
            "id": f"doc-{i}",
            "memory_type": MemoryType.NOTE.value,
            "validation_status": ValidationStatus.VALIDATED.value,
            "timestamp": timestamp,
        },
    )


@pytest.fixture
def dated_env(tmp_path, monkeypatch):
    """Three docs with distinct timestamps + real stores in a tmp dir."""
    from plugins._memory.helpers import memory as fw_memory
    from usr.plugins.neuro_core.api import advanced_filters as api_mod

    monkeypatch.setattr(
        fw_memory, "abs_db_dir", lambda subdir: str(tmp_path)
    )

    docs = [
        _doc(0, "2026-01-01T00:00:00+00:00"),
        _doc(1, "2026-06-15T12:00:00+00:00"),
        _doc(2, 1750000000.0),  # numeric epoch (docs: numeric accepted)
    ]

    graph_store = GraphStore("projects/neuro_core")
    graph_store.add_edge(
        GraphEdge(from_id="doc-0", to_id="doc-1", type="related_to")
    )

    score_store = ScoreStore("projects/neuro_core")
    score_store.set("doc-0", importance=0.5, confidence=0.5, stability=0.5)
    score_store.set("doc-1", importance=0.5, confidence=0.5, stability=0.5)
    score_store.set("doc-2", importance=0.5, confidence=0.5, stability=0.5)

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
    # filters_applied.date_range holds raw datetimes; the real serving
    # layer serializes later — use default=str in the test harness only.
    return json.loads(json.dumps(result, default=str))


# ---------------------------------------------------------------------------
# KI-018-AO: handler-level date_range contract (documented shape)
# ---------------------------------------------------------------------------


class TestDateRangeHandler:
    def test_date_range_json_string_parsed_and_applied(self, dated_env):
        """The documented single date_range param (JSON string) is parsed and
        filters documents by timestamp."""
        api_mod, memory, subdir, req = dated_env
        result = _run(
            api_mod, memory, subdir, req,
            date_range=json.dumps(
                {"start": "2026-01-01T00:00:00Z",
                 "end": "2026-02-01T00:00:00Z"}
            ),
        )
        assert result["success"] is True
        assert {n["id"] for n in result["nodes"]} == {"doc-0"}
        assert result["filters_applied"]["date_range"] is not None

    def test_date_range_inclusive_bounds(self, dated_env):
        """Docs: timestamp window — bounds are inclusive (a doc exactly at
        start or end is kept; handler skips only if < start or > end)."""
        api_mod, memory, subdir, req = dated_env
        result = _run(
            api_mod, memory, subdir, req,
            date_range=json.dumps(
                {"start": "2026-06-15T12:00:00Z",
                 "end": "2026-06-15T12:00:00Z"}
            ),
        )
        assert result["success"] is True
        assert {n["id"] for n in result["nodes"]} == {"doc-1"}

    def test_date_range_accepts_numeric_epoch_doc(self, dated_env):
        """Docs: numeric epoch doc timestamps are accepted."""
        api_mod, memory, subdir, req = dated_env
        epoch_dt = datetime.fromtimestamp(1750000000.0, tz=timezone.utc)
        result = _run(
            api_mod, memory, subdir, req,
            date_range=json.dumps(
                {"start": "2025-01-01T00:00:00Z",
                 "end": epoch_dt.isoformat()}
            ),
        )
        assert result["success"] is True
        assert "doc-2" in {n["id"] for n in result["nodes"]}

    def test_missing_date_range_means_no_filter(self, dated_env):
        """Docs default: date_range absent -> no timestamp filtering."""
        api_mod, memory, subdir, req = dated_env
        result = _run(api_mod, memory, subdir, req)
        assert result["success"] is True
        assert result["node_count"] == 3
        assert result["filters_applied"]["date_range"] is None

    def test_invalid_date_range_json_ignored(self, dated_env):
        """Unparseable date_range value -> treated as absent (no filter,
        no rejection), per the handler's documented lenient parsing."""
        api_mod, memory, subdir, req = dated_env
        result = _run(api_mod, memory, subdir, req, date_range="not-json{")
        assert result["success"] is True
        assert result["node_count"] == 3
        assert result["filters_applied"]["date_range"] is None

    def test_unparseable_doc_date_skipped_not_rejected(self, dated_env):
        """Docs: unparseable per-doc dates are skipped, not rejected — the
        implemented handler behavior (api/advanced_filters.py: the date
        comparison is skipped on parse failure) keeps the doc in the result
        rather than excluding it or crashing the query."""
        api_mod, memory, subdir, req = dated_env
        memory.db.docstore._dict["doc-0"].metadata["timestamp"] = "garbage"
        result = _run(
            api_mod, memory, subdir, req,
            date_range=json.dumps(
                {"start": "2026-01-01T00:00:00Z",
                 "end": "2026-12-31T00:00:00Z"}
            ),
        )
        assert result["success"] is True
        # doc-0 kept (unparseable date -> comparison skipped), doc-1 in range;
        # doc-2 (epoch 1750000000 = 2025-06-15) is outside the 2026 window.
        assert {n["id"] for n in result["nodes"]} == {"doc-0", "doc-1"}


# ---------------------------------------------------------------------------
# KI-018-AO: panel sends the documented param shape (source pins)
# ---------------------------------------------------------------------------


PANEL = "/a0/usr/plugins/neuro_core/webui/right-canvas-panels/graph-panel.html"


class TestPanelDateRangeParam:
    def _source(self) -> str:
        with open(PANEL, "r", encoding="utf-8") as f:
            return f.read()

    def test_panel_sends_documented_single_date_range_param(self):
        src = self._source()
        assert "params.set('date_range', JSON.stringify(" in src

    def test_panel_no_longer_sends_undocumented_params(self):
        src = self._source()
        assert "params.set('date_range_start'" not in src
        assert "params.set('date_range_end'" not in src

    def test_panel_sends_both_bounds_as_utc_iso(self):
        """The handler requires both start and end (_parse_date_range), and
        compares against tz-aware bounds — the panel must gate on both
        datetime-local inputs and convert them to UTC ISO strings."""
        src = self._source()
        assert (
            "if (this.advancedFilters.date_range_start && "
            "this.advancedFilters.date_range_end) params.set('date_range', "
            "JSON.stringify({ start: new Date(this.advancedFilters."
            "date_range_start).toISOString(), end: new Date(this."
            "advancedFilters.date_range_end).toISOString() }));"
        ) in src


# ---------------------------------------------------------------------------
# KI-018-AP: docs bullet matches implemented match-nothing semantics
# ---------------------------------------------------------------------------


DOCS = "/a0/usr/plugins/neuro_core/docs/api.md"


class TestDocsAllInvalidEnumBullet:
    def test_bullet_states_match_nothing_semantics(self):
        with open(DOCS, "r", encoding="utf-8") as f:
            text = f.read()
        assert (
            "a list with no valid values after dropping matches nothing"
            in text
        )
        assert "success=true, node_count=0" in text
        assert 'becomes "no filter"' not in text
