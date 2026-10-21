'''WI-P82-KI053-REMEDIATION — insert-path decoration tests (KI-053).

Covers the binding ARC conditions recorded in the work item's
implementation-contract.yaml rev 1:

- C1 (double-fire idempotency): an insert_text call fires BOTH the
  insert_text/start and insert_documents/start hooks (insert_text forwards
to insert_documents internally); the final persisted metadata is identical
to a single-fire result. No re-entrancy guard is added anywhere.
- C2 (neutrality): inserts carrying fully-populated valid Neuro Core
  metadata are byte-identical before/after the hooks (validate-only
  narrowing; seeding only fills ABSENT fields) — the memory_consolidation
call-site pattern (plain dicts with area/source keys and no score fields)
is covered explicitly.
- C3 (framework-runtime probe): after decorate_memory(), the REAL framework
  Memory.insert_text and Memory.insert_documents ARE extensible and their
  derived _functions start points resolve the shipped _10_neuro_metadata.py
  handlers. The probe runs in the framework runtime (/opt/venv-a0/bin/python
  with PYTHONPATH=/a0) — symmetric to the KI-053 defect proof — and is
  skipped gracefully when that runtime is unavailable. The task runtime is
  never used as proof.

The pytest environment stubs helpers.extension / helpers.print_style
(conftest), so the in-process tests use a faithful test-double decorator
reproducing the framework wrapper semantics (same pattern as
test_startup_decoration.py) while loading the REAL shipped handler classes
from the plugin's extensions tree.
'''

from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from usr.plugins.neuro_core.helpers import decorate as dec

PLUGIN_ROOT = Path(dec.__file__).resolve().parents[1]
HANDLER_BASE = (
    PLUGIN_ROOT
    / 'extensions' / 'python' / '_functions'
    / 'plugins' / '_memory' / 'helpers' / 'memory' / 'Memory'
)

A0_RUNTIME = '/opt/venv-a0/bin/python'


def _load_handler(rel_path, mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, str(rel_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Doc:
    """Minimal Document stand-in: mutable metadata dict, like the real one."""

    def __init__(self, metadata):
        self.metadata = metadata


class _FakeExtensible:
    """Test-double of the framework extensible wrapper (start dispatch only
    for these hooks: the shipped insert-path handlers are start hooks that
    mutate args in place and never set data['result'])."""

    _handlers = {}

    def __call__(self, func):
        handlers = _FakeExtensible._handlers.get(func.__qualname__, [])

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            data = {'args': args, 'kwargs': kwargs, 'result': None, 'exception': None}
            for h in handlers:
                try:
                    h(agent=None).execute(data=data)
                except Exception:
                    pass  # ARC condition 3: handler failure never breaks the host call
            return await func(*args, **kwargs)

        wrapper.__module__ = func.__module__
        wrapper.__qualname__ = func.__qualname__
        wrapper._neuro_extensible = True
        return wrapper


def _set_handlers(mapping):
    _FakeExtensible._handlers = dict(mapping)


class _StubMemory:
    """Mirrors the framework call shape: insert_text constructs a Document
    from the raw metadata dict and forwards to insert_documents."""

    def __init__(self):
        self.persisted = None

    async def insert_text(self, text, metadata=None):
        doc = _Doc(dict(metadata or {}))  # Document(text, metadata=metadata)
        return await self.insert_documents([doc])

    async def insert_documents(self, docs):
        # Framework overwrites id/timestamp and persists doc.metadata.
        self.persisted = [dict(d.metadata) for d in docs]
        return 'id'


for _name in ('insert_text', 'insert_documents'):
    _m = getattr(_StubMemory, _name)
    _m.__module__ = 'plugins._memory.helpers.memory'
    _m.__qualname__ = 'Memory.' + _name

_ORIGINALS = {
    _name: getattr(_StubMemory, _name) for _name in ('insert_text', 'insert_documents')
}


@pytest.fixture(autouse=True)
def _restore_stub_methods():
    yield
    for _name, _obj in _ORIGINALS.items():
        setattr(_StubMemory, _name, _obj)
    _set_handlers({})


def _insert_handlers():
    text_mod = _load_handler(
        HANDLER_BASE / 'insert_text' / 'start' / '_10_neuro_metadata.py',
        'p82_insert_text_hook',
    )
    docs_mod = _load_handler(
        HANDLER_BASE / 'insert_documents' / 'start' / '_10_neuro_metadata.py',
        'p82_insert_documents_hook',
    )
    return {
        'Memory.insert_text': [text_mod.NeuroInsertTextMetadata],
        'Memory.insert_documents': [docs_mod.NeuroInsertMetadata],
    }


# ---------------------------------------------------------------------
# C1: double-fire idempotency
# ---------------------------------------------------------------------


def test_double_fire_final_metadata_identical_to_single_fire():
    _set_handlers(_insert_handlers())
    dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())

    async def run_once(metadata):
        mem = _StubMemory()
        await mem.insert_text('hello', dict(metadata))
        return mem.persisted[0]

    # Consolidation-style input: area/source keys, no score fields — both
    # hooks fire (double fire) and seed the same values a single fire would.
    metadata = {'id': 'm1', 'area': 'main', 'source': 'agent'}
    double_fire = asyncio.run(run_once(metadata))

    # Single-fire control: only the insert_documents hook registered.
    _set_handlers({'Memory.insert_documents': _insert_handlers()['Memory.insert_documents']})
    single_fire = asyncio.run(run_once(metadata))

    assert double_fire == single_fire
    # Seeded by the hooks (fill-absent heuristics for area=main/source=agent).
    assert double_fire['importance'] == 0.5
    assert double_fire['confidence'] == 0.7

    # Re-running the double-fire path is idempotent (no double-seeding).
    _set_handlers(_insert_handlers())
    repeat = asyncio.run(run_once(metadata))
    assert repeat == double_fire


def test_insert_text_hook_fires_before_document_construction():
    # The insert_text/start hook mutates the RAW metadata dict in args[2]
    # before Document construction, so seeded fields survive persistence.
    _set_handlers(_insert_handlers())
    dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
    mem = _StubMemory()
    asyncio.run(mem.insert_text('hello', {'id': 'm1', 'area': 'solutions'}))
    assert mem.persisted[0]['importance'] == 0.8  # seeded from area=solutions


# ---------------------------------------------------------------------
# C2: neutrality for fully-populated valid inserts
# ---------------------------------------------------------------------


def test_fully_populated_valid_insert_is_untouched():
    _set_handlers(_insert_handlers())
    dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
    mem = _StubMemory()
    metadata = {
        'id': 'm1',
        'memory_type': 'fact',
        'importance': 0.9,
        'confidence': 0.8,
        'stability': 0.7,
        'validation_status': 'validated',
        'area': 'main',
        'source': 'agent',
    }
    asyncio.run(mem.insert_text('hello', dict(metadata)))
    # Validate-only narrowing + fill-absent seeding: nothing changed.
    assert mem.persisted[0] == metadata


def test_invalid_values_narrowed_not_dropped():
    _set_handlers(_insert_handlers())
    dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
    mem = _StubMemory()
    asyncio.run(
        mem.insert_text(
            'hello',
            {'id': 'm1', 'memory_type': 'bogus', 'importance': 7.5, 'validation_status': 'bogus'},
        )
    )
    persisted = mem.persisted[0]
    assert persisted['memory_type'] == 'note'
    assert persisted['importance'] == 1.0  # clamped to [0.0, 1.0]
    assert persisted['validation_status'] == 'unvalidated'


def test_raising_insert_hook_does_not_break_host_insert():
    class ExplodingHandler:
        def __init__(self, agent=None, **kwargs):
            pass

        def execute(self, **kwargs):
            raise RuntimeError('hook boom')

    handlers = _insert_handlers()
    handlers['Memory.insert_text'] = [ExplodingHandler] + handlers['Memory.insert_text']
    _set_handlers(handlers)
    dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
    mem = _StubMemory()
    result = asyncio.run(mem.insert_text('hello', {'id': 'm1', 'area': 'main'}))
    assert result == 'id'  # host insert succeeded despite the exploding hook
    assert mem.persisted[0]['importance'] == 0.5  # surviving hook still seeded


# ---------------------------------------------------------------------
# C3: framework-runtime extensibility probe (real Memory class)
# ---------------------------------------------------------------------


_PROBE_SCRIPT = r'''
import json, sys
sys.path.insert(0, "/a0")
from plugins._memory.helpers.memory import Memory
from usr.plugins.neuro_core.helpers.decorate import decorate_memory, _MARKER
results = decorate_memory()
out = {
    "results": results,
    "insert_text_extensible": getattr(Memory.insert_text, _MARKER, False),
    "insert_documents_extensible": getattr(Memory.insert_documents, _MARKER, False),
    "insert_text_module": getattr(Memory.insert_text, "__module__", None),
    "insert_text_qualname": getattr(Memory.insert_text, "__qualname__", None),
    "insert_documents_module": getattr(Memory.insert_documents, "__module__", None),
    "insert_documents_qualname": getattr(Memory.insert_documents, "__qualname__", None),
}
print(json.dumps(out))
'''


def _probe_framework_runtime():
    if not Path(A0_RUNTIME).exists() or shutil.which('true') is None:
        pytest.skip(f'framework runtime unavailable: {A0_RUNTIME}')
    proc = subprocess.run(
        [A0_RUNTIME, '-c', _PROBE_SCRIPT],
        env={'PYTHONPATH': '/a0', 'PATH': '/usr/bin:/bin', 'HOME': '/root'},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f'probe failed:\n{proc.stdout}\n{proc.stderr}'
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_framework_runtime_probe_insert_paths_extensible():
    out = _probe_framework_runtime()
    # decorate_memory() reported success for all six targets.
    assert out['results']['insert_text'] == 'decorated'
    assert out['results']['insert_documents'] == 'decorated'
    # The insert paths ARE extensible after decoration (KI-053 closed).
    assert out['insert_text_extensible'] is True
    assert out['insert_documents_extensible'] is True
    # Full identity preserved so the framework's _functions discovery resolves
    # the shipped handler paths.
    assert out['insert_text_module'] == 'plugins._memory.helpers.memory'
    assert out['insert_text_qualname'] == 'Memory.insert_text'
    assert out['insert_documents_module'] == 'plugins._memory.helpers.memory'
    assert out['insert_documents_qualname'] == 'Memory.insert_documents'


def test_derived_start_extension_points_resolve_shipped_handlers():
    # The derived _functions start folders for the two new targets exist in
    # the plugin tree and contain the shipped _10_neuro_metadata.py handlers.
    for method in ('insert_text', 'insert_documents'):
        handler = HANDLER_BASE / method / 'start' / '_10_neuro_metadata.py'
        assert handler.is_file(), f'missing shipped handler: {handler}'
''