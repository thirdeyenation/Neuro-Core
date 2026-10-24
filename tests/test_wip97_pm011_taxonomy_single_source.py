'''Tests for WI-P97-PM011-TAXONOMY-SINGLE-SOURCE — single source of truth
for the memory-type taxonomy shared by capture-time classification and the
graph panel's type-filter enumeration (D-NC1-177 approved acceptance
criterion):

  - helpers/metadata.py MemoryType is the backend authority (8 values).
  - webui/right-canvas-panels/graph-panel.html carries exactly ONE in-panel
    taxonomy literal: the MEMORY_TYPE_ENUM constant in the x-data scope.
  - The panel's type-filter enumeration (Advanced Filters x-for) derives
    from that constant.
  - DRIFT GUARD: the panel constant equals the backend MemoryType enum.
    If the backend enum changes (PM-014 territory) without updating the
    panel constant — or vice versa — this test fails.

The taxonomy itself is NOT changed by this work item: no types
added/removed/renamed (PM-014), no auto-classification (PM-007), no
score-semantics changes (PM-008/009). Manual type assignment/editing in
the graph UI remains fully intact.
'''

from __future__ import annotations

import re
from pathlib import Path

from usr.plugins.neuro_core.helpers.metadata import MemoryType

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"


def _panel_src():
    return PANEL.read_text(encoding="utf-8")


def _panel_memory_type_enum():
    """Extract the single MEMORY_TYPE_ENUM constant from the panel x-data."""
    src = _panel_src()
    m = re.search(r"MEMORY_TYPE_ENUM:\s*(\[[^\]]*\])", src)
    assert m, "MEMORY_TYPE_ENUM constant must exist in graph-panel.html x-data"
    return re.findall(r"'([^']+)'", m.group(1))


def test_drift_guard_panel_enum_equals_backend_enum():
    """D-NC1-177 acceptance criterion: the panel's type-filter enumeration
    equals the backend MemoryType enum. This is the standing drift guard."""
    panel_types = _panel_memory_type_enum()
    backend_types = [m.value for m in MemoryType]
    assert panel_types == backend_types, (
        "graph-panel.html MEMORY_TYPE_ENUM has drifted from the backend "
        "MemoryType enum (helpers/metadata.py). Update the panel constant "
        "to match the backend authority (panel follows the backend enum)."
    )


def test_panel_has_exactly_one_taxonomy_literal():
    """Single-source invariant: the 8-type taxonomy literal appears exactly
    once in the panel — inside the MEMORY_TYPE_ENUM constant. Any new inline
    duplication reintroduces the drift surface this work item removed."""
    src = _panel_src()
    literal = "['fact','concept','task','event','decision','skill','preference','note']"
    assert src.count(literal) == 1
    # ...and that single occurrence is the constant definition itself.
    assert re.search(r"MEMORY_TYPE_ENUM:\s*" + re.escape(literal), src)


def test_filter_enumeration_derives_from_constant():
    """The Advanced Filters Memory Type checkbox group iterates the shared
    constant, not an inline literal."""
    src = _panel_src()
    # The filter x-for (and the primary-select x-for) reference the constant.
    assert src.count('x-for="t in MEMORY_TYPE_ENUM"') == 2
    # No inline-literal x-for remains anywhere in the panel.
    assert not re.search(
        r"x-for=\"t in \['fact','concept','task','event','decision','skill',"
        r"'preference','note'\]\"",
        src,
    )


def test_taxonomy_unchanged_eight_values():
    """The taxonomy itself is untouched by WI-P97 (PM-014 territory): the
    backend enum still carries exactly the 8 v1 values, in order."""
    assert [m.value for m in MemoryType] == [
        "fact",
        "concept",
        "task",
        "event",
        "decision",
        "skill",
        "preference",
        "note",
    ]


def test_manual_type_editing_affordances_reference_constant():
    """Manual type assignment/editing stays fully intact (PM-007 constraint):
    the type-edit affordances (addType enum guard, chip classification,
    current-primary display check) now read the shared constant instead of
    private inline copies — same semantics, single source."""
    src = _panel_src()
    assert "this.MEMORY_TYPE_ENUM.indexOf(t) !== -1) { this.typeAddError = 'Enum types cannot" in src
    assert "(this.MEMORY_TYPE_ENUM.indexOf(t) !== -1) ? 'nc-details__chip'" in src
    assert "custom: this.MEMORY_TYPE_ENUM.indexOf(t) === -1" in src
    assert "MEMORY_TYPE_ENUM.indexOf(typeLoadedTypes.primary) === -1" in src
