"""NC1 startup decoration extension (WI-2026-09-04-PHASE0-PATCH-ARCH).

Executed by the framework's startup_migration pass:

    initialize.py:initialize_migration()
      -> helpers/migration.py:startup_migration()
      -> helpers.extension.call_extensions_sync("startup_migration", None)

which discovers Extension subclasses under each plugin's
``extensions/python/startup_migration/`` directory and runs them at every
framework startup. hooks.py wires decoration only at plugin-install time,
which does not re-run on container restart — this extension closes that gap
(HITL Restart Check #2) by re-applying decoration at startup.

Contract:
- Calls ``usr.plugins.neuro_core.helpers.decorate.decorate_memory()`` on the
  REAL framework Memory class (idempotent; never double-wraps).
- Exception-safe: any failure here is logged non-fatally and must NEVER
  crash framework startup.
"""

from __future__ import annotations

import logging

from helpers.extension import Extension

log = logging.getLogger("neuro_core.startup_decoration")


class NeuroStartupDecoration(Extension):
    """Re-apply NC1 Memory decoration at framework startup."""

    def execute(self, **kwargs):
        try:
            # Same import path as hooks.py (framework startup runs from the
            # /a0 root, where ``usr`` is importable).
            from usr.plugins.neuro_core.helpers.decorate import decorate_memory
        except Exception as e:  # non-fatal: never break framework startup
            log.warning(
                "[neuro_core] startup decoration skipped (import failed): %s", e
            )
            return None
        try:
            results = decorate_memory()
        except Exception as e:  # non-fatal: never break framework startup
            log.warning("[neuro_core] startup decoration non-fatal: %s", e)
            return None
        log.info("[neuro_core] startup decoration applied: %s", results)
        return None
