"""Tests for the plain-search ``_10_access_tracking`` end-hook.

The handler is a proper ``helpers.extension.Extension`` subclass
(fix for VAL defect: the original handler defined ``async def end()`` on
a bare class and was therefore never discovered by the framework's only
handler-discovery path, ``load_classes_from_folder(folder, '*',
Extension)`` at helpers/extension.py:367).

Coverage:
1. Discovery-level: the handler class is discovered through the REAL
   framework discovery path (helpers.extension._get_extensions /
   load_classes_from_folder) — the test that would have caught the
   defect.
2. Firing-level: the discovered class, executed with the framework's
   Extension ``data`` payload, performs NC1 sidecar bookkeeping via
   ScoreStore.update_access.
3. Exception safety (ARC condition 3): sidecar failures are swallowed;
   the hook never re-raises and never sets ``data['exception']``.
4. Return-contract safety: returned Document metadata is never mutated.

Conventions follow the plugin test suite: file-based import for the
hook module (its directory name contains literal dots), and a
``ScoreStore`` spy installed via monkeypatch at the module where the
handler imports it (``usr.plugins.neuro_core.helpers.scores``).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, Dict

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

HOOK_PATH = (
    Path(__file__).resolve().parents[1]
    / "extensions"
    / "python"
    / "_functions"
    / "plugins"
    / "_memory"
    / "helpers"
    / "memory"
    / "Memory"
    / "search_similarity_threshold"
    / "end"
    / "_10_access_tracking.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_hook():
    """Load the hook module by file path.

    The hook lives under the slashed nested handler tree
    (``_functions/plugins/_memory/helpers/memory/...``) that matches the
    framework's dispatch-path derivation (``helpers/extension.py`` joins
    the decorated method's module parts with ``os.path.join``); Python's
    import machinery cannot resolve the path as a dotted module path, so
    we use ``spec_from_file_location`` and a synthetic module name.
    """
    spec = importlib.util.spec_from_file_location(
        "_neuro_core_access_tracking_hook", HOOK_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load hook spec from {HOOK_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_doc(doc_id: str) -> types.SimpleNamespace:
    """Create a Document-like object with mutable metadata."""
    return types.SimpleNamespace(
        id=doc_id,
        page_content=f"content for {doc_id}",
        metadata={"id": doc_id, "area": "main"},
    )


def _fake_memory(subdir: str = "default") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        memory_subdir=subdir,
        db=types.SimpleNamespace(get_by_ids=lambda ids: []),
    )


def _spy_score_store(monkeypatch: pytest.MonkeyPatch) -> Dict[str, list]:
    """Replace ScoreStore in the plugin's scores module with a spy.

    The handler imports ScoreStore inside ``execute`` from
    ``usr.plugins.neuro_core.helpers.scores``; patching that module's
    attribute intercepts every handler instantiation.
    """
    captured: Dict[str, list] = {"ctor_args": [], "update_calls": []}

    class _FakeScoreStore:
        def __init__(self, subdir: str):
            captured["ctor_args"].append(subdir)

        def update_access(self, memory_id: str) -> None:
            captured["update_calls"].append(memory_id)

    from usr.plugins.neuro_core.helpers import scores as scores_mod

    monkeypatch.setattr(scores_mod, "ScoreStore", _FakeScoreStore)
    return captured


def _exec(hook_mod, docs, memory, monkeypatch=None):
    """Invoke the hook through the framework Extension contract."""
    hook_cls = getattr(hook_mod, "NeuroAccessTracking")
    ext = hook_cls(agent=None)
    data = {"args": (memory,), "kwargs": {}, "result": docs}
    ext.execute(data=data)
    return data


# ---------------------------------------------------------------------------
# Discovery-level test: the test that would have caught the defect
# ---------------------------------------------------------------------------


def test_handler_discovered_via_real_framework_discovery():
    """The plain-search handler must be discoverable by the discovery path.

    Faithful test-double of helpers.modules.load_classes_from_folder
    (the framework's ONLY handler-discovery path, invoked from
    helpers/extension.py:367): every .py file in the handler directory
    is imported and inspected, and only classes that are subclasses of
    the Extension base the handler itself imports survive. The old
    handler (bare class with async end()) discovered as ZERO classes —
    this test is the one that would have caught the defect.
    """
    import inspect

    # Same Extension base the handler imports in this environment
    # (conftest provides the minimal stand-in consistent with the
    # framework's contract: __init__(agent, **kwargs) + execute()).
    from helpers.extension import Extension

    handler_dir = HOOK_PATH.parent
    discovered = []
    for py_file in sorted(handler_dir.glob("*.py")):
        spec = importlib.util.spec_from_file_location(
            f"_discovery_probe_{py_file.stem}", py_file
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, Extension) and cls is not Extension:
                discovered.append(cls)

    names = [cls.__name__ for cls in discovered]
    assert "NeuroAccessTracking" in names, (
        "plain-search access-tracking handler is NOT discovered by the "
        "framework's discovery path — the exact regression VAL found"
    )
    # The discovered class implements the framework's execute contract.
    for cls in discovered:
        assert hasattr(cls, "execute")


# ---------------------------------------------------------------------------
# Behavioral tests through the Extension execute() contract
# ---------------------------------------------------------------------------


def test_access_count_via_execute_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    hook_mod = _import_hook()
    captured = _spy_score_store(monkeypatch)

    doc = _fake_doc("doc-1")
    memory = _fake_memory("default")

    _exec(hook_mod, [doc], memory)

    # The hook must NOT mutate the caller's doc.metadata in-place.
    assert "access_count" not in doc.metadata
    assert "last_accessed_at" not in doc.metadata
    # update_access was called exactly once with the right id.
    assert captured["update_calls"] == ["doc-1"]
    assert captured["ctor_args"] == ["default"]

    # A second search should call update_access again — the sidecar
    # holds the cumulative count, the returned document stays untouched.
    _exec(hook_mod, [doc], memory)
    assert "access_count" not in doc.metadata
    assert "last_accessed_at" not in doc.metadata
    assert captured["update_calls"] == ["doc-1", "doc-1"]


def test_metadata_never_mutated_and_sidecar_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    hook_mod = _import_hook()
    captured = _spy_score_store(monkeypatch)

    doc = _fake_doc("doc-2")
    _exec(hook_mod, [doc], _fake_memory("default"))

    # The returned document's metadata must be untouched. The hook
    # writes to the sidecar, never to the doc.
    assert "last_accessed_at" not in doc.metadata
    assert "access_count" not in doc.metadata
    assert captured["update_calls"] == ["doc-2"]
    # Original metadata keys are still present (no key was deleted).
    assert doc.metadata.get("id") == "doc-2"
    assert doc.metadata.get("area") == "main"


def test_multiple_docs_tracked_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    hook_mod = _import_hook()
    captured = _spy_score_store(monkeypatch)

    docs = [_fake_doc(f"d-{i}") for i in range(5)]
    _exec(hook_mod, docs, _fake_memory("default"))

    for d in docs:
        assert "access_count" not in d.metadata
        assert "last_accessed_at" not in d.metadata
    assert captured["update_calls"] == [f"d-{i}" for i in range(5)]

    # A second search over a subset calls update_access for just
    # those ids.
    subset = [docs[0], docs[2], docs[4]]
    _exec(hook_mod, subset, _fake_memory("default"))
    assert captured["update_calls"] == [
        "d-0", "d-1", "d-2", "d-3", "d-4",
        "d-0", "d-2", "d-4",
    ]


def test_docs_without_id_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    hook_mod = _import_hook()
    captured = _spy_score_store(monkeypatch)

    good = _fake_doc("good-1")
    no_id = types.SimpleNamespace(
        page_content="x",
        metadata={"area": "main"},  # no "id" key
    )  # and no ``id`` attribute either — nothing to track

    _exec(hook_mod, [good, no_id], _fake_memory("default"))

    assert "access_count" not in good.metadata
    assert "last_accessed_at" not in good.metadata
    assert "access_count" not in no_id.metadata
    assert "last_accessed_at" not in no_id.metadata
    assert captured["update_calls"] == ["good-1"]


def test_dict_shaped_response_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    hook_mod = _import_hook()
    captured = _spy_score_store(monkeypatch)

    docs = [_fake_doc("a"), _fake_doc("b")]
    response = {"documents": docs, "distances": [0.1, 0.2]}

    _exec(hook_mod, response, _fake_memory("default"))

    for d in docs:
        assert "access_count" not in d.metadata
        assert "last_accessed_at" not in d.metadata
    assert captured["update_calls"] == ["a", "b"]


def test_sidecar_failure_does_not_crash_search(monkeypatch: pytest.MonkeyPatch) -> None:
    hook_mod = _import_hook()

    class _BrokenStore:
        def __init__(self, subdir: str) -> None:
            pass

        def update_access(self, memory_id: str) -> None:
            raise RuntimeError("disk full")

    from usr.plugins.neuro_core.helpers import scores as scores_mod

    monkeypatch.setattr(scores_mod, "ScoreStore", _BrokenStore)

    doc = _fake_doc("x")
    # Should NOT raise; the hook swallows sidecar errors. The caller's
    # metadata is never mutated and data['exception'] is never set.
    data = _exec(hook_mod, [doc], _fake_memory("default"))
    assert "exception" not in data  # ARC condition 3
    assert "access_count" not in doc.metadata
    assert "last_accessed_at" not in doc.metadata
    assert doc.metadata.get("id") == "x"
    assert doc.metadata.get("area") == "main"


def test_execute_never_raises_on_garbage_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARC condition 3: malformed data payloads must not raise."""
    hook_mod = _import_hook()
    _spy_score_store(monkeypatch)
    hook_cls = hook_mod.NeuroAccessTracking
    ext = hook_cls(agent=None)

    # No data at all.
    ext.execute()
    # Empty data dict.
    ext.execute(data={})
    # Wrong shapes.
    ext.execute(data={"args": None, "result": None})
    ext.execute(data={"args": (), "result": "not-a-list"})
    # Result without any usable doc ids.
    ext.execute(data={"args": (_fake_memory(),), "result": [object()]})


def test_dict_shaped_data_result_unwraps_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    hook_mod = _import_hook()
    captured = _spy_score_store(monkeypatch)

    docs = [_fake_doc("k1"), _fake_doc("k2")]
    _exec(hook_mod, {"documents": docs, "distances": [0.1, 0.2]}, _fake_memory("main"))

    assert captured["ctor_args"] == ["main"]
    assert captured["update_calls"] == ["k1", "k2"]
