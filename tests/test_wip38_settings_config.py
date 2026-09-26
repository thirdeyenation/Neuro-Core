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


def test_default_config_has_exactly_21_keys():
    keys = _defaults_keys()
    assert len(keys) == 21, f"expected exactly 21 keys, got {len(keys)}: {sorted(keys)}"


def test_every_x_model_bound_key_exists_in_default_config():
    """Key-correctness boundary pin: every x-model / x-model.number binding in
    config.html must reference a key that exists in default_config.yaml."""
    keys = _defaults_keys()
    text = _config_html_text()
    bound = set(re.findall(r"x-model(?:\.number)?=\"(?:this\.)?config\.([a-z_]+)\"", text))
    assert len(bound) == 21, f"expected 21 bound keys, got {len(bound)}: {sorted(bound)}"
    missing = bound - keys
    assert not missing, f"config.html binds keys absent from default_config.yaml: {sorted(missing)}"
    # and the reconciliation is exact: all 21 default keys are surfaced
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
    assert panel.count("withCsrfRetry(async () => fetch") == 6
    # no second retry wrapper was introduced
    assert panel.count("async withCsrfRetry") == 1


def test_help_surface_exists_and_is_referenced():
    assert HELP_HTML.is_file(), "webui/help/configuration.html missing"
    help_text = HELP_HTML.read_text(encoding="utf-8")
    # all territory anchors exist (config.html links target these)
    for anchor in ("lifecycle", "contradiction", "graph-analytics", "retrieval", "storage-recovery", "manual-reboot"):
        assert f'id="{anchor}"' in help_text, f"help anchor missing: #{anchor}"
    # key-level coverage: all 21 keys named in the help surface
    keys = _defaults_keys()
    for key in keys:
        assert key in help_text, f"help surface does not cover key: {key}"
    # config.html links to it with target=_blank and fragment anchors
    text = _config_html_text()
    refs = re.findall(r'href="(/usr/plugins/neuro_core/webui/help/configuration.html#([a-z-]+))" target="_blank"', text)
    assert len(refs) >= 6, f"expected >=6 Learn More links, got {len(refs)}"
    linked_anchors = {a for _, a in refs}
    assert linked_anchors <= {"lifecycle", "contradiction", "graph-analytics", "retrieval", "storage-recovery", "manual-reboot"}


def test_docs_configuration_covers_all_21_keys():
    docs = (PLUGIN / "docs" / "configuration.md").read_text(encoding="utf-8")
    for key in _defaults_keys():
        assert f"`{key}`" in docs, f"docs/configuration.md missing key: {key}"
    # territory anchors mirror the help surface
    for heading in ("Lifecycle internals", "Contradiction internals", "Graph analytics internals", "Retrieval internals", "Storage & recovery"):
        assert heading in docs
