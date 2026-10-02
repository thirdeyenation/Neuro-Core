'''Tests for WI-P55-KI040-GRAPHPARSE-HARDENING — GraphStore sidecar parsing
defense-in-depth (KI-040).

KI-040 root cause (WI-P54): the pre-WI-P52 GraphStore loader treated ANY
non-list top-level key in relationships.json as an adjacency bucket;
iterating a dict/string value yields strings, and GraphEdge.from_dict's
raw["to_id"] subscript raised ``TypeError: string indices must be
integers, not 'str'`` — surfaced as the recall_shaping "graph expansion
degraded" warning.

Current code (WI-P52) pops the specific reserved key ``_cluster_names``,
but the adjacency path remained fragile against any FUTURE non-list
top-level key. This pin group hardens the ingestion point (``_read_file``):

  (a) neighbors() and the _read_file path on a sidecar carrying the
      reserved ``_cluster_names`` key — the exact KI-040 scenario —
      return clean triples with no exception (old HEAD code fails here).
  (b) unknown NON-list top-level keys are skipped as adjacency buckets
      (warning-logged), never indexed as edges.
  (c) unknown UNDERSCORE-PREFIXED (reserved-namespace) keys are preserved
      verbatim across adjacency writes (additive-only, same posture as
      _cluster_names).
  (d) valid adjacency data is parsed identically to before — no behavior
      change for well-formed sidecars.
'''

from __future__ import annotations

import json
from pathlib import Path

import pytest

from usr.plugins.neuro_core.helpers.graph_store import (
    RESERVED_CLUSTER_NAMES_KEY,
    GraphEdge,
    GraphStore,
)


def _patch_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tag: str
) -> None:
    import usr.plugins.neuro_core.helpers.graph_store as gsm

    monkeypatch.setattr(
        gsm,
        "_relationships_path",
        lambda subdir: str(tmp_path / f"rel_{subdir}_{tag}.json"),
    )


def _write_sidecar(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


_VALID_EDGE_A = {
    "to_id": "b",
    "type": "supports",
    "confidence": 0.9,
    "source": "agent",
    "weight": 1.0,
    "created_at": "2026-06-01T00:00:00Z",
}


# ---------------------------------------------------------------------
# (a) exact KI-040 scenario: _cluster_names in the sidecar
# ---------------------------------------------------------------------


def test_neighbors_with_reserved_cluster_names_key_no_typeerror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_store(monkeypatch, tmp_path, "ki040a")
    path = Path(tmp_path / "rel_p55a_ki040a.json")
    _write_sidecar(
        path,
        {
            "a": [_VALID_EDGE_A],
            RESERVED_CLUSTER_NAMES_KEY: {"a": "Cluster Persist"},
        },
    )
    gs = GraphStore("p55a")
    # KI-040 TypeError ("string indices must be integers, not 'str'")
    # must NOT recur. Old HEAD code raised exactly here.
    result = gs.neighbors("a", hops=2)
    assert [(n, hop) for n, hop, _ in result] == [("b", 1)]
    assert result[0][2].to_id == "b"
    assert result[0][2].type == "supports"
    # Reserved key is never exposed as adjacency.
    assert "_cluster_names" not in gs.get_edges("a")
    assert gs.get_cluster_names() == {"a": "Cluster Persist"}


def test_read_file_splits_reserved_key_from_adjacency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_store(monkeypatch, tmp_path, "ki040b")
    path = Path(tmp_path / "rel_p55b_ki040b.json")
    _write_sidecar(
        path,
        {"a": [_VALID_EDGE_A], RESERVED_CLUSTER_NAMES_KEY: {"a": "X"}},
    )
    gs = GraphStore("p55b")
    adj = gs.load()
    assert adj == {"a": [_VALID_EDGE_A]}
    assert RESERVED_CLUSTER_NAMES_KEY not in adj
    assert gs.get_cluster_names() == {"a": "X"}


# ---------------------------------------------------------------------
# (b) unknown non-list top-level keys are skipped, never indexed
# ---------------------------------------------------------------------


def test_unknown_non_list_top_level_key_skipped_no_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog
) -> None:
    _patch_store(monkeypatch, tmp_path, "ki040c")
    path = Path(tmp_path / "rel_p55c_ki040c.json")
    # Simulates the KI-040 stale-module condition generically: a future
    # reserved-style key written by older/newer code, plus an unknown
    # non-reserved scalar. Neither may be indexed as an adjacency bucket.
    _write_sidecar(
        path,
        {
            "a": [_VALID_EDGE_A],
            "_future_reserved": {"meta": True},
            "unknown_key": "just a string",
        },
    )
    gs = GraphStore("p55c")
    result = gs.neighbors("a", hops=2)
    assert [(n, hop) for n, hop, _ in result] == [("b", 1)]
    # Non-reserved junk key is dropped from adjacency.
    assert "unknown_key" not in gs.load()
    # The skip is observable (KI-040 hardening warning).
    assert any("non-list top-level key" in r.message for r in caplog.records)


def test_non_list_key_does_not_reach_any_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_store(monkeypatch, tmp_path, "ki040d")
    path = Path(tmp_path / "rel_p55d_ki040d.json")
    _write_sidecar(
        path,
        {
            "a": [_VALID_EDGE_A],
            "poison": {"to_id": "x"},  # dict, not a list
        },
    )
    gs = GraphStore("p55d")
    assert [(n, hop) for n, hop, _ in gs.neighbors("a", hops=1)] == [("b", 1)]
    edges = gs.get_edges("a")
    assert len(edges) == 1 and edges[0].to_id == "b"
    assert gs.all_edges()[0].to_id == "b"
    assert len(gs) == 1


# ---------------------------------------------------------------------
# (c) unknown reserved-namespace keys preserved verbatim across writes
# ---------------------------------------------------------------------


def test_unknown_underscore_key_preserved_across_adjacency_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_store(monkeypatch, tmp_path, "ki040e")
    path = Path(tmp_path / "rel_p55e_ki040e.json")
    _write_sidecar(
        path,
        {
            "a": [_VALID_EDGE_A],
            "_future_reserved": ["payload", {"k": 1}],
        },
    )
    gs = GraphStore("p55e")
    gs.add_edge(GraphEdge("a", "c", "related_to"))
    raw = json.loads(Path(gs._path).read_text(encoding="utf-8"))
    # Additive-only: the unknown reserved key rides the write verbatim.
    assert raw["_future_reserved"] == ["payload", {"k": 1}]
    # And the new adjacency bucket landed beside it.
    assert any(e["to_id"] == "c" for e in raw["a"])


def test_mixed_sidecar_full_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_store(monkeypatch, tmp_path, "ki040f")
    path = Path(tmp_path / "rel_p55f_ki040f.json")
    payload = {
        "a": [_VALID_EDGE_A],
        RESERVED_CLUSTER_NAMES_KEY: {"a": "Alpha"},
        "_other_reserved": {"shape": "dict"},
        "unknown_key": 42,
    }
    _write_sidecar(path, payload)
    gs = GraphStore("p55f")
    assert [(n, hop) for n, hop, _ in gs.neighbors("a", hops=2)] == [("b", 1)]
    gs.add_edge(GraphEdge("a", "d", "contradicts"))
    raw = json.loads(Path(gs._path).read_text(encoding="utf-8"))
    assert raw[RESERVED_CLUSTER_NAMES_KEY] == {"a": "Alpha"}
    assert raw["_other_reserved"] == {"shape": "dict"}
    # Non-reserved junk is not re-materialized by the write.
    assert "unknown_key" not in raw
    # Fresh instance reads the same preserved state cleanly.
    fresh = GraphStore("p55f")
    assert fresh.get_cluster_names() == {"a": "Alpha"}
    assert {e.to_id for e in fresh.get_edges("a")} == {"b", "d"}


# ---------------------------------------------------------------------
# (d) valid adjacency data: no behavior change
# ---------------------------------------------------------------------


def test_valid_adjacency_unchanged_by_hardening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_store(monkeypatch, tmp_path, "ki040g")
    gs = GraphStore("p55g")
    gs.add_edge(GraphEdge("a", "b", "supports"))
    gs.add_edge(GraphEdge("a", "c", "related_to"))
    gs.add_edge(GraphEdge("b", "d", "depends_on"))
    # Pure adjacency file — hardening must not alter parsing.
    result = gs.neighbors(["a", "b"], hops=2)
    reached = {(n, hop) for n, hop, _ in result}
    assert ("b", 1) in reached
    assert ("c", 1) in reached
    assert ("d", 1) in reached
    fresh = GraphStore("p55g")
    assert {e.to_id for e in fresh.get_edges("a")} == {"b", "c"}
    assert len(fresh) == 3
    raw = json.loads(Path(fresh._path).read_text(encoding="utf-8"))
    assert set(raw) == {"a", "b"}
