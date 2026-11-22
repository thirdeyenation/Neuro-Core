"""Hermetic lifecycle tests for the Neuro Core plugin hooks (WI-005).

Covers the exact 3-step ``install()`` lifecycle contract:

  1. HARD DEPENDENCY GUARD: ``plugins._memory.helpers.memory`` import
     failure -> logged error, ``install() -> False``, zero subprocesses,
     zero decoration.
  2. OPTIONAL networkx>=3.0 PROVISIONING via ``_ensure_networkx()``:
     verify-before-install with NUMERIC version parsing (multi-digit
     majors like ``10.1.0`` never compare lexicographically), uv-preferred
     / pip-fallback installer pinned to ``sys.executable``, and non-fatal
     graceful degradation (missing installer, offline, non-zero exit,
     timeout, OSError) that NEVER raises.
  3. STARTUP DECORATION: ``decorate_memory()`` always runs (non-fatally)
     and ``install() -> True`` even when networkx provisioning degrades.

Plus: ``uninstall()`` is a non-fatal no-op with zero subprocesses, and
``plugin.yaml`` / ``pyproject.toml`` version parity at ``2.0.0`` with the
exact 8-key manifest schema.

100% HERMETIC per HITL Amendment 3: ``importlib.metadata.version``,
``shutil.which``, ``subprocess.run``, ``importlib.invalidate_caches`` and
``decorate_memory`` are ALL mocked — pytest NEVER touches PyPI and never
mutates the global container Python environment.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import subprocess
import sys
import tomllib
import unittest.mock as mock
from pathlib import Path

import pytest
import yaml

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_hooks():
    """Load ``src/hooks.py`` as a module, layout-independently."""
    spec = importlib.util.spec_from_file_location(
        "neuro_core_hooks_under_test", _PLUGIN_ROOT / "hooks.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def hooks():
    return _load_hooks()


@pytest.fixture()
def decorate_mock():
    """Mock decorate_memory at its source module (lazy import target)."""
    from usr.plugins.neuro_core.helpers import decorate as dec

    with mock.patch.object(dec, "decorate_memory") as m:
        yield m


@pytest.fixture()
def metadata_mock():
    """Mock importlib.metadata.version (the verify-before-install probe)."""
    with mock.patch("importlib.metadata.version") as m:
        yield m


@pytest.fixture()
def which_mock():
    with mock.patch("shutil.which") as m:
        yield m


@pytest.fixture()
def run_mock():
    with mock.patch("subprocess.run") as m:
        m.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        yield m


@pytest.fixture()
def invalidate_mock():
    with mock.patch("importlib.invalidate_caches") as m:
        yield m


_UV = "/usr/local/bin/uv"


# ---------------------------------------------------------------------------
# Component design item 1: _memory hard guard
# ---------------------------------------------------------------------------


class TestMemoryHardGuard:
    def test_memory_absent_returns_false_no_subprocess_no_decoration(
        self,
        hooks,
        decorate_mock,
        run_mock,
        metadata_mock,
        which_mock,
        caplog,
    ):
        # Simulate the _memory plugin being absent: a None entry in
        # sys.modules makes the import raise ImportError.
        with mock.patch.dict(
            sys.modules, {"plugins._memory.helpers.memory": None}
        ):
            with caplog.at_level(logging.ERROR, logger="neuro_core.hooks"):
                result = hooks.install()
        assert result is False
        assert run_mock.call_count == 0
        assert which_mock.call_count == 0
        assert decorate_mock.call_count == 0
        assert any("not importable" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Component design item 2: satisfied networkx -> zero subprocesses
# ---------------------------------------------------------------------------


class TestSatisfiedNetworkx:
    @pytest.mark.parametrize("version", ["3.4.2", "10.1.0"])
    def test_satisfied_versions_skip_install_but_still_decorate(
        self,
        hooks,
        metadata_mock,
        which_mock,
        run_mock,
        invalidate_mock,
        decorate_mock,
        version,
    ):
        metadata_mock.return_value = version
        assert hooks._ensure_networkx() is True
        assert run_mock.call_count == 0
        assert invalidate_mock.call_count == 0
        # install() still reaches Step 3 decoration and returns True.
        assert hooks.install() is True
        assert decorate_mock.call_count == 1

    def test_multi_digit_major_compares_numerically_not_lexicographically(
        self, hooks
    ):
        # '10.1.0' < '3.0' lexicographically; must compare numerically.
        assert hooks._networkx_at_least_3("10.1.0") is True
        assert hooks._networkx_at_least_3("3.4.2") is True
        assert hooks._networkx_at_least_3("2.8.8") is False


# ---------------------------------------------------------------------------
# Component design items 3-4: provisioning command shapes
# ---------------------------------------------------------------------------


class TestProvisioning:
    def test_missing_networkx_with_uv_uses_exact_uv_command(
        self,
        hooks,
        metadata_mock,
        which_mock,
        run_mock,
        invalidate_mock,
        decorate_mock,
    ):
        metadata_mock.side_effect = importlib.metadata.PackageNotFoundError("networkx")
        which_mock.return_value = _UV
        result = hooks.install()
        assert result is True
        run_mock.assert_called_once()
        cmd = run_mock.call_args.args[0]
        assert cmd == [
            _UV,
            "pip",
            "install",
            "--python",
            sys.executable,
            "networkx>=3.0",
        ]
        kwargs = run_mock.call_args.kwargs
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        assert kwargs.get("check") is False
        assert kwargs.get("timeout") == 120
        assert invalidate_mock.call_count == 1
        assert decorate_mock.call_count == 1
        assert not any("/opt/venv-a0" in str(c) for c in run_mock.call_args.args)

    def test_version_below_minimum_triggers_install(
        self, hooks, metadata_mock, which_mock, run_mock
    ):
        metadata_mock.return_value = "2.8.8"
        which_mock.return_value = _UV
        assert hooks._ensure_networkx() is True
        assert run_mock.call_count == 1

    def test_uv_absent_falls_back_to_pip(
        self, hooks, metadata_mock, which_mock, run_mock, invalidate_mock
    ):
        metadata_mock.side_effect = importlib.metadata.PackageNotFoundError("networkx")
        which_mock.return_value = None
        assert hooks._ensure_networkx() is True
        run_mock.assert_called_once()
        cmd = run_mock.call_args.args[0]
        assert cmd == [sys.executable, "-m", "pip", "install", "networkx>=3.0"]
        assert invalidate_mock.call_count == 1


# ---------------------------------------------------------------------------
# Component design item 5: non-fatal provisioning failure
# ---------------------------------------------------------------------------


class TestProvisioningFailure:
    @pytest.mark.parametrize(
        "failure",
        ["nonzero-exit", "timeout", "oserror", "installer-missing"],
        ids=["non-zero-exit", "timeout-expired", "oserror", "installer-missing"],
    )
    def test_failure_degrades_gracefully_but_install_still_succeeds(
        self,
        hooks,
        metadata_mock,
        which_mock,
        run_mock,
        decorate_mock,
        caplog,
        failure,
    ):
        metadata_mock.side_effect = importlib.metadata.PackageNotFoundError("networkx")
        if failure == "installer-missing":
            which_mock.return_value = None
            run_mock.side_effect = OSError("pip not found")
        else:
            which_mock.return_value = _UV
            if failure == "nonzero-exit":
                run_mock.return_value = mock.Mock(
                    returncode=1, stdout="", stderr="Network unreachable"
                )
            elif failure == "timeout":
                run_mock.side_effect = subprocess.TimeoutExpired(cmd="uv", timeout=120)
            elif failure == "oserror":
                run_mock.side_effect = OSError("no such installer")

        with caplog.at_level(logging.WARNING, logger="neuro_core.hooks"):
            ensure_result = hooks._ensure_networkx()
            install_result = hooks.install()  # must NEVER raise

        assert ensure_result is False
        assert install_result is True
        assert decorate_mock.call_count == 1
        warnings = [r.getMessage() for r in caplog.records]
        assert any("degraded" in w and "core memory" in w for w in warnings)


# ---------------------------------------------------------------------------
# Component design item 6: decorate_memory() raising is non-fatal
# ---------------------------------------------------------------------------


class TestDecorationFailure:
    def test_decorate_memory_raising_is_caught_and_logged(
        self, hooks, metadata_mock, decorate_mock, caplog
    ):
        metadata_mock.return_value = "3.4.2"
        decorate_mock.side_effect = RuntimeError("decoration boom")
        with caplog.at_level(logging.WARNING, logger="neuro_core.hooks"):
            result = hooks.install()
        assert result is True
        assert any("non-fatal" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Component design item 7: uninstall() no-op
# ---------------------------------------------------------------------------


class TestUninstall:
    def test_uninstall_returns_none_with_zero_subprocesses(self, hooks, run_mock):
        assert hooks.uninstall() is None
        assert run_mock.call_count == 0


# ---------------------------------------------------------------------------
# Component design item 8: manifest / packaging version parity
# ---------------------------------------------------------------------------


class TestVersionParity:
    def test_plugin_yaml_version_2_0_0_with_exact_8_key_schema(self):
        with open(_PLUGIN_ROOT / "plugin.yaml") as fh:
            manifest = yaml.safe_load(fh)
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
        assert manifest["version"] == "2.0.0"

    def test_pyproject_version_parity_2_0_0(self):
        with open(_PLUGIN_ROOT / "pyproject.toml", "rb") as fh:
            pyproject = tomllib.load(fh)
        assert pyproject["project"]["version"] == "2.0.0"
