"""Pytest configuration for Neuro Core tests.

The plugin lives at ``/a0/usr/plugins/neuro_core/`` and is imported by
Agent Zero via ``usr.plugins.neuro_core.helpers.X``. Standalone pytest
runs do not have the framework's import machinery, so we add the
framework root (``/a0``) to ``sys.path`` here.

The same fixture also patches ``abs_db_dir`` to return a
per-test temporary directory, so the on-disk sidecars (FAISS index,
``relationships.json``, ``scores.json``) live in a sandbox that pytest
can clean up automatically.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest.mock as mock
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# sys.path setup
# ---------------------------------------------------------------------------


ROOT = Path(__file__).resolve().parents[3]  # tests/ -> neuro_core/ -> plugins/ -> usr/ -> /a0
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Stub minimal framework modules so ``Tool`` and friends can be imported
# without booting the full framework (litellm, models, agent, ...).
# ---------------------------------------------------------------------------


def _ensure_stub(module_name: str, **attrs):
    """Insert a stub module into ``sys.modules`` if not already present."""
    if module_name in sys.modules:
        return
    mod = types.ModuleType(module_name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[module_name] = mod
    return mod


# ``helpers`` is a regular package; without an __init__ it is importable
# but its submodules (like ``helpers.tool``) are not. We give it an empty
# __path__ and then stub ``helpers.tool`` and ``agent`` so ``Tool`` works.
if "helpers" not in sys.modules:
    _helpers = types.ModuleType("helpers")
    _helpers.__path__ = []
    sys.modules["helpers"] = _helpers

if "agent" not in sys.modules:
    _agent = types.ModuleType("agent")
    _agent.Agent = object
    _agent.LoopData = object
    sys.modules["agent"] = _agent

if "helpers.tool" not in sys.modules:
    _tool_mod = types.ModuleType("helpers.tool")

    class _Response:
        def __init__(self, message: str, break_loop: bool = False):
            self.message = message
            self.break_loop = break_loop

    class _Tool:
        """Minimal Tool base class used by tool unit tests."""

        def __init__(self, agent=None, name="", method=None, args=None,
                     message="", loop_data=None, **kwargs):
            self.agent = agent
            self.name = name
            self.method = method
            self.args = args or {}
            self.message = message
            self.loop_data = loop_data

        async def execute(self, **kwargs):  # pragma: no cover - abstract
            raise NotImplementedError

    _tool_mod.Response = _Response
    _tool_mod.Tool = _Tool
    sys.modules["helpers.tool"] = _tool_mod

# Stub ``helpers.extension`` and ``helpers.print_style`` so the
# job_loop extension classes can be imported in tests without
# booting the full framework. The real ``Extension`` base class lives
# in ``/a0/helpers/extension.py`` and the real ``PrintStyle`` in
# ``/a0/helpers/print_style.py``; we only need the attributes the
# job_loop extensions actually touch (``self.agent`` and the
# ``warning``/``error`` print methods).
if "helpers.extension" not in sys.modules:
    _ext_mod = types.ModuleType("helpers.extension")

    class _Extension:
        """Minimal Extension base class used by job_loop unit tests."""

        def __init__(self, agent=None, **kwargs):
            self.agent = agent

        async def execute(self, **kwargs):  # pragma: no cover - abstract
            raise NotImplementedError

    _ext_mod.Extension = _Extension
    sys.modules["helpers.extension"] = _ext_mod

if "helpers.print_style" not in sys.modules:
    _ps_mod = types.ModuleType("helpers.print_style")

    class _PrintStyle:
        """Minimal PrintStyle stand-in — records messages for test inspection."""

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def warning(self, message: str) -> None:
            # In tests, warnings are silent — the test cares about
            # ``execute()`` not raising, not about the exact log
            # output. The real ``PrintStyle`` prints to stdout.
            return None

        def error(self, message: str) -> None:
            return None

        def info(self, message: str) -> None:
            return None

        def print(self, *args, **kwargs) -> None:
            return None

    _ps_mod.PrintStyle = _PrintStyle
    sys.modules["helpers.print_style"] = _ps_mod

# Stub ``helpers.api`` so the API handler module
# (``usr/plugins/neuro_core/api/context_graph.py``) can be imported in
# tests without booting the full Flask / werkzeug stack. The real
# ``ApiHandler`` lives in ``/a0/helpers/api.py`` and is a Flask-dependent
# abstract base class; tests only need a class that can be instantiated
# with no arguments and exposes a ``process`` / handler method that the
# test can call directly. ``Request`` and ``Response`` are imported as
# type names by the API module — we provide placeholder classes so the
# ``from helpers.api import ApiHandler, Request, Response`` line resolves
# without the Flask / werkzeug dependency chain.
if "helpers.api" not in sys.modules:
    _api_mod = types.ModuleType("helpers.api")

    class _Request:
        """Minimal stand-in for the Flask Request class."""

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _Response:
        """Minimal stand-in for the Flask Response class."""

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _ApiHandler:
        """Minimal stand-in for ``helpers.api.ApiHandler``.

        Real ``ApiHandler`` is an abstract base class wired to Flask,
        session, cache, and PrintStyle. For unit tests we only need a
        class that can be instantiated with no arguments; the
        ``requires_auth`` class attribute is read by the production
        handler, so we expose it as well.
        """

        requires_auth: bool = False

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def process(self, input: dict, request: _Request):  # pragma: no cover
            raise NotImplementedError

    _api_mod.ApiHandler = _ApiHandler
    _api_mod.Request = _Request
    _api_mod.Response = _Response
    sys.modules["helpers.api"] = _api_mod


# ---------------------------------------------------------------------------
# Stub the _memory plugin so the helpers can be imported in isolation
# ---------------------------------------------------------------------------


class _MemoryStub(types.ModuleType):
    """Tiny in-memory stand-in for ``plugins._memory.helpers.memory``.

    The helpers that depend on it (``graph_store``, ``scores``,
    ``memory_score`` tool) need at minimum:
    - ``abs_db_dir(subdir)`` to return a real directory
    - ``Memory.get(agent)`` to be a settable attribute (overridden per
      test by ``monkeypatch.setattr``)
    This stub provides both without booting the full framework.
    """

    class _Memory:
        # ``get`` is a placeholder so tests can ``monkeypatch.setattr``
        # over it. Each test installs its own async fake via the
        # ``memory_subdir`` fixture or test-local monkeypatching.
        get = None

        @staticmethod
        def _get_abs_db_dir(memory_subdir: str) -> str:
            # The test fixture overrides this through monkey-patch, but
            # we keep a deterministic fallback for direct import-time use.
            return os.path.join(tempfile.gettempdir(), "neuro_core_test", memory_subdir)

    @staticmethod
    def _abs_db_dir_default(memory_subdir: str) -> str:
        """Default module-level ``abs_db_dir`` implementation.

        Tests override this via ``mock.patch`` on the stub module.
        """
        return os.path.join(tempfile.gettempdir(), "neuro_core_test", memory_subdir)


_m = sys.modules.get("plugins._memory.helpers.memory")
if _m is None or not hasattr(_m, "Memory"):
    pkg = types.ModuleType("plugins")
    pkg.__path__ = []
    sys.modules["plugins"] = pkg
    sub = types.ModuleType("plugins._memory")
    sub.__path__ = []
    sys.modules["plugins._memory"] = sub
    helpers = types.ModuleType("plugins._memory.helpers")
    helpers.__path__ = []
    sys.modules["plugins._memory.helpers"] = helpers
    mem_module = types.ModuleType("plugins._memory.helpers.memory")
    mem_module.Memory = _MemoryStub._Memory
    # Expose ``abs_db_dir`` as a module-level function so the helper
    # imports ``from plugins._memory.helpers.memory import abs_db_dir``
    # succeed. Tests monkey-patch this attribute per-test.
    mem_module.abs_db_dir = _MemoryStub._abs_db_dir_default
    sys.modules["plugins._memory.helpers.memory"] = mem_module
    sub.helpers = helpers
    sub.memory = mem_module
    helpers.memory = mem_module
    pkg._memory = sub
    pkg.plugins = pkg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_subdir(tmp_path):
    """Return a unique ``memory_subdir`` name and monkey-patch the path."""
    subdir = "neuro_test"
    target = tmp_path / subdir
    target.mkdir(parents=True, exist_ok=True)

    # The helpers import the module-level ``abs_db_dir`` function from
    # ``plugins._memory.helpers.memory`` *inside the function body* (not at
    # module top-level), so each call re-resolves the name. Patching the
    # source module's attribute is therefore sufficient — the next call to
    # ``from plugins._memory.helpers.memory import abs_db_dir`` picks up
    # the patched version. This is verified by the graph_store and scores
    # helpers, both of which do the import lazily inside their path
    # helper functions.
    with mock.patch(
        "plugins._memory.helpers.memory.abs_db_dir",
        side_effect=lambda s: str(target),
    ):
        yield subdir


@pytest.fixture
def empty_metadata():
    """Return a fresh metadata dict for tests that mutate it in place."""
    return {"area": "main", "source": "agent"}
