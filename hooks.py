"""Neuro Core plugin hooks.

Provides `install()` and `uninstall()` entry points called by the
Agent Zero plugin lifecycle.
"""

from __future__ import annotations

import subprocess
import sys


def install() -> bool:
    """Install Neuro Core runtime dependencies.

    Steps:
      1. Runtime guard: verify that the `_memory` plugin is importable.
         The Neuro Core plugin extends `_memory` (FAISS, Memory class) and
         cannot function without it. If the import fails, print a clear
         error and return False without installing anything else.
      2. Install `networkx>=3.0` via `pip` (idempotent).

    Returns:
        True on success, False if a required dependency is missing.
    """
    # --- Runtime guard: _memory is a hard dependency ---------------------
    try:
        from plugins._memory.helpers.memory import Memory  # noqa: F401
    except ImportError as exc:
        print(
            "[neuro_core] FATAL: `plugins._memory.helpers.memory` is not "
            "importable. The `_memory` plugin is a required dependency for "
            "`neuro_core`. Enable the `_memory` plugin in Settings → "
            f"Plugins and try again. Underlying error: {exc}"
        )
        return False

    # --- Optional runtime dependency: networkx (graph analytics) ---------
    print("[neuro_core] Installing networkx>=3.0 via pip ...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "networkx>=3.0"],
        check=False,
    )
    if result.returncode != 0:
        print(
            "[neuro_core] WARNING: `pip install networkx>=3.0` exited with "
            f"code {result.returncode}. Graph analytics features will be "
            "degraded, but core memory operations will still work."
        )
    else:
        print("[neuro_core] networkx>=3.0 is ready.")

    # --- Runtime monkey-patch: wrap Memory methods that lack @extensible ---
    # The Agent Zero framework's _memory plugin Memory class has no
    # @extensible decorators, so the hook directory tree under
    # extensions/python/_functions/.../Memory/ is never invoked by the
    # framework. We install thin wrappers at plugin init time to restore
    # the intended behavior. Wrappers are idempotent.
    from usr.plugins.neuro_core.helpers._patch import install_patches
    install_patches()

    return True


def uninstall() -> None:
    """Uninstall Neuro Core (no-op: dependencies are intentionally kept).

    We do NOT remove `networkx` or any other pip package on uninstall —
    other plugins may depend on them. The plugin-local files under
    `usr/plugins/neuro_core/` are removed by Agent Zero's plugin manager.
    """
    # --- Restore the original Memory methods (reverse the runtime monkey-patch) ---
    try:
        from usr.plugins.neuro_core.helpers._patch import uninstall_patches
        uninstall_patches()
    except Exception as _e:
        print(f"[neuro_core] uninstall_patches warning: {_e}")

    print(
        "[neuro_core] uninstall() called. Persistent pip dependencies are "
        "left in place; plugin files will be removed by the framework."
    )
