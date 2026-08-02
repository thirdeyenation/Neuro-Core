"""Neuro Core ``memory_subdirs`` API handler.

Exposes the subdir discovery endpoint under ``/api/plugins/neuro_core/``:

* ``GET /memory_subdirs`` — list all available memory subdirectories.

Discovers two path patterns:

* **Standard subdirs:** ``/a0/usr/memory/<subdir>/``
* **Project subdirs:** ``/a0/usr/projects/<project>/memory/``

All endpoints require an authenticated session (cookie or API key).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from helpers.api import ApiHandler, Request, Response


# ---------------------------------------------------------------------------
# Path constants — verified against abs_db_dir() in
# /a0/plugins/_memory/helpers/memory.py:646
# ---------------------------------------------------------------------------

_STANDARD_MEMORY_ROOT = "/a0/usr/memory"
_PROJECTS_ROOT = "/a0/usr/projects"
_PROJECT_MEMORY_SUBDIR = "memory"


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
        if os.path.isdir(path):
            results.append({
                "name": name,
                "path": path + "/",
                "type": "standard",
            })
    return results


def _list_project_subdirs() -> list[dict[str, str]]:
    """List project memory subdirs under ``/a0/usr/projects/<project>/memory/``."""
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
            _PROJECTS_ROOT, project, _PROJECT_MEMORY_SUBDIR
        )
        if os.path.isdir(memory_path):
            results.append({
                "name": project,
                "path": memory_path + "/",
                "type": "project",
            })
    return results


def _discover_subdirs() -> list[dict[str, str]]:
    """Discover all available memory subdirs (standard + project)."""
    return _list_standard_subdirs() + _list_project_subdirs()
