"""WI-P101 (PM-024) — runtime-exposure sweep for the documentation guard.

Extends the pre-release documentation guard (tests/test_wip71_prerelease_leak_pin.py,
textual only) into the runtime: tool ``Response`` messages, API error payloads,
and serialized context-graph output must be free of

* internal absolute paths (``/a0/...`` host paths),
* host identifiers (the running container's hostname), and
* internal governance references (``.a0proj``, ``notepad_temp``,
  ``WI-``/``KI-``/``D-NC1-`` record ids, workflow/decision file names,
  internal team role names, prose ``NC1``).

This converts the README Known Limitations statement from "textual only" to
"textual + runtime-output-tested". It is NOT a data-leak prevention mechanism
and must not be described as one.

Fixture discipline: disposable fixtures only (tmp_path SQLite DB, synthetic
graph objects, stubbed Memory backend). No live data writes.
"""
from __future__ import annotations

import json
import os
import re
import socket
import types

import pytest

from helpers.tool import Response


# ---------------------------------------------------------------------------
# Guard vocabulary (reused from tests/test_wip71_prerelease_leak_pin.py)
# ---------------------------------------------------------------------------

# Internal absolute host paths — any /a0/... reference in runtime output.
_INTERNAL_ABS_PATH = re.compile(r"/a0(?:/|\b)")

# Internal governance references.
_FORBIDDEN_SUBSTRINGS = (
    ".a0proj",
    "notepad_temp",
    "workflow-state.yaml",
    "decision_log",
    "decisions.md",
    "nc1-orchestrator",
    "nc1-integrator",
    "nc1-architect",
    "nc1-validator",
    "nc1-narrator",
)

# Record identifiers and prose NC1 (same shape as WI-P71's _RECORD_ID/_PROSE_NC1,
# but for runtime outputs even record identifiers are leaks).
_RECORD_ID = re.compile(r"\b(?:WI|KI|D-NC1|ADR-NC1|RG|OA|RV|PM)-[A-Za-z0-9]+")
_PROSE_NC1 = re.compile(r"(?<![\w-])NC1(?!-\d)")


def _leaks(text: str, hostname: str) -> list[str]:
    """Return a list of leak descriptions found in ``text``."""
    found: list[str] = []
    if _INTERNAL_ABS_PATH.search(text):
        found.append("internal absolute path")
    for sub in _FORBIDDEN_SUBSTRINGS:
        if sub in text:
            found.append(f"governance reference: {sub!r}")
    if _RECORD_ID.search(text):
        found.append("internal record identifier")
    if _PROSE_NC1.search(text):
        found.append("prose NC1 team name")
    if hostname and len(hostname) >= 8 and hostname in text:
        found.append("host identifier (hostname)")
    return found


def _assert_clean(text: str, surface: str, hostname: str = "") -> None:
    hits = _leaks(text, hostname)
    assert not hits, f"{surface} leaked internal content: {hits}\n---\n{text}\n---"


def _hostname() -> str:
    try:
        return socket.gethostname() or ""
    except Exception:  # pragma: no cover - defensive
        return ""


def _make_tool(tool_cls, **kwargs):
    return tool_cls(
        agent=object(),
        name=tool_cls.__name__,
        method=None,
        args={},
        message=types.SimpleNamespace(content=""),
        loop_data=None,
        **kwargs,
    )


def _make_request(path: str = "/api/plugins/neuro_core/x", method: str = "GET"):
    return types.SimpleNamespace(path=path, method=method, args={})


async def _collect_response(resp) -> str:
    """Render a tool result (Response or raised ValueError) as text."""
    if isinstance(resp, Response):
        parts = [resp.message or ""]
        additional = getattr(resp, "additional", None) or {}
        parts.extend(f"{k}={v}" for k, v in additional.items())
        return "\n".join(str(p) for p in parts)
    return str(resp)


# ---------------------------------------------------------------------------
# 1. Tool Response messages — all six tools, success and error paths
# ---------------------------------------------------------------------------


class TestToolResponseSurfaces:
    @pytest.mark.asyncio
    async def test_capture_success_and_error_clean(self, tmp_path):
        from usr.plugins.neuro_core.tools import neuro_capture

        neuro_capture._resolve_db_path = lambda: str(tmp_path / "capture.db")
        tool = _make_tool(neuro_capture.NeuroCapture)

        ok = await tool.execute(text="synthetic memory text", project="p1")
        _assert_clean(await _collect_response(ok), "neuro_capture success")

        with pytest.raises(ValueError) as exc_info:
            await tool.execute(text="", project="p1")
        _assert_clean(str(exc_info.value), "neuro_capture arg error")

    @pytest.mark.asyncio
    async def test_validate_success_and_error_clean(self, tmp_path):
        from usr.plugins.neuro_core.tools import neuro_capture, neuro_validate

        # Both tools must resolve to the SAME disposable DB — the capture
        # inside this test must never touch the real plugin database.
        db_path = str(tmp_path / "validate.db")
        neuro_capture._resolve_db_path = lambda: db_path
        neuro_validate._resolve_db_path = lambda: db_path
        cap = _make_tool(neuro_capture.NeuroCapture)
        ok = await cap.execute(text="synthetic memory text", project="p1")
        memory_id = ok.additional["memory_id"]

        tool = _make_tool(neuro_validate.NeuroValidate)
        ok2 = await tool.execute(memory_id=memory_id, state="validated")
        _assert_clean(await _collect_response(ok2), "neuro_validate success")

        with pytest.raises(ValueError) as exc_info:
            await tool.execute(memory_id=memory_id, state="bogus-state")
        _assert_clean(str(exc_info.value), "neuro_validate state error")

    @pytest.mark.asyncio
    async def test_retrieve_error_clean(self):
        from usr.plugins.neuro_core.tools import neuro_retrieve

        tool = _make_tool(neuro_retrieve.NeuroRetrieve)
        resp = await tool.execute(query="")
        _assert_clean(await _collect_response(resp), "neuro_retrieve error")

    @pytest.mark.asyncio
    async def test_score_error_paths_clean(self, memory_subdir):
        from plugins._memory.helpers import memory as memory_mod
        from usr.plugins.neuro_core.tools import memory_score

        fake = types.SimpleNamespace(
            db=types.SimpleNamespace(get_by_ids=lambda ids: []),
            memory_subdir=memory_subdir,
        )

        async def _fake_get(*a, **kw):
            return fake

        memory_mod.Memory.get = staticmethod(_fake_get)
        tool = _make_tool(memory_score.MemoryScore)

        resp = await tool.execute(id="")
        _assert_clean(await _collect_response(resp), "memory_score missing-id")

        resp = await tool.execute(id="nope-123")
        _assert_clean(await _collect_response(resp), "memory_score not-found")

    @pytest.mark.asyncio
    async def test_reflect_error_paths_clean(self, memory_subdir):
        from usr.plugins.neuro_core.tools import memory_reflect

        async def _fake_collect(subdir, episode_id, db, limit=20):
            return []

        memory_reflect.collect_episode_memories = _fake_collect
        tool = _make_tool(memory_reflect.MemoryReflect)

        resp = await tool.execute(episode_id="")
        _assert_clean(await _collect_response(resp), "memory_reflect missing-id")

        resp = await tool.execute(episode_id="ep-1")
        _assert_clean(await _collect_response(resp), "memory_reflect no-memories")

    @pytest.mark.asyncio
    async def test_relate_error_paths_clean(self, memory_subdir):
        from plugins._memory.helpers import memory as memory_mod
        from usr.plugins.neuro_core.tools import memory_relate

        fake = types.SimpleNamespace(
            db=types.SimpleNamespace(get_by_ids=lambda ids: []),
            memory_subdir=memory_subdir,
        )

        async def _fake_get(*a, **kw):
            return fake

        memory_mod.Memory.get = staticmethod(_fake_get)
        tool = _make_tool(memory_relate.MemoryRelate)

        for kwargs in (
            {"from_id": "", "to_id": "b", "rel_type": "supports"},
            {"from_id": "a", "to_id": "", "rel_type": "supports"},
            {"from_id": "a", "to_id": "b", "rel_type": "bogus"},
            {"from_id": "a", "to_id": "a", "rel_type": "supports"},
            {"from_id": "a", "to_id": "b", "rel_type": "supports"},
        ):
            resp = await tool.execute(**kwargs)
            _assert_clean(
                await _collect_response(resp),
                f"memory_relate error {kwargs}",
            )


# ---------------------------------------------------------------------------
# 2. API error payloads — all handlers, validation error paths
# ---------------------------------------------------------------------------


class TestApiErrorPayloads:
    def _check(self, result, surface):
        assert isinstance(result, dict), f"{surface}: expected dict payload"
        assert result.get("success") is False or "error" in result, (
            f"{surface}: expected an error payload, got {result}"
        )
        _assert_clean(json.dumps(result), surface, _hostname())

    @pytest.mark.asyncio
    async def test_context_graph_error_clean(self):
        from usr.plugins.neuro_core.api import context_graph as api_mod

        handler = api_mod.ContextGraphApi()
        req = _make_request("/api/plugins/neuro_core/context_graph")
        self._check(await handler.process({}, req), "context_graph missing-query")
        self._check(
            await handler.process({"query": "q"}, req),
            "context_graph missing-subdir",
        )
        req_bad = _make_request("/api/plugins/neuro_core/bogus", method="PATCH")
        self._check(await handler.process({}, req_bad), "context_graph unknown-route")

    @pytest.mark.asyncio
    async def test_relationships_error_clean(self):
        from usr.plugins.neuro_core.api import relationships as api_mod

        handler = api_mod.RelationshipsApi()
        req = _make_request("/api/plugins/neuro_core/relationships")
        # GET with `id` but no `memory_subdir` hits the missing-subdir
        # validation error (GET without `id` routes to the list-all path,
        # which legitimately succeeds on an empty disposable store).
        self._check(await handler.process({"id": "x"}, req), "relationships missing-subdir")
        self._check(
            await handler.process(
                {"memory_subdir": "main", "from_id": "a", "to_id": "a",
                 "rel_type": "supports"},
                _make_request(method="POST"),
            ),
            "relationships self-reference",
        )
        self._check(
            await handler.process({}, _make_request(method="PATCH")),
            "relationships unknown-route",
        )

    @pytest.mark.asyncio
    async def test_memory_edit_error_clean(self):
        from usr.plugins.neuro_core.api import memory_edit as api_mod

        handler = api_mod.MemoryEditApi()
        req = _make_request("/api/plugins/neuro_core/memory_edit")
        self._check(await handler.process({}, req), "memory_edit missing-subdir")
        self._check(
            await handler.process({"memory_subdir": "main"}, req),
            "memory_edit missing-id",
        )
        self._check(
            await handler.process({"memory_subdir": "main", "id": "x"}, req),
            "memory_edit nothing-to-update",
        )
        self._check(
            await handler.process(
                {"memory_subdir": "main", "id": "x", "scores": {"bogus": 1}},
                _make_request(method="POST"),
            ),
            "memory_edit bad-scores",
        )

    @pytest.mark.asyncio
    async def test_memory_names_error_clean(self):
        from usr.plugins.neuro_core.api import memory_names as api_mod

        handler = api_mod.MemoryNamesApi()
        req = _make_request("/api/plugins/neuro_core/memory_names")
        self._check(await handler.process({}, req), "memory_names missing-subdir")
        self._check(
            await handler.process({"memory_subdir": "main"}, req),
            "memory_names missing-id",
        )
        self._check(
            await handler.process(
                {"memory_subdir": "main", "id": "x", "cluster_key": "y"},
                _make_request(method="POST"),
            ),
            "memory_names ambiguous-target",
        )

    @pytest.mark.asyncio
    async def test_reflection_audit_error_clean(self):
        from usr.plugins.neuro_core.api import reflection_audit as api_mod

        handler = api_mod.ReflectionAuditApi()
        req = _make_request("/api/plugins/neuro_core/reflection_audit")
        self._check(await handler.process({}, req), "reflection_audit missing-subdir")
        self._check(
            await handler.process({}, _make_request(method="PATCH")),
            "reflection_audit unknown-route",
        )

    @pytest.mark.asyncio
    async def test_episode_audit_error_clean(self):
        from usr.plugins.neuro_core.api import episode_audit as api_mod

        handler = api_mod.EpisodeAuditApi()
        req = _make_request("/api/plugins/neuro_core/episode_audit")
        self._check(await handler.process({}, req), "episode_audit missing-subdir")
        self._check(
            await handler.process({}, _make_request(method="PATCH")),
            "episode_audit unknown-route",
        )

    @pytest.mark.asyncio
    async def test_advanced_filters_error_clean(self):
        from usr.plugins.neuro_core.api import advanced_filters as api_mod

        handler = api_mod.AdvancedFiltersApi()
        req = _make_request("/api/plugins/neuro_core/advanced_filters")
        self._check(await handler.process({}, req), "advanced_filters missing-subdir")
        self._check(
            await handler.process({}, _make_request(method="PATCH")),
            "advanced_filters unknown-route",
        )


# ---------------------------------------------------------------------------
# 3. Serialized graph output — synthetic ContextGraph
# ---------------------------------------------------------------------------


class TestSerializedGraphOutput:
    def _synthetic_graph(self):
        from usr.plugins.neuro_core.helpers.context_graph import (
            ContextGraph,
            GraphEdge,
            GraphNode,
        )

        nodes = [
            GraphNode(doc_id="seed-a", content="synthetic seed content",
                      metadata={"memory_type": "fact", "area": "main"},
                      score=0.9, hop=0),
            GraphNode(doc_id="hop1-b", content="synthetic neighbor content",
                      metadata={"memory_type": "concept", "area": "main"},
                      score=0.5, hop=1),
        ]
        edges = [
            GraphEdge(from_id="seed-a", to_id="hop1-b", type="supports",
                      confidence=0.9),
        ]
        return ContextGraph(nodes=nodes, edges=edges, query="synthetic query",
                            seed_ids=["seed-a"])

    def test_serialized_graph_clean(self):
        from usr.plugins.neuro_core.api.context_graph import (
            _serialize_context_graph,
        )

        serialized = _serialize_context_graph(self._synthetic_graph())
        encoded = json.dumps(serialized)
        _assert_clean(encoded, "serialized context graph", _hostname())

    def test_prompt_text_clean(self):
        graph = self._synthetic_graph()
        _assert_clean(graph.to_prompt_text(), "graph prompt text", _hostname())

    def test_empty_graph_serialization_clean(self):
        from usr.plugins.neuro_core.api.context_graph import (
            _serialize_context_graph,
        )
        from usr.plugins.neuro_core.helpers.context_graph import ContextGraph

        encoded = json.dumps(_serialize_context_graph(ContextGraph()))
        _assert_clean(encoded, "empty serialized graph", _hostname())
