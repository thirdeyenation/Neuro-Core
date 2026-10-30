"""KI-060 (WI-P99): Response-contract sweep for all six Neuro Core tools.

Contract (grounded in the framework extension
``/a0/extensions/python/tool_execute_after/_10_mask_secrets.py``): every
tool return value reaches ``response.message`` — a raw dict return
crashes the post-tool extension chain with ``AttributeError``.

Coverage:
1. Behavioral: every reachable return path of all six tools returns a
   ``helpers.tool.Response`` instance (never a dict), with a string
   ``message`` and ``break_loop=False``.
2. Static (AST): no tool module contains a ``return {…}`` statement —
   the KI-060 defect shape is structurally excluded.

Isolation: capture/validate DB paths are monkeypatched to tmp files;
sidecar writes go through the conftest ``memory_subdir`` fixture
(disposable/synthetic policy). The live ``neuro_core.db`` is never
touched. Collaborator doubles for neuro_retrieve replicate the verified
doubles in test_neuro_retrieve_tool.py (the tool's ``_serialize`` needs
``ScoreStore.get_optional`` semantics).
"""

from __future__ import annotations

import ast
import importlib
import sys
import types
from pathlib import Path

import pytest

from helpers.tool import Response


_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

pytestmark = pytest.mark.asyncio

_TOOLS = [
    "neuro_capture",
    "neuro_retrieve",
    "neuro_validate",
    "memory_reflect",
    "memory_relate",
    "memory_score",
]


def _assert_response(result) -> Response:
    """Assert the framework Response contract on one return value."""
    assert isinstance(result, Response), (
        f"tool returned {type(result).__name__}, not Response "
        "(KI-060 contract violation)"
    )
    assert isinstance(result.message, str) and result.message
    assert result.break_loop is False
    return result


def _tool(mod, class_name):
    return getattr(mod, class_name)(
        agent=None, name=class_name, method=None,
        args={}, message="", loop_data=None,
    )


# ---------------------------------------------------------------------------
# 1. neuro_capture — success path (invalid args raise, they never return)
# ---------------------------------------------------------------------------


async def test_capture_success_returns_response(monkeypatch, tmp_path):
    mod = importlib.import_module("tools.neuro_capture")
    monkeypatch.setattr(mod, "_resolve_db_path", lambda: str(tmp_path / "c.db"))
    tool = _tool(mod, "NeuroCapture")
    result = _assert_response(await tool.execute(text="t", project="p"))
    assert result.additional["outcome"] == "stored"
    assert result.additional["memory_id"]
    assert result.additional["scope"] == "p"
    assert result.additional["neuro_core_ack"]


# ---------------------------------------------------------------------------
# 2. neuro_validate — success path (invalid args raise, they never return)
# ---------------------------------------------------------------------------


async def test_validate_success_returns_response(monkeypatch, tmp_path):
    mod = importlib.import_module("tools.neuro_validate")
    db_path = tmp_path / "v.db"
    monkeypatch.setattr(mod, "_resolve_db_path", lambda: str(db_path))

    from neuro_core import Memory, Scope
    from neuro_service import NeuroCoreService
    from sqlite_store import SQLiteStore

    store = SQLiteStore(str(db_path))
    try:
        seeded = NeuroCoreService(store).capture(
            Memory("seed", "agent_zero", Scope("p1"))
        )
    finally:
        store.close()

    tool = _tool(mod, "NeuroValidate")
    result = _assert_response(
        await tool.execute(memory_id=seeded.memory_id, state="validated")
    )
    assert result.additional["validation"] == "validated"
    assert result.additional["outcome"] == "updated"
    assert result.additional["neuro_core_ack"]


# ---------------------------------------------------------------------------
# 3. neuro_retrieve — all three return paths
# ---------------------------------------------------------------------------


class _FakeScoreStore:
    """Inert ScoreStore double with get_optional semantics (KI-008 path)."""

    def __init__(self, subdir: str):
        self.subdir = subdir
        self.records: dict = {}

    def get_optional(self, doc_id: str):
        return self.records.get(doc_id)


class _FakeGraphStore:
    """Inert GraphStore double (no filesystem access)."""

    def __init__(self, subdir: str):
        self.subdir = subdir


def _patch_retrieve(monkeypatch, graph):
    """Patch every lazily-imported collaborator at its source module
    (same verified pattern as test_neuro_retrieve_tool.py)."""
    from plugins._memory.helpers import memory as host_memory_mod
    from usr.plugins.neuro_core.helpers import graph_store as graph_mod
    from usr.plugins.neuro_core.helpers import scores as scores_mod
    from usr.plugins.neuro_core.helpers import retrieval as retrieval_mod

    fake_memory = types.SimpleNamespace(memory_subdir="neuro_test")

    async def _fake_get_by_subdir(memory_subdir, log_item=None,
                                  preload_knowledge=True):
        return fake_memory

    monkeypatch.setattr(host_memory_mod.Memory, "get_by_subdir",
                        _fake_get_by_subdir, raising=False)
    monkeypatch.setattr(graph_mod, "GraphStore", _FakeGraphStore)
    monkeypatch.setattr(scores_mod, "ScoreStore", _FakeScoreStore)

    async def _fake_pipeline(memory, query, graph_store, score_store, config):
        return graph

    monkeypatch.setattr(retrieval_mod, "search_context_graph",
                        _fake_pipeline, raising=True)


def _retrieve_graph():
    return types.SimpleNamespace(
        query="alpha", seed_ids=["d1"],
        nodes=[types.SimpleNamespace(
            doc_id="d1", hop=0, score=0.8, content="c",
            metadata={"id": "d1"},
        )],
        edges=[],
    )


async def test_retrieve_success_returns_response(monkeypatch):
    mod = importlib.import_module("tools.neuro_retrieve")
    _patch_retrieve(monkeypatch, _retrieve_graph())
    tool = _tool(mod, "NeuroRetrieve")
    result = _assert_response(
        await tool.execute(query="alpha", memory_subdir="neuro_test")
    )
    # The message is the JSON-encoded graph payload (agent-visible).
    import json
    payload = json.loads(result.message)
    assert payload["query"] == "alpha"
    assert payload["nodes"][0]["doc_id"] == "d1"
    assert result.additional["result"] == payload
    assert result.additional["neuro_core_ack"]


async def test_retrieve_empty_query_returns_response():
    mod = importlib.import_module("tools.neuro_retrieve")
    tool = _tool(mod, "NeuroRetrieve")
    _assert_response(await tool.execute(query="   "))


async def test_retrieve_unavailable_memory_returns_response(monkeypatch):
    from plugins._memory.helpers import memory as host_memory_mod

    async def _boom(memory_subdir, log_item=None, preload_knowledge=True):
        raise RuntimeError("faiss down")

    monkeypatch.setattr(host_memory_mod.Memory, "get_by_subdir", _boom,
                        raising=False)
    mod = importlib.import_module("tools.neuro_retrieve")
    tool = _tool(mod, "NeuroRetrieve")
    result = _assert_response(
        await tool.execute(query="alpha", memory_subdir="neuro_test")
    )
    assert "unavailable" in result.message.lower()


# ---------------------------------------------------------------------------
# 4. memory_reflect — error, no-docs, and success return paths
# ---------------------------------------------------------------------------


async def test_reflect_all_return_paths_are_responses(monkeypatch):
    mod = importlib.import_module("tools.memory_reflect")
    from plugins._memory.helpers import memory as host_memory_mod

    backend = types.SimpleNamespace(memory_subdir="neuro_test", db=None)

    async def _fake_get(agent):
        return backend

    monkeypatch.setattr(host_memory_mod.Memory, "get", _fake_get,
                        raising=False)

    async def _no_docs(subdir, episode_id, db, limit=20):
        return []

    monkeypatch.setattr(mod, "collect_episode_memories", _no_docs)
    tool = _tool(mod, "MemoryReflect")

    # Missing-arg path.
    _assert_response(await tool.execute(episode_id=""))
    # No-docs path.
    result = _assert_response(await tool.execute(episode_id="e1"))
    assert "no memories" in result.message.lower()

    # Success path.
    async def _docs(subdir, episode_id, db, limit=20):
        return [object()]

    async def _reflect(docs, agent):
        return "insight text"

    async def _write(subdir, content, episode_id, db):
        return "new-id-1"

    monkeypatch.setattr(mod, "collect_episode_memories", _docs)
    monkeypatch.setattr(mod, "reflect_memories", _reflect)
    monkeypatch.setattr(mod, "write_reflection", _write)
    result = _assert_response(await tool.execute(episode_id="e1"))
    assert result.additional["neuro_core_ack"]


# ---------------------------------------------------------------------------
# 5. memory_relate — error and success return paths
# ---------------------------------------------------------------------------


async def test_relate_all_return_paths_are_responses(monkeypatch,
                                                     memory_subdir):
    mod = importlib.import_module("tools.memory_relate")
    from plugins._memory.helpers import memory as host_memory_mod

    doc = types.SimpleNamespace(metadata={"id": "a"})
    doc_b = types_SimpleNamespace_b()
    backend = types.SimpleNamespace(
        memory_subdir=memory_subdir,
        db=types.SimpleNamespace(
            get_by_ids=lambda ids: [doc, doc_b]
            if "a" in ids and "b" in ids else ([doc] if "a" in ids else [])
        ),
    )

    async def _fake_get(agent):
        return backend

    monkeypatch.setattr(host_memory_mod.Memory, "get", _fake_get,
                        raising=False)
    tool = _tool(mod, "MemoryRelate")

    # Missing-arg paths.
    _assert_response(await tool.execute(from_id="", to_id="b",
                                        rel_type="related_to"))
    _assert_response(await tool.execute(from_id="a", to_id="",
                                        rel_type="related_to"))
    _assert_response(await tool.execute(from_id="a", to_id="b",
                                        rel_type=""))
    # Self-referential path.
    _assert_response(await tool.execute(from_id="a", to_id="a",
                                        rel_type="related_to"))
    # Unknown rel_type path.
    _assert_response(await tool.execute(from_id="a", to_id="b",
                                        rel_type="bogus"))
    # Missing-memory path.
    result = _assert_response(
        await tool.execute(from_id="missing", to_id="b",
                           rel_type="related_to")
    )
    assert "not found" in result.message.lower()

    # Success (add-edge) path — both endpoints exist; GraphStore writes
    # into the tmp memory_subdir fixture (disposable policy).
    result = _assert_response(
        await tool.execute(from_id="a", to_id="b", rel_type="related_to")
    )
    assert result.additional["neuro_core_ack"]

    # Remove path with no matching edge (store unchanged).
    result = _assert_response(
        await tool.execute(from_id="a", to_id="b", rel_type="supports",
                           remove=True)
    )
    assert "no matching edge" in result.message.lower()


def types_SimpleNamespace_b():
    return types.SimpleNamespace(metadata={"id": "b"})


# ---------------------------------------------------------------------------
# 6. memory_score — error and success return paths
# ---------------------------------------------------------------------------


async def test_score_all_return_paths_are_responses(monkeypatch,
                                                    memory_subdir):
    mod = importlib.import_module("tools.memory_score")
    from plugins._memory.helpers import memory as host_memory_mod

    doc = types.SimpleNamespace(metadata={"id": "a", "memory_type": "note"})
    backend = types.SimpleNamespace(
        memory_subdir=memory_subdir,
        db=types.SimpleNamespace(get_by_ids=lambda ids: [doc]),
        update_documents=None,
    )

    async def _fake_update_documents(docs):
        return None

    backend.update_documents = _fake_update_documents

    async def _fake_get(agent):
        return backend

    monkeypatch.setattr(host_memory_mod.Memory, "get", _fake_get,
                        raising=False)
    tool = _tool(mod, "MemoryScore")

    # Missing-id path.
    _assert_response(await tool.execute(id=""))
    # Not-found path.
    empty_backend = types.SimpleNamespace(
        memory_subdir=memory_subdir,
        db=types.SimpleNamespace(get_by_ids=lambda ids: []),
    )

    async def _fake_get_empty(agent):
        return empty_backend

    monkeypatch.setattr(host_memory_mod.Memory, "get", _fake_get_empty,
                        raising=False)
    result = _assert_response(await tool.execute(id="nope"))
    assert "no memory found" in result.message.lower()

    # No-updatable-fields path.
    monkeypatch.setattr(host_memory_mod.Memory, "get", _fake_get,
                        raising=False)
    result = _assert_response(await tool.execute(id="a"))
    assert "no updatable fields" in result.message.lower()

    # Success path (sidecar write via the tmp memory_subdir).
    result = _assert_response(await tool.execute(id="a", importance=0.9))
    assert result.additional["neuro_core_ack"]


# ---------------------------------------------------------------------------
# 7. Static AST sweep — no raw dict returns anywhere in the six tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", _TOOLS)
def test_no_raw_dict_return_statements(tool_name):
    """KI-060 defect shape: ``return {…}`` inside a tool's ``execute``
    method is structurally excluded — every value returned to the
    framework must be a Response (or a raise). Helper functions (e.g. a
    serializer whose dict output is JSON-encoded into the Response
    message, or a config fallback) are out of contract scope."""
    source = (Path(_PLUGIN_ROOT) / "tools" / f"{tool_name}.py").read_text()
    tree = ast.parse(source)
    dict_returns = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name != "execute":
                continue
            for ret in ast.walk(node):
                if (isinstance(ret, ast.Return)
                        and isinstance(ret.value, ast.Dict)):
                    dict_returns.append(ret.lineno)
    assert dict_returns == [], (
        f"tools/{tool_name}.py execute() returns raw dicts at lines "
        f"{dict_returns} (KI-060 contract violation)"
    )
