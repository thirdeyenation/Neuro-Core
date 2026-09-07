"""
Neuro Core metadata seeder for the start of ``Memory.insert_text``.

Hook path:
    extensions/python/_functions/plugins._memory.helpers.memory/
    Memory/insert_text/start/_10_neuro_metadata.py

Why this hook exists instead of (or in addition to) insert_documents/start/:
    ``Memory.insert_text`` constructs ``Document(text, metadata=metadata)``
    before forwarding to ``insert_documents``.  Pydantic v2's Document model
    stores only the declared fields (page_content, metadata, id); any key
    injected *after* construction via ``doc.metadata = new_meta`` is not
    re-validated and is silently lost when the docstore is pickled.

    By hooking ``insert_text/start/`` we enrich the raw metadata *dict*
    before it is passed to ``Document()``, ensuring the five Neuro Core
    fields are present at construction time and survive FAISS persistence.

Extension contract:
    ``data["args"]`` for a bound ``Memory.insert_text(text, metadata)``
    call is ``(self, text, metadata)``.  ``data["args"][2]`` is the
    metadata dict (mutable in-place).

    This hook never sets ``data["result"]`` — control always falls through
    to the original ``insert_text`` implementation.

Stability contract (identical to insert_documents/start/ hook):
    - Plugin-local imports are inside ``execute()`` only.
    - Broad ``except Exception`` wraps the entire body.
    - Never re-raises — a failure here must not block ``memory_save``.
"""

from __future__ import annotations

from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle


class NeuroInsertTextMetadata(Extension):
    """Seed Neuro Core metadata fields before Document construction."""

    def execute(self, **kwargs: Any) -> None:
        """Hook entry point — NEVER re-raises."""
        try:
            from usr.plugins.neuro_core.helpers.metadata import (
                apply_seeding,
                validate_neuro_metadata,
            )

            data: dict = kwargs.get("data") or {}
            args: tuple = tuple(data.get("args") or ())

            # Bound-method call: (self, text, metadata).
            # args[0] = Memory instance
            # args[1] = text (str)
            # args[2] = metadata (dict) — enrich this before Document() is called
            if len(args) < 3:
                return
            metadata = args[2]
            if not isinstance(metadata, dict):
                return

            validate_neuro_metadata(metadata)
            apply_seeding(metadata)

        except Exception as e:
            PrintStyle().warning(
                f"[neuro_core] insert_text metadata hook non-fatal: {e}"
            )
