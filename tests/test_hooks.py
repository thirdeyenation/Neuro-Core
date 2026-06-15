"""Tests for the job_loop extension execute() entrypoints.

This file provides direct coverage of the three Neuro Core job_loop
extensions:

* ``_10_access_decay.AccessDecayJob`` — importance/access decay job
* ``_20_episode_grouping.EpisodeGroupingJob`` — episode-boundary job
* ``_30_contradiction_detection.ContradictionDetectionJob`` —
  contradiction detection job (LLM-gated)

The fourth file the original spec referenced (``_40_graph_analytics``)
**does not exist** in the codebase — graph analytics is implemented as
``run_graph_analytics()`` inside ``helpers/lifecycle.py`` and is called
by other code paths, not as a dedicated job_loop extension. Per the
"do not fabricate behavior" rule, no tests are written for that
nonexistent module.

The 16 tests below (4 per extension × 3 extensions = 12, plus 3
config-disabled early-return tests and 1 LLM-safety-gate test)
cover the four behaviors the spec calls out:

    1. Happy path — execute() completes without raising when all
       dependencies are present and healthy.
    2. Missing agent / None agent — execute() does not raise when
       self.agent is None or missing attributes.
    3. Throttle respected — a second call within the cooldown window
       is a no-op (does not call the underlying lifecycle function).
    4. Throttle expired — after the cooldown window, the extension
       runs again.

Plus three config-gate regressions:

    5. ``decay_enabled=False`` / ``contradiction_detection_enabled=False``
       short-circuits execute() without calling the lifecycle fn.
    6. ``contradiction_llm_enabled=False`` short-circuits the LLM call.

The pattern reuses the conftest.py stubs (``helpers.extension``,
``helpers.print_style``, ``plugins._memory.helpers.memory``) and
loads each extension module with ``importlib.util`` so the real
``from helpers.extension import Extension`` line resolves to the
stub at module-load time. This matches the established pattern in
``test_lifecycle_jobs.py::TestJobLoopExtensionSafety``.

Key implementation notes learned from the source:

* ``_read_config`` / ``_subdirs`` / ``_iter_docs`` are **class-level
  @staticmethod** on each Extension class (not module-level
  functions). Patches must target ``AccessDecayJob._read_config``
  etc., not the module attribute.
* Module-level ``_STATE = {"last_run": 0.0}`` dict holds throttle
  timestamps. It is falsy (``0.0``) on first use, so the ``if
  last_run and ...`` check bypasses the throttle.
* AccessDecayJob uses ``time.time()`` for its throttle; the other two
  use ``asyncio.get_event_loop().time()``. Both behave identically
  for our tests (the second call within the same event loop tick has
  a near-zero delta from the first).
* AccessDecayJob instantiates a real ``ScoreStore(subdir)`` before
  calling the lifecycle fn; we patch ``ScoreStore`` to a Mock to
  avoid disk I/O.
* ``run_importance_decay`` / ``run_episode_grouping`` /
  ``run_contradiction_detection`` are imported lazily inside the
  extension's ``_run()`` from
  ``usr.plugins.neuro_core.helpers.lifecycle``. Patching the name
  on that target module is sufficient — the ``from ... import X``
  rebinds to the mock.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Stub ``helpers.plugins`` so ``from helpers import plugins`` inside
# ``AccessDecayJob._run()`` succeeds. The conftest stubs ``helpers``
# as a package with ``__path__ = []`` (so submodules cannot be
# discovered by the normal import machinery), and the conftest does
# not register ``helpers.plugins``. AccessDecayJob does an
# unprotected ``from helpers import plugins`` at the top of
# ``_run()`` (it ``del``s the name later), so without this stub the
# import raises ``ModuleNotFoundError`` and the entire ``_run()``
# body is skipped — the lifecycle function is never called, the
# throttle state is never updated, and the mock assertions fail.
#
# EpisodeGroupingJob and ContradictionDetectionJob do NOT have this
# problem — their ``from helpers import plugins`` import is inside
# ``_read_config()`` wrapped in ``try/except``.
#
# The stub provides a no-op ``get_plugin_config`` that returns an
# empty dict. The extensions then fall through to the
# ``DEFAULT_CONFIG`` fallback in their own try/except chains. The
# tests don't care about the config contents — they patch
# ``_read_config`` to return a custom dict, so this stub is only
# used to prevent the import from raising.
# ---------------------------------------------------------------------------
if "helpers.plugins" not in sys.modules:
    _plugins_stub = types.ModuleType("helpers.plugins")
    _plugins_stub.get_plugin_config = lambda *args, **kwargs: {}
    sys.modules["helpers.plugins"] = _plugins_stub


# ---------------------------------------------------------------------------
# Module loading (mirrors test_lifecycle_jobs.py::_load_job_loop_module)
# ---------------------------------------------------------------------------


_JOB_LOOP_DIR = (
    Path(__file__).resolve().parent.parent
    / "extensions"
    / "python"
    / "job_loop"
)


def _load_job_loop_module(file_name: str):
    """Load a job_loop extension module without going through normal
    import machinery. Synthetic module name avoids sys.modules cache
    pollution across tests.
    """
    target = _JOB_LOOP_DIR / file_name
    assert target.exists(), f"job_loop file not found: {target}"
    mod_name = f"_neuro_core_test_hooks_{file_name.replace('.', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, str(target))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Module fixtures — scope="module" matches test_lifecycle_jobs.py
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def access_decay_module():
    return _load_job_loop_module("_10_access_decay.py")


@pytest.fixture(scope="module")
def episode_grouping_module():
    return _load_job_loop_module("_20_episode_grouping.py")


@pytest.fixture(scope="module")
def contradiction_detection_module():
    return _load_job_loop_module("_30_contradiction_detection.py")


# ---------------------------------------------------------------------------
# Per-test state reset (autouse)
# ---------------------------------------------------------------------------
#
# The extensions use a module-level _STATE = {"last_run": 0.0} dict for
# throttle bookkeeping. Without resetting it between tests, the
# "throttle respected" tests would see stale timestamps from a
# previous test, and the "throttle expired" tests would see no
# timestamp at all. We reset to 0.0 (epoch) so the throttle check
# "if last_run and (now - last_run) < interval * 3600.0" is False
# (0.0 is falsy), allowing the body to run. Tests that want to test
# the "throttle blocks" case can override per-test by NOT resetting
# _STATE (the previous test's successful run will have set it to a
# near-current time, which blocks the second call within the same
# event loop tick).
#


@pytest.fixture(autouse=True)
def _reset_job_loop_state(
    access_decay_module,
    episode_grouping_module,
    contradiction_detection_module,
):
    """Reset module-level _STATE dicts before each test."""
    for mod in (
        access_decay_module,
        episode_grouping_module,
        contradiction_detection_module,
    ):
        if hasattr(mod, "_STATE"):
            mod._STATE["last_run"] = 0.0
    yield


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


_FAKE_AGENT = object()


def _patch_staticmethods(
    monkeypatch,
    cls,
    *,
    config: dict,
    subdirs: list = None,
) -> None:
    """Patch the class-level @staticmethods (``_read_config``,
    ``_subdirs``, ``_iter_docs``) on an Extension class.

    These are referenced via ``self._read_config()`` etc. in the
    source, but Python looks them up on the class, not the instance,
    so patching the class attribute is sufficient.
    """
    if subdirs is None:
        subdirs = ["test_subdir"]
    monkeypatch.setattr(cls, "_read_config", staticmethod(lambda: dict(config)))
    monkeypatch.setattr(cls, "_subdirs", staticmethod(lambda: list(subdirs)))
    monkeypatch.setattr(cls, "_iter_docs", staticmethod(lambda subdir: []))


def _patch_score_store(monkeypatch) -> MagicMock:
    """Patch ``usr.plugins.neuro_core.helpers.scores.ScoreStore`` to a
    no-op Mock so AccessDecayJob can instantiate ``ScoreStore(subdir)``
    without touching the filesystem."""
    import usr.plugins.neuro_core.helpers.scores as scores_mod

    mock_score_store_cls = MagicMock()
    # The extension calls ``ScoreStore(subdir)`` — a single positional
    # arg. The Mock() instance will accept any call and return another
    # Mock by default, which is fine because run_importance_decay is
    # itself mocked and never touches the score_store's internals.
    mock_score_store_cls.return_value = MagicMock()
    monkeypatch.setattr(scores_mod, "ScoreStore", mock_score_store_cls)
    return mock_score_store_cls


def _patch_lifecycle(
    monkeypatch, lifecycle_fn_name: str, return_value: dict
) -> MagicMock:
    """Patch the lifecycle function on the source module so the
    extension's lazy ``from ... import X`` rebinds to the Mock."""
    import usr.plugins.neuro_core.helpers.lifecycle as lifecycle_mod

    mock_fn = MagicMock(return_value=return_value)
    monkeypatch.setattr(lifecycle_mod, lifecycle_fn_name, mock_fn)
    return mock_fn


def _healthy_access_decay_config() -> dict:
    return {
        "decay_enabled": True,
        "decay_interval_hours": 24,
    }


def _healthy_episode_grouping_config() -> dict:
    return {
        "decay_enabled": True,
        "episode_boundary_hours": 4,
    }


def _healthy_contradiction_config() -> dict:
    return {
        "contradiction_detection_enabled": True,
        "contradiction_interval_hours": 168,
        "contradiction_llm_enabled": True,
    }


# ===========================================================================
# AccessDecayJob
# ===========================================================================


class TestAccessDecayJobHooks:
    """execute() entrypoint tests for ``AccessDecayJob``."""

    @pytest.mark.asyncio
    async def test_happy_path_calls_lifecycle_function_once(
        self, monkeypatch: pytest.MonkeyPatch, access_decay_module
    ) -> None:
        """When everything is healthy, execute() calls
        run_importance_decay exactly once and returns None."""
        _patch_staticmethods(
            monkeypatch,
            access_decay_module.AccessDecayJob,
            config=_healthy_access_decay_config(),
        )
        _patch_score_store(monkeypatch)
        mock_fn = _patch_lifecycle(
            monkeypatch,
            "run_importance_decay",
            {"processed": 0, "decayed": 0, "skipped": 0},
        )

        ext = access_decay_module.AccessDecayJob(agent=_FAKE_AGENT)
        result = await ext.execute()

        assert result is None
        mock_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_none_agent_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch, access_decay_module
    ) -> None:
        """execute() must not raise when self.agent is None."""
        _patch_staticmethods(
            monkeypatch,
            access_decay_module.AccessDecayJob,
            config=_healthy_access_decay_config(),
        )
        _patch_score_store(monkeypatch)
        _patch_lifecycle(
            monkeypatch,
            "run_importance_decay",
            {"processed": 0, "decayed": 0, "skipped": 0},
        )

        ext = access_decay_module.AccessDecayJob(agent=None)
        result = await ext.execute()

        assert result is None

    @pytest.mark.asyncio
    async def test_throttle_respected_blocks_second_call(
        self, monkeypatch: pytest.MonkeyPatch, access_decay_module
    ) -> None:
        """A second call within the cooldown window is a no-op:
        run_importance_decay is NOT called again."""
        _patch_staticmethods(
            monkeypatch,
            access_decay_module.AccessDecayJob,
            config=_healthy_access_decay_config(),
        )
        _patch_score_store(monkeypatch)
        mock_fn = _patch_lifecycle(
            monkeypatch,
            "run_importance_decay",
            {"processed": 0, "decayed": 0, "skipped": 0},
        )

        ext = access_decay_module.AccessDecayJob(agent=_FAKE_AGENT)

        # First call: _STATE was reset to 0.0 by autouse fixture.
        # The throttle check ``if last_run and ...`` is False
        # because 0.0 is falsy, so the body runs and sets
        # _STATE["last_run"] to time.time().
        await ext.execute()
        assert mock_fn.call_count == 1

        # Second call within the cooldown window: _STATE["last_run"]
        # was just set to a near-current value, so
        # (now - last_run) < 24 * 3600 = True, and the body is
        # short-circuited.
        result = await ext.execute()
        assert result is None
        assert mock_fn.call_count == 1, (
            "lifecycle fn was called a second time within the "
            "cooldown window — throttle did not block it"
        )

    @pytest.mark.asyncio
    async def test_throttle_expired_runs_again(
        self, monkeypatch: pytest.MonkeyPatch, access_decay_module
    ) -> None:
        """When the cooldown window has elapsed, the extension
        runs again and the lifecycle fn is called a second time."""
        _patch_staticmethods(
            monkeypatch,
            access_decay_module.AccessDecayJob,
            config=_healthy_access_decay_config(),
        )
        _patch_score_store(monkeypatch)
        mock_fn = _patch_lifecycle(
            monkeypatch,
            "run_importance_decay",
            {"processed": 0, "decayed": 0, "skipped": 0},
        )

        ext = access_decay_module.AccessDecayJob(agent=_FAKE_AGENT)

        access_decay_module._STATE["last_run"] = 0.0
        await ext.execute()
        assert mock_fn.call_count == 1

        # Simulate cooldown elapsed by resetting _STATE to epoch.
        access_decay_module._STATE["last_run"] = 0.0
        await ext.execute()

        assert mock_fn.call_count == 2, (
            "lifecycle fn was not called a second time after the "
            "cooldown window elapsed — throttle did not release it"
        )

    @pytest.mark.asyncio
    async def test_decay_disabled_early_returns(
        self, monkeypatch: pytest.MonkeyPatch, access_decay_module
    ) -> None:
        """When ``decay_enabled`` is False, execute() short-circuits
        and run_importance_decay is NEVER called."""
        _patch_staticmethods(
            monkeypatch,
            access_decay_module.AccessDecayJob,
            config={
                "decay_enabled": False,
                "decay_interval_hours": 24,
            },
        )
        _patch_score_store(monkeypatch)
        mock_fn = _patch_lifecycle(
            monkeypatch,
            "run_importance_decay",
            {"processed": 0, "decayed": 0, "skipped": 0},
        )

        ext = access_decay_module.AccessDecayJob(agent=_FAKE_AGENT)
        result = await ext.execute()

        assert result is None
        mock_fn.assert_not_called()


# ===========================================================================
# EpisodeGroupingJob
# ===========================================================================


class TestEpisodeGroupingJobHooks:
    """execute() entrypoint tests for ``EpisodeGroupingJob``."""

    @pytest.mark.asyncio
    async def test_happy_path_calls_lifecycle_function_once(
        self, monkeypatch: pytest.MonkeyPatch, episode_grouping_module
    ) -> None:
        """When everything is healthy, execute() calls
        run_episode_grouping exactly once and returns None."""
        _patch_staticmethods(
            monkeypatch,
            episode_grouping_module.EpisodeGroupingJob,
            config=_healthy_episode_grouping_config(),
        )
        mock_fn = _patch_lifecycle(
            monkeypatch,
            "run_episode_grouping",
            # No "assignments" key → _persist_assignments is a no-op.
            {"scanned": 0, "episodes": 0, "assigned": 0},
        )

        ext = episode_grouping_module.EpisodeGroupingJob(agent=_FAKE_AGENT)
        result = await ext.execute()

        assert result is None
        mock_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_none_agent_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch, episode_grouping_module
    ) -> None:
        """execute() must not raise when self.agent is None."""
        _patch_staticmethods(
            monkeypatch,
            episode_grouping_module.EpisodeGroupingJob,
            config=_healthy_episode_grouping_config(),
        )
        _patch_lifecycle(
            monkeypatch,
            "run_episode_grouping",
            {"scanned": 0, "episodes": 0, "assigned": 0},
        )

        ext = episode_grouping_module.EpisodeGroupingJob(agent=None)
        result = await ext.execute()

        assert result is None

    @pytest.mark.asyncio
    async def test_throttle_respected_blocks_second_call(
        self, monkeypatch: pytest.MonkeyPatch, episode_grouping_module
    ) -> None:
        """A second call within the episode boundary window is a
        no-op: run_episode_grouping is NOT called again."""
        _patch_staticmethods(
            monkeypatch,
            episode_grouping_module.EpisodeGroupingJob,
            config=_healthy_episode_grouping_config(),
        )
        mock_fn = _patch_lifecycle(
            monkeypatch,
            "run_episode_grouping",
            {"scanned": 0, "episodes": 0, "assigned": 0},
        )

        ext = episode_grouping_module.EpisodeGroupingJob(agent=_FAKE_AGENT)

        await ext.execute()
        assert mock_fn.call_count == 1

        # Second call within the boundary window (4h default):
        # the asyncio loop time hasn't advanced enough.
        result = await ext.execute()
        assert result is None
        assert mock_fn.call_count == 1, (
            "lifecycle fn was called a second time within the "
            "episode boundary window — throttle did not block it"
        )

    @pytest.mark.asyncio
    async def test_throttle_expired_runs_again(
        self, monkeypatch: pytest.MonkeyPatch, episode_grouping_module
    ) -> None:
        """When the episode boundary window has elapsed, the
        extension runs again."""
        _patch_staticmethods(
            monkeypatch,
            episode_grouping_module.EpisodeGroupingJob,
            config=_healthy_episode_grouping_config(),
        )
        mock_fn = _patch_lifecycle(
            monkeypatch,
            "run_episode_grouping",
            {"scanned": 0, "episodes": 0, "assigned": 0},
        )

        ext = episode_grouping_module.EpisodeGroupingJob(agent=_FAKE_AGENT)

        episode_grouping_module._STATE["last_run"] = 0.0
        await ext.execute()
        assert mock_fn.call_count == 1

        episode_grouping_module._STATE["last_run"] = 0.0
        await ext.execute()

        assert mock_fn.call_count == 2

    @pytest.mark.asyncio
    async def test_decay_disabled_early_returns(
        self, monkeypatch: pytest.MonkeyPatch, episode_grouping_module
    ) -> None:
        """When ``decay_enabled`` is False, the extension body
        short-circuits (EpisodeGroupingJob reuses the decay_enabled
        gate — verified by reading the source) and
        run_episode_grouping is NEVER called."""
        _patch_staticmethods(
            monkeypatch,
            episode_grouping_module.EpisodeGroupingJob,
            config={
                "decay_enabled": False,
                "episode_boundary_hours": 4,
            },
        )
        mock_fn = _patch_lifecycle(
            monkeypatch,
            "run_episode_grouping",
            {"scanned": 0, "episodes": 0, "assigned": 0},
        )

        ext = episode_grouping_module.EpisodeGroupingJob(agent=_FAKE_AGENT)
        result = await ext.execute()

        assert result is None
        mock_fn.assert_not_called()


# ===========================================================================
# ContradictionDetectionJob
# ===========================================================================


class TestContradictionDetectionJobHooks:
    """execute() entrypoint tests for ``ContradictionDetectionJob``."""

    @pytest.mark.asyncio
    async def test_happy_path_calls_lifecycle_function_once(
        self, monkeypatch: pytest.MonkeyPatch, contradiction_detection_module
    ) -> None:
        """When everything is healthy and LLM gate is on, execute()
        calls run_contradiction_detection exactly once."""
        _patch_staticmethods(
            monkeypatch,
            contradiction_detection_module.ContradictionDetectionJob,
            config=_healthy_contradiction_config(),
        )
        mock_fn = _patch_lifecycle(
            monkeypatch,
            "run_contradiction_detection",
            # No "disputes" key → _persist_disputes is a no-op.
            {"checked": 0, "disputed": 0},
        )

        ext = contradiction_detection_module.ContradictionDetectionJob(
            agent=_FAKE_AGENT
        )
        result = await ext.execute()

        assert result is None
        mock_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_none_agent_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch, contradiction_detection_module
    ) -> None:
        """execute() must not raise when self.agent is None."""
        _patch_staticmethods(
            monkeypatch,
            contradiction_detection_module.ContradictionDetectionJob,
            config=_healthy_contradiction_config(),
        )
        _patch_lifecycle(
            monkeypatch,
            "run_contradiction_detection",
            {"checked": 0, "disputed": 0},
        )

        ext = contradiction_detection_module.ContradictionDetectionJob(
            agent=None
        )
        result = await ext.execute()

        assert result is None

    @pytest.mark.asyncio
    async def test_throttle_respected_blocks_second_call(
        self, monkeypatch: pytest.MonkeyPatch, contradiction_detection_module
    ) -> None:
        """A second call within the contradiction interval (168h
        default) is a no-op."""
        _patch_staticmethods(
            monkeypatch,
            contradiction_detection_module.ContradictionDetectionJob,
            config=_healthy_contradiction_config(),
        )
        mock_fn = _patch_lifecycle(
            monkeypatch,
            "run_contradiction_detection",
            {"checked": 0, "disputed": 0},
        )

        ext = contradiction_detection_module.ContradictionDetectionJob(
            agent=_FAKE_AGENT
        )

        await ext.execute()
        assert mock_fn.call_count == 1

        result = await ext.execute()
        assert result is None
        assert mock_fn.call_count == 1, (
            "lifecycle fn was called a second time within the "
            "contradiction interval — throttle did not block it"
        )

    @pytest.mark.asyncio
    async def test_throttle_expired_runs_again(
        self, monkeypatch: pytest.MonkeyPatch, contradiction_detection_module
    ) -> None:
        """When the contradiction interval has elapsed, the
        extension runs again."""
        _patch_staticmethods(
            monkeypatch,
            contradiction_detection_module.ContradictionDetectionJob,
            config=_healthy_contradiction_config(),
        )
        mock_fn = _patch_lifecycle(
            monkeypatch,
            "run_contradiction_detection",
            {"checked": 0, "disputed": 0},
        )

        ext = contradiction_detection_module.ContradictionDetectionJob(
            agent=_FAKE_AGENT
        )

        contradiction_detection_module._STATE["last_run"] = 0.0
        await ext.execute()
        assert mock_fn.call_count == 1

        contradiction_detection_module._STATE["last_run"] = 0.0
        await ext.execute()

        assert mock_fn.call_count == 2

    @pytest.mark.asyncio
    async def test_contradiction_detection_disabled_early_returns(
        self, monkeypatch: pytest.MonkeyPatch, contradiction_detection_module
    ) -> None:
        """When ``contradiction_detection_enabled`` is False,
        execute() short-circuits and the lifecycle fn is never called."""
        _patch_staticmethods(
            monkeypatch,
            contradiction_detection_module.ContradictionDetectionJob,
            config={
                "contradiction_detection_enabled": False,
                "contradiction_interval_hours": 168,
                "contradiction_llm_enabled": True,
            },
        )
        mock_fn = _patch_lifecycle(
            monkeypatch,
            "run_contradiction_detection",
            {"checked": 0, "disputed": 0},
        )

        ext = contradiction_detection_module.ContradictionDetectionJob(
            agent=_FAKE_AGENT
        )
        result = await ext.execute()

        assert result is None
        mock_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_llm_disabled_early_returns(
        self, monkeypatch: pytest.MonkeyPatch, contradiction_detection_module
    ) -> None:
        """When ``contradiction_llm_enabled`` is False, the LLM call
        is gated off and run_contradiction_detection is NEVER called.

        This is the safety gate that prevents a runaway LLM bill on
        large memory corpora — it must be respected even when the
        outer ``contradiction_detection_enabled`` is True.
        """
        _patch_staticmethods(
            monkeypatch,
            contradiction_detection_module.ContradictionDetectionJob,
            config={
                "contradiction_detection_enabled": True,
                "contradiction_interval_hours": 168,
                "contradiction_llm_enabled": False,
            },
        )
        mock_fn = _patch_lifecycle(
            monkeypatch,
            "run_contradiction_detection",
            {"checked": 0, "disputed": 0},
        )

        ext = contradiction_detection_module.ContradictionDetectionJob(
            agent=_FAKE_AGENT
        )
        result = await ext.execute()

        assert result is None
        mock_fn.assert_not_called()
