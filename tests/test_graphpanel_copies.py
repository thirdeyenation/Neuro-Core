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


def test_no_hardcoded_fallback_chip_path():
    """KI-056 (WI-P77): the served content copy must not contain any hardcoded
    dev-environment project path as the chip fallback. The fallback must be
    ['default'] in both the try and catch branches of the localStorage IIFE."""
    text = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "projects/neuro_core" not in text
    assert "projects/neruo_core" not in text
    assert text.count("['default']") >= 2  # try + catch fallback branches


def test_chip_fallback_iife_resolves_to_default():
    """KI-056 (WI-P77): execute the actual chip-list IIFE logic headlessly.
    Empty/corrupt localStorage must resolve to ['default'], never a hardcoded
    project path; a persisted non-empty list must be preserved."""
    import json
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available for headless JS execution")

    text = PANEL_CONTENT.read_text(encoding="utf-8")
    match = re.search(r"subdirs: \((.*?)\)\(\),", text, re.DOTALL)
    assert match, "chip-list IIFE not found in graph-panel.html"
    iife_src = match.group(1)
    assert "localStorage" in iife_src
    assert "document" not in iife_src, "IIFE must remain DOM-free"

    def run_iife(localStorage_value):
        """Execute the extracted IIFE with a stubbed localStorage via node."""
        script = (
            "const localStorage = { getItem: (k) => "
            + ("null" if localStorage_value is None else json.dumps(localStorage_value))
            + " };\n"
            "console.log(JSON.stringify((" + iife_src + ")()));"
        )
        result = subprocess.run(
            [node, "-e", script], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, f"node failed: {result.stderr}"
        return json.loads(result.stdout.strip())

    assert run_iife(None) == ["default"]
    assert run_iife("") == ["default"]
    assert run_iife("not json") == ["default"]
    assert run_iife(json.dumps([])) == ["default"]
    assert run_iife(json.dumps(["default", "projects/x"])) == [
        "default",
        "projects/x",
    ]


def test_shell_component_target_is_plugin_served():
    """The extensions/ shell's x-component target must exist and be under the
    plugin webui/ dir served by the plugin-asset route."""
    shell = PANEL_SHELL_TEXT = (PLUGIN_ROOT / "extensions/webui/right-canvas-panels/graph-panel.html").read_text(encoding="utf-8")
    marker = '<x-component path="/usr/plugins/neuro_core/webui/right-canvas-panels/graph-panel.html" mode="canvas">'
    assert marker in shell
    assert PANEL_CONTENT.is_file()


def test_webui_copy_is_the_content_source_not_a_duplicate():
    """The webui/ copy is the 29KB content source (not a shell); it must contain
    the x-data graph panel and its serving chain (graph-store.js was removed in WI-P11 as dormant divergent code)."""
    text = PANEL_CONTENT.read_text(encoding="utf-8")
    assert 'x-data=' in text
    assert 'nc-cy' in text  # cytoscape container
    assert len(text) > 10000
