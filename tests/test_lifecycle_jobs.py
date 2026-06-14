"""Tests for ``helpers/lifecycle.py`` and the throttle helper.

The test suite is split into two sections that mirror the two
phases of the implementation:

* ``TestImportanceDecay`` — six tests covering the run_importance_decay
  helper and the throttle.
* ``TestEpisodeGrouping`` — four tests covering the episode grouping
  helper and the ``ep_{date}_{idx}`` naming format.

A small in-memory stand-in for ``ScoreStore`` is defined inside the
fixtures — the real one is a file-backed ``RLock``-guarded class and
would force these tests to write to disk.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from usr.plugins.neuro_core.helpers import lifecycle


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeScores:
    importance: float = 0.5
    confidence: float = 0.5
    stability: float = 0.5
    access_count: int = 0
    last_accessed_at: Optional[str] = None


class _FakeScoreStore:
    """In-memory stand-in for ``ScoreStore``."""

    def __init__(self, initial: Optional[Dict[str, _FakeScores]] = None) -> None:
        self._data: Dict[str, _FakeScores] = dict(initial or {})
        self.set_calls: List[Tuple[str, Dict[str, Any]]] = []

    def get(self, memory_id: str) -> Optional[_FakeScores]:
        return self._data.get(memory_id)

    def set(
        self,
        memory_id: str,
        *,
        importance: Optional[float] = None,
        confidence: Optional[float] = None,
        stability: Optional[float] = None,
        access_count: Optional[int] = None,
        last_accessed_at: Optional[str] = None,
    ) -> _FakeScores:
        current = self._data.setdefault(memory_id, _FakeScores())
        if importance is not None:
            current.importance = importance
        if confidence is not None:
            current.confidence = confidence
        if stability is not None:
            current.stability = stability
        if access_count is not None:
            current.access_count = access_count
        if last_accessed_at is not None:
            current.last_accessed_at = last_accessed_at
        self.set_calls.append((memory_id, {
            "importance": importance,
            "confidence": confidence,
            "stability": stability,
            "access_count": access_count,
            "last_accessed_at": last_accessed_at,
        }))
        return current


@dataclass
class _Doc:
    id: str
    metadata: dict = field(default_factory=dict)
    page_content: str = ""


def _mk_docs(items: List[Tuple[str, dict]]) -> List[Tuple[str, dict]]:
    return [(i, m) for (i, m) in items]


# ---------------------------------------------------------------------------
# 1. Importance decay (6 tests)
# ---------------------------------------------------------------------------


class TestImportanceDecay:
    """Tests for ``lifecycle.run_importance_decay`` and the throttle."""

    def test_decay_for_eligible_doc(self) -> None:
        """A doc with no validation_status and low stability decays."""
        store = _FakeScoreStore({"m1": _FakeScores(importance=0.8)})
        docs = _mk_docs([
            ("m1", {"validation_status": "unvalidated", "stability": 0.5}),
        ])
        result = lifecycle.run_importance_decay(
            "default", {"importance_decay_rate": 0.10}, store, docs
        )
        assert result == {"processed": 1, "decayed": 1, "skipped": 0}
        # 0.8 * 0.9 = 0.72
        assert store.get("m1").importance == pytest.approx(0.72, rel=1e-9)

    def test_skips_validated_docs(self) -> None:
        """Validated docs are pinned — never decayed."""
        store = _FakeScoreStore({"m1": _FakeScores(importance=0.8)})
        docs = _mk_docs([
            ("m1", {"validation_status": "validated", "stability": 0.5}),
        ])
        result = lifecycle.run_importance_decay(
            "default", {"importance_decay_rate": 0.10}, store, docs
        )
        assert result == {"processed": 1, "decayed": 0, "skipped": 1}
        assert store.get("m1").importance == pytest.approx(0.8)

    def test_skips_high_stability_docs(self) -> None:
        """Docs with stability >= 0.8 are skipped (consolidated)."""
        store = _FakeScoreStore({"m1": _FakeScores(importance=0.8)})
        docs = _mk_docs([
            ("m1", {"validation_status": "unvalidated", "stability": 0.85}),
        ])
        result = lifecycle.run_importance_decay(
            "default", {"importance_decay_rate": 0.10}, store, docs
        )
        assert result == {"processed": 1, "decayed": 0, "skipped": 1}
        assert store.get("m1").importance == pytest.approx(0.8)

    def test_decay_clamps_to_zero(self) -> None:
        """Decayed importance never goes below 0.0 even with large rate."""
        store = _FakeScoreStore({"m1": _FakeScores(importance=0.01)})
        docs = _mk_docs([
            ("m1", {"validation_status": "unvalidated", "stability": 0.3}),
        ])
        # decay_rate=0.99 → 0.01 * 0.01 = 0.0001, fine; use rate 2.0 to force
        # the clamp path: 0.01 * (1-1) = 0, then * (1-2) = -0.01, clamp to 0.0
        result = lifecycle.run_importance_decay(
            "default", {"importance_decay_rate": 2.0}, store, docs
        )
        assert result["decayed"] == 1
        assert store.get("m1").importance == 0.0
        assert store.get("m1").importance >= 0.0

    def test_summary_counts_correct(self) -> None:
        """processed / decayed / skipped counts are correct in a mixed pass."""
        store = _FakeScoreStore({
            f"m{i}": _FakeScores(importance=0.8) for i in range(5)
        })
        docs = _mk_docs([
            ("m0", {"validation_status": "unvalidated", "stability": 0.3}),  # decay
            ("m1", {"validation_status": "validated", "stability": 0.3}),   # skip
            ("m2", {"validation_status": "unvalidated", "stability": 0.9}),  # skip
            ("m3", {"validation_status": "disputed",   "stability": 0.3}),   # decay
            ("m4", {"validation_status": "deprecated", "stability": 0.3}),   # decay
        ])
        result = lifecycle.run_importance_decay(
            "default", {"importance_decay_rate": 0.05}, store, docs
        )
        assert result == {"processed": 5, "decayed": 3, "skipped": 2}

    def test_throttle_blocks_second_call_within_interval(self) -> None:
        """The throttle gate returns False when the gap < interval_hours."""
        # Use a tiny module to host the timestamp attribute.
        mod = pytest.MonkeyPatch().contextclass() if False else None  # noqa
        # Easier: use a fresh types.ModuleType-like simple object.
        class _Mod:
            pass
        m = _Mod()
        m._last_decay = 0  # first run
        t0 = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)
        assert lifecycle.should_run("_last_decay", m, 24, now=t0) is True
        # Second call 1h later: must NOT run again.
        t1 = t0 + timedelta(hours=1)
        assert lifecycle.should_run("_last_decay", m, 24, now=t1) is False
        # Third call 25h later: must run again.
        t2 = t0 + timedelta(hours=25)
        assert lifecycle.should_run("_last_decay", m, 24, now=t2) is True


# ---------------------------------------------------------------------------
# 2. Episode grouping (4 tests)
# ---------------------------------------------------------------------------


def _ts(s: str) -> str:
    """Helper: ISO timestamp (no tz) → UTC-aware datetime."""
    return s


class TestEpisodeGrouping:
    """Tests for ``lifecycle.run_episode_grouping``."""

    def test_assigns_same_episode_within_window(self) -> None:
        """Three memos within 4h → same episode_id."""
        docs = [
            {"id": "a", "metadata": {"timestamp": "2026-06-06T10:00:00Z"}},
            {"id": "b", "metadata": {"timestamp": "2026-06-06T11:00:00Z"}},
            {"id": "c", "metadata": {"timestamp": "2026-06-06T13:00:00Z"}},
        ]
        result = lifecycle.run_episode_grouping(
            "default",
            {"episode_boundary_hours": 4, "episode_min_memories": 3},
            docs,
        )
        assert result["scanned"] == 3
        assert result["episodes"] == 1
        assert result["assigned"] == 3
        ep_ids = {a["episode_id"] for a in result["assignments"]}
        assert len(ep_ids) == 1
        assert next(iter(ep_ids)).startswith("ep_2026-06-06_")

    def test_starts_new_episode_after_gap(self) -> None:
        """A 5h gap starts a new episode."""
        docs = [
            {"id": "a", "metadata": {"timestamp": "2026-06-06T10:00:00Z"}},
            {"id": "b", "metadata": {"timestamp": "2026-06-06T11:00:00Z"}},
            {"id": "c", "metadata": {"timestamp": "2026-06-06T11:30:00Z"}},
            {"id": "d", "metadata": {"timestamp": "2026-06-06T17:00:00Z"}},  # 5.5h gap
            {"id": "e", "metadata": {"timestamp": "2026-06-06T18:00:00Z"}},
            {"id": "f", "metadata": {"timestamp": "2026-06-06T19:00:00Z"}},
        ]
        result = lifecycle.run_episode_grouping(
            "default",
            {"episode_boundary_hours": 4, "episode_min_memories": 3},
            docs,
        )
        assert result["episodes"] == 2
        ep_ids = [a["episode_id"] for a in result["assignments"]]
        assert ep_ids.count(ep_ids[0]) == 3  # first group of 3
        assert ep_ids.count(ep_ids[-1]) == 3  # second group of 3
        assert ep_ids[0] != ep_ids[-1]

    def test_small_group_not_assigned(self) -> None:
        """A group with fewer than ``episode_min_memories`` is skipped."""
        docs = [
            {"id": "a", "metadata": {"timestamp": "2026-06-06T10:00:00Z"}},
            {"id": "b", "metadata": {"timestamp": "2026-06-06T11:00:00Z"}},  # 1h gap
            # 5h gap → new group of 1
            {"id": "c", "metadata": {"timestamp": "2026-06-06T16:00:00Z"}},
        ]
        result = lifecycle.run_episode_grouping(
            "default",
            {"episode_boundary_hours": 4, "episode_min_memories": 3},
            docs,
        )
        assert result["scanned"] == 3
        # Two groups are formed (1h gap, then 5h gap → new group).
        # Neither has >= episode_min_memories, so 0 are assigned.
        assert result["episodes"] == 2
        assert result["assigned"] == 0

    def test_episode_id_format(self) -> None:
        """Episode IDs follow the ``ep_{date}_{idx}`` pattern."""
        docs = [
            {"id": "a", "metadata": {"timestamp": "2026-06-06T10:00:00Z"}},
            {"id": "b", "metadata": {"timestamp": "2026-06-06T10:30:00Z"}},
            {"id": "c", "metadata": {"timestamp": "2026-06-06T11:00:00Z"}},
        ]
        result = lifecycle.run_episode_grouping(
            "default",
            {"episode_boundary_hours": 4, "episode_min_memories": 3},
            docs,
        )
        assert result["assigned"] == 3
        ep_id = result["assignments"][0]["episode_id"]
        # Pattern: ep_YYYY-MM-DD_NNN where NNN is 3-digit zero-padded index.
        import re
        m = re.match(r"^ep_(\d{4}-\d{2}-\d{2})_(\d{3})$", ep_id)
        assert m is not None, f"episode_id {ep_id!r} does not match pattern"
        assert m.group(1) == "2026-06-06"
        assert int(m.group(2)) >= 1


# ---------------------------------------------------------------------------
# Test the execute.py import-fallback helper
# ---------------------------------------------------------------------------


class TestExecuteImportFallback:
    """Verify that ``usr.plugins.neuro_core.execute._import_memory()``
    gracefully handles a patched ``sys.path``.

    The original bug was a ``No module named 'plugins'`` crash when
    ``execute.py`` was invoked from a context where the Agent Zero root
    (``/a0``) was not on ``sys.path``. The fix wraps the import in a
    helper that tries the standard import first, then falls back to
    inserting the A0 root into ``sys.path`` before retrying.

    These tests must NOT crash regardless of whether
    ``plugins._memory.helpers.memory`` is actually importable in the
    test environment — the function's contract is "never raise, return
    ``(None, None)`` on failure, or ``(fn, cls)`` on success".
    """

    def test_returns_two_tuple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``_import_memory()`` always returns a 2-tuple (never raises)."""
        # Import the execute module fresh. It is safe to import in the
        # test environment because the top-level imports are minimal
        # (asyncio, os, sys) and ``_import_memory()`` is the only thing
        # that touches ``plugins._memory``.
        import importlib
        from usr.plugins.neuro_core import execute

        # Force a re-resolution: call the function with the current
        # (possibly augmented) sys.path. The result must be a 2-tuple.
        result = execute._import_memory()
        assert isinstance(result, tuple)
        assert len(result) == 2
        # Either both are None (failure path) or both are not None
        # (success path). Never a mix.
        if result[0] is None:
            assert result[1] is None
        else:
            assert result[1] is not None

    def test_fallback_does_not_crash_with_patched_sys_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``sys.path`` is scrubbed of the A0 root, the fallback
        branch must still complete without raising. The function should
        either succeed via the A0-root re-insert, or return
        ``(None, None)`` if the module genuinely is not on disk."""
        from usr.plugins.neuro_core import execute

        # Resolve the A0 root the same way the helper does: three
        # parents up from ``execute.py``.
        a0_root = os.path.abspath(
            os.path.join(os.path.dirname(execute.__file__), "..", "..", "..")
        )

        # Snapshot the original path so we can restore it.
        original_path = list(sys.path)
        try:
            # Remove the A0 root from sys.path to force the fallback
            # branch. This simulates running ``execute.py`` from a
            # context where /a0 is not on the path.
            scrubbed = [p for p in sys.path if os.path.abspath(p) != a0_root]
            monkeypatch.setattr(sys, "path", scrubbed)

            # The function MUST NOT raise. It may either:
            #   (a) re-insert the A0 root and succeed (returning real
            #       ``get_existing_memory_subdirs`` and ``Memory``), or
            #   (b) return ``(None, None)`` if the module genuinely
            #       cannot be found.
            result = execute._import_memory()
        finally:
            # Restore sys.path (monkeypatch would do this on teardown,
            # but we restore early so subsequent assertions are clean).
            sys.path[:] = original_path

        assert isinstance(result, tuple)
        assert len(result) == 2
        # The fallback branch may have re-inserted the A0 root into
        # sys.path; that is acceptable (it's the documented behaviour).
        # The contract is just: no exception, and the result is a tuple.

    def test_module_level_path_bootstrap(self) -> None:
        """The A0 root is inserted into ``sys.path`` at module import
        time (before any ``usr.plugins.neuro_core.*`` or
        ``plugins._memory.*`` import is attempted).

        The original bug was a ``No module named 'usr'`` crash because
        the path insertion happened inside a function that ran *after*
        the module-level ``from usr.plugins.neuro_core...`` import had
        already failed. The fix moves the path insertion to a
        module-level block at the top of ``execute.py``.

        This test verifies:

        1. The module exposes the constant ``_A0_ROOT`` at module level.
        2. ``_A0_ROOT`` is present on ``sys.path`` after the module
           has been imported (i.e. the bootstrap block ran).
        3. ``_A0_ROOT`` is the actual Agent Zero root directory.
        """
        from usr.plugins.neuro_core import execute

        # 1. The module-level constant must exist and be a string.
        assert hasattr(execute, "_A0_ROOT"), (
            "execute.py must expose _A0_ROOT as a module-level constant "
            "(the path bootstrap block at the top of the file)."
        )
        assert isinstance(execute._A0_ROOT, str)
        assert os.path.isabs(execute._A0_ROOT)

        # 2. The A0 root must be on sys.path after the module was
        #    imported (the module-level block inserts it).
        #    We compare with ``os.path.abspath`` to handle any ``..``
        #    resolution differences between the two paths.
        a0_root_resolved = os.path.abspath(execute._A0_ROOT)
        assert a0_root_resolved in sys.path, (
            f"_A0_ROOT ({a0_root_resolved!r}) must be on sys.path after "
            "execute.py is imported. The module-level bootstrap block is "
            "either missing or not running before the plugin-local imports."
        )

        # 3. _A0_ROOT must be the directory three parents up from
        #    execute.py — i.e. the Agent Zero root (/a0).
        expected_a0_root = os.path.abspath(
            os.path.join(os.path.dirname(execute.__file__), "..", "..", "..")
        )
        assert a0_root_resolved == expected_a0_root

    def test_module_level_block_runs_before_imports(self) -> None:
        """Regression test for the original bug: importing
        ``usr.plugins.neuro_core.execute`` must succeed even when /a0
        is not on ``sys.path`` *before* the import is attempted.

        We simulate this by spawning a subprocess with a sanitized
        ``PYTHONPATH`` and confirming the import completes without
        raising ``ModuleNotFoundError: No module named 'usr'``.
        """
        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import usr.plugins.neuro_core.execute as e; "
                "assert hasattr(e, '_A0_ROOT'); "
                "import os, sys; "
                "assert os.path.abspath(e._A0_ROOT) in sys.path; "
                "print('OK')",
            ],
            cwd="/a0",
            env={**__import__("os").environ, "PYTHONPATH": ""},
            capture_output=True,
            text=True,
            timeout=30,
        )
        # If the module-level bootstrap is broken, the subprocess will
        # fail with ``ModuleNotFoundError: No module named 'usr'``.
        assert result.returncode == 0, (
            f"execute.py failed to import in a clean subprocess.\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        assert "OK" in result.stdout

    def test_main_returns_one_when_import_fails(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """``main()`` must exit with code 1 and print a clear error
        when ``_import_memory()`` returns ``(None, None)``.

        This simulates a completely broken environment (no A0 root on
        path, no ``plugins._memory`` on disk) and verifies the
        never-raise contract at the top level."""
        from usr.plugins.neuro_core import execute

        # Monkey-patch ``_import_memory`` to always return (None, None).
        monkeypatch.setattr(
            execute, "_import_memory", lambda: (None, None)
        )

        rc = execute.main()
        assert rc == 1

        captured = capsys.readouterr()
        # The error message must mention the dependency so the user
        # knows what to do (enable the _memory plugin).
        assert "_memory" in captured.out
        assert "FATAL" in captured.out or "fatal" in captured.out.lower()

    def test_main_returns_zero_when_import_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """``main()`` must exit with code 0 when ``_import_memory()``
        succeeds. We stub the migration helpers to avoid touching FAISS."""
        from usr.plugins.neuro_core import execute

        # Stub _import_memory to return fake (fn, cls) that produces
        # an empty subdir list (no FAISS access required).
        monkeypatch.setattr(
            execute,
            "_import_memory",
            lambda: (lambda: [], object),
        )
        # Stub _run_migration to return zeros — no real FAISS work.
        async def _fake_run(get_fn, mem_cls):
            return 0, 0, 0
        monkeypatch.setattr(execute, "_run_migration", _fake_run)

        rc = execute.main()
        assert rc == 0

        captured = capsys.readouterr()
        # Success path prints the migration summary.
        assert "migration complete" in captured.out or "no subdirs" in captured.out.lower() or "subdirs_processed=0" in captured.out


# ---------------------------------------------------------------------------
# 3. Job-loop extension execute() safety (6 tests)
#
# Stability audit fix verification: every job_loop extension's
# ``execute()`` method must be wrapped in a two-layer error guard so
# that an unhandled exception or an ``asyncio.wait_for`` timeout in
# the inner ``_run()`` body NEVER propagates out of ``execute()``.
# If it did, the framework's 60s job_loop tick would crash-loop and
# take down the whole agent process — which is exactly the bug we
# are repairing.
#
# The three job_loop modules live under
# ``extensions/python/job_loop/`` and have no ``__init__.py`` in
# their parent directories, so we load them via
# ``importlib.util.spec_from_file_location`` rather than the normal
# dotted import. The conftest stubs ``helpers.extension`` and
# ``helpers.print_style`` so the module-level imports inside the
# job_loop files succeed.
# ---------------------------------------------------------------------------


def _load_job_loop_module(file_name: str) -> Any:
    """Load ``extensions/python/job_loop/<file_name>.py`` by file path.

    The directory tree has no ``__init__.py`` files, so the normal
    ``import usr.plugins.neuro_core.extensions.python.job_loop.X``
    mechanism does not work. We use ``spec_from_file_location`` with
    a stable synthetic module name to avoid re-import collisions
    across multiple test runs in the same process.
    """
    job_loop_dir = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "python"
        / "job_loop"
    )
    target = job_loop_dir / file_name
    assert target.exists(), f"job_loop file not found: {target}"

    # Synthetic name to avoid sys.modules cache pollution across tests.
    mod_name = f"_neuro_core_test_job_loop_{file_name.replace('.', '_').replace('/', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, str(target))
    assert spec is not None and spec.loader is not None, (
        f"Could not build import spec for {target}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def access_decay_module():
    return _load_job_loop_module("_10_access_decay.py")


@pytest.fixture(scope="module")
def episode_grouping_module():
    return _load_job_loop_module("_20_episode_grouping.py")


@pytest.fixture(scope="module")
def contradiction_detection_module():
    return _load_job_loop_module("_30_contradiction_detection.py")


# Job-loop extensions are long-lived module-level objects; the
# Extension base class is satisfied with a ``None`` agent in tests
# because we never reach the inner ``_run()`` body (we patch it
# out). This keeps the tests hermetic and fast.
_FAKE_AGENT = object()


class TestJobLoopExtensionSafety:
    """Verify the two-layer error guard on every job_loop extension.

    Each extension's ``execute()`` method must:

    1. Wrap the inner ``_run()`` call in
       ``asyncio.wait_for(self._run(**kwargs), timeout=30.0)`` and
       catch ``asyncio.TimeoutError``.
    2. Wrap the whole body in a broad ``except Exception``.
    3. Return ``None`` (or any non-raising value) in all failure
       cases — never re-raise.

    These tests instantiate each extension, patch ``_run`` to
    simulate failure modes, and assert ``execute()`` does not raise.
    """

    # ---- Exception-safety tests (3) -------------------------------------

    @pytest.mark.asyncio
    async def test_access_decay_execute_swallows_run_exception(
        self, access_decay_module
    ) -> None:
        """``AccessDecayJob.execute()`` must not raise when
        ``_run()`` raises an unhandled exception."""
        ext = access_decay_module.AccessDecayJob(agent=_FAKE_AGENT)

        async def _explode(**kwargs: Any) -> None:
            raise RuntimeError("simulated crash in AccessDecayJob._run")

        ext._run = _explode  # type: ignore[method-assign]

        result = await ext.execute()
        assert result is None

    @pytest.mark.asyncio
    async def test_episode_grouping_execute_swallows_run_exception(
        self, episode_grouping_module
    ) -> None:
        """``EpisodeGroupingJob.execute()`` must not raise when
        ``_run()`` raises an unhandled exception."""
        ext = episode_grouping_module.EpisodeGroupingJob(agent=_FAKE_AGENT)

        async def _explode(**kwargs: Any) -> None:
            raise RuntimeError(
                "simulated crash in EpisodeGroupingJob._run"
            )

        ext._run = _explode  # type: ignore[method-assign]

        result = await ext.execute()
        assert result is None

    @pytest.mark.asyncio
    async def test_contradiction_detection_execute_swallows_run_exception(
        self, contradiction_detection_module
    ) -> None:
        """``ContradictionDetectionJob.execute()`` must not raise when
        ``_run()`` raises an unhandled exception."""
        ext = contradiction_detection_module.ContradictionDetectionJob(
            agent=_FAKE_AGENT
        )

        async def _explode(**kwargs: Any) -> None:
            raise RuntimeError(
                "simulated crash in ContradictionDetectionJob._run"
            )

        ext._run = _explode  # type: ignore[method-assign]

        result = await ext.execute()
        assert result is None

    # ---- Timeout-safety tests (3) ---------------------------------------

    @pytest.mark.asyncio
    async def test_access_decay_execute_returns_on_timeout(
        self, access_decay_module
    ) -> None:
        """``AccessDecayJob.execute()`` must return within ~35s when
        ``_run()`` exceeds the 30s ``asyncio.wait_for`` deadline."""
        ext = access_decay_module.AccessDecayJob(agent=_FAKE_AGENT)

        async def _slow_run(**kwargs: Any) -> None:
            await asyncio.sleep(60)

        ext._run = _slow_run  # type: ignore[method-assign]

        start = time.monotonic()
        result = await ext.execute()
        elapsed = time.monotonic() - start

        assert result is None
        # The timeout is 30s; we allow 5s of scheduling jitter.
        assert elapsed < 35.0, (
            f"AccessDecayJob.execute() took {elapsed:.1f}s after "
            "_run() was patched to sleep 60s; expected ~30s timeout"
        )

    @pytest.mark.asyncio
    async def test_episode_grouping_execute_returns_on_timeout(
        self, episode_grouping_module
    ) -> None:
        """``EpisodeGroupingJob.execute()`` must return within ~35s
        when ``_run()`` exceeds the 30s ``asyncio.wait_for`` deadline."""
        ext = episode_grouping_module.EpisodeGroupingJob(agent=_FAKE_AGENT)

        async def _slow_run(**kwargs: Any) -> None:
            await asyncio.sleep(60)

        ext._run = _slow_run  # type: ignore[method-assign]

        start = time.monotonic()
        result = await ext.execute()
        elapsed = time.monotonic() - start

        assert result is None
        assert elapsed < 35.0, (
            f"EpisodeGroupingJob.execute() took {elapsed:.1f}s after "
            "_run() was patched to sleep 60s; expected ~30s timeout"
        )

    @pytest.mark.asyncio
    async def test_contradiction_detection_execute_returns_on_timeout(
        self, contradiction_detection_module
    ) -> None:
        """``ContradictionDetectionJob.execute()`` must return within
        ~35s when ``_run()`` exceeds the 30s ``asyncio.wait_for``
        deadline."""
        ext = contradiction_detection_module.ContradictionDetectionJob(
            agent=_FAKE_AGENT
        )

        async def _slow_run(**kwargs: Any) -> None:
            await asyncio.sleep(60)

        ext._run = _slow_run  # type: ignore[method-assign]

        start = time.monotonic()
        result = await ext.execute()
        elapsed = time.monotonic() - start

        assert result is None
        assert elapsed < 35.0, (
            f"ContradictionDetectionJob.execute() took {elapsed:.1f}s "
            "after _run() was patched to sleep 60s; expected ~30s timeout"
        )


# ---------------------------------------------------------------------------
# Bug-fix batch 2 regression tests
# ---------------------------------------------------------------------------


class TestContradictionDetectionConfigKey:
    """Regression tests for the duplicated config-key typo.

    A previous version of ``_30_contradiction_detection.py`` looked up
    ``_LC_DEFAULTS["contradiction_detection_detection_enabled"]`` (the
    word "detection" was duplicated). The correct key — matching both
    the outer ``config.get()`` call and the ``DEFAULT_CONFIG`` dict in
    ``helpers/lifecycle.py`` — is ``contradiction_detection_enabled``.
    """

    def test_runtxt_uses_non_duplicated_key(
        self, contradiction_detection_module
    ) -> None:
        """The ``_run()`` method must read ``contradiction_detection_enabled``,
        never the duplicated variant."""
        import inspect

        src = inspect.getsource(contradiction_detection_module)
        # Must not contain the duplicated form anywhere
        assert "contradiction_detection_detection_enabled" not in src, (
            "Contradiction detection extension still references the "
            "duplicated config key 'contradiction_detection_detection_enabled'"
        )
        # Must contain the correct non-duplicated form
        assert "contradiction_detection_enabled" in src, (
            "Contradiction detection extension does not reference the "
            "correct config key 'contradiction_detection_enabled'"
        )

    def test_default_config_has_correct_key(self) -> None:
        """``helpers/lifecycle.DEFAULT_CONFIG`` must define the correct key."""
        from usr.plugins.neuro_core.helpers.lifecycle import DEFAULT_CONFIG

        assert "contradiction_detection_enabled" in DEFAULT_CONFIG
        # Must not contain the duplicated form
        assert "contradiction_detection_detection_enabled" not in DEFAULT_CONFIG
        # The value must be a bool (True or False)
        assert isinstance(DEFAULT_CONFIG["contradiction_detection_enabled"], bool)


class TestExecuteMigrationMemoryParam:
    """Regression tests for the ``Memory`` NameError in ``execute.py``.

    A previous version of ``execute.py`` bound ``Memory`` inside
    ``_import_memory()`` and then referenced the module-level ``Memory``
    name in ``_migrate_subdir()`` — which raised ``NameError: name
    'Memory' is not defined`` because the function was not in the
    same scope as the import.

    The fix threads ``memory_cls`` through ``_run_migration()`` →
    ``_migrate_subdir()`` so the function never needs to look up the
    ``Memory`` name in its own scope.
    """

    def test_migrate_subdir_signature_accepts_memory_cls(self) -> None:
        """``_migrate_subdir`` must accept ``memory_cls`` as a parameter."""
        import inspect

        import usr.plugins.neuro_core.execute as execute_mod

        sig = inspect.signature(execute_mod._migrate_subdir)
        assert "memory_cls" in sig.parameters, (
            f"_migrate_subdir() must accept 'memory_cls' as a parameter; "
            f"got parameters: {list(sig.parameters)}"
        )

    def test_run_migration_passes_memory_cls_to_migrate_subdir(
        self,
    ) -> None:
        """``_run_migration()`` must call ``_migrate_subdir(subdir, memory_cls)``,
        not ``_migrate_subdir(subdir)``."""
        import inspect

        import usr.plugins.neuro_core.execute as execute_mod

        src = inspect.getsource(execute_mod._run_migration)
        assert "_migrate_subdir(subdir, memory_cls)" in src, (
            "_run_migration() must pass memory_cls to _migrate_subdir(); "
            "this is the regression that caused 'name Memory is not defined'"
        )
        # The old broken call pattern must not be present
        assert "_migrate_subdir(subdir)" not in src, (
            "_run_migration() still calls _migrate_subdir(subdir) without "
            "passing memory_cls — this triggers NameError at runtime"
        )

    def test_migrate_subdir_uses_memory_cls_not_module_name(self) -> None:
        """``_migrate_subdir()`` must call ``memory_cls.get_by_subdir(...)``,
        never the module-level ``Memory.get_by_subdir(...)``."""
        import inspect

        import usr.plugins.neuro_core.execute as execute_mod

        src = inspect.getsource(execute_mod._migrate_subdir)
        # Must use the parameter name
        assert "memory_cls.get_by_subdir" in src, (
            "_migrate_subdir() must call memory_cls.get_by_subdir() — "
            "using Memory.get_by_subdir() triggers NameError"
        )
        # Must not reference the bare Memory name in get_by_subdir context
        assert "Memory.get_by_subdir" not in src, (
            "_migrate_subdir() still references Memory.get_by_subdir() "
            "directly — this triggers 'name Memory is not defined' at runtime"
        )

    def test_run_migration_completes_without_name_error(self) -> None:
        """End-to-end: ``_run_migration(stub, stub)`` must complete without
        raising ``NameError`` when the function is the only scope that
        knows about the ``Memory`` class."""
        import asyncio

        import usr.plugins.neuro_core.execute as execute_mod

        class _StubMemory:
            def __init__(self) -> None:
                self.get_by_subdir_calls: list = []

            @staticmethod
            async def get_by_subdir(subdir, log_item=None):
                # Note: log_item is accepted to match the real Memory.get_by_subdir
                # signature. The test does not assert on it.
                pass

                class _MemObj:
                    class _Db:
                        def get_all_docs(self_inner):
                            return {}

                    db = _Db()

                    async def update_documents(self_inner, docs):
                        return None

                return _MemObj()

        def _stub_get_subdirs() -> list[str]:
            return ["default", "projects/neuro_core"]

        # This call would have raised NameError before the fix
        processed, scanned, updated = asyncio.run(
            execute_mod._run_migration(_stub_get_subdirs, _StubMemory)
        )
        assert processed == 2
        assert scanned == 0
        assert updated == 0


class TestAbsDbDirMigration:
    """Regression tests for the ``Memory._get_abs_db_dir`` → ``abs_db_dir``
    migration in helpers/scores.py and helpers/graph_store.py."""

    def test_scores_helper_uses_abs_db_dir(self) -> None:
        """``helpers/scores.py`` must call ``abs_db_dir`` (module-level
        function from ``plugins._memory.helpers.memory``), not
        ``Memory._get_abs_db_dir`` (which does not exist)."""
        import inspect

        from usr.plugins.neuro_core.helpers import scores as scores_mod

        src = inspect.getsource(scores_mod)
        # The correct function call must be present
        assert "abs_db_dir(memory_subdir)" in src, (
            "helpers/scores.py must call abs_db_dir(memory_subdir) — "
            "this is the correct module-level function from _memory"
        )
        # The wrong attribute access must not be present
        assert "Memory._get_abs_db_dir" not in src, (
            "helpers/scores.py still references Memory._get_abs_db_dir — "
            "this attribute does not exist on the Memory class"
        )

    def test_graph_store_helper_uses_abs_db_dir(self) -> None:
        """``helpers/graph_store.py`` must call ``abs_db_dir`` (module-level
        function from ``plugins._memory.helpers.memory``), not
        ``Memory._get_abs_db_dir`` (which does not exist)."""
        import inspect

        from usr.plugins.neuro_core.helpers import graph_store as gs_mod

        src = inspect.getsource(gs_mod)
        assert "abs_db_dir(memory_subdir)" in src, (
            "helpers/graph_store.py must call abs_db_dir(memory_subdir)"
        )
        assert "Memory._get_abs_db_dir" not in src, (
            "helpers/graph_store.py still references Memory._get_abs_db_dir — "
            "this attribute does not exist on the Memory class"
        )

    def test_job_loop_extensions_do_not_reference_old_api(self) -> None:
        """None of the three job_loop extensions should reference the old
        ``Memory._get_abs_db_dir`` API. (A previous run warned that the
        decay job called this non-existent method on every subdir.)"""
        import inspect

        from usr.plugins.neuro_core.extensions.python.job_loop import (
            _10_access_decay,
            _20_episode_grouping,
            _30_contradiction_detection,
        )

        for mod in (
            _10_access_decay,
            _20_episode_grouping,
            _30_contradiction_detection,
        ):
            src = inspect.getsource(mod)
            assert "Memory._get_abs_db_dir" not in src, (
                f"{mod.__name__} still references Memory._get_abs_db_dir — "
                "this attribute does not exist on the Memory class"
            )
            assert "Memory.get_abs_db_dir" not in src, (
                f"{mod.__name__} still references Memory.get_abs_db_dir — "
                "this attribute does not exist on the Memory class"
            )

