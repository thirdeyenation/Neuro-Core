'''Tests for WI-P106-PM022-VALIDATION-FILTER-SINGLE-SOURCE — single source of
truth for the validation-status enumeration shared by the backend metadata
model and the graph panel's Validation Status filter (PM-022, replicating
the WI-P97-PM011 mechanism for memory types):

  - helpers/metadata.py ValidationStatus is the backend authority (4 values).
  - webui/right-canvas-panels/graph-panel.html carries exactly ONE in-panel
    validation-status literal: the VALIDATION_STATUS constant in the x-data
    scope.
  - The panel's Validation Status filter (Advanced Filters x-for) derives
    from that constant.
  - DRIFT GUARD: the panel constant equals the backend ValidationStatus
    enum. If the backend enum changes without updating the panel constant —
    or vice versa — this test fails.

The enumeration itself is NOT changed by this work item: no statuses
added/removed/renamed, no transition-matrix or filter-behavior changes.
'''

from __future__ import annotations

import re
from pathlib import Path

from usr.plugins.neuro_core.helpers.metadata import ValidationStatus

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PANEL = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"


def _panel_src():
    return PANEL.read_text(encoding="utf-8")


def _panel_validation_status_enum():
    """Extract the single VALIDATION_STATUS constant from the panel x-data."""
    src = _panel_src()
    m = re.search(r"VALIDATION_STATUS:\s*(\[[^\]]*\])", src)
    assert m, "VALIDATION_STATUS constant must exist in graph-panel.html x-data"
    return re.findall(r"'([^']+)'", m.group(1))


def test_drift_guard_panel_enum_equals_backend_enum():
    """PM-022 acceptance criterion: the panel's Validation Status filter
    enumeration equals the backend ValidationStatus enum. This is the
    standing drift guard."""
    panel_statuses = _panel_validation_status_enum()
    backend_statuses = [v.value for v in ValidationStatus]
    assert panel_statuses == backend_statuses, (
        "graph-panel.html VALIDATION_STATUS has drifted from the backend "
        "ValidationStatus enum (helpers/metadata.py). Update the panel "
        "constant to match the backend authority (panel follows the "
        "backend enum)."
    )


def test_panel_has_exactly_one_validation_status_literal():
    """Single-source invariant: the 4-status validation literal appears
    exactly once in the panel — inside the VALIDATION_STATUS constant. Any
    new inline duplication reintroduces the drift surface this work item
    removed."""
    src = _panel_src()
    literal = "['unvalidated','validated','disputed','deprecated']"
    assert src.count(literal) == 1
    # ...and that single occurrence is the constant definition itself.
    assert re.search(r"VALIDATION_STATUS:\s*" + re.escape(literal), src)


def test_filter_enumeration_derives_from_constant():
    """The Advanced Filters Validation Status checkbox group iterates the
    shared constant, not an inline literal."""
    src = _panel_src()
    assert src.count('x-for="s in VALIDATION_STATUS"') == 1
    # No inline-literal validation-status x-for remains anywhere in the panel.
    assert not re.search(
        r"x-for=\"s in \['unvalidated','validated','disputed','deprecated'\]\"",
        src,
    )


def test_validation_status_unchanged_four_values():
    """The enumeration itself is untouched by WI-P106: the backend enum
    still carries exactly the 4 v1 values, in order."""
    assert [v.value for v in ValidationStatus] == [
        "unvalidated",
        "validated",
        "disputed",
        "deprecated",
    ]
