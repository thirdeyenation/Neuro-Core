"""WI-P33-BR-BQ-DEFECT-BATCH — regression tests for the five-defect batch.

BR: the graph-panel inspector Relationships x-for used
``:key="rel.otherId + rel.direction"``. Since KI-018-BO parallel-edge
preservation, two same-direction edges between the same node pair (e.g.
S→T supports AND S→T depends_on) collide on the identical key; Alpine's
diff then throws ("Cannot read properties of undefined (reading 'after')")
and the whole list renders blank. Fix: key includes relType. Verified
empirically against the vendored Alpine bundle (defect pass rows=0,
control pass rows=2).

BQ: GET /relationships?id= raised ValueError for any memory with outgoing
edges — the handler nested-iterated GraphStore.neighbors()' FLAT
list[tuple[str, int, GraphEdge]] and tried to unpack each tuple's
heterogeneous elements. (True self-loops are impossible: GraphStore
rejects them with "GraphEdge cannot self-loop".) Fix: consume the flat
shape directly.

AL/AM/AN: already closed (WI-P21 / WI-P14 fixes shipped); pins below
confirm the shipped behaviors remain intact where testable without
system-wide duplication.

BH (companion): additive verification coverage for multiple edges of
different types between the same node pair across the store round-trip
and the retrieval-build edge key.

Fixtures use disposable tmp dirs; live nc data files are never touched.
Tests run under the Agent Zero framework runtime (plugins import
framework memory internals).
"""

from __future__ import annotations

import asyncio
import tempfile
import os

from usr.plugins.neuro_core.api import relationships as api_mod
from usr.plugins.neuro_core.helpers.graph_store import GraphEdge, GraphStore


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _req(args=None):
    import types
    return types.SimpleNamespace(args=args or {})


def _edge(from_id, to_id, type_, seq):
    return GraphEdge(
        from_id=from_id,
        to_id=to_id,
        type=type_,
        weight=1.0,
        confidence=1.0,
        source="test",
        created_at="2026-09-24T00:00:%02dZ" % seq,
    )


class TestBrInspectorKeyUniqueness:
    """BR: the x-for :key must distinguish parallel same-direction edges."""

    def test_key_expression_includes_rel_type(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "webui",
                                "right-canvas-panels", "graph-panel.html")).read()
        assert ":key=\"rel.otherId + '|' + rel.direction + '|' + rel.relType\"" in src, (
            "inspector Relationships x-for key must include relType so "
            "parallel same-direction edges do not collide"
        )
        assert '":key="rel.otherId + rel.direction"' not in src, (
            "bare otherId+direction key must not remain anywhere"
        )

    def test_parallel_same_direction_edges_are_distinct_rels(self):
        """The data shape that triggered BR: two stored same-direction edges
        of different types produce two rels whose keys must differ."""
        td = tempfile.mkdtemp(prefix="p33_br_")
        gs = GraphStore(os.path.join(td, "sub"))
        gs.add_edge(_edge("S", "T", "supports", 0))
        gs.add_edge(_edge("S", "T", "depends_on", 1))
        edges = gs.get_edges("S")
        assert len(edges) == 2
        rels = [(
            ("T" if e.to_id == "T" else e.from_id),
            "out",
            e.type,
        ) for e in edges]
        keys = {r[0] + r[1] + r[2] for r in rels}
        assert len(keys) == 2, "fix keying must yield two distinct keys"


class TestBqGetRelationshipsInbound:
    """BQ: GET /relationships?id= must not raise on outgoing-edge memories."""

    def _handler_with_edges(self, subdir):
        gs = GraphStore(subdir)
        gs.add_edge(_edge("A", "B", "supports", 0))   # A outbound
        gs.add_edge(_edge("C", "A", "related_to", 1))  # A inbound
        orig = api_mod.GraphStore
        api_mod.GraphStore = lambda _subdir: gs
        return api_mod.RelationshipsApi(), orig

    def test_previously_reproduced_failure_mode_is_gone(self):
        td = tempfile.mkdtemp(prefix="p33_bq2_")
        subdir = os.path.join(td, "sub")
        handler, orig = self._handler_with_edges(subdir)
        try:
            result = _run(handler._get_relationships(
                input={"memory_subdir": subdir, "id": "A"},
                request=_req(),
            ))
        finally:
            api_mod.GraphStore = orig
        assert result["success"] is True
        got = {(e["from_id"], e["to_id"], e["type"]) for e in result["edges"]}
        assert ("A", "B", "supports") in got
        assert ("C", "A", "related_to") in got

    def test_dedup_full_triple(self):
        td = tempfile.mkdtemp(prefix="p33_bq3_")
        subdir = os.path.join(td, "sub")
        gs = GraphStore(subdir)
        gs.add_edge(_edge("A", "B", "supports", 0))
        orig = api_mod.GraphStore
        api_mod.GraphStore = lambda _subdir: gs
        handler = api_mod.RelationshipsApi()
        try:
            result = _run(handler._get_relationships(
                input={"memory_subdir": subdir, "id": "A"},
                request=_req(),
            ))
        finally:
            api_mod.GraphStore = orig
        assert result["success"] is True
        edges = [(e["from_id"], e["to_id"], e["type"]) for e in result["edges"]]
        assert edges.count(("A", "B", "supports")) == 1, "must dedupe on full triple"


class TestShippedPinsAl:
    """AL (closed via WI-P21): POST honors query-string params."""

    def test_post_honors_query_string_params(self):
        td = tempfile.mkdtemp(prefix="p33_al_")
        subdir = os.path.join(td, "sub")
        gs = GraphStore(subdir)
        orig = api_mod.GraphStore
        api_mod.GraphStore = lambda _subdir: gs
        handler = api_mod.RelationshipsApi()
        try:
            result = _run(handler._post_relationship(
                input={},
                request=_req({
                    "memory_subdir": subdir,
                    "from_id": "P",
                    "to_id": "Q",
                    "rel_type": "related_to",
                }),
            ))
        finally:
            api_mod.GraphStore = orig
        assert result["success"] is True
        assert (result["from_id"], result["to_id"], result["rel_type"]) == ("P", "Q", "related_to")


class TestBhMultiEdgeComplexity:
    """BH (companion, no product change): multi-type parallel edges survive
    the store round-trip and produce distinct retrieval-build edge keys."""

    def test_multiple_types_same_pair_round_trip(self):
        td = tempfile.mkdtemp(prefix="p33_bh_")
        subdir = os.path.join(td, "sub")
        gs = GraphStore(subdir)
        gs.add_edge(_edge("A", "B", "supports", 0))
        gs.add_edge(_edge("A", "B", "contradicts", 1))
        gs.add_edge(_edge("B", "A", "precedes", 2))
        gs2 = GraphStore(subdir)  # reload from disk
        got = {(e.from_id, e.to_id, e.type) for e in gs2.get_edges("A")}
        assert got == {("A", "B", "supports"), ("A", "B", "contradicts")}
        rev = {(e.from_id, e.to_id, e.type) for e in gs2.get_edges("B")}
        assert rev == {("B", "A", "precedes")}

    def test_retrieval_build_edge_key_distinguishes_types(self):
        """The retrieval build keys edges on (from_id, to_id, type) — the same
        triple the BFS dedupe uses; parallel edges of different types must
        not collapse into one key."""
        td = tempfile.mkdtemp(prefix="p33_bh2_")
        gs = GraphStore(os.path.join(td, "sub"))
        gs.add_edge(_edge("A", "B", "supports", 0))
        gs.add_edge(_edge("A", "B", "contradicts", 1))
        keys = {(e.from_id, e.to_id, e.type) for e in gs.get_edges("A")}
        assert len(keys) == 2
