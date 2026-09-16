"""WI-P21 — ledger-zero batch (KI-018-AA/AL/BH + CHANGELOG/BA-docs) pin tests.

Pins the fixes and dispositions applied under WI-P21:

* AL — ``POST /relationships`` accepts parameters from the parsed ``input``
  dict OR the query string (same ``input`` -> ``request.args`` fallback order
  as the ``?id=`` route, list-all, DELETE, and all sibling GET handlers).
  Discriminating pin: query-string-only POST previously failed with
  ```memory_subdir` is required``. ``weight`` uses an explicit absence check
  so a legitimate ``0.0`` from either source is preserved.
* BH — per-rel-type and multi-edge verification of the panel: the offered
  relationship vocabulary (filter checkboxes AND add-form select) equals the
  store ``RelationshipType`` enum exactly (both directions); ``relLabel``
  direction-aware labeling is verified per type via real Node.js execution
  (only precedes<->follows invert for incoming edges; all other types pass
  through verbatim in both directions); deleteEdge consumes the stored
  rel_type verbatim regardless of type; renderGraph keeps every edge whose
  endpoints survive the keep-set (multi-edge complexity renders, no
  per-type or per-node dedupe).
* AA — documentation accuracy: ``docs/architecture.md`` no longer claims the
  dormant ``webui/graph-panel.css`` "holds the panel styling"; the CSS is
  documented as dormant and remains unreferenced by any loader.
* CHANGELOG — count-lineage continuation anchors (446 -> 570, WI-P13..P21).

The extensions shell copy stays the small pointer copy (WI-P5B) — untouched.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
import types

import pytest

from usr.plugins.neuro_core.api import relationships as api_mod
from usr.plugins.neuro_core.helpers.graph_store import (
    VALID_RELATIONSHIP_TYPES,
    GraphEdge,
    GraphStore,
)

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"
SHELL = PLUGIN_ROOT / "extensions" / "webui" / "right-canvas-panels" / "graph-panel.html"
ARCH = PLUGIN_ROOT / "docs" / "architecture.md"
API_DOCS = PLUGIN_ROOT / "docs" / "api.md"
CHANGELOG = PLUGIN_ROOT / "CHANGELOG.md"

EXPECTED_TYPES = sorted(VALID_RELATIONSHIP_TYPES)


def _body_between(anchor: str, next_anchor: str) -> str:
    src = PANEL.read_text(encoding="utf-8")
    start = src.index(anchor)
    end = src.index(next_anchor, start)
    return src[start:end]


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


def _req(method: str = "POST", args: dict | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        path="/api/plugins/neuro_core/relationships",
        method=method,
        args=args or {},
    )


def _edge(fid: str, tid: str, rel: str = "related_to") -> GraphEdge:
    return GraphEdge(
        from_id=fid,
        to_id=tid,
        type=rel,
        weight=0.8,
        confidence=0.9,
        source="test",
        created_at="2026-09-16T00:00:00+00:00",
    )


def _store_fixture(monkeypatch, tmp_path: Path) -> None:
    import usr.plugins.neuro_core.helpers.graph_store as gsm

    monkeypatch.setattr(
        gsm,
        "_relationships_path",
        lambda subdir: str(tmp_path / f"rel_{subdir}.json"),
    )


# --------------------------------------------------------------------- AL


class TestPostQueryParamConsistency:
    """KI-018-AL: POST /relationships input-or-args contract."""

    def test_post_accepts_query_string_params(self, monkeypatch, tmp_path):
        """Discriminating pin: a POST with ALL parameters on the query string
        and an empty input dict must succeed (previously rejected with
        ```memory_subdir` is required``)."""
        _store_fixture(monkeypatch, tmp_path)
        handler = api_mod.RelationshipsApi()
        result = _run(handler.process(
            input={},
            request=_req(method="POST", args={
                "memory_subdir": "wip21a",
                "from_id": "doc-a",
                "to_id": "doc-b",
                "rel_type": "supports",
            }),
        ))
        assert result["success"] is True, f"POST must succeed: {result}"
        assert result["from_id"] == "doc-a"
        assert result["to_id"] == "doc-b"
        assert result["rel_type"] == "supports"
        # The edge actually landed in the store.
        store = GraphStore("wip21a")
        pairs = {
            (e.from_id, e.to_id, getattr(e.type, "value", e.type))
            for edges in store.get_edges().values()
            for e in edges
        }
        assert pairs == {("doc-a", "doc-b", "supports")}

    def test_post_input_keeps_precedence_over_query_string(
        self, monkeypatch, tmp_path
    ):
        """Parsed input keeps precedence over the query string (same order
        as the sibling handlers)."""
        _store_fixture(monkeypatch, tmp_path)
        handler = api_mod.RelationshipsApi()
        result = _run(handler.process(
            input={
                "memory_subdir": "wip21b",
                "from_id": "doc-a",
                "to_id": "doc-b",
                "rel_type": "related_to",
            },
            request=_req(method="POST", args={
                "memory_subdir": "wip21c",
                "from_id": "x",
                "to_id": "y",
                "rel_type": "part_of",
            }),
        ))
        assert result["success"] is True, f"POST must succeed: {result}"
        assert result["rel_type"] == "related_to"
        store = GraphStore("wip21b")
        pairs = {
            (e.from_id, e.to_id, getattr(e.type, "value", e.type))
            for edges in store.get_edges().values()
            for e in edges
        }
        assert pairs == {("doc-a", "doc-b", "related_to")}
        # The query-string subdir must NOT have received an edge.
        assert GraphStore("wip21c").get_edges() == {}

    def test_post_weight_zero_from_query_string_preserved(
        self, monkeypatch, tmp_path
    ):
        """A legitimate 0.0 weight on the query string must not be clobbered
        by the falsy-or default (explicit absence check)."""
        _store_fixture(monkeypatch, tmp_path)
        handler = api_mod.RelationshipsApi()
        result = _run(handler.process(
            input={},
            request=_req(method="POST", args={
                "memory_subdir": "wip21d",
                "from_id": "doc-a",
                "to_id": "doc-b",
                "rel_type": "related_to",
                "weight": "0.0",
            }),
        ))
        assert result["success"] is True, f"POST must succeed: {result}"
        assert result["weight"] == 0.0

    def test_post_weight_zero_from_input_body_preserved(
        self, monkeypatch, tmp_path
    ):
        """A legitimate 0.0 weight in the JSON body must survive (regression
        guard for the AL rework of the weight line)."""
        _store_fixture(monkeypatch, tmp_path)
        handler = api_mod.RelationshipsApi()
        result = _run(handler.process(
            input={
                "memory_subdir": "wip21e",
                "from_id": "doc-a",
                "to_id": "doc-b",
                "rel_type": "related_to",
                "weight": 0.0,
            },
            request=_req(method="POST"),
        ))
        assert result["success"] is True, f"POST must succeed: {result}"
        assert result["weight"] == 0.0

    def test_post_body_still_works_without_query_string(
        self, monkeypatch, tmp_path
    ):
        """The panel's JSON-body POST path is unchanged by the AL fix."""
        _store_fixture(monkeypatch, tmp_path)
        handler = api_mod.RelationshipsApi()
        result = _run(handler.process(
            input={
                "memory_subdir": "wip21f",
                "from_id": "doc-a",
                "to_id": "doc-b",
                "rel_type": "depends_on",
                "weight": 0.5,
            },
            request=_req(method="POST"),
        ))
        assert result["success"] is True, f"POST must succeed: {result}"
        assert result["weight"] == 0.5


# --------------------------------------------------------------------- BH


def _panel_type_list(fragment: str) -> list[str]:
    """Extract the rel-type literal list from a panel x-for template."""
    m = re.search(
        r"x-for=\"r in \[([^\]]+)\]\"", fragment
    )
    assert m, "rel-type x-for list not found"
    return sorted(x.strip().strip("'") for x in m.group(1).split(","))


def test_bh_filter_checkboxes_match_store_enum():
    """The advanced-filter relationship_type checkbox list offers exactly the
    8 store enum values (both directions pinned)."""
    src = PANEL.read_text(encoding="utf-8")
    offered = _panel_type_list(src)
    assert offered == EXPECTED_TYPES
    for t in EXPECTED_TYPES:
        assert f"'{t}'" in src


def test_bh_addform_select_matches_store_enum():
    """The inspector add-edge form select offers exactly the 8 store enum
    values — an edge created for any offered type is store-valid."""
    body = _body_between(
        'x-model="addForm.rel_type"', "Created edge:"
    )
    offered = _panel_type_list(body)
    assert offered == EXPECTED_TYPES


def _rellabel_js() -> str:
    """Extract the relLabel method source from the panel x-data scope."""
    body = _body_between("relLabel(rel) {", "async refocusInspectNode")
    return body.rstrip().rstrip(",")


def _run_node(script: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as f:
        f.write(script)
        path = f.name
    try:
        proc = subprocess.run(
            ["node", path], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, f"node failed: {proc.stderr}"
        return proc.stdout.strip()
    finally:
        Path(path).unlink(missing_ok=True)


def test_bh_rellabel_per_type_behavior():
    """Per-type labeling pin (BB fix generalized): executed in Node.js, the
    real relLabel implementation must

    * render every outbound edge verbatim for ALL 8 types, and
    * render every incoming edge verbatim EXCEPT the direction-symmetric
      pair precedes<->follows, which invert.

    No other type may be inverted (they have no inverse in the store enum)."""
    js = _rellabel_js()
    script = (
        "const o = { "
        + js
        + " };\n"
        "const types = "
        + repr(EXPECTED_TYPES).replace("'", '"')
        + ";\n"
        "const res = {};\n"
        "for (const t of types) {\n"
        "  res[t + '|out'] = o.relLabel({ direction: 'out', relType: t });\n"
        "  res[t + '|in'] = o.relLabel({ direction: 'in', relType: t });\n"
        "}\n"
        "console.log(JSON.stringify(res));\n"
    )
    import json

    res = json.loads(_run_node(script))
    inverse = {"precedes": "follows", "follows": "precedes"}
    for t in EXPECTED_TYPES:
        assert res[f"{t}|out"] == t, f"outbound {t} must render verbatim"
        expected_in = inverse.get(t, t)
        assert res[f"{t}|in"] == expected_in, (
            f"incoming {t} must render as {expected_in}"
        )


def test_bh_deleteedge_type_agnostic():
    """deleteEdge consumes the stored rel_type verbatim for every type —
    the delete path is type-agnostic (no per-type branching)."""
    body = _body_between("async deleteEdge(", "async openAddForm() {")
    assert "rel.relType" in body
    assert "rel.direction === 'out' ? id : rel.otherId" in body
    # No per-type special-casing in the delete path.
    for t in EXPECTED_TYPES:
        assert t not in body or t == "related_to", (
            f"deleteEdge must not special-case {t}"
        )


def test_bh_multiedge_render_keeps_all_endpoint_surviving_edges():
    """Multi-edge pin: renderGraph pushes EVERY edge whose endpoints are in
    the keep-set — no per-type filtering, no per-node-pair dedupe — so
    multiple edges per node (mixed directions, mixed types) all render."""
    body = _body_between("renderGraph() {", "watchTheme() {")
    assert "keep.has(e.from_id)" in body
    assert "keep.has(e.to_id)" in body
    # The only edge filter is endpoint membership; no rel_type condition.
    assert "e.rel_type ===" not in body


def test_bh_multiedge_store_roundtrip_all_types(
    monkeypatch, tmp_path
):
    """Multi-edge complexity at the store level: multiple edges per node with
    mixed types AND mixed directions all persist and read back — the data the
    panel renders under complexity."""
    _store_fixture(monkeypatch, tmp_path)
    gs = GraphStore("wip21g")
    gs.add_edge(_edge("a", "b", "precedes"))
    gs.add_edge(_edge("b", "a", "supports"))
    gs.add_edge(_edge("a", "c", "related_to"))
    gs.add_edge(_edge("a", "b", "depends_on"))  # second edge a->b, other type

    adjacency = GraphStore("wip21g").get_edges()
    triples = {
        (e.from_id, e.to_id, getattr(e.type, "value", e.type))
        for edges in adjacency.values()
        for e in edges
    }
    assert triples == {
        ("a", "b", "precedes"),
        ("b", "a", "supports"),
        ("a", "c", "related_to"),
        ("a", "b", "depends_on"),
    }


def test_bh_form_reset_defaults_are_store_valid():
    """The add-form default rel_type ('related_to') is a valid store enum
    value, and the reset sites (node tap + openAddForm) both reset to it."""
    assert "related_to" in EXPECTED_TYPES
    reset_literal = "addForm = { to_id: '', rel_type: 'related_to', weight: 1.0 }"
    src = PANEL.read_text(encoding="utf-8")
    tap_start = src.index("this.cy.on('tap', 'node'")
    tap_end = src.index("this.cy.on('tap', function(evt)")
    assert reset_literal in src[tap_start:tap_end], (
        "node-tap handler must reset the add form"
    )
    open_body = _body_between("async openAddForm() {", "closeAddForm() {")
    assert reset_literal in open_body


# --------------------------------------------------------------------- AA


def test_aa_architecture_no_longer_claims_css_holds_styling():
    """KI-018-AA document-as-dormant: architecture.md must not claim the
    dormant graph-panel.css 'holds the panel styling' (doc-vs-code
    contradiction), and must document its dormancy."""
    src = ARCH.read_text(encoding="utf-8")
    assert "graph-panel.css` holds the panel styling" not in src
    assert "holds the panel styling" not in src
    assert "graph-panel.css" in src, "dormant asset must still be documented"
    assert "dormant" in src


def test_aa_graph_panel_css_has_no_loader_reference():
    """Dormancy pin: no plugin source surface references graph-panel.css as
    a loadable asset (only docs may mention it)."""
    hits: list[str] = []
    for base in ("webui", "extensions", "api", "tools", "helpers"):
        for p in (PLUGIN_ROOT / base).rglob("*"):
            if p.is_file() and p.suffix in {
                ".html", ".js", ".py", ".yaml", ".json", ".html"
            } and "__pycache__" not in str(p):
                try:
                    if "graph-panel.css" in p.read_text(encoding="utf-8"):
                        hits.append(str(p))
                except (UnicodeDecodeError, OSError):
                    pass
    assert hits == [], f"unexpected loader references: {hits}"


def test_aa_graph_store_js_still_absent():
    """WI-P11 half-resolution holds: webui/graph-store.js remains removed."""
    assert not (PLUGIN_ROOT / "webui" / "graph-store.js").exists()


# ------------------------------------------------------------- CHANGELOG


def test_changelog_count_lineage_continuation_present():
    """CHANGELOG count-lineage must continue past 446 through the actual
    per-WI anchors up to 570 (WI-P21 reconciliation). Anchors are matched
    against whitespace-normalized text (the file hard-wraps lines)."""
    flat = " ".join(CHANGELOG.read_text(encoding="utf-8").split())
    for anchor in (
        "470 adds 24",
        "482 adds 12",
        "492 adds 10",
        "499 adds 7",
        "510 adds 11",
        "525 adds 15",
        "539 adds 14",
        "552 adds 13",
        "WI-P20-APISIDE-REMEDIATION",
        "570 adds 18",
        "WI-P21-LEDGER-ZERO",
    ):
        assert anchor in flat, f"CHANGELOG lineage missing: {anchor}"


def test_api_docs_post_documents_query_string_acceptance():
    """docs/api.md POST /relationships section documents the input-or-args
    fallback consistent with the fixed handler (BA-docs/AL reconciliation)."""
    src = API_DOCS.read_text(encoding="utf-8")
    post_section = src[src.index("### `POST /api/plugins/neuro_core/relationships"):
                       src.index("### `DELETE /api/plugins/neuro_core/relationships")]
    assert "query string" in post_section
    assert "request.args" in post_section


# ------------------------------------------------------------- shell copy


def test_shell_copy_untouched_pointer():
    """The extensions shell copy stays the small pointer copy (WI-P5B)."""
    shell = SHELL.read_text(encoding="utf-8")
    assert "x-component" in shell
    assert "relLabel" not in shell
    assert "addForm" not in shell
