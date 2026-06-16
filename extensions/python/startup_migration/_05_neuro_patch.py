"""Neuro Core startup extension — installs Memory method patches at process init.

Extension point: extensions/python/startup_migration/
Filename prefix _05_ runs early, before any memory operations.

The Agent Zero _memory plugin's Memory class has no @extensible decorators, so
the Neuro Core hook directory tree under extensions/python/_functions/.../Memory/
is never invoked by the framework. This extension calls install_patches() from
helpers/_patch.py at process startup to install three idempotent wrappers on:
    - Memory.insert_text              (seed Neuro Core metadata fields)
    - Memory.search_similarity_threshold (update access counts in scores.json)
    - Memory.delete_documents_by_ids  (cascade-delete graph edges on deletion)

The import of install_patches is deferred inside execute() — not at module level —
so a plugin-local import failure cannot silently kill this extension before it runs.
"""

from __future__ import annotations

from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle


class NeuroPatchStartup(Extension):
    """Install Neuro Core Memory patches at process startup."""

    def execute(self, **kwargs: Any) -> None:
        try:
            from usr.plugins.neuro_core.helpers._patch import install_patches
            install_patches()
        except Exception as exc:
            PrintStyle.warning(f"[neuro_core] startup patch non-fatal: {exc}")
