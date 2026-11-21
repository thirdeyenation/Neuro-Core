"""Neuro Core ``memory_subdirs`` API handler.

Exposes the subdir discovery endpoint under ``/api/plugins/neuro_core/``:

* ``GET /memory_subdirs`` — list all available memory subdirectories.

Discovers two path patterns:

* **Standard subdirs:** ``/a0/usr/memory/<subdir>/``
* **Project subdirs:** ``/a0/usr/projects/<project>/.a0proj/memory/``

Scan-shape contract (grounded in framework source):

* ``helpers/projects.py`` defines ``PROJECT_META_DIR = ".a0proj"`` and
  ``get_project_meta(name, *sub_dirs)`` resolving to
  ``<projects_root>/<project>/.a0proj/<sub_dirs>``.
* ``plugins/_memory/helpers/memory.py`` resolves ``projects/<name>``
  subdirs to ``get_project_meta(name) + "memory"`` — i.e. the project
  memory store lives at ``<project>/.a0proj/memory/`` (store sidecar
  location; under v3 the store is ``memory.sqlite3`` (NativeStore),
  under legacy v2 it was ``index.faiss``/``index.pkl`` — presence is
  signaled by any of these markers, mirroring
  ``get_existing_memory_subdirs()``).
* Project entries are returned only when that directory exists, so
  projects without project memory are gracefully omitted (never fatal).

All endpoints require an authenticated session (cookie or API key).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from helpers.api import ApiHandler, Request, Response


# ---------------------------------------------------------------------------
# Path constants — verified against abs_db_dir() in
# /a0/plugins/_memory/helpers/memory.py:646 and the project-memory resolution
# at /a0/plugins/_memory/helpers/memory.py:669-671 (get_project_meta +
# "memory"), with helpers/projects.py PROJECT_META_DIR = ".a0proj".
# ---------------------------------------------------------------------------

_STANDARD_MEMORY_ROOT = "/a0/usr/memory"
_PROJECTS_ROOT = "/a0/usr/projects"
_PROJECT_META_DIR = ".a0proj"
_PROJECT_MEMORY_SUBDIR = "memory"

# Store-marker files that signal a real memory store inside a subdir
# directory. Under v3 the NativeStore persists to ``memory.sqlite3``;
# legacy v2 MyFaiss stores used ``index.faiss`` + ``index.pkl``.
# Mirrors the v3 Memory presence check (memory.py ``memory.sqlite3``)
# while remaining backward-compatible with legacy on-disk stores.
_STORE_MARKERS = ("memory.sqlite3", "index.faiss", "index.pkl")


def _has_store_marker(path: str) -> bool:
    """True when ``path`` contains a recognized memory-store marker file.

    An empty directory (no marker files at all) is still treated as a
    valid memory subdir — discovery stays permissive for freshly
    initialized stores whose first write has not landed yet.
    """
    try:
        entries = set(os.listdir(path))
    except OSError:  # pragma: no cover - defensive
        return False
    return not entries or any(m in entries for m in _STORE_MARKERS)


class MemorySubdirsApi(ApiHandler):
    """REST surface for Neuro Core memory subdir discovery."""

    @classmethod
    def requires_auth(cls) -> bool:
        return True

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET"]

    async def process(self, input: dict, request: Request) -> dict | Response:
        try:
            subdirs = _discover_subdirs()
            return {
                "success": True,
                "subdirs": subdirs,
                "count": len(subdirs),
            }
        except Exception as e:  # pragma: no cover - defensive top-level
            return {"success": False, "error": str(e)}

# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _list_standard_subdirs() -> list[dict[str, str]]:
    """List standard memory subdirs under ``/a0/usr/memory/``."""
    results: list[dict[str, str]] = []
    if not os.path.isdir(_STANDARD_MEMORY_ROOT):
        return results
    try:
        entries = os.listdir(_STANDARD_MEMORY_ROOT)
    except PermissionError:
        logging.getLogger(__name__).warning(
            "Permission denied listing %s", _STANDARD_MEMORY_ROOT
        )
        return results
    for name in sorted(entries):
        path = os.path.join(_STANDARD_MEMORY_ROOT, name)
        if os.path.isdir(path) and _has_store_marker(path):
            results.append({
                "name": name,
                "path": path + "/",
                "type": "standard",
            })
    return results


def _list_project_subdirs() -> list[dict[str, str]]:
    """List project memory subdirs.

    A project memory store lives at
    ``/a0/usr/projects/<project>/.a0proj/memory/`` (the framework's
    ``get_project_meta(name) + "memory"`` resolution). Projects without
    that directory have no project memory and are gracefully omitted.
    """
    results: list[dict[str, str]] = []
    if not os.path.isdir(_PROJECTS_ROOT):
        return results
    try:
        projects = os.listdir(_PROJECTS_ROOT)
    except PermissionError:
        logging.getLogger(__name__).warning(
            "Permission denied listing %s", _PROJECTS_ROOT
        )
        return results
    for project in sorted(projects):
        memory_path = os.path.join(
            _PROJECTS_ROOT, project, _PROJECT_META_DIR, _PROJECT_MEMORY_SUBDIR
        )
        if os.path.isdir(memory_path) and _has_store_marker(memory_path):
            results.append({
                "name": project,
                "path": memory_path + "/",
                "type": "project",
            })
    return results


def _discover_subdirs() -> list[dict[str, str]]:
    """Discover all available memory subdirs (standard + project)."""
    return _list_standard_subdirs() + _list_project_subdirs()
