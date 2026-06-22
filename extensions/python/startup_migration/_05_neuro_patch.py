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

After patches are installed, D55 (D39-D closure) calls
reconcile_sidecars() to perform a read-only FAISS <-> sidecar consistency
scan. The scan logs structural orphans via log.warning() but does NOT
modify any files; auto-fix is intentionally out of scope for v1. See
helpers/_patch.py:reconcile_sidecars for the implementation.

The imports of install_patches and reconcile_sidecars are deferred inside
execute() — not at module level — so a plugin-local import failure cannot
silently kill this extension before it runs.
"""

from __future__ import annotations

from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle


class NeuroPatchStartup(Extension):
    """Install Neuro Core Memory patches at process startup."""

    def execute(self, **kwargs: Any) -> None:
        try:
            from usr.plugins.neuro_core.helpers._patch import (
                install_patches,
                reconcile_sidecars,
            )
            install_patches()
        except Exception as exc:
            PrintStyle.warning(f"[neuro_core] startup patch non-fatal: {exc}")

        # D55 (D39-D closure): read-only FAISS <-> sidecar consistency scan.
        # Runs AFTER install_patches() so any sidecar files written during
        # patch installation are observed. Wrapped in a separate try/except
        # so a reconcile failure cannot roll back successful patch installs.
        try:
            from usr.plugins.neuro_core.helpers._patch import (
                reconcile_sidecars as _reconcile,
            )
            _reconcile()
        except Exception as exc:
            PrintStyle.warning(f"[neuro_core] startup reconcile non-fatal: {exc}")
