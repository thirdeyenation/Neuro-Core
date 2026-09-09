"""Tests for the Neuro Core plugin manifest and default configuration.

Closes the test gap for KI-006 (plugin.yaml was a 3-line stub) and pins
the config contract established in WI-P3-MANIFEST-CONFIG:

- ``plugin.yaml`` carries the full framework manifest schema
  (``helpers.plugins.PluginMetadata`` fields) with valid values.
- ``default_config.yaml`` contains exactly the keys that are actually
  read at runtime — no dead keys, no undocumented keys.
- Lifecycle keys match ``helpers/lifecycle.py`` ``DEFAULT_CONFIG``.
- The API retrieval config builder (``api/context_graph.py``) reads the
  plugin config via ``get_plugin_config`` and falls back to the
  established effective defaults when config is unavailable.

Framework discovery itself (plugin listing through the live plugin
system) is verified under the framework runtime and recorded in the
work item's implementation report; the pytest environment stubs parts
of ``helpers`` and cannot boot the full plugin registry.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest
import yaml

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
# conftest.py inserts /a0/usr (its parents[3]) rather than /a0, so
# ``import usr...`` fails there; add the true framework root explicitly.
_A0_ROOT = Path(__file__).resolve().parents[4]
if str(_A0_ROOT) not in sys.path:
    sys.path.insert(0, str(_A0_ROOT))


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


class TestPluginManifest:
    @pytest.fixture()
    def manifest(self) -> dict:
        with open(_PLUGIN_ROOT / "plugin.yaml") as fh:
            data = yaml.safe_load(fh)
        assert isinstance(data, dict)
        return data

    def test_full_manifest_schema(self, manifest):
        """Manifest carries exactly the framework PluginMetadata fields."""
        expected = {
            "name",
            "title",
            "description",
            "version",
            "settings_sections",
            "per_project_config",
            "per_agent_config",
            "always_enabled",
        }
        assert set(manifest.keys()) == expected

    def test_name_matches_directory(self, manifest):
        assert manifest["name"] == "neuro_core"
        assert manifest["name"] == _PLUGIN_ROOT.name

    def test_identity_fields_non_empty(self, manifest):
        assert manifest["title"].strip()
        assert manifest["description"].strip()

    def test_version_format(self, manifest):
        parts = str(manifest["version"]).split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_settings_sections_valid(self, manifest):
        valid = {"agent", "external", "mcp", "developer", "backup"}
        sections = manifest["settings_sections"]
        assert isinstance(sections, list) and sections
        assert set(sections) <= valid

    def test_scoped_config_flags_true(self, manifest):
        """docs/configuration.md declares per-project/per-agent overrides;
        the manifest must declare the same contract."""
        assert manifest["per_project_config"] is True
        assert manifest["per_agent_config"] is True

    def test_always_enabled_false(self, manifest):
        assert manifest["always_enabled"] is False


# ---------------------------------------------------------------------------
# Default config contract
# ---------------------------------------------------------------------------


# Grounded runtime-read inventory (WI-P3-MANIFEST-CONFIG grounding notes):
# - database_path: tools/neuro_capture.py, neuro_retrieve.py, neuro_validate.py
# - lifecycle keys: helpers/lifecycle.py DEFAULT_CONFIG + contradiction_llm_enabled
#   and contradiction_interval_hours (job_loop/_30_contradiction_detection.py)
# - retrieval keys: helpers/retrieval.py via api/context_graph.py
_EXPECTED_CONFIG_KEYS = {
    "database_path",
    "decay_enabled",
    "decay_interval_hours",
    "importance_decay_rate",
    "contradiction_detection_enabled",
    "contradiction_batch_size",
    "contradiction_similarity_threshold",
    "contradiction_llm_enabled",
    "contradiction_interval_hours",
    "graph_analytics_enabled",
    "graph_analytics_top_pct",
    "graph_analytics_boost",
    "episode_boundary_hours",
    "episode_min_memories",
    "graph_max_hops",
    "graph_neighbors_max",
    "similarity_weight",
    "importance_weight",
    "recency_weight",
    "semantic_limit",
    "semantic_threshold",
}


class TestDefaultConfig:
    @pytest.fixture()
    def config(self) -> dict:
        with open(_PLUGIN_ROOT / "default_config.yaml") as fh:
            data = yaml.safe_load(fh)
        assert isinstance(data, dict)
        return data

    def test_exact_key_set(self, config):
        """No dead keys, no undocumented keys: the file contains exactly
        the keys that are read at runtime."""
        assert set(config.keys()) == _EXPECTED_CONFIG_KEYS

    def test_database_path_relative(self, config):
        """ADR-NC1-002: bundled default must be config-relative."""
        assert not os.path.isabs(str(config["database_path"]))

    def test_lifecycle_keys_match_defaults(self, config):
        from usr.plugins.neuro_core.helpers.lifecycle import DEFAULT_CONFIG

        for key, value in DEFAULT_CONFIG.items():
            assert config[key] == value, key

    def test_contradiction_extras_match_runtime_fallbacks(self, config):
        """contradiction_llm_enabled (default False) and
        contradiction_interval_hours (fallback 168) are read in
        extensions/python/job_loop/_30_contradiction_detection.py."""
        assert config["contradiction_llm_enabled"] is False
        assert config["contradiction_interval_hours"] == 168

    def test_retrieval_keys_match_api_effective_defaults(self, config):
        """Retrieval keys must document the API's established effective
        defaults (behavior-preserving; see implementation report)."""
        assert config["graph_max_hops"] == 2
        assert config["graph_neighbors_max"] == 10
        assert config["semantic_limit"] == 5
        assert config["semantic_threshold"] == 0.6
        assert config["similarity_weight"] == 0.5
        assert config["importance_weight"] == 0.3
        assert config["recency_weight"] == 0.2


# ---------------------------------------------------------------------------
# API retrieval-config wiring
# ---------------------------------------------------------------------------


def _import_context_graph_api():
    """Import api.context_graph with minimal stubs for its framework
    imports (helpers.api, plugins._memory...), which the standalone
    pytest environment does not provide."""
    mod = types.ModuleType("helpers.api")

    class ApiHandler:  # minimal stand-in
        pass

    mod.ApiHandler = ApiHandler
    mod.Request = object
    mod.Response = object
    sys.modules.setdefault("helpers.api", mod)

    # conftest stubs ``helpers`` with an empty __path__, so the real
    # ``helpers.plugins`` is not importable here. Register a stub with
    # ``get_plugin_config``; tests monkeypatch this attribute.
    # Other test files register ``helpers.plugins`` in two different
    # ways: as a bare sys.modules stub (test_hooks.py) or as an
    # attribute on the helpers package without a sys.modules entry
    # (test_lifecycle_jobs.py). That split makes ``import
    # helpers.plugins as hp`` and the API's ``from helpers.plugins
    # import ...`` resolve different objects. Normalize both to one
    # module object that carries ``get_plugin_config``.
    import helpers as _helpers_pkg
    hp_mod = sys.modules.get("helpers.plugins")
    if hp_mod is None:
        hp_mod = getattr(_helpers_pkg, "plugins", None)
    if hp_mod is None:
        hp_mod = types.ModuleType("helpers.plugins")
    if not hasattr(hp_mod, "get_plugin_config"):
        hp_mod.get_plugin_config = lambda name: None
    sys.modules["helpers.plugins"] = hp_mod
    _helpers_pkg.plugins = hp_mod

    for name in ("plugins", "plugins._memory", "plugins._memory.helpers",
                 "plugins._memory.helpers.memory"):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            stub.__path__ = []
            sys.modules[name] = stub
    if not hasattr(sys.modules["plugins._memory.helpers.memory"], "Memory"):
        sys.modules["plugins._memory.helpers.memory"].Memory = object

    import importlib

    return importlib.import_module("usr.plugins.neuro_core.api.context_graph")


_API_DEFAULTS = {
    "graph_max_hops": 2,
    "graph_neighbors_max": 10,
    "semantic_limit": 5,
    "semantic_threshold": 0.6,
    "similarity_weight": 0.5,
    "importance_weight": 0.3,
    "recency_weight": 0.2,
}


class TestRetrievalConfigWiring:
    def test_reads_plugin_config_overrides(self, monkeypatch):
        api = _import_context_graph_api()
        import helpers.plugins as hp

        monkeypatch.setattr(
            hp,
            "get_plugin_config",
            lambda name: {"graph_max_hops": 4, "semantic_limit": 7},
        )
        cfg = api._default_retrieval_config()
        assert cfg["graph_max_hops"] == 4
        assert cfg["semantic_limit"] == 7
        # Untouched keys keep the established defaults.
        assert cfg["graph_neighbors_max"] == 10

    def test_fallback_when_config_none(self, monkeypatch):
        api = _import_context_graph_api()
        import helpers.plugins as hp

        monkeypatch.setattr(hp, "get_plugin_config", lambda name: None)
        assert api._default_retrieval_config() == _API_DEFAULTS

    def test_fallback_when_import_fails(self, monkeypatch):
        api = _import_context_graph_api()
        import helpers.plugins as hp

        def _boom(name):
            raise RuntimeError("framework unavailable")

        monkeypatch.setattr(hp, "get_plugin_config", _boom)
        assert api._default_retrieval_config() == _API_DEFAULTS

    def test_partial_config_merges_per_key(self, monkeypatch):
        api = _import_context_graph_api()
        import helpers.plugins as hp

        monkeypatch.setattr(hp, "get_plugin_config", lambda name: {"semantic_threshold": 0.9})
        cfg = api._default_retrieval_config()
        assert cfg["semantic_threshold"] == 0.9
        assert cfg["similarity_weight"] == 0.5

    def test_none_values_do_not_override(self, monkeypatch):
        api = _import_context_graph_api()
        import helpers.plugins as hp

        monkeypatch.setattr(
            hp, "get_plugin_config", lambda name: {"graph_max_hops": None}
        )
        assert api._default_retrieval_config()["graph_max_hops"] == 2
