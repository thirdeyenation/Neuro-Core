"""
Neuro Core metadata sanitizer for the start of ``Memory.update_documents``.

Hook path (KI-036, WI-P45-KI036-SANITIZER, ADR-NC1-001 amendment):
    extensions/python/_functions/plugins/_memory/helpers/memory/
    Memory/update_documents/start/_05_neuro_sanitizer.py

The start-hook contract (see ``helpers/extension.py``) is:

- ``data["args"]``   = positional args of the wrapped call; for a bound
                        ``Memory.update_documents(docs)`` invocation that is
                        ``(self, docs)`` where ``docs`` is a list of
                        ``Document`` instances (the identical bound-method
                        shape documented by the shipped
                        insert_documents/start/_10_neuro_metadata.py hook).
- ``data["kwargs"]`` = keyword args (usually empty for this call).
- ``data["result"]`` = the wrapped function's return value; a start hook
                        that does NOT set it lets the framework call the
                        wrapped function with the (possibly modified)
                        ``data["args"]``/``data["kwargs"]``.

Behavior (prevent-not-repair):
    The dashboard edit-save round trip echoes full metadata (including any
    delivery-time shaped ``neuro_*`` markers) back through
    ``Memory.update_documents``, which persists it into FAISS via delete +
    re-add + ``_save_db()``. This start hook runs BEFORE the wrapped
    persistence body and strips every metadata key starting with the
    NC1-owned ``neuro_`` prefix from every Document in the incoming docs
    list, so shaped markers can never persist into FAISS metadata.

    Resolution of the docs list is defensive, in this pinned order:
    1. ``data["args"][1]`` when it is a list or tuple (the real bound-method
       shape);
    2. ``data["kwargs"].get("docs")`` when a list or tuple;
    3. the FIRST positional element that is a list/tuple whose members are
       duck-typed Documents.
    If no docs list is found, the hook returns without action.

    For each Document found, ``doc.metadata`` is REPLACED with a shallow
    filtered copy (a new dict). The caller-supplied metadata dict is never
    mutated in place. Stripping is idempotent (a second pass removes
    nothing).

Exception-safety contract (binding, ARC condition 7):
    - The entire body is wrapped in a broad ``except Exception``.
    - On any internal failure the hook LOGS and returns WITHOUT stripping
      — it never re-raises and never sets ``data["exception"]``. An
      unhandled exception here would break ALL ``Memory.update_documents()`
      calls system-wide (not just Neuro Core operations), so failure is
      non-fatal: persistence must never break because of the sanitizer.

Stability contract (v2, per the shipped _10_neuro_metadata.py precedent):
    - Plugin-local imports (``from usr.plugins.neuro_core...``) are moved
      inside the method that uses them. Module-level imports of
      ``usr.plugins.*`` are NOT permitted in hook files. (This hook is
      fully self-contained and requires no plugin-local imports at all.)
    - The method never re-raises under any circumstances.
"""

from __future__ import annotations

from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle


class NeuroUpdateSanitizer(Extension):
    """Strip shaped neuro_* metadata markers before FAISS persistence."""

    # The NC1-owned metadata namespace. The framework _memory plugin uses
    # no neuro_* metadata keys (ARC-verified scoped grep), so a generic
    # prefix strip is future-proof without collateral risk.
    _PREFIX = "neuro_"

    def execute(self, **kwargs: Any) -> None:
        """Hook entry point — NEVER re-raises.

        The wrapper logs a warning on any failure and returns normally.
        An unhandled exception here would propagate to
        ``Memory.update_documents()`` and break ALL dashboard/FAISS
        update persistence in the system, not just Neuro Core
        operations.
        """
        try:
            data: dict = kwargs.get("data") or {}
            args: tuple = tuple(data.get("args") or ())
            call_kwargs: dict = data.get("kwargs") or {}

            docs = self._resolve_docs(args, call_kwargs)
            if docs is None:
                return

            for doc in docs:
                metadata = getattr(doc, "metadata", None)
                if not isinstance(metadata, dict):
                    continue
                filtered = {
                    k: v for k, v in metadata.items()
                    if not str(k).startswith(self._PREFIX)
                }
                if len(filtered) != len(metadata):
                    # Shallow filtered copy REPLACES doc.metadata; the
                    # caller's original dict is never mutated in place.
                    doc.metadata = filtered

            # No data["result"] = no short-circuit; the original function
            # runs with the (sanitized) docs and persists as usual.
        except Exception as e:
            # Never re-raise — dashboard update persistence must not be
            # blocked by this hook.
            PrintStyle().warning(
                f"[neuro_core] update_documents sanitizer non-fatal: {e}"
            )

    def _resolve_docs(self, args: tuple, call_kwargs: dict):
        """Resolve the docs list defensively; return None when not found.

        Pinned order (ARC condition 2, confirmed): data['args'][1] when a
        list -> kwargs['docs'] -> first positional element that is a
        list/tuple of Documents. If no docs list is found, no action.
        """
        # 1. Bound-method shape: (self, docs).
        if len(args) >= 2 and isinstance(args[1], (list, tuple)):
            return args[1]

        # 2. Keyword form.
        kw = call_kwargs.get("docs")
        if isinstance(kw, (list, tuple)):
            return kw

        # 3. Defensive scan: first positional element that is a
        #    list/tuple of duck-typed Documents.
        for a in args:
            if isinstance(a, (list, tuple)) and a and all(
                hasattr(item, "metadata") for item in a
            ):
                return a

        return None
