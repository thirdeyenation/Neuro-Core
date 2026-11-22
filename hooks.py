"""Neuro Core plugin hooks.

Provides `install()` and `uninstall()` entry points called by the
Agent Zero plugin lifecycle.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import re
import shutil
import subprocess
import sys

log = logging.getLogger("neuro_core.hooks")

_NETWORKX_REQUIREMENT = "networkx>=3.0"
_MIN_NETWORKX_MAJOR = 3
_INSTALL_TIMEOUT_SECONDS = 120


def _networkx_at_least_3(version_str: str) -> bool:
    """Return True when ``version_str`` is numerically >= 3.0.

    Uses :mod:`packaging.version` when available and falls back to a
    leading-integer regex so multi-digit majors (e.g. ``10.1.0``) always
    compare NUMERICALLY, never lexicographically.
    """
    try:
        from packaging.version import Version

        return Version(version_str) >= Version("3.0")
    except Exception:
        match = re.match(r"\s*v?(\d+)", version_str or "")
        if not match:
            return False
        return int(match.group(1)) >= _MIN_NETWORKX_MAJOR


def _ensure_networkx() -> bool:
    """Verify-before-install ``networkx>=3.0`` (optional dependency).

    Returns:
        True when networkx>=3.0 is satisfied (pre-existing or newly
        installed). False on ANY non-fatal provisioning failure
        (missing installer, offline/network error, timeout, non-zero
        exit, OSError) — never raises.
    """
    # --- Verify before install (idempotent, zero subprocesses on hit) -----
    try:
        installed = importlib.metadata.version("networkx")
    except importlib.metadata.PackageNotFoundError:
        installed = None
    except Exception as exc:  # unexpected metadata failure: treat as missing
        log.warning("networkx metadata probe failed (%s); attempting install.", exc)
        installed = None

    if installed is not None:
        if _networkx_at_least_3(installed):
            return True
        log.info("networkx %s found but >=3.0 required; upgrading.", installed)

    # --- Provision via uv when present, pip fallback otherwise ------------
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--python", sys.executable, _NETWORKX_REQUIREMENT]
    else:
        cmd = [sys.executable, "-m", "pip", "install", _NETWORKX_REQUIREMENT]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        log.warning(
            "networkx provisioning timed out after %ss (%s). Graph analytics "
            "is degraded; core memory operations remain intact.",
            _INSTALL_TIMEOUT_SECONDS,
            exc,
        )
        return False
    except OSError as exc:
        log.warning(
            "networkx provisioning failed (%s). Graph analytics is degraded; "
            "core memory operations remain intact.",
            exc,
        )
        return False

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        log.warning(
            "networkx provisioning failed (exit %s): %s. Graph analytics is "
            "degraded; core memory operations remain intact.",
            result.returncode,
            stderr,
        )
        return False

    importlib.invalidate_caches()
    return True


def install() -> bool:
    """Install Neuro Core runtime dependencies.

    Steps:
      1. Runtime guard: verify that the `_memory` plugin is importable.
         The Neuro Core plugin extends `_memory` (NativeStore backed by
         memory.sqlite3, Memory class) and cannot function without it.
         If the import fails, log an error and return False without
         installing anything else.
      2. Optionally provision `networkx>=3.0` (graph analytics) via
         `_ensure_networkx()` — verify-before-install, uv preferred,
         pip fallback, non-fatal graceful degradation on failure.
      3. Startup decoration: re-apply @extensible at FULL identity.

    Returns:
        True on success, False ONLY if the required `_memory` dependency
        is missing. Optional networkx degradation never fails install().
    """
    # --- Step 1: Runtime guard: _memory is a hard dependency --------------
    try:
        from plugins._memory.helpers.memory import Memory  # noqa: F401
    except ImportError as exc:
        log.error(
            "FATAL: `plugins._memory.helpers.memory` is not importable. The "
            "`_memory` plugin is a required dependency for `neuro_core`. "
            "Enable the `_memory` plugin in Settings → Plugins and try "
            "again. Underlying error: %s",
            exc,
        )
        return False

    # --- Step 2: Optional runtime dependency: networkx (graph analytics) --
    # Non-fatal: a provisioning failure degrades graph analytics only.
    _ensure_networkx()

    # --- Step 3: Startup decoration: re-apply @extensible at FULL identity
    # Re-applies the framework's public decorator to the Memory methods
    # so the _functions handler tree fires for host-initiated operations.
    # Idempotent and exception-safe (ARC conditions 3/4).
    try:
        from usr.plugins.neuro_core.helpers.decorate import decorate_memory
        decorate_memory()
    except Exception as e:
        log.warning("startup decoration non-fatal: %s", e)

    return True


def uninstall() -> None:
    """Uninstall Neuro Core (no-op: dependencies are intentionally kept).

    We do NOT remove `networkx` or any other pip package on uninstall —
    other plugins may depend on them. The plugin-local files under
    `usr/plugins/neuro_core/` are removed by Agent Zero's plugin manager.
    """
    log.info(
        "uninstall() called. Persistent pip dependencies are left in place; "
        "plugin files will be removed by the framework."
    )
