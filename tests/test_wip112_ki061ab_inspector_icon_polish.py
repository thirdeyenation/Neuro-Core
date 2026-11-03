"""WI-P112-KI061AB-INSPECTOR-ICON-POLISH: tests pinning the three confirmed
changes on top of the WI-P110 pane-affordance implementation.

Covers the implementation in webui/right-canvas-panels/graph-panel.html:

* KI-061-AB — node selection (cytoscape node tap; search results are rendered
  as graph nodes, so the tap handler is the panel's single node-selection
  site) must auto-un-yield the details pane: selecting a node always shows
  its inspector, at any width. Manual toggle behavior (restoreMiddlePane)
  is unchanged, and pane state stays consistent across a subsequent
  collapse/restore cycle.
* Toggle icon — the pane-toggle button uses an inline SVG panel-layout glyph
  (rectangle divided into three vertical sections, middle collapsed) stroked
  with currentColor; NOT a material-symbols ligature span, NOT the old
  unicode glyph.
* Manual Reboot — button sits AFTER the Settings gear (separated from Refresh
  and the pane toggle), uses an inline SVG power symbol (circle-arc + line)
  stroked with currentColor, and carries theme-aware red caution styling
  (icon color var(--color-error-text), red border on hover, never a filled
  background). The Refresh icon is untouched.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL_CONTENT = PLUGIN_ROOT / "webui/right-canvas-panels/graph-panel.html"


@pytest.fixture(scope="module")
def src() -> str:
    return PANEL_CONTENT.read_text(encoding="utf-8")


# ------------------------------------------------- KI-061-AB: auto-un-yield


def test_ki061ab_node_tap_calls_reveal_inspector(src):
    """The cytoscape node tap handler (the panel's single node-selection site)
    must call revealInspector() after opening the inspector."""
    m = re.search(r"this\.cy\.on\('tap', 'node', function\(evt\) \{(.*?)\}\);", src, re.DOTALL)
    assert m, "cytoscape node tap handler not found"
    body = m.group(1)
    open_pos = body.find("self.inspectOpen = true")
    reveal_pos = body.find("self.revealInspector()")
    assert open_pos != -1, "tap handler must set inspectOpen = true"
    assert reveal_pos != -1, "tap handler must call self.revealInspector()"
    assert open_pos < reveal_pos, "revealInspector must follow inspector open"


def test_ki061ab_reveal_inspector_clears_both_yields(src):
    """revealInspector must clear both yields (searchYielded implies
    detailsYielded under the C2 yield order, so clearing details alone would
    create an unreachable state)."""
    m = re.search(r"revealInspector\(\) \{(.*?)\},", src, re.DOTALL)
    assert m, "revealInspector not found"
    body = m.group(1)
    assert "searchYielded = false" in body
    assert "detailsYielded = false" in body


def test_ki061ab_manual_toggle_unchanged(src):
    """restoreMiddlePane semantics are untouched: Details first, then Search."""
    m = re.search(r"restoreMiddlePane\(\) \{(.*?)\},", src, re.DOTALL)
    assert m, "restoreMiddlePane not found"
    body = m.group(1)
    details_pos = body.find("detailsYielded = true")
    search_pos = body.find("searchYielded = true")
    assert -1 < details_pos < search_pos


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available for headless JS execution")
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return result.stdout.strip().splitlines()[-1]


def test_ki061ab_select_node_while_yielded_reveals_inspector_headless(src):
    """Headless: with both panes yielded (fully collapsed middle), a node
    selection (revealInspector via the tap-handler path) must reveal the
    details pane; a subsequent collapse/restore cycle must leave state
    consistent (no unreachable states, no-op at the fully-collapsed end)."""
    m = re.search(r"revealInspector\(\) \{(.*?)\},", src, re.DOTALL)
    assert m, "revealInspector not found"
    reveal = m.group(1).replace("this.", "self.")
    m2 = re.search(r"restoreMiddlePane\(\) \{(.*?)\},", src, re.DOTALL)
    assert m2, "restoreMiddlePane not found"
    restore = m2.group(1).replace("this.", "self.")
    script = (
        "const log = [];\n"
        "const self = { detailsYielded: true, searchYielded: true };\n"
        "function revealInspector() { " + reveal + " }\n"
        "function restoreMiddlePane() { " + restore + " }\n"
        "// select node while yielded -> inspector must be revealed\n"
        "revealInspector();\n"
        "log.push([self.detailsYielded, self.searchYielded]);\n"
        "// subsequent collapse/restore cycle must stay consistent\n"
        "restoreMiddlePane(); log.push([self.detailsYielded, self.searchYielded]);\n"
        "restoreMiddlePane(); log.push([self.detailsYielded, self.searchYielded]);\n"
        "restoreMiddlePane(); log.push([self.detailsYielded, self.searchYielded]);\n"
        "// selection again from the fully-collapsed state\n"
        "revealInspector(); log.push([self.detailsYielded, self.searchYielded]);\n"
        "console.log(JSON.stringify(log));"
    )
    log = json.loads(_run_node(script))
    assert log == [
        [False, False],  # node selection while yielded -> inspector visible
        [True, False],   # manual collapse: details first
        [True, True],    # then search
        [True, True],    # third click is a no-op (unchanged manual behavior)
        [False, False],  # selection from fully-collapsed state reveals again
    ]


# ------------------------------------------------- toggle icon pins


def test_toggle_icon_is_inline_svg_currentcolor(src):
    """The pane-toggle button must contain an inline SVG stroked with
    currentColor (dark/light theming), shaped as a panel-layout glyph
    (outer rect + internal vertical dividers)."""
    m = re.search(
        r'nc-header__btn--pane-toggle"[^>]*>(.*?)</button>', src, re.DOTALL
    )
    assert m, "pane-toggle button not found"
    inner = m.group(1)
    assert "<svg" in inner, "toggle icon must be an inline SVG"
    assert "currentColor" in inner, "toggle SVG must use currentColor"
    assert "<rect" in inner and "<line" in inner, (
        "toggle SVG must be a panel-layout glyph (rect + vertical dividers)"
    )


def test_toggle_icon_no_ligature_span_no_unicode_glyph(src):
    """Standing no-new-ligature-span pin: the toggle must not use a
    material-symbols ligature span, and the old unicode glyph is gone."""
    m = re.search(
        r'nc-header__btn--pane-toggle"[^>]*>(.*?)</button>', src, re.DOTALL
    )
    assert m, "pane-toggle button not found"
    inner = m.group(1)
    assert "material-symbols" not in inner
    assert "\u27f2" not in src, "old unicode toggle glyph must be removed"


# ------------------------------------------------- reboot button pins


def test_reboot_button_after_settings_gear(src):
    """Manual Reboot must sit to the RIGHT of the Settings gear, separated
    from Refresh and the pane toggle."""
    toggle_idx = src.find("nc-header__btn--pane-toggle")
    refresh_idx = src.find('@click="refresh()"')
    settings_idx = src.find('@click="openSettings()"')
    reboot_idx = src.find('@click="rebootPanel()"')
    assert -1 < toggle_idx < refresh_idx < settings_idx < reboot_idx


def test_reboot_icon_is_power_symbol_inline_svg(src):
    """The reboot icon must be an inline SVG power symbol (vertical line +
    circle arc) stroked with currentColor — not a ligature span."""
    m = re.search(r'@click="rebootPanel\(\)"[^>]*>(.*?)</button>', src, re.DOTALL)
    assert m, "reboot button not found"
    inner = m.group(1)
    assert "<svg" in inner
    assert "material-symbols" not in inner
    assert "currentColor" in inner
    # power-symbol geometry: vertical line plus a large arc path
    assert re.search(r'<path d="M12 3v8"', inner), "power symbol vertical line"
    assert re.search(r'<path d="M6\.2 6\.5a8 8 0 1 0 11\.6 0"', inner), "power symbol arc"


def test_reboot_caution_styling_theme_aware_red(src):
    """The reboot button must carry the caution class: theme-aware red icon
    color (var(--color-error-text)) and a red border on hover — icon color
    only, never a filled background."""
    assert "nc-header__btn nc-header__btn--reboot" in src
    assert ".nc-header__btn--reboot { color: var(--color-error-text); }" in src
    hover = re.search(r"\.nc-header__btn--reboot:hover \{(.*?)\}", src, re.DOTALL)
    assert hover, "reboot hover rule missing"
    assert "border-color: var(--color-error-text)" in hover.group(1)
    assert "background: transparent" in hover.group(1)


def test_refresh_icon_untouched(src):
    """The Refresh button keeps its material-symbols refresh icon."""
    m = re.search(r'@click="refresh\(\)"[^>]*>(.*?)</button>', src, re.DOTALL)
    assert m, "refresh button not found"
    assert '<span class="material-symbols-outlined">refresh</span>' in m.group(1)