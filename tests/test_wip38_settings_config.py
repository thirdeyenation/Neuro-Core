"""Pins for the WI-P38-SETTINGS-CONFIG-SURFACE settings surface (D-NC1-092 ratified
 design; 21-key contract per D-NC1-093; R2 per D-NC1-095).

 These tests are static content pins: there is no server-side key validation for
 plugin config (api/plugins.py), so webui/config.html is the sole key-correctness
 boundary. The pins enforce that boundary mechanically.
"""
from __future__ import annotations

import re
from pathlib import Path

PLUGIN = Path("/a0/usr/plugins/neuro_core")
CONFIG_HTML = PLUGIN / "webui" / "config.html"
HELP_HTML = PLUGIN / "webui" / "help" / "configuration.html"
DEFAULTS_YAML = PLUGIN / "default_config.yaml"
PANEL = PLUGIN / "webui" / "right-canvas-panels" / "graph-panel.html"
SHELL = PLUGIN / "extensions" / "webui" / "right-canvas-panels" / "graph-panel.html"


def _config_html_text() -> str:
    return CONFIG_HTML.read_text(encoding="utf-8")


def _defaults_keys() -> set[str]:
    """Top-level keys of default_config.yaml (comments/section headers excluded)."""
    keys = set()
    for line in DEFAULTS_YAML.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([a-z_]+):", line)
        if m:
            keys.add(m.group(1))
    return keys


def test_default_config_has_exactly_27_keys():
    keys = _defaults_keys()
    assert len(keys) == 27, f"expected exactly 27 keys, got {len(keys)}: {sorted(keys)}"


def test_every_x_model_bound_key_exists_in_default_config():
    """Key-correctness boundary pin: every x-model / x-model.number binding in
    config.html must reference a key that exists in default_config.yaml."""
    keys = _defaults_keys()
    text = _config_html_text()
    bound = set(re.findall(r"x-model(?:\.number)?=\"(?:this\.)?config\.([a-z_]+)\"", text))
    assert len(bound) == 27, f"expected 27 bound keys, got {len(bound)}: {sorted(bound)}"
    missing = bound - keys
    assert not missing, f"config.html binds keys absent from default_config.yaml: {sorted(missing)}"
    # and the reconciliation is exact: all 27 default keys are surfaced
    assert bound == keys


def test_preset_weights_sum_to_one_per_preset():
    text = _config_html_text()
    presets = re.findall(
        r"(\w+):\s*\{\s*similarity_weight:\s*([\d.]+),\s*importance_weight:\s*([\d.]+),"
        r"\s*recency_weight:\s*([\d.]+)\s*\}",
        text,
    )
    assert len(presets) >= 3, f"expected at least 3 presets, found {len(presets)}"
    named = {name: (float(s), float(i), float(r)) for name, s, i, r in presets}
    # Confirmed preset points (WI-P38 grounding): exact values, exact keys
    assert named["balanced"] == (0.5, 0.3, 0.2)
    assert named["freshest"] == (0.2, 0.3, 0.5)
    assert named["importance"] == (0.3, 0.5, 0.2)
    for name, triple in named.items():
        total = sum(triple)
        assert abs(total - 1.0) < 1e-9, f"preset '{name}' sums to {total}, expected 1.0"


def test_freshness_points_match_named_values():
    text = _config_html_text()
    points = re.findall(
        r"(\w+):\s*\{\s*decay_interval_hours:\s*(\d+),\s*importance_decay_rate:\s*([\d.]+)\s*\}",
        text,
    )
    named = {name: (int(h), float(r)) for name, h, r in points}
    assert len(named) >= 3
    assert named["slow"] == (72, 0.01)
    assert named["standard"] == (24, 0.02)
    assert named["fast"] == (8, 0.05)
    # named points must be real default_config keys
    keys = _defaults_keys()
    assert {"decay_interval_hours", "importance_decay_rate"} <= keys


def test_basic_view_has_five_controls():
    text = _config_html_text()
    for bound_key in (
        "decay_enabled",
        "contradiction_detection_enabled",
        "graph_analytics_enabled",
        "database_path",
    ):
        assert f'x-model="config.{bound_key}"' in text or f"config.{bound_key}" in text
    # the two derived selectors (preset + freshness) are wired as x-model getters
    assert 'x-model="activePreset"' in text
    assert 'x-model="freshnessSel"' in text
    # exactly five Basic control inputs before the Advanced toggle
    assert text.count("Advanced Settings") >= 1
    # plain-language labels present
    for label in ("Memory fading", "Freshness speed", "Contradiction detection", "Graph insights", "Retrieval balance"):
        assert label in text, f"Basic label missing: {label}"


def test_advanced_region_groups_present():
    text = _config_html_text()
    for group in (
        "Lifecycle internals",
        "Contradiction internals",
        "Graph analytics internals",
        "Retrieval internals",
        "Storage &amp; recovery",
    ):
        assert group in text, f"Advanced group missing: {group}"
    # per-group restore + window-level reset, both confirm-based
    assert "restoreGroup" in text
    assert "resetAll" in text
    assert "Reset all to defaults" in text


def test_coherence_rule_custom_display_wired():
    """Editing any raw weight must flip the preset display to Custom: the getter
    derives from the raw keys only, and the custom option exists but is disabled
    (display-only)."""
    text = _config_html_text()
    assert "get activePreset()" in text
    assert "get freshnessSel()" in text
    assert text.count("return 'custom'") >= 2
    # custom option present and disabled in both selectors
    assert text.count('<option value="custom" disabled>') == 2


def test_reboot_failsafe_in_graph_panel_without_retry_semantics_change():
    """WI-P38 reboot failsafe: present in the served panel content copy, alongside
    the existing refresh control, WITHOUT altering the pinned withCsrfRetry
    six-site structure (test_wip29_403_recovery.py pins it independently)."""
    panel = PANEL.read_text(encoding="utf-8")
    shell = SHELL.read_text(encoding="utf-8")
    # serving chain untouched: shell still delegates to the content copy
    assert '<x-component path="/usr/plugins/neuro_core/webui/right-canvas-panels/graph-panel.html" mode="canvas">' in shell
    # reboot function + button present, same deliberate token reset as refresh();
    # icon is an inline SVG (AGENTS.md ligature rule — no new ligature spans)
    assert "rebootPanel() { this._csrfToken = null" in panel
    assert '@click="rebootPanel()"' in panel
    assert re.search(r'@click="rebootPanel\(\)"[^>]*><svg[^>]*aria-hidden="true"', panel)
    # the WI-P29 pins still hold verbatim
    assert "async withCsrfRetry(doCall)" in panel
    # WI-P52-KI031: six original + three additive memory_names sites, same shared wrapper
    # WI-P53-KI030: + two additive memory_edit sites (openEdit prefill GET, saveEdits POST)
    # WI-P58-KI041 (guard-pin maintenance, disclosed): + one additive memory_edit GET
    # site (loadInspectScores Details-plate score load, read-only display path),
    # same shared wrapper.
    assert panel.count("withCsrfRetry(async () => fetch") == 12
    # no second retry wrapper was introduced
    assert panel.count("async withCsrfRetry") == 1


def test_help_surface_exists_and_is_referenced():
    assert HELP_HTML.is_file(), "webui/help/configuration.html missing"
    help_text = HELP_HTML.read_text(encoding="utf-8")
    # all territory anchors exist (config.html links target these)
    for anchor in ("lifecycle", "contradiction", "graph-analytics", "retrieval", "recall-shaping", "storage-recovery", "manual-reboot"):
        assert f'id="{anchor}"' in help_text, f"help anchor missing: #{anchor}"
    # key-level coverage: all 21 keys named in the help surface
    keys = _defaults_keys()
    for key in keys:
        assert key in help_text, f"help surface does not cover key: {key}"
    # config.html links to it with target=_blank and fragment anchors
    text = _config_html_text()
    refs = re.findall(r'href="(/usr/plugins/neuro_core/webui/help/configuration.html#([a-z-]+))" target="_blank"', text)
    assert len(refs) >= 7, f"expected >=7 Learn More links, got {len(refs)}"
    linked_anchors = {a for _, a in refs}
    assert linked_anchors <= {"lifecycle", "contradiction", "graph-analytics", "retrieval", "recall-shaping", "storage-recovery", "manual-reboot"}


def test_docs_configuration_covers_all_27_keys():
    docs = (PLUGIN / "docs" / "configuration.md").read_text(encoding="utf-8")
    for key in _defaults_keys():
        assert f"`{key}`" in docs, f"docs/configuration.md missing key: {key}"
    # territory anchors mirror the help surface
    for heading in ("Lifecycle internals", "Contradiction internals", "Graph analytics internals", "Retrieval internals", "Storage & recovery"):
        assert heading in docs


def _xdata_object_literal() -> str:
    """Extract the full x-data object literal from config.html (brace-matching
    scan that tracks quote state), as a standalone JS expression string."""
    text = _config_html_text()
    start = text.index('x-data="') + len('x-data="')
    assert text[start] == "{", "x-data does not start with an object literal"
    depth = 0
    in_str = None
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if c == in_str:
                in_str = None
        elif c in ('"', "'"):
            in_str = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError("no matching closing brace for x-data object literal")


def test_nc_defaults_xdata_object_parses_as_javascript():
    """KI-035 remediation pin: the x-data object literal in config.html must be
    syntactically valid JavaScript (a missing comma once broke the whole
    settings-page Alpine scope). Uses node --check on the extracted literal."""
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    assert node, "node is required to pin the config.html x-data JS syntax"
    obj = _xdata_object_literal()
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write("(" + obj + ");\n")
        tmp = f.name
    try:
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
    finally:
        Path(tmp).unlink(missing_ok=True)
    assert r.returncode == 0, f"config.html x-data object is not valid JS: {r.stderr}"


def test_nc_defaults_recall_shaping_enabled_matches_default_config_true():
    """KI-035 remediation pin (final state per D-NC1-113 fast-follow): NC_DEFAULTS
    carries the shipped default recall_shaping_enabled: true — the KI-036
    sanitizer (update_documents metadata round-trip) shipped in 2461102 —
    and matches default_config.yaml."""
    text = _config_html_text()
    m = re.search(r"recall_shaping_enabled:\s*(true|false)", text)
    assert m, "NC_DEFAULTS.recall_shaping_enabled not found in config.html"
    assert m.group(1) == "true", "NC_DEFAULTS.recall_shaping_enabled must match shipped default true (D-NC1-113 fast-follow, sanitizer shipped)"
    # coherence with the shipped config default
    yaml_val = re.search(r"^recall_shaping_enabled:\s*(true|false)", DEFAULTS_YAML.read_text(encoding="utf-8"), re.M)
    assert yaml_val, "recall_shaping_enabled not found in default_config.yaml"
    assert yaml_val.group(1) == m.group(1), "NC_DEFAULTS and default_config.yaml disagree on recall_shaping_enabled"
