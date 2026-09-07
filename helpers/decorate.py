"""NC1 startup decoration hook (WI-2026-09-04-PHASE0-PATCH-ARCH, C).

Re-applies the framework's public helpers.extension.extensible decorator to
Memory.search_similarity_threshold, .search_similarity_threshold_with_scores,
and .delete_documents_by_ids at FULL identity, derived from the live method
objects (ARC condition 4). Idempotent: never double-wraps. Asserts identity
after decoration. Zero framework source modification. All bookkeeping is
exception-safe (ARC condition 3).
"""

from __future__ import annotations

import logging

log = logging.getLogger("neuro_core.decorate")

_MARKER = "_neuro_extensible"

_TARGETS = (
    "search_similarity_threshold",
    "search_similarity_threshold_with_scores",
    "delete_documents_by_ids",
)


def _memory_class():
    from plugins._memory.helpers.memory import Memory

    return Memory


def _get_decorator():
    """Lazily import the framework's public extensible decorator."""
    from helpers.extension import extensible

    return extensible


def _decorate_one_for_class(cls, method_name, decorator=None):
    """Decorate one method on a Memory class with @extensible at full identity.

    Returns a status string: 'decorated', 'already-decorated', or
    'skipped:<reason>'. Never raises.
    """
    try:
        ext = decorator if decorator is not None else _get_decorator()
    except Exception as e:
        return f"skipped:extensible-import-failed:{e}"

    method = getattr(cls, method_name, None)
    if method is None:
        return f"skipped:method-missing:{method_name}"

    # Idempotency: never double-wrap (ARC condition 4). Detect both our
    # marker and the legacy _neuro_patched marker, so decoration is safe
    # even if the framework later adds @extensible itself.
    if getattr(method, _MARKER, False) or getattr(method, "_neuro_patched", False):
        return "already-decorated"

    # Derive FULL identity from the LIVE method object (ARC condition 4:
    # qualname-only derivation is prohibited — empirically failed).
    module = getattr(method, "__module__", "")
    qualname = getattr(method, "__qualname__", "")
    if not module or not qualname:
        return f"skipped:identity-unavailable:{method_name}"

    try:
        decorated = ext(method)
        # Preserve the wrapped function's identity on the wrapper so the
        # framework's _functions discovery resolves the same paths.
        decorated.__module__ = module
        decorated.__qualname__ = qualname
        setattr(decorated, _MARKER, True)
        setattr(cls, method_name, decorated)
    except Exception as e:
        return f"skipped:decoration-failed:{method_name}:{e}"

    # Identity assertion AFTER decoration (ARC condition 4). Roll back to
    # the original method rather than leave a wrong-identity wrapper.
    applied = getattr(cls, method_name, None)
    if (
        getattr(applied, "__module__", None) != module
        or getattr(applied, "__qualname__", None) != qualname
        or not getattr(applied, _MARKER, False)
    ):
        try:
            setattr(cls, method_name, method)
        except Exception:
            pass
        return f"skipped:identity-assertion-failed:{method_name}"

    return "decorated"


def decorate_memory_for_class(cls, decorator=None):
    """Decorate all three target methods on a Memory class. Never raises.

    Returns a status dict keyed by method name (testable, loggable).
    """
    results = {}
    for name in _TARGETS:
        try:
            results[name] = _decorate_one_for_class(cls, name, decorator)
        except Exception as e:  # belt-and-braces
            results[name] = f"skipped:unexpected:{e}"
            log.warning(f"[neuro_core] decorate {name} unexpected: {e}")
    return results


def decorate_memory():
    """Decorate the three target methods on the REAL framework Memory class.

    Stores originals for undecorate_memory(); idempotent and exception-safe.
    """
    Memory = _memory_class()
    results = {}
    for name in _TARGETS:
        try:
            current = getattr(Memory, name, None)
            if not getattr(current, _MARKER, False) and not getattr(current, "_neuro_patched", False):
                originals = Memory.__dict__.get(_orig_key())
                if originals is None:
                    originals = {}
                    setattr(Memory, _orig_key(), originals)
                if name not in originals:
                    originals[name] = current
            results[name] = _decorate_one_for_class(Memory, name)
        except Exception as e:
            results[name] = f"skipped:unexpected:{e}"
            log.warning(f"[neuro_core] decorate {name} unexpected: {e}")
    log.info(f"[neuro_core] startup decoration results: {results}")
    return results


def _orig_key():
    return "_neuro_pre_decorate"


def undecorate_memory():
    """Restore the original methods on the REAL Memory class. Never raises."""
    try:
        Memory = _memory_class()
        originals = Memory.__dict__.get("_neuro_pre_decorate")
        if not isinstance(originals, dict):
            return
        for name, original in originals.items():
            try:
                setattr(Memory, name, original)
            except Exception as e:
                log.warning(f"[neuro_core] undecorate {name} failed: {e}")
        try:
            delattr(Memory, "_neuro_pre_decorate")
        except Exception:
            pass
    except Exception as e:
        log.warning(f"[neuro_core] undecorate_memory failed: {e}")
