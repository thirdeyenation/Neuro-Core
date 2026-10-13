"""Phase-1 recall-shaping pins (WI-P43, ADR-NC1-003, D-NC1-106).

Pins required by the approved implementation contract:
- byte-identity of both return shapes with the gate OFF (C2/C3);
- dispatch-order coupling (_05_recall_shaping before _10_access_tracking);
- shaping effect with the gate ON (re-rank, markers, native score verbatim);
- bounded neighbor expansion (recall_shaping_neighbors_max, additive-only);
- telemetry ZERO writes with the gate OFF;
- handler registration health (Extension subclass + collision-unique names);
- V2 access separation (neighbor deliveries excluded from access tracking);
- V1 defensive persistence pin (neuro_* keys never reach sidecar writes).

Store isolation: score/graph store collaborators are fakes; telemetry and
sidecar paths are redirected to tmp_path. The live neuro_core.db and
plugin sidecars are never touched (disposable-fixture policy).
"""

from __future__ import annotations

import json
import os
import sys
import types

import pytest


_PROJECT_ROOT = "/a0/usr/plugins/neuro_core"
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


class _Doc:
    """Minimal duck-typed Document (page_content + metadata)."""

    def __init__(self, doc_id, content="", metadata=None):
        self.page_content = content
        self.metadata = dict(metadata or {})
        self.metadata["id"] = doc_id


class _FakeScoreStore:
    """Inert ScoreStore double recording update_access calls."""

    seed: dict = {}
    access_calls: list = []

    def __init__(self, subdir: str):
        self.subdir = subdir

    def get_optional(self, doc_id: str):
        return _FakeScoreStore.seed.get(doc_id)

    def update_access(self, doc_id: str):
        _FakeScoreStore.access_calls.append(doc_id)


class _FakeGraphStore:
    """Inert GraphStore double returning pre-seeded neighbor triples."""

    seed_triples: list = []

    def __init__(self, subdir: str):
        self.subdir = subdir

    def neighbors(self, from_id=None, hops=1):
        return list(_FakeGraphStore.seed_triples)


class _FakeEdge:
    def __init__(self, confidence=0.9):
        self.confidence = confidence


class _FakeMemory:
    """Duck-typed host Memory with a doc lookup table."""

    def __init__(self, docs=None, subdir="neuro_test"):
        self.memory_subdir = subdir
        self._docs = {d.metadata["id"]: d for d in (docs or [])}

    def get_document_by_id(self, doc_id):
        return self._docs.get(doc_id)


def _reset_fakes(seed_scores=None, triples=None):
    _FakeScoreStore.seed = dict(seed_scores or {})
    _FakeScoreStore.access_calls = []
    _FakeGraphStore.seed_triples = list(triples or [])


def _patch_stores(monkeypatch):
    from usr.plugins.neuro_core.helpers import graph_store as graph_mod
    from usr.plugins.neuro_core.helpers import scores as scores_mod

    monkeypatch.setattr(graph_mod, "GraphStore", _FakeGraphStore)
    monkeypatch.setattr(scores_mod, "ScoreStore", _FakeScoreStore)


def _patch_gate(monkeypatch, enabled: bool):
    """Patch the shaping gate at the module's own resolution point."""
    from usr.plugins.neuro_core.helpers import recall_shaping as rs

    monkeypatch.setattr(
        rs, "_plugin_config",
        lambda: {
            "recall_shaping_enabled": enabled,
            "recall_shaping_neighbors_max": 3,
            "similarity_weight": 0.5,
            "importance_weight": 0.3,
            "recency_weight": 0.2,
        },
    )


def _patch_telemetry(monkeypatch, tmp_path):
    """Redirect the telemetry sidecar to a tmp directory."""
    from usr.plugins.neuro_core.helpers import recall_shaping as rs

    path = os.path.join(str(tmp_path), "shaping_telemetry.json")
    monkeypatch.setattr(rs, "_telemetry_path", lambda subdir: path)
    return path


def _read_telemetry(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _doc_set():
    """Three native docs with distinguishable factor inputs."""
    return [
        _Doc("doc-a", content="alpha", metadata={
            "importance": 0.9, "confidence": 0.9,
            "validation_status": "validated",
            "timestamp": "2026-09-28T00:00:00+00:00",
        }),
        _Doc("doc-b", content="beta", metadata={
            "importance": 0.3, "confidence": 0.5,
            "validation_status": "deprecated",
            "timestamp": "2025-01-01T00:00:00+00:00",
        }),
        _Doc("doc-c", content="gamma", metadata={
            "importance": 0.5, "confidence": 0.7,
        }),
    ]


# ---------------------------------------------------------------------------
# Byte-identity with the gate OFF (C2/C3)
# ---------------------------------------------------------------------------


def test_gate_off_plain_returns_none_and_neutral_results(monkeypatch, tmp_path):
    _reset_fakes()
    _patch_stores(monkeypatch)
    _patch_gate(monkeypatch, enabled=False)
    tpath = _patch_telemetry(monkeypatch, tmp_path)

    from usr.plugins.neuro_core.helpers import recall_shaping as rs

    docs = _doc_set()
    assert rs.shape_plain(_FakeMemory(docs), docs) is None
    assert rs.shape_with_scores(
        _FakeMemory(docs), [(d, 0.9) for d in docs]
    ) is None
    # Zero telemetry writes when the gate is off.
    assert _read_telemetry(tpath) is None
    # Originals untouched (no marker keys anywhere).
    for d in docs:
        assert "neuro_shaped" not in d.metadata


def test_gate_off_handler_leaves_result_byte_identical(monkeypatch, tmp_path):
    _reset_fakes()
    _patch_stores(monkeypatch)
    _patch_gate(monkeypatch, enabled=False)
    _patch_telemetry(monkeypatch, tmp_path)

    mod = _load_handler("plain")[0]
    docs = _doc_set()
    data = {"args": (_FakeMemory(docs), "q"), "result": docs}
    mod.NeuroRecallShaping().execute(data=data)
    # data['result'] is the SAME list object with the SAME doc objects.
    assert data["result"] is docs
    for d in data["result"]:
        assert "neuro_shaped" not in d.metadata


def test_non_list_and_empty_results_unchanged(monkeypatch, tmp_path):
    _reset_fakes()
    _patch_stores(monkeypatch)
    _patch_gate(monkeypatch, enabled=True)
    _patch_telemetry(monkeypatch, tmp_path)

    from usr.plugins.neuro_core.helpers import recall_shaping as rs

    mem = _FakeMemory([])
    assert rs.shape_plain(mem, []) is None
    assert rs.shape_plain(mem, "not-a-list") is None
    assert rs.shape_with_scores(mem, [("not-a-tuple",)]) is None
    assert rs.shape_with_scores(mem, []) is None


# ---------------------------------------------------------------------------
# Shaping effect with the gate ON
# ---------------------------------------------------------------------------


def test_gate_on_reranks_and_marks_without_mutating_originals(monkeypatch, tmp_path):
    _reset_fakes()
    _patch_stores(monkeypatch)
    _patch_gate(monkeypatch, enabled=True)
    _patch_telemetry(monkeypatch, tmp_path)

    from usr.plugins.neuro_core.helpers import recall_shaping as rs

    docs = _doc_set()
    originals = [dict(d.metadata) for d in docs]
    # Native order: c, b, a — shaping must move doc-a (validated, high
    # importance) to the front and demote deprecated doc-b to the end.
    delivered = rs.shape_plain(_FakeMemory(docs), [docs[2], docs[1], docs[0]])

    assert delivered is not None and len(delivered) == 3
    ids = [d.metadata["id"] for d in delivered]
    assert ids[0] == "doc-a"
    assert ids[-1] == "doc-b"
    # Every delivery is a marked fresh copy.
    for d in delivered:
        assert d.metadata["neuro_shaped"] is True
        assert "neuro_factors" in d.metadata
        f = d.metadata["neuro_factors"]
        assert {"similarity", "importance", "confidence", "recency",
                "validation_status", "validation_factor", "shaped_score"} <= set(f)
    # Deprecated doc carries vf 0.4; validated carries 1.0.
    assert delivered[0].metadata["neuro_factors"]["validation_factor"] == 1.0
    assert delivered[-1].metadata["neuro_factors"]["validation_factor"] == 0.4
    # Originals are NOT mutated (shallow-copy discipline / V1 hazard guard).
    for d, before in zip(docs, originals):
        assert d.metadata == before
        assert "neuro_shaped" not in d.metadata
    # Delivered metadata dicts are fresh objects, not shared with originals.
    for d in delivered:
        assert all(d.metadata is not orig for orig in
                   [docs[0].metadata, docs[1].metadata, docs[2].metadata])


def test_with_scores_native_relevance_preserved_verbatim(monkeypatch, tmp_path):
    _reset_fakes()
    _patch_stores(monkeypatch)
    _patch_gate(monkeypatch, enabled=True)
    _patch_telemetry(monkeypatch, tmp_path)

    from usr.plugins.neuro_core.helpers import recall_shaping as rs

    docs = _doc_set()
    native = {"doc-a": 0.83, "doc-b": 0.41, "doc-c": 0.66}
    result = [(docs[0], native["doc-a"]),
              (docs[1], native["doc-b"]),
              (docs[2], native["doc-c"])]
    out = rs.shape_with_scores(_FakeMemory(docs), result)

    assert out is not None
    assert all(isinstance(p, tuple) and len(p) == 2 for p in out)
    # Native relevance floats are re-attached per document VERBATIM (C3).
    for doc, score in out:
        assert score == pytest.approx(native[doc.metadata["id"]])
    # Shaped score lives ONLY in neuro_factors, never replaces the tuple's
    # native float and never lands in page content or top-level metadata.
    for doc, score in out:
        fscore = doc.metadata["neuro_factors"]["shaped_score"]
        assert fscore != score or abs(score - fscore) < 1e-9
        assert doc.metadata.get("score") is None


# ---------------------------------------------------------------------------
# Bounded neighbor expansion (additive-only)
# ---------------------------------------------------------------------------


def test_neighbor_expansion_bounded_and_additive(monkeypatch, tmp_path):
    _reset_fakes(triples=[
        ("doc-a", 0, _FakeEdge(0.9)),
        ("nbr-1", 1, _FakeEdge(0.95)),
        ("nbr-2", 1, _FakeEdge(0.8)),
        ("nbr-3", 1, _FakeEdge(0.7)),
        ("nbr-4", 1, _FakeEdge(0.6)),
    ])
    _patch_stores(monkeypatch)
    _patch_gate(monkeypatch, enabled=True)
    _patch_telemetry(monkeypatch, tmp_path)

    from usr.plugins.neuro_core.helpers import recall_shaping as rs

    docs = _doc_set()
    mem = _FakeMemory(docs + [_Doc(f"nbr-{i}") for i in range(1, 5)])
    delivered = rs.shape_plain(mem, docs)

    ids = [d.metadata["id"] for d in delivered]
    # All natives survive (never dropped).
    for native_id in ("doc-a", "doc-b", "doc-c"):
        assert native_id in ids
    # Cap: at most recall_shaping_neighbors_max (3) neighbors appended.
    neighbors = [i for i in ids if i.startswith("nbr-")]
    assert len(neighbors) == 3
    # Neighbors are appended AFTER natives (additive-only, never displace).
    assert set(ids[:3]) == {"doc-a", "doc-b", "doc-c"}
    # Neighbor deliveries carry the distinct marker.
    for d in delivered:
        if d.metadata["id"].startswith("nbr-"):
            assert d.metadata.get("neuro_neighbor") is True
        else:
            assert "neuro_neighbor" not in d.metadata


def test_neighbor_expansion_failure_degrades_not_breaks(monkeypatch, tmp_path):
    _reset_fakes()
    _patch_stores(monkeypatch)
    _patch_gate(monkeypatch, enabled=True)
    _patch_telemetry(monkeypatch, tmp_path)

    from usr.plugins.neuro_core.helpers import recall_shaping as rs
    from usr.plugins.neuro_core.helpers import graph_store as graph_mod

    def _boom(*a, **k):
        raise RuntimeError("graph store down")

    monkeypatch.setattr(graph_mod, "GraphStore", _boom)

    docs = _doc_set()
    delivered = rs.shape_plain(_FakeMemory(docs), docs)
    # C1: expansion failure degrades — natives still delivered, re-ranked.
    assert delivered is not None and len(delivered) == 3
    assert any(d.metadata.get("neuro_degraded") for d in delivered)


# ---------------------------------------------------------------------------
# Dispatch-order coupling + handler registration health (C4)
# ---------------------------------------------------------------------------


def _handler_dir(method):
    return os.path.join(
        _PROJECT_ROOT, "extensions", "python", "_functions", "plugins",
        "_memory", "helpers", "memory", "Memory", method, "end",
    )


def _load_handler(kind):
    """kind: 'plain' | 'scores' -> (module, handler class).

    C4 collision-uniqueness: the two end-hook handlers intentionally use
    DISTINCT class names (NeuroRecallShaping / NeuroWithScoresRecallShaping)
    so the framework's extension-class merge never sees a duplicate name.
    """
    method = ("search_similarity_threshold" if kind == "plain"
              else "search_similarity_threshold_with_scores")
    path = os.path.join(_handler_dir(method), "_05_recall_shaping.py")
    assert os.path.exists(path), f"handler missing: {path}"
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"_h05_{kind}", path,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls = getattr(mod, "NeuroRecallShaping", None) or getattr(
        mod, "NeuroWithScoresRecallShaping", None
    )
    assert cls is not None, f"no handler class in {path}"
    return mod, cls


@pytest.mark.parametrize("method", [
    "search_similarity_threshold",
    "search_similarity_threshold_with_scores",
])
def test_dispatch_order_shaping_before_access_tracking(method):
    end_dir = _handler_dir(method)
    names = sorted(
        f for f in os.listdir(end_dir)
        if f.endswith(".py") and not f.startswith("__")
    )
    shaping_idx = next(
        i for i, n in enumerate(names) if n.startswith("_05_recall_shaping")
    )
    tracking_idx = next(
        i for i, n in enumerate(names) if n.startswith("_10_access_tracking")
    )
    # Lexicographic discovery order (helpers/extension.py) must place the
    # shaping handler BEFORE access tracking so tracking sees the final set.
    assert shaping_idx < tracking_idx


@pytest.mark.parametrize("method,expected_cls", [
    ("search_similarity_threshold", "NeuroRecallShaping"),
    ("search_similarity_threshold_with_scores", "NeuroWithScoresRecallShaping"),
])
def test_handler_registration_health(method, expected_cls):
    _mod, cls = _load_handler(
        "plain" if method == "search_similarity_threshold" else "scores"
    )
    from helpers.extension import Extension

    assert cls.__name__ == expected_cls
    assert issubclass(cls, Extension)
    assert hasattr(cls, "execute")


def test_handler_names_collision_unique_across_plugin():
    # C4: each handler class name must be unique across the plugin's
    # extension tree (framework merge hazard when two folders contribute
    # the same class name). The two end-hook handlers intentionally use
    # DISTINCT names.
    hits = {}
    for root, _dirs, files in os.walk(
        os.path.join(_PROJECT_ROOT, "extensions", "python")
    ):
        for f in files:
            if f.endswith(".py") and not f.startswith("__"):
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    src = fh.read()
                for name in ("class NeuroRecallShaping",
                             "class NeuroWithScoresRecallShaping"):
                    if name in src:
                        hits.setdefault(name, []).append(
                            os.path.join(root, f)
                        )
    # Each distinct class name is defined in exactly one file.
    assert sorted(hits) == [
        "class NeuroRecallShaping",
        "class NeuroWithScoresRecallShaping",
    ]
    assert len(hits["class NeuroRecallShaping"]) == 1
    assert len(hits["class NeuroWithScoresRecallShaping"]) == 1
    assert all("_05_recall_shaping.py" in h
               for files in hits.values() for h in files)


# ---------------------------------------------------------------------------
# V2: neighbor deliveries excluded from access tracking
# ---------------------------------------------------------------------------


def test_v2_neighbors_excluded_from_access_tracking(monkeypatch, tmp_path):
    _reset_fakes()
    _patch_stores(monkeypatch)
    _patch_gate(monkeypatch, enabled=True)
    _patch_telemetry(monkeypatch, tmp_path)

    # Run the REAL access-tracking handler over a shaped result containing
    # a neighbor delivery: only native ids may be counted.
    method = "search_similarity_threshold"
    path = os.path.join(_handler_dir(method), "_10_access_tracking.py")
    import importlib.util

    spec = importlib.util.spec_from_file_location("_h10_plain", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    native = _Doc("doc-a")
    neighbor = _Doc("nbr-1", metadata={"neuro_neighbor": True})
    mem = _FakeMemory([native, neighbor])
    data = {"args": (mem, "q"), "result": [native, neighbor]}
    mod.NeuroAccessTracking().execute(data=data)

    assert _FakeScoreStore.access_calls == ["doc-a"]


# ---------------------------------------------------------------------------
# V1: defensive persistence pin — neuro_* keys never reach sidecar writes
# ---------------------------------------------------------------------------


def test_v1_shaped_metadata_never_reaches_score_sidecar(monkeypatch, tmp_path):
    """Direct read evidence (implementation report): no consumer of the
    shaped results persists Document metadata back into FAISS or a sidecar
    (reflection.py:352 inserts only freshly built reflection docs;
    native_access.py:106 writes only access counters). This pin defends the
    contract: ScoreStore writes must never receive neuro_* marker keys."""
    _reset_fakes()
    _patch_stores(monkeypatch)
    _patch_gate(monkeypatch, enabled=True)
    _patch_telemetry(monkeypatch, tmp_path)

    from usr.plugins.neuro_core.helpers import recall_shaping as rs

    docs = _doc_set()
    delivered = rs.shape_plain(_FakeMemory(docs), docs)
    assert delivered is not None

    # The only sidecar write surface exercised by the recall path is
    # ScoreStore.update_access(doc_id) — a bare id string. Assert that the
    # shaped metadata keys never appear in any recorded write payload.
    from usr.plugins.neuro_core.helpers import scores as scores_mod

    recorded = getattr(_FakeScoreStore, "access_calls", [])
    for call in recorded:
        for key in ("neuro_shaped", "neuro_factors", "neuro_degraded",
                    "neuro_neighbor"):
            assert key not in str(call)
    # And the delivered docs' marker keys are metadata-only, never ids.
    for d in delivered:
        assert d.metadata.get("neuro_shaped") is True


# ---------------------------------------------------------------------------
# Telemetry content discipline
# ---------------------------------------------------------------------------


def test_telemetry_gate_on_records_bounded_event_without_text(monkeypatch, tmp_path):
    _reset_fakes()
    _patch_stores(monkeypatch)
    _patch_gate(monkeypatch, enabled=True)
    tpath = _patch_telemetry(monkeypatch, tmp_path)

    from usr.plugins.neuro_core.helpers import recall_shaping as rs

    docs = _doc_set()
    rs.shape_plain(_FakeMemory(docs), docs)

    data = _read_telemetry(tpath)
    assert data is not None and len(data["events"]) == 1
    event = data["events"][0]
    assert event["kind"] == "recall_shaped"
    assert event["gate"] is True
    assert event["native_count"] == 3
    assert event["delivered_count"] == 3
    # NEVER memory text or query text — only ids/counters/factors/gate.
    blob = json.dumps(event)
    for forbidden in ("alpha", "beta", "gamma"):
        assert forbidden not in blob
