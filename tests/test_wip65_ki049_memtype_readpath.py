"""WI-P65-KI049-MEMTYPE-READPATH tests.

Covers:
1. KI-049 root-cause fix — the C1 lenient normalizer
   (``helpers.metadata.normalize_memory_types``) now resolves the
   nested-legacy fallback: records whose ``memory_type`` lives only inside
   the nested ``metadata`` dict (creation-time kwarg nesting) read as
   type-known instead of 'type unknown'. Read-only: the input metadata is
   never mutated (C1 discipline).
2. ``resolved_memory_type_fields`` — additive-only injection helper for API
   producers; existing top-level values are never overridden.
3. ``api.context_graph._serialize_node`` — serialized node metadata carries
   the resolved type fields for nested-only legacy records.
4. ``api.advanced_filters._with_resolved_types`` — filter node payloads
   carry the resolved type fields.
5. KI-050 — the Inspector details-plate type section is restructured per
   the HITL spec: 'Memory Type' section title, label-first 'primary' row,
   label-first 'custom' row with wrapping; the WI-P24 C5 badge pin
   (``nc-details__badge`` + ``memory_type`` in source) is preserved.
"""

import re

import pytest

from usr.plugins.neuro_core.helpers import metadata as md_mod


# ---------------------------------------------------------------------------
# 1. Lenient normalizer nested-legacy fallback (KI-049)
# ---------------------------------------------------------------------------


class TestLenientNestedLegacyFallback:
    def test_nested_only_scalar_resolves(self):
        meta = {"id": "m1", "metadata": {"memory_type": "fact"}}
        norm = md_mod.normalize_memory_types(meta)
        assert norm.primary == "fact"
        assert norm.types == ["fact"]
        assert norm.additional == []
        assert norm.inconsistent is False

    def test_nested_only_collection_resolves(self):
        meta = {
            "id": "m2",
            "metadata": {"memory_types": ["fact", "hypothesis"]},
        }
        norm = md_mod.normalize_memory_types(meta)
        # Lenient semantics: a collection-only record has no scalar primary;
        # the collection is surfaced as the type set without deriving one.
        assert norm.primary is None
        assert norm.types == ["fact", "hypothesis"]
        assert norm.additional == ["fact", "hypothesis"]

    def test_nested_scalar_and_collection(self):
        meta = {
            "id": "m3",
            "metadata": {
                "memory_type": "note",
                "memory_types": ["note", "summary"],
            },
        }
        norm = md_mod.normalize_memory_types(meta)
        assert norm.primary == "note"
        assert norm.types == ["note", "summary"]
        assert norm.additional == ["summary"]

    def test_top_level_wins_over_nested(self):
        meta = {
            "memory_type": "task",
            "metadata": {"memory_type": "fact"},
        }
        norm = md_mod.normalize_memory_types(meta)
        assert norm.primary == "task"

    def test_no_mutation_of_input(self):
        meta = {"id": "m4", "metadata": {"memory_type": "fact"}}
        before = {k: (dict(v) if isinstance(v, dict) else v) for k, v in meta.items()}
        md_mod.normalize_memory_types(meta)
        assert meta == before
        assert "memory_type" not in meta  # nothing hoisted to top level

    def test_non_dict_nested_ignored(self):
        meta = {"id": "m5", "metadata": "not-a-dict"}
        norm = md_mod.normalize_memory_types(meta)
        assert norm.primary is None
        assert norm.types == []

    def test_nested_non_string_scalar_ignored(self):
        meta = {"id": "m6", "metadata": {"memory_type": 42}}
        norm = md_mod.normalize_memory_types(meta)
        assert norm.primary is None
        assert norm.types == []

    def test_both_forms_on_disk_record_resolves_once(self):
        # Shape verified on disk 2026-10-08 for kEqTZi2sRU / rgiN7MPlLH:
        # top-level added by the later HITL edit, nested legacy form intact.
        meta = {
            "memory_type": "fact",
            "memory_types": ["fact"],
            "metadata": {"memory_type": "fact", "note": "n", "source": "s"},
        }
        norm = md_mod.normalize_memory_types(meta)
        assert norm.primary == "fact"
        assert norm.types == ["fact"]
        assert norm.inconsistent is False


# ---------------------------------------------------------------------------
# 2. resolved_memory_type_fields helper
# ---------------------------------------------------------------------------


class TestResolvedMemoryTypeFields:
    def test_nested_only_gets_both_fields(self):
        meta = {"id": "m1", "metadata": {"memory_type": "fact"}}
        out = md_mod.resolved_memory_type_fields(meta)
        assert out == {"memory_type": "fact", "memory_types": ["fact"]}

    def test_top_level_present_returns_empty(self):
        meta = {
            "memory_type": "fact",
            "memory_types": ["fact"],
            "metadata": {"memory_type": "fact"},
        }
        # Fully-specified top-level record: nothing to add.
        assert md_mod.resolved_memory_type_fields(meta) == {}

    def test_top_level_scalar_gains_collection_additively(self):
        # Additive semantics: an existing top-level scalar is never
        # overridden, but a missing collection is completed from the
        # resolved type set.
        meta = {"memory_type": "fact", "metadata": {"memory_type": "fact"}}
        out = md_mod.resolved_memory_type_fields(meta)
        assert out == {"memory_types": ["fact"]}

    def test_type_unknown_returns_empty(self):
        assert md_mod.resolved_memory_type_fields({"id": "m2"}) == {}

    def test_non_dict_returns_empty(self):
        assert md_mod.resolved_memory_type_fields(None) == {}
        assert md_mod.resolved_memory_type_fields("x") == {}

    def test_does_not_mutate_input(self):
        meta = {"id": "m3", "metadata": {"memory_type": "fact"}}
        md_mod.resolved_memory_type_fields(meta)
        assert "memory_type" not in meta


# ---------------------------------------------------------------------------
# 3. context_graph serializer injection
# ---------------------------------------------------------------------------


class TestSerializeNodeResolvedTypes:
    def _node(self, metadata):
        from usr.plugins.neuro_core.helpers.context_graph import GraphNode

        return GraphNode(
            doc_id="doc1",
            content="c",
            metadata=metadata,
            score=0.5,
            hop=0,
        )

    def test_nested_only_node_gets_resolved_fields(self):
        from usr.plugins.neuro_core.api.context_graph import _serialize_node

        node = self._node({"id": "doc1", "metadata": {"memory_type": "fact"}})
        data = _serialize_node(node)
        assert data["metadata"]["memory_type"] == "fact"
        assert data["metadata"]["memory_types"] == ["fact"]
        # source node metadata untouched
        assert "memory_type" not in node.metadata

    def test_top_level_node_unchanged(self):
        from usr.plugins.neuro_core.api.context_graph import _serialize_node

        node = self._node({"id": "doc1", "memory_type": "task"})
        data = _serialize_node(node)
        # Top-level scalar is never overridden; the missing collection is
        # completed additively from the resolved type set.
        assert data["metadata"]["memory_type"] == "task"
        assert data["metadata"]["memory_types"] == ["task"]
        # source node metadata untouched
        assert "memory_types" not in node.metadata

    def test_serialize_context_graph_end_to_end(self):
        from usr.plugins.neuro_core.api.context_graph import (
            _serialize_context_graph,
        )
        from usr.plugins.neuro_core.helpers.context_graph import ContextGraph

        g = ContextGraph(
            nodes=[self._node({"id": "d1", "metadata": {"memory_type": "fact"}})],
            edges=[],
            query="q",
            seed_ids=["d1"],
        )
        out = _serialize_context_graph(g)
        assert out["nodes"][0]["metadata"]["memory_type"] == "fact"


# ---------------------------------------------------------------------------
# 4. advanced_filters payload injection
# ---------------------------------------------------------------------------


class TestAdvancedFiltersResolvedTypes:
    def test_nested_only_meta_gets_resolved_fields(self):
        from usr.plugins.neuro_core.api.advanced_filters import (
            _with_resolved_types,
        )

        meta = {"id": "d1", "metadata": {"memory_type": "fact"}}
        out = _with_resolved_types(meta)
        assert out["memory_type"] == "fact"
        assert out["memory_types"] == ["fact"]
        assert "memory_type" not in meta  # input untouched

    def test_top_level_meta_returned_as_is(self):
        from usr.plugins.neuro_core.api.advanced_filters import (
            _with_resolved_types,
        )

        # Fully-specified top-level record: returned unchanged (same object).
        meta = {"memory_type": "task", "memory_types": ["task"]}
        assert _with_resolved_types(meta) is meta

    def test_top_level_scalar_gains_collection_additively(self):
        from usr.plugins.neuro_core.api.advanced_filters import (
            _with_resolved_types,
        )

        # Additive semantics: existing top-level scalar never overridden;
        # the missing collection is completed in the returned COPY.
        meta = {"memory_type": "task"}
        out = _with_resolved_types(meta)
        assert out is not meta
        assert out["memory_type"] == "task"
        assert out["memory_types"] == ["task"]
        assert "memory_types" not in meta

    def test_non_dict_passthrough(self):
        from usr.plugins.neuro_core.api.advanced_filters import (
            _with_resolved_types,
        )

        assert _with_resolved_types(None) is None


# ---------------------------------------------------------------------------
# 5. KI-050 Inspector badge layout (presentational pins)
# ---------------------------------------------------------------------------


PANEL = "/a0/usr/plugins/neuro_core/webui/right-canvas-panels/graph-panel.html"


@pytest.fixture(scope="module")
def panel_src():
    with open(PANEL, "r", encoding="utf-8") as f:
        return f.read()


class TestKi050BadgeLayout:
    def test_memory_type_section_title_present(self, panel_src):
        assert ">Memory Type<" in panel_src

    def test_label_first_primary_row(self, panel_src):
        # 'primary' label appears before the badge inside the restructured block
        m = re.search(
            r"Memory Type</span>.*?primary</span>.*?nc-details__badge",
            panel_src,
            re.S,
        )
        assert m, "primary label must precede the primary badge"

    def test_label_first_custom_row_with_wrap(self, panel_src):
        m = re.search(
            r">custom</span>.*?typeChipsFor\(inspectNode\)", panel_src, re.S
        )
        assert m, "custom label must precede the chips container"
        # wrapping container: the custom row uses a flex-wrap container
        assert "flex-wrap: wrap" in panel_src

    def test_wip24_c5_badge_pin_preserved(self, panel_src):
        # WI-P24 test_c5_memory_type_badge_renders contract stays valid
        assert re.search(r"memory_type", panel_src) and "nc-details__badge" in panel_src

    def test_type_unknown_fallback_preserved(self, panel_src):
        assert "type unknown" in panel_src