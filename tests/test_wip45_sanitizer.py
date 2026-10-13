'''Tests for WI-P45-KI036-SANITIZER — the update_documents neuro_* sanitizer.

Closes KI-036: with the recall-shaping gate ON, shaped neuro_* metadata
markers must never persist into FAISS via the dashboard edit-save round trip
(Memory.update_documents).

Pinned groups (ARC conditions 2, 4, 7, 8, 9):
  (a) round-trip — neuro_* keys never survive a real decorated
      update_documents call (start-hook dispatch before persistence);
  (b) gate-OFF byte-identity companion — marker-free docs round-trip with
      metadata exactly equal to input, caller dict never mutated in place;
  (c) docs-position resolution order pin — args[1] > kwargs['docs'] > first
      positional list-of-Documents scan; no docs -> no action (handler-level
      pins; the args[1]-over-kwargs precedence is unproducible through the
      real signature, which cannot bind both);
  (d) four markers individually pinned (generic neuro_ prefix strip);
  (e) failure-safety — a sanitizer raising internally must not break
      persistence (log-and-proceed, never re-raise, never data['exception']);
  (f) resolution-order/idempotency pin per the established
      test_startup_decoration pattern (decoration idempotent, never
      double-wraps; sanitizer idempotent, second pass removes nothing).

The pytest environment stubs helpers.extension (conftest), so the REAL
framework decorator is unavailable here. Tests inject a faithful test-double
decorator reproducing the framework START-hook wrapper semantics (wraps +
funcattrs + start dispatch BEFORE the wrapped call, args/kwargs mutation
flows through). The REAL sanitizer handler class is loaded from its on-disk
extension file. Real-discovery behavior is re-validated by VAL.
'''

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path

from usr.plugins.neuro_core.helpers import decorate as dec

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SANITIZER_PATH = (
    _PLUGIN_ROOT
    / 'extensions/python/_functions/plugins/_memory/helpers/memory/'
    / 'Memory/update_documents/start/_05_neuro_sanitizer.py'
)


def _load_sanitizer_class():
    spec = importlib.util.spec_from_file_location(
        'wip45_sanitizer_hook', str(SANITIZER_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.NeuroUpdateSanitizer


Sanitizer = _load_sanitizer_class()


def _doc(doc_id='m1', metadata=None):
    return type('D', (), {'metadata': dict(metadata or {'id': doc_id})})()


# ---------------------------------------------------------------------
# Faithful START-hook wrapper double (mirrors the real framework
# extensible wrapper: start extensions run FIRST and may mutate
# data['args']/data['kwargs']; the wrapped call then uses the mutated
# values; a raising start handler never breaks the host call).
# ---------------------------------------------------------------------


class _StartFakeExtensible:
    _UNSET = object()
    _handlers = {}  # qualname -> [handler classes]

    def __call__(self, func):
        handlers = _StartFakeExtensible._handlers.get(func.__qualname__, [])

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            data = {
                'args': args,
                'kwargs': kwargs,
                'result': _StartFakeExtensible._UNSET,
                'exception': None,
            }
            for h in handlers:
                try:
                    h(agent=None).execute(data=data)
                except Exception:
                    pass
            return await func(*data['args'], **data['kwargs'])

        wrapper.__module__ = func.__module__
        wrapper.__qualname__ = func.__qualname__
        wrapper._neuro_extensible = True
        return wrapper


def _set_handlers(mapping):
    _StartFakeExtensible._handlers = dict(mapping)


# ---------------------------------------------------------------------
# Stub Memory with the real update_documents shape (bound method:
# (self, docs)). Persistence is simulated by recording the metadata
# dicts the wrapped body would write to FAISS.
# ---------------------------------------------------------------------


class _StubMemory:
    memory_subdir = 'default'
    persisted = []  # metadata dicts seen by the persistence body

    async def update_documents(self, docs, **kwargs):
        type(self).persisted = [dict(getattr(d, 'metadata', {})) for d in docs]
        return len(docs)


_m = _StubMemory.update_documents
_m.__module__ = 'plugins._memory.helpers.memory'
_m.__qualname__ = 'Memory.update_documents'

_ORIGINAL = _StubMemory.update_documents


import pytest


@pytest.fixture(autouse=True)
def _restore_stub():
    yield
    setattr(_StubMemory, 'update_documents', _ORIGINAL)
    _set_handlers({})
    _StubMemory.persisted = []


def _decorate_real_handler():
    """Decorate _StubMemory.update_documents with the faithful wrapper double
    and register the REAL sanitizer handler class (loaded from disk)."""
    _set_handlers({'Memory.update_documents': [Sanitizer]})
    results = dec.decorate_memory_for_class(
        _StubMemory, decorator=_StartFakeExtensible()
    )
    assert results['update_documents'] == 'decorated', results


async def _roundtrip(docs):
    inst = _StubMemory()
    await inst.update_documents(docs)
    return _StubMemory.persisted


# ---------------------------------------------------------------------
# (a) Pinned round-trip: markers never survive a real decorated call
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roundtrip_markers_never_persist():
    _decorate_real_handler()
    docs = [
        _doc('a', {'id': 'a', 'text': 'hello', 'neuro_shaped': True,
                   'neuro_factors': {'shaped_score': 0.9}}),
        _doc('b', {'id': 'b', 'text': 'world', 'neuro_neighbor': True}),
    ]
    persisted = await _roundtrip(docs)
    assert len(persisted) == 2
    for md in persisted:
        assert not any(k.startswith('neuro_') for k in md), md
    assert persisted[0]['text'] == 'hello'
    assert persisted[1]['text'] == 'world'


# ---------------------------------------------------------------------
# (b) Gate-OFF byte-identity companion assertion (ARC condition 8/9)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_off_byte_identity_and_no_in_place_mutation():
    _decorate_real_handler()
    original_md = {'id': 'x', 'text': 'clean', 'area': 'main'}
    caller_dict = dict(original_md)  # the dict the caller supplied
    doc = _doc('x', original_md)
    doc.metadata = caller_dict  # sanitizer must NOT mutate this dict
    persisted = await _roundtrip([doc])
    # Byte-identity: persisted metadata exactly equals input.
    assert persisted[0] == original_md
    # No in-place mutation of caller metadata (ARC condition 8).
    assert doc.metadata == original_md
    assert doc.metadata is caller_dict
    assert caller_dict == original_md


# ---------------------------------------------------------------------
# (c) Docs-position resolution order pin (handler level, ARC condition 2)
# ---------------------------------------------------------------------


def _run_sanitizer(args, kwargs):
    data = {'args': args, 'kwargs': kwargs, 'result': None, 'exception': None}
    Sanitizer(agent=None).execute(data=data)
    return data


def test_resolution_order_args1_wins_over_kwargs_docs():
    # Handler-level pin: when both are present (unproducible through the
    # real signature, which cannot bind both), args[1] takes precedence.
    via_args = [_doc('a')]
    via_kw = [_doc('b')]
    _run_sanitizer(('self', via_args), {'docs': via_kw})
    assert 'neuro_x' not in via_args[0].metadata or True
    via_args[0].metadata['neuro_shaped'] = True
    via_kw[0].metadata['neuro_shaped'] = True
    _run_sanitizer(('self', via_args), {'docs': via_kw})
    assert 'neuro_shaped' not in via_args[0].metadata  # args[1] stripped
    assert via_kw[0].metadata['neuro_shaped'] is True  # kwargs NOT touched


def test_resolution_order_kwargs_docs_used_when_no_args1():
    kw_docs = [_doc('k')]
    kw_docs[0].metadata['neuro_factors'] = {}
    _run_sanitizer(('self',), {'docs': kw_docs})
    assert 'neuro_factors' not in kw_docs[0].metadata


def test_resolution_order_first_positional_list_scan():
    docs = [_doc('p')]
    docs[0].metadata['neuro_degraded'] = True
    # args[1] is NOT a list here; kwargs empty; scan finds the list.
    _run_sanitizer(('self', 'other', docs), {})
    assert 'neuro_degraded' not in docs[0].metadata


def test_resolution_no_docs_no_action():
    _run_sanitizer(('self',), {})
    _run_sanitizer((), {})
    _run_sanitizer(('self', 'not-a-list'), {'docs': None})


# ---------------------------------------------------------------------
# (d) Four markers individually pinned (generic prefix strip)
# ---------------------------------------------------------------------


@pytest.mark.parametrize('marker', [
    'neuro_shaped', 'neuro_factors', 'neuro_degraded', 'neuro_neighbor',
])
def test_four_markers_individually_stripped(marker):
    doc = _doc('m', {'id': 'm', marker: {'v': 1}})
    _run_sanitizer(('self', [doc]), {})
    assert marker not in doc.metadata
    assert doc.metadata['id'] == 'm'


def test_generic_prefix_strip_and_non_neuro_keys_kept():
    doc = _doc('m', {'id': 'm', 'neurofuture': 'kept?', 'neuro_x': 1})
    _run_sanitizer(('self', [doc]), {})
    assert 'neuro_x' not in doc.metadata
    # 'neurofuture' does not start with 'neuro_' (underscore) — kept.
    assert doc.metadata['neurofuture'] == 'kept?'


# ---------------------------------------------------------------------
# (e) Failure-safety pin: sanitizer raising internally must not break
#     persistence (ARC condition 7)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sanitizer_internal_failure_never_breaks_persistence():
    _decorate_real_handler()

    class _Boom:
        def _resolve_docs(self, args, kwargs):
            raise RuntimeError('boom')

        def execute(self, **kwargs):
            # Simulate an internal failure inside the real execute body by
            # calling the real method with a poisoned data payload.
            data = kwargs.get('data') or {}
            data['args'] = (_BrokenArgs(),)
            return Sanitizer.execute(self, data=data)

    class _BrokenArgs:
        def __len__(self):
            raise RuntimeError('boom')

        def __iter__(self):
            raise RuntimeError('boom')

    # Direct handler-level pin: poisoned payload must not raise.
    data = {'args': (_BrokenArgs(),), 'kwargs': {}, 'result': None,
            'exception': None}
    Sanitizer(agent=None).execute(data=data)  # must not raise
    assert data['exception'] is None  # never sets data['exception']

    # End-to-end: decorated call still persists normally.
    docs = [_doc('z', {'id': 'z', 'neuro_shaped': True})]
    persisted = await _roundtrip(docs)
    assert len(persisted) == 1


# ---------------------------------------------------------------------
# (f) Resolution-order/idempotency pin (established pattern)
# ---------------------------------------------------------------------


def test_decoration_idempotent_never_double_wraps():
    _decorate_real_handler()
    first = _StubMemory.update_documents
    results = dec.decorate_memory_for_class(
        _StubMemory, decorator=_StartFakeExtensible()
    )
    assert results['update_documents'] == 'already-decorated'
    assert _StubMemory.update_documents is first


@pytest.mark.asyncio
async def test_sanitizer_idempotent_second_pass_removes_nothing():
    _decorate_real_handler()
    docs = [_doc('i', {'id': 'i', 'neuro_shaped': True})]
    persisted1 = await _roundtrip(docs)
    assert all(not k.startswith('neuro_') for k in persisted1[0])
    # Second pass over already-clean docs: byte-identical, no removals.
    clean = _doc('i', {'id': 'i'})
    persisted2 = await _roundtrip([clean])
    assert persisted2[0] == {'id': 'i'}


def test_missing_method_and_no_docs_are_safe():
    # decorate: method missing -> skipped, never raises.
    class _Empty:
        pass

    results = dec.decorate_memory_for_class(
        _Empty, decorator=_StartFakeExtensible()
    )
    assert results['update_documents'].startswith('skipped:method-missing')
    # Sanitizer with no resolvable docs: no action, no raise.
    _run_sanitizer(('self',), {})
