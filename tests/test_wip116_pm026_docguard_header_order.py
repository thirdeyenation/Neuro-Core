"""WI-P116 (PM-026) — doc-guard coverage for header-order / UI-affordance wording.

WI-P113 rev 2 exposed a coverage gap: stale manual-reboot header-order
references in docs/configuration.md and webui/help/configuration.html passed
doc-guard both before and after the fix, because no test pinned the documented
header order against the implemented order.

This module pins:

* the implemented graph-panel header order, derived from the panel HTML
  source (webui/right-canvas-panels/graph-panel.html) at test time —
  pane-visibility toggles (Search, Graph, Inspector), then Refresh,
  Settings, Manual Reboot;
* the documented header order in docs/configuration.md and
  webui/help/configuration.html against that implemented order, so a stale
  doc line fails with a clear message;
* the cheaply verifiable affordance-wording claims the docs make about the
  same header ("after the Settings gear", Refresh "left of the Settings gear").

This is a coverage extension of the documentation guard
(tests/test_wip71_prerelease_leak_pin.py, tests/test_wip101_docguard_runtime.py),
not a framework. Static source analysis only; no fixtures, no live data writes.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PANEL_SOURCE = PLUGIN_ROOT / "webui" / "right-canvas-panels" / "graph-panel.html"
DOC_MD = PLUGIN_ROOT / "docs" / "configuration.md"
DOC_HTML = PLUGIN_ROOT / "webui" / "help" / "configuration.html"

# The ratified header order (WI-P112 / WI-P113 state): pane-visibility
# toggles first, then Refresh, Settings, Manual Reboot.
EXPECTED_PANE_TOGGLES = ["Search", "Graph", "Inspector"]
EXPECTED_TAIL = ["Refresh", "Settings", "Manual Reboot"]

# The exact documented order phrase both doc surfaces must carry.
EXPECTED_DOC_PHRASE = (
    "header order: pane-visibility toggles, Refresh, Settings, Manual Reboot"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _implemented_header_order() -> list[str]:
    """Derive the implemented header-button order from the panel HTML source.

    Each button in the ``nc-header__actions`` block is classified by its
    title/aria-label attributes: pane toggles by their ``Toggle X pane``
    title, Refresh and Settings by their titles, and the manual reboot by
    its ``Manual reboot`` title prefix.
    """
    html = _read(PANEL_SOURCE)
    actions_match = re.search(
        r'<div class="nc-header__actions">(.*?)</div>\s*</div>',
        html,
        re.DOTALL,
    )
    assert actions_match, (
        "graph-panel.html: could not locate the nc-header__actions block — "
        "the header markup changed; update this pin to the new structure"
    )
    block = actions_match.group(1)
    buttons = re.findall(r"<button\b(.*?)</button>", block, re.DOTALL)
    assert buttons, "graph-panel.html: no header buttons found"

    order: list[str] = []
    for attrs in buttons:
        if 'title="Toggle Search pane"' in attrs:
            order.append("Search")
        elif 'title="Toggle Graph pane"' in attrs:
            order.append("Graph")
        elif 'title="Toggle Inspector pane"' in attrs:
            order.append("Inspector")
        elif 'title="Refresh"' in attrs:
            order.append("Refresh")
        elif 'title="Settings"' in attrs:
            order.append("Settings")
        elif re.search(r'title="Manual reboot', attrs):
            order.append("Manual Reboot")
        else:
            pytest.fail(
                "graph-panel.html: unrecognized header button — classify it in "
                "this pin so the documented header order stays guarded: "
                f"{attrs[:120]!r}"
            )
    return order


def _doc_header_order_phrase(text: str, surface: str) -> str:
    """Extract the documented 'header order: ...' phrase from a doc surface.

    Whitespace (including line wrapping) is normalized so the pin compares
    the documented ORDER, not the doc's line layout.
    """
    match = re.search(r"header order:\s*([^.<)]+)", text)
    assert match, (
        f"{surface}: no 'header order: ...' statement found — the documented "
        "header order must be stated and guarded, not dropped"
    )
    normalized = " ".join(match.group(1).split())
    return normalized.strip()


# ---------------------------------------------------------------------------
# Implemented order (derived from source, pinned so source drift is caught)
# ---------------------------------------------------------------------------


class TestImplementedHeaderOrder:
    def test_implemented_order_matches_ratified_state(self):
        order = _implemented_header_order()
        pane = [b for b in order if b in EXPECTED_PANE_TOGGLES]
        tail = [b for b in order if b in EXPECTED_TAIL]
        assert pane == EXPECTED_PANE_TOGGLES, (
            "graph-panel.html: pane-toggle order changed — update the "
            "documented header order in docs/configuration.md and "
            "webui/help/configuration.html together with this pin"
        )
        assert tail == EXPECTED_TAIL, (
            "graph-panel.html: Refresh/Settings/Manual-Reboot order changed — "
            "update the documented header order in docs/configuration.md and "
            "webui/help/configuration.html together with this pin"
        )
        # Full sequence: all pane toggles first, then the tail, nothing else.
        assert order == EXPECTED_PANE_TOGGLES + EXPECTED_TAIL, (
            f"graph-panel.html: unexpected header button sequence {order!r}"
        )


# ---------------------------------------------------------------------------
# Documented order vs implemented order (the PM-026 gap)
# ---------------------------------------------------------------------------


class TestDocumentedHeaderOrder:
    @pytest.mark.parametrize("surface,path", [
        ("docs/configuration.md", DOC_MD),
        ("webui/help/configuration.html", DOC_HTML),
    ])
    def test_documented_order_matches_implemented(self, surface, path):
        doc_text = _read(path)
        documented = _doc_header_order_phrase(doc_text, surface)
        assert documented == EXPECTED_DOC_PHRASE.split("header order: ", 1)[1], (
            f"{surface}: documented header order is stale.\n"
            f"  documented: {documented!r}\n"
            f"  implemented: {', '.join(_implemented_header_order())!r}\n"
            "Update the documentation to match the implemented panel header."
        )

    @pytest.mark.parametrize("surface,path", [
        ("docs/configuration.md", DOC_MD),
        ("webui/help/configuration.html", DOC_HTML),
    ])
    def test_reboot_documented_after_settings(self, surface, path):
        """Affordance wording: the reboot control is documented as living
        after the Settings gear — pin that the implemented source agrees."""
        doc_text = _read(path)
        assert "after the Settings gear" in doc_text, (
            f"{surface}: the 'after the Settings gear' affordance wording for "
            "the manual reboot control was removed or reworded — keep it "
            "consistent with the implemented header or update this pin"
        )
        order = _implemented_header_order()
        assert order.index("Manual Reboot") > order.index("Settings"), (
            "graph-panel.html: Manual Reboot is no longer after Settings, "
            f"but {surface} still documents it that way"
        )

    def test_refresh_documented_left_of_settings(self):
        """Affordance wording: webui/help/configuration.html describes the
        Refresh button as sitting left of the Settings gear."""
        doc_text = _read(DOC_HTML)
        assert "left of the" in doc_text and "Settings gear" in doc_text, (
            "webui/help/configuration.html: the Refresh 'left of the Settings "
            "gear' affordance wording was removed or reworded — keep it "
            "consistent with the implemented header or update this pin"
        )
        order = _implemented_header_order()
        assert order.index("Refresh") < order.index("Settings"), (
            "graph-panel.html: Refresh is no longer left of Settings, but "
            "webui/help/configuration.html still documents it that way"
        )
