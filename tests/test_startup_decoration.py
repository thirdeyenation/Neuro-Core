'''Tests for the NC1 startup decoration hook (WI-2026-09-04-PHASE0-PATCH-ARCH, C).

Covers ARC conditions 3 and 4 at unit level plus in-process startup-ordering
behavior (condition 6, in-process scope; cross-restart ordering is
integration-validated by VAL per restart-verification-record.yaml).

The pytest environment stubs helpers.extension (conftest), so the REAL
framework decorator is unavailable here. Tests inject a faithful test-double
decorator reproducing the framework wrapper semantics (wraps + funcattrs +
handler dispatch). Real-discovery behavior was proven by probe3/probe5b and
the restart record and is re-validated by VAL.
'''

from __future__ import annotations

import asyncio
import functools
import importlib.util

from usr.plugins.neuro_core.helpers import decorate as dec

FUNC_DIR = (
    '/tmp/nc1-val-fixtures/sandbox/usr/plugins/neuro_core/extensions/python/'
    '_functions/plugins._memory.helpers.memory/Memory'
)


def _load_handler(rel_path, mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, FUNC_DIR + rel_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _doc(doc_id='m1'):
    return type('D', (), {'metadata': {'id': doc_id}})()


class _FakeExtensible:
    # Test-double of the framework extensible wrapper. Handlers are
    # registered per qualname; each is a class instantiated with
    # agent=None and invoked as execute(data=...), matching the real
    # handler contract (NeuroWithScoresAccessTracking / NeuroDeleteCascade).

    _UNSET = object()
    _handlers = {}

    def __call__(self, func):
        handlers = _FakeExtensible._handlers.get(func.__qualname__, [])

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            data = {
                'args': args,
                'kwargs': kwargs,
                'result': _FakeExtensible._UNSET,
                'exception': None,
            }
            try:
                result = await func(*args, **kwargs)
                data['result'] = result
            except Exception as exc:
                data['exception'] = exc
            # End-hook contract: handlers run AFTER the result exists,
            # matching the real framework end() dispatch and the real
            # handlers' result-reading contract.
            for h in handlers:
                try:
                    h(agent=None).execute(data=data)
                except Exception:
                    # ARC condition 3: a raising handler never breaks the
                    # host call and never sets data['exception'].
                    pass
            if data['exception']:
                raise data['exception']
            return data['result']

        wrapper.__module__ = func.__module__
        wrapper.__qualname__ = func.__qualname__
        wrapper._neuro_extensible = True
        return wrapper


def _set_handlers(mapping):
    _FakeExtensible._handlers = dict(mapping)


class _StubMemory:
    memory_subdir = 'default'

    async def search_similarity_threshold(self, query, limit=10, threshold=0.6, filter='', embedding=None):
        return [_doc()]

    async def search_similarity_threshold_with_scores(self, query, limit=10, threshold=0.6, filter=''):
        return [(_doc(), 0.9)]

    async def delete_documents_by_ids(self, ids, cascade=False, filter=''):
        return []


for _name in (
    'search_similarity_threshold',
    'search_similarity_threshold_with_scores',
    'delete_documents_by_ids',
):
    _m = getattr(_StubMemory, _name)
    _m.__module__ = 'plugins._memory.helpers.memory'
    _m.__qualname__ = 'Memory.' + _name


# Pristine originals captured BEFORE any test decorates the class.
_ORIGINALS = {
    _name: getattr(_StubMemory, _name)
    for _name in (
        'search_similarity_threshold',
        'search_similarity_threshold_with_scores',
        'delete_documents_by_ids',
    )
}


import pytest


@pytest.fixture(autouse=True)
def _restore_stub_methods():
    # Test isolation: undo any decoration left by the previous test.
    yield
    for _name, _obj in _ORIGINALS.items():
        setattr(_StubMemory, _name, _obj)
    _set_handlers({})


# ---------------------------------------------------------------------
# Idempotency + identity (ARC condition 4)
# ---------------------------------------------------------------------


def test_decorate_is_idempotent_never_double_wraps():
    _set_handlers({})
    r1 = dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
    assert r1['search_similarity_threshold'] == 'decorated'
    first = _StubMemory.search_similarity_threshold
    r2 = dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
    assert r2['search_similarity_threshold'] == 'already-decorated'
    assert _StubMemory.search_similarity_threshold is first
    _set_handlers({})


def test_identity_preserved_at_full_identity():
    _set_handlers({})
    dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
    m = _StubMemory.search_similarity_threshold
    assert m.__module__ == 'plugins._memory.helpers.memory'
    assert m.__qualname__ == 'Memory.search_similarity_threshold'
    assert getattr(m, dec._MARKER) is True
    _set_handlers({})


def test_legacy_marker_prevents_double_wrap():
    _set_handlers({})
    dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
    first = _StubMemory.search_similarity_threshold
    setattr(first, '_neuro_patched', True)  # legacy marker (defense-in-depth)
    r = dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
    assert r['search_similarity_threshold'] == 'already-decorated'
    assert _StubMemory.search_similarity_threshold is first
    _set_handlers({})


def test_identity_unavailable_is_skipped_not_wrapped():
    async def bare(self, query):
        return []

    # A normal function HAS __module__/__qualname__; __qualname__ cannot be
    # deleted on functions, but __module__ can — removing it legitimately
    # triggers the identity-unavailable skip path (ARC condition 4 guard).
    del bare.__module__
    _StubMemory.bare_method = bare
    status = dec._decorate_one_for_class(_StubMemory, 'bare_method', decorator=_FakeExtensible())
    assert status.startswith('skipped:identity-unavailable')
    assert not hasattr(_StubMemory.bare_method, dec._MARKER)
    del _StubMemory.bare_method
    _set_handlers({})


def test_all_three_methods_decorated():
    _set_handlers({})
    r = dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
    assert all(v == 'decorated' for v in r.values())
    assert set(r) == {
        'search_similarity_threshold',
        'search_similarity_threshold_with_scores',
        'delete_documents_by_ids',
    }
    _set_handlers({})


# ---------------------------------------------------------------------
# Exception-safety (ARC condition 3)
# ---------------------------------------------------------------------


def test_raising_handler_does_not_break_host_call():
    class ExplodingHandler:
        def __init__(self, agent=None, **kwargs):
            pass

        def execute(self, **kwargs):
            raise RuntimeError('handler boom')

    _set_handlers({'Memory.search_similarity_threshold': [ExplodingHandler]})
    dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
    result = asyncio.run(_StubMemory().search_similarity_threshold('q'))
    assert len(result) == 1  # host call succeeded despite the exploding handler
    _set_handlers({})


def test_handler_error_never_sets_data_exception():
    captured = {}

    class CapturingHandler:
        def __init__(self, agent=None, **kwargs):
            pass

        def execute(self, **kwargs):
            data = kwargs.get('data') or {}
            captured['exception_before'] = data.get('exception')
            raise ValueError('boom')

    _set_handlers({'Memory.delete_documents_by_ids': [CapturingHandler]})
    dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
    result = asyncio.run(_StubMemory().delete_documents_by_ids(['x']))
    assert result == []
    assert captured['exception_before'] is None
    _set_handlers({})


# ---------------------------------------------------------------------
# Real handlers fire through the decorated call path (KI-021 closure)
# ---------------------------------------------------------------------


def test_with_scores_handler_tracks_access():
    calls = []

    class FakeStore:
        def __init__(self, subdir):
            self.subdir = subdir

        def update_access(self, doc_id):
            calls.append(doc_id)

    mod = _load_handler(
        '/search_similarity_threshold_with_scores/end/_10_access_tracking.py',
        'ws_access_tracking_test',
    )
    from usr.plugins.neuro_core.helpers import scores as scores_mod

    original = scores_mod.ScoreStore
    scores_mod.ScoreStore = FakeStore
    try:
        _set_handlers({'Memory.search_similarity_threshold_with_scores': [mod.NeuroWithScoresAccessTracking]})
        dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
        result = asyncio.run(_StubMemory().search_similarity_threshold_with_scores('q'))
        assert isinstance(result, list) and len(result) == 1  # result shape unchanged
        pair = result[0]
        assert isinstance(pair, tuple) and pair[1] == 0.9
        assert pair[0].metadata['id'] == 'm1'
        assert calls == ['m1']  # KI-021: with-scores access tracked
    finally:
        scores_mod.ScoreStore = original
        _set_handlers({})


def test_with_scores_handler_swallows_store_failure():
    class BoomStore:
        def __init__(self, subdir):
            raise RuntimeError('sidecar unavailable')

    mod = _load_handler(
        '/search_similarity_threshold_with_scores/end/_10_access_tracking.py',
        'ws_access_tracking_boom',
    )
    from usr.plugins.neuro_core.helpers import scores as scores_mod

    original = scores_mod.ScoreStore
    scores_mod.ScoreStore = BoomStore
    try:
        _set_handlers({'Memory.search_similarity_threshold_with_scores': [mod.NeuroWithScoresAccessTracking]})
        dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
        result = asyncio.run(_StubMemory().search_similarity_threshold_with_scores('q'))
        assert isinstance(result, list) and len(result) == 1  # host search unaffected
        assert result[0][1] == 0.9
    finally:
        scores_mod.ScoreStore = original
        _set_handlers({})


def test_delete_cascade_handler_swallows_graph_failure():
    class BoomGraph:
        def __init__(self, subdir):
            raise RuntimeError('graph unavailable')

    mod = _load_handler('/delete_documents_by_ids/end/_10_graph_cascade.py', 'del_cascade_test')
    from usr.plugins.neuro_core.helpers import graph_store as graph_mod

    original = graph_mod.GraphStore
    graph_mod.GraphStore = BoomGraph
    try:
        _set_handlers({'Memory.delete_documents_by_ids': [mod.NeuroDeleteCascade]})
        dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
        result = asyncio.run(_StubMemory().delete_documents_by_ids(['x']))
        assert result == []  # FAISS delete result unaffected by cascade failure
    finally:
        graph_mod.GraphStore = original
        _set_handlers({})


# ---------------------------------------------------------------------
# In-process startup ordering (ARC condition 6, in-process scope)
# ---------------------------------------------------------------------


def test_startup_ordering_patch_then_decorate_is_safe():
    # Defense-in-depth: if any legacy _neuro_patched marker is present on a
    # method (e.g., from an older NC1 version), decoration must detect it
    # and skip rather than double-wrap, so patch-then-decorate ordering
    # remains safe.
    _set_handlers({})
    original = _StubMemory.__dict__['search_similarity_threshold']

    async def patched(self, *a, **k):
        return await original(self, *a, **k)

    patched.__module__ = original.__module__
    patched.__qualname__ = original.__qualname__
    patched._neuro_patched = True
    _StubMemory.search_similarity_threshold = patched
    try:
        r = dec.decorate_memory_for_class(_StubMemory, decorator=_FakeExtensible())
        assert r['search_similarity_threshold'] == 'already-decorated'
        assert _StubMemory.search_similarity_threshold is patched
    finally:
        _StubMemory.search_similarity_threshold = original
        _set_handlers({})


# ---------------------------------------------------------------------
# Startup-time decoration extension (HITL Restart Check #2 remediation)
# ---------------------------------------------------------------------

import sys
from pathlib import Path

import pytest


_EXT_FILE = (
    Path(dec.__file__).resolve().parents[1]
    / 'extensions' / 'python' / 'startup_migration'
    / '_10_neuro_decoration.py'
)


def test_startup_decoration_extension_file_exists():
    # The startup extension must exist at the canonical plugin path so the
    # framework's startup_migration discovery can find it.
    assert _EXT_FILE.is_file(), f'missing startup extension: {_EXT_FILE}'


def test_startup_decoration_is_extension_subclass():
    import importlib

    from helpers.extension import Extension  # conftest stub in test env

    spec = importlib.util.spec_from_file_location(
        'nc1_startup_decoration_test', _EXT_FILE
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls = getattr(mod, 'NeuroStartupDecoration', None)
    assert cls is not None, 'NeuroStartupDecoration class missing'
    assert issubclass(cls, Extension)
    assert callable(getattr(cls, 'execute', None))


def test_startup_extension_execute_calls_decorate_memory():
    import importlib
    import unittest.mock as mock

    from usr.plugins.neuro_core.helpers import decorate as real_decorate

    spec = importlib.util.spec_from_file_location(
        'nc1_startup_decoration_exec_test', _EXT_FILE
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # execute() does `from usr.plugins.neuro_core.helpers.decorate import
    # decorate_memory` at call time, so patching the module attribute works.
    with mock.patch.object(
        real_decorate, 'decorate_memory', return_value={'search_similarity_threshold': 'decorated'}
    ) as fake:
        inst = mod.NeuroStartupDecoration(agent=None)
        inst.execute()  # must not raise
        assert fake.call_count == 1


def test_startup_extension_execute_is_non_fatal_on_decorate_failure():
    import importlib
    import unittest.mock as mock

    from usr.plugins.neuro_core.helpers import decorate as real_decorate

    spec = importlib.util.spec_from_file_location(
        'nc1_startup_decoration_fail_test', _EXT_FILE
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with mock.patch.object(
        real_decorate, 'decorate_memory', side_effect=RuntimeError('boom')
    ):
        inst = mod.NeuroStartupDecoration(agent=None)
        inst.execute()  # must never raise (framework startup must survive)


def test_startup_extension_execute_is_non_fatal_on_import_failure(monkeypatch):
    import importlib

    spec = importlib.util.spec_from_file_location(
        'nc1_startup_decoration_importfail_test', _EXT_FILE
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Setting the module entry to None makes `from X import Y` raise
    # ImportError, simulating the decorate module being unavailable.
    monkeypatch.setitem(
        sys.modules, 'usr.plugins.neuro_core.helpers.decorate', None
    )
    inst = mod.NeuroStartupDecoration(agent=None)
    inst.execute()  # must never raise
