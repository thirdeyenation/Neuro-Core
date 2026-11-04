"""WI-P112-KI061AB-INSPECTOR-ICON-POLISH: tests pinning the three confirmed
changes, updated for the WI-P113 pane-control redesign (HITL-ratified
mechanism replacement; every changed/removed pin is enumerated in the WI-P113
implementation report — no silent removals).

Covers the implementation in webui/right-canvas-panels/graph-panel.html:

* KI-061-AB — node selection (cytoscape node tap; search results are rendered
  as graph nodes, so the tap handler is the panel's single node-selection
  site) must call revealInspector(), which reveals the inspector unless the
  user has explicitly toggled the Inspector off (WI-P113 harmonization —
  replaces the WI-P112 auto-un-yield semantics; no yield fighting).
* Toggle icon — REMOVED in the WI-P113 rev2 HITL decision: the pane-control
  button and popover were removed entirely, so the former toggle-icon pins
  are retired (enumerated in the WI-P113 implementation report rev 2).
* Manual Reboot — button sits AFTER the Settings gear (separated from Refresh
  and the pane control), uses an inline SVG power symbol (circle-arc + line)
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


# ------------------------------------------------- KI-061-AB: node-click reveal


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


def test_ki061ab_reveal_respects_explicit_inspector_off(src):
    """revealInspector must be a no-op when the user explicitly toggled the
    Inspector off (WI-P113 harmonization replacing the WI-P112 un-yield
    semantics — explicit choice beats reveal)."""
    m = re.search(r"revealInspector\(\) \{(.*?)\},", src, re.DOTALL)
    assert m, "revealInspector not found"
    assert "if (!this.paneUser.inspector) return;" in m.group(1)


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available for headless JS execution")
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return result.stdout.strip().splitlines()[-1]


def test_ki061ab_select_node_with_inspector_off_headless(src):
    """Headless: with the Inspector explicitly toggled off, node selection
    (revealInspector via the tap-handler path) must NOT re-enable it; with
    the Inspector enabled, selection is a safe no-op."""
    m = re.search(r"revealInspector\(\) \{(.*?)\},", src, re.DOTALL)
    assert m, "revealInspector not found"
    reveal = m.group(1).replace("this.", "self.")
    script = (
        "const log = [];\n"
        "const self = { paneUser: { search: true, graph: true, inspector: false }, "
        "paneWidthOff: { search: false, graph: false, inspector: false } };\n"
        "function revealInspector() { " + reveal + " }\n"
        "// select node while Inspector explicitly off -> must stay off\n"
        "revealInspector();\n"
        "log.push(JSON.parse(JSON.stringify(self.paneUser)));\n"
        "// enable Inspector, select again -> stays enabled (safe no-op)\n"
        "self.paneUser.inspector = true;\n"
        "revealInspector(); log.push(JSON.parse(JSON.stringify(self.paneUser)));\n"
        "console.log(JSON.stringify(log));"
    )
    log = json.loads(_run_node(script))
    assert log[0]["inspector"] is False, "explicit-off Inspector was overridden"
    assert log[1]["inspector"] is True


# ------------------------------------------------- reboot button pins


def test_reboot_button_after_settings_gear(src):
    """Manual Reboot must sit to the RIGHT of the Settings gear, separated
    from Refresh (the pane control was removed in rev 2)."""
    refresh_idx = src.find('@click="refresh()"')
    settings_idx = src.find('@click="openSettings()"')
    reboot_idx = src.find('@click="rebootPanel()"')
    assert -1 < refresh_idx < settings_idx < reboot_idx


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
