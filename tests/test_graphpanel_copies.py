"""Pinning tests for the graph-panel.html serving chain (WI-P5B-GRAPHPANEL-COPIES).

Grounded serving-path verdict: extensions/webui/right-canvas-panels/graph-panel.html
is the live surface registration shell; its <x-component path=...> loads
/usr/plugins/neuro_core/webui/right-canvas-panels/graph-panel.html (the content copy)
through the plugin-asset route /usr/plugins/<plugin>/<asset> (helpers/ui_server.py:
_serve_plugin_asset serves files under the plugin's webui/ or extensions/webui/ dir).
The two files are not divergent duplicates: shell (surface wrapper) vs content.
"""

from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL_SHELL = PLUGIN_ROOT / "extensions/webui/right-canvas-panels/graph-panel.html"
PANEL_CONTENT = PLUGIN_ROOT / "webui/right-canvas-panels/graph-panel.html"


def test_no_misspelled_fallback_chip_path():
    """The served content copy must not contain the legacy misspelled path."""
    text = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "projects/neruo_core" not in text
    assert "projects/neuro_core" in text


def test_shell_component_target_is_plugin_served():
    """The extensions/ shell's x-component target must exist and be under the
    plugin webui/ dir served by the plugin-asset route."""
    shell = PANEL_SHELL_TEXT = (PLUGIN_ROOT / "extensions/webui/right-canvas-panels/graph-panel.html").read_text(encoding="utf-8")
    marker = '<x-component path="/usr/plugins/neuro_core/webui/right-canvas-panels/graph-panel.html" mode="canvas">'
    assert marker in shell
    assert PANEL_CONTENT.is_file()


def test_webui_copy_is_the_content_source_not_a_duplicate():
    """The webui/ copy is the 29KB content source (not a shell); it must contain
    the x-data graph panel and be referenced by graph-store.js docs."""
    text = PANEL_CONTENT.read_text(encoding="utf-8")
    assert 'x-data=' in text
    assert 'nc-cy' in text  # cytoscape container
    assert len(text) > 10000
