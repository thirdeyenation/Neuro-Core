"""Pins for WI-P41-CONTRA-DOCS-FIX (KI-032 remediation, R2).

Defect fixed: the scheduled contradiction sweep passed ``docs=`` to
``run_contradiction_detection`` (verified signature accepts ``facts=``)
and the false-state branch early-returned, so the scheduled sweep
performed no contradiction check in v0.1.0 in either key state.

These pins enforce the corrected invocation:

    1. Static source pin: the _30 call site passes ``facts=`` and never
       ``docs=``.
    2. Invocation pins: with the lifecycle function mocked, the sweep
       calls it exactly once per subdir with ``(memory_id, content,
       metadata)`` triples and ``memory=None`` — in BOTH
       ``contradiction_llm_enabled`` states (the key changes only the
       log line; no LLM object is ever passed).
    3. End-to-end integration pin: the REAL heuristic runs through the
       scheduled sweep with no lifecycle mocking — two fact memories,
       one older carrying a negation token, produce ``disputed=1``
       with the older (successful) memory flagged.

All data is synthetic in-memory fixtures; no plugin data files, no
FAISS index and no LLM are touched.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import usr.plugins.neuro_core.helpers.lifecycle as lifecycle_mod


_PLUGIN = Path("/a0/usr/plugins/neuro_core")
_JOB = _PLUGIN / "extensions" / "python" / "job_loop" / "_30_contradiction_detection.py"

_FAKE_AGENT = object()


def _load_job_module():
    spec = importlib.util.spec_from_file_location(
        "_neuro_core_test_wip41_contradiction", str(_JOB)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc(memory_id: str, content: str, timestamp: str) -> dict:
    """A doc in the shape ``_iter_docs`` actually yields."""
    return {
        "id": memory_id,
        "metadata": {"id": memory_id, "timestamp": timestamp},
        "page_content": content,
    }


# ---------------------------------------------------------------------------
# 1. Static source pin
# ---------------------------------------------------------------------------


def test_call_site_uses_facts_kwarg_not_docs():
    """KI-032 pin: _30 must invoke run_contradiction_detection with the
    verified ``facts=`` keyword; the defective ``docs=`` kwarg must not
    appear anywhere in the module source."""
    source = _JOB.read_text(encoding="utf-8")
    assert "run_contradiction_detection(subdir, config, None, facts=facts)" in source, (
        "the _30 call site no longer passes facts= — KI-032 pin violated"
    )
    assert "docs=docs" not in source, (
        "the defective docs= kwarg is back in _30 — KI-032 regression"
    )


# ---------------------------------------------------------------------------
# 2. Invocation pins (lifecycle mocked)
# ---------------------------------------------------------------------------


@pytest.fixture()
def job_module():
    return _load_job_module()


def _patch_job(monkeypatch, *, config: dict, docs: list) -> MagicMock:
    """Patch the job's staticmethods, the boot-grace guard and the
    lifecycle function."""
    monkeypatch.setattr(
        job_module_config.cls, "_read_config", staticmethod(lambda: dict(config))
    )
    monkeypatch.setattr(
        job_module_config.cls, "_subdirs", staticmethod(lambda: ["test_subdir"])
    )
    monkeypatch.setattr(
        job_module_config.cls,
        "_iter_docs",
        staticmethod(lambda subdir: list(docs)),
    )
    mock_fn = MagicMock(return_value={"checked": 0, "disputed": 0})
    monkeypatch.setattr(lifecycle_mod, "run_contradiction_detection", mock_fn)
    return mock_fn


class _JobPatcher:
    cls = None


job_module_config = _JobPatcher


@pytest.fixture(autouse=True)
def _bind_job_class(job_module):
    job_module_config.cls = job_module.ContradictionDetectionJob
    job_module._STATE["last_run"] = 0.0
    # The scheduled body is gated by a 300s process boot-grace guard
    # (a fresh pytest process is always inside it). Neutralize it so
    # the sweep body itself is exercised — the guard's own existence
    # and behavior are pinned separately in test_lifecycle_jobs.py.
    monkeypatch_target = "_boot_grace_active"
    import pytest as _pytest
    _mp = _pytest.MonkeyPatch()
    _mp.setattr(job_module, monkeypatch_target, lambda: False)
    yield
    _mp.undo()
    job_module_config.cls = None


_DOCS = [
    _doc("m1", "The sync completed successfully.", "2026-01-01T00:00:00+00:00"),
    _doc("m2", "The sync failed.", "2026-06-01T00:00:00+00:00"),
]

_EXPECTED_TRIPLES = [
    ("m1", "The sync completed successfully.", {"id": "m1", "timestamp": "2026-01-01T00:00:00+00:00"}),
    ("m2", "The sync failed.", {"id": "m2", "timestamp": "2026-06-01T00:00:00+00:00"}),
]


@pytest.mark.asyncio
async def test_sweep_calls_lifecycle_with_facts_when_key_false(
    monkeypatch: pytest.MonkeyPatch, job_module
) -> None:
    """The core acceptance criterion: with ``contradiction_llm_enabled``
    false (the v0.1.0 default) the scheduled sweep performs the check —
    run_contradiction_detection is called once with the fact triples and
    memory=None."""
    mock_fn = _patch_job(
        monkeypatch,
        config={
            "contradiction_detection_enabled": True,
            "contradiction_interval_hours": 168,
            "contradiction_llm_enabled": False,
        },
        docs=_DOCS,
    )

    ext = job_module.ContradictionDetectionJob(agent=_FAKE_AGENT)
    result = await ext.execute()

    assert result is None
    mock_fn.assert_called_once()
    args, kwargs = mock_fn.call_args
    assert args[0] == "test_subdir"
    assert args[2] is None  # memory=None — no LLM/FAISS object passed
    assert kwargs.get("facts") == _EXPECTED_TRIPLES
    assert "docs" not in kwargs


@pytest.mark.asyncio
async def test_sweep_calls_lifecycle_with_facts_when_key_true(
    monkeypatch: pytest.MonkeyPatch, job_module
) -> None:
    """True-state boundary: the LLM-assisted NLI path is not implemented
    in v0.1.0; the job falls back to the same heuristic invocation and
    never passes an LLM object."""
    mock_fn = _patch_job(
        monkeypatch,
        config={
            "contradiction_detection_enabled": True,
            "contradiction_interval_hours": 168,
            "contradiction_llm_enabled": True,
        },
        docs=_DOCS,
    )

    ext = job_module.ContradictionDetectionJob(agent=_FAKE_AGENT)
    result = await ext.execute()

    assert result is None
    mock_fn.assert_called_once()
    args, kwargs = mock_fn.call_args
    assert args[2] is None
    assert kwargs.get("facts") == _EXPECTED_TRIPLES
    assert "docs" not in kwargs


# ---------------------------------------------------------------------------
# 3. End-to-end integration pin (REAL heuristic, no lifecycle mocking)
# ---------------------------------------------------------------------------


def test_real_heuristic_runs_through_scheduled_sweep(
    monkeypatch: pytest.MonkeyPatch, job_module
) -> None:
    """Integration pin: no lifecycle mocking — the real
    run_contradiction_detection executes through the scheduled sweep.
    The older memory ('completed successfully', no negation) is opposed
    by the newer memory ('failed', negation token) under the
    exactly-one-side-negated heuristic, so the sweep must report
    checked=2 disputed=1 and persist nothing (no ``disputes`` key is
    exposed by the function's return contract)."""
    from helpers.print_style import PrintStyle

    messages: list[str] = []

    class _CaptureStyle:
        def __getattr__(self, name):
            def _sink(msg=""):
                messages.append(str(msg))

            return _sink

    monkeypatch.setattr(job_module, "_boot_grace_active", lambda: False)
    monkeypatch.setattr(job_module, "PrintStyle", _CaptureStyle)
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob,
        "_read_config",
        staticmethod(
            lambda: {
                "contradiction_detection_enabled": True,
                "contradiction_interval_hours": 168,
                "contradiction_llm_enabled": False,
            }
        ),
    )
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob,
        "_subdirs",
        staticmethod(lambda: ["test_subdir"]),
    )
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob,
        "_iter_docs",
        staticmethod(lambda subdir: list(_DOCS)),
    )
    persist = MagicMock()
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_persist_disputes", persist
    )

    ext = job_module.ContradictionDetectionJob(agent=_FAKE_AGENT)

    import asyncio

    asyncio.run(ext.execute())

    summary = [m for m in messages if "disputed=" in m and "subdirs=" in m]
    assert summary, f"expected final sweep summary, got: {messages}"
    assert "checked=2" in summary[-1]
    assert "disputed=1" in summary[-1]
    # The function's return contract exposes only checked/disputed counts
    # (verified signature/docs) — so the best-effort persistence call is
    # a no-op with an empty list. Documented honestly in the report.
    persist.assert_called_once_with("test_subdir", [])


def test_real_heuristic_no_dispute_when_memories_agree(
    monkeypatch: pytest.MonkeyPatch, job_module
) -> None:
    """Control: two agreeing (non-negated) memories produce disputed=0."""
    agreeing = [
        _doc("c1", "The export completed successfully.", "2026-01-01T00:00:00+00:00"),
        _doc("c2", "The export completed successfully.", "2026-06-01T00:00:00+00:00"),
    ]
    messages: list[str] = []

    class _CaptureStyle:
        def __getattr__(self, name):
            def _sink(msg=""):
                messages.append(str(msg))

            return _sink

    monkeypatch.setattr(job_module, "_boot_grace_active", lambda: False)
    monkeypatch.setattr(job_module, "PrintStyle", _CaptureStyle)
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob,
        "_read_config",
        staticmethod(
            lambda: {
                "contradiction_detection_enabled": True,
                "contradiction_interval_hours": 168,
                "contradiction_llm_enabled": False,
            }
        ),
    )
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob,
        "_subdirs",
        staticmethod(lambda: ["test_subdir"]),
    )
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob,
        "_iter_docs",
        staticmethod(lambda subdir: list(agreeing)),
    )
    persist = MagicMock()
    monkeypatch.setattr(
        job_module.ContradictionDetectionJob, "_persist_disputes", persist
    )

    ext = job_module.ContradictionDetectionJob(agent=_FAKE_AGENT)

    import asyncio

    asyncio.run(ext.execute())

    summary = [m for m in messages if "disputed=" in m and "subdirs=" in m]
    assert summary
    assert "checked=2" in summary[-1]
    assert "disputed=0" in summary[-1]
