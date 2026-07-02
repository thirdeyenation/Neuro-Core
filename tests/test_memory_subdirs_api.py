"""Tests for ``api/memory_subdirs.py`` — Neuro Core subdir discovery API.

Three mandatory test cases:

1. Standard subdir exists → returns standard entry with correct path.
2. Project subdir exists → returns project entry with correct path.
3. Neither exists → returns empty list (no exception).

The tests monkeypatch the path constants in the API module to point
at ``tmp_path`` so the discovery logic can be exercised without
touching the real filesystem.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_handler(tmp_path: Path) -> Any:
    """Build a ``MemorySubdirsApi`` instance with path constants patched."""
    from usr.plugins.neuro_core.api import memory_subdirs as mod

    # Patch the module-level path constants to point at tmp_path.
    standard_root = tmp_path / "memory"
    projects_root = tmp_path / "projects"
    mod._STANDARD_MEMORY_ROOT = str(standard_root)
    mod._PROJECTS_ROOT = str(projects_root)

    # ApiHandler.__init__ requires (app, thread_lock) — pass dummies.
    handler = mod.MemorySubdirsApi(app=None, thread_lock=None)
    return handler


async def _call_process(handler: Any) -> dict:
    """Invoke ``handler.process()`` with a stub request and return the dict."""
    fake_request = type("R", (), {"method": "GET", "args": {}})()
    result = await handler.process(input={}, request=fake_request)
    assert isinstance(result, dict)
    return result


# ---------------------------------------------------------------------------
# Test 1: Standard subdir exists → returns standard entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standard_subdir_discovered(tmp_path: Path) -> None:
    # Create a standard subdir under tmp_path/memory/
    (tmp_path / "memory" / "default").mkdir(parents=True)

    handler = _make_handler(tmp_path)
    result = await _call_process(handler)

    assert result["success"] is True
    assert result["count"] == 1
    subdirs = result["subdirs"]
    assert len(subdirs) == 1
    entry = subdirs[0]
    assert entry["name"] == "default"
    assert entry["type"] == "standard"
    assert entry["path"].endswith("/default/")


# ---------------------------------------------------------------------------
# Test 2: Project subdir exists → returns project entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_subdir_discovered(tmp_path: Path) -> None:
    # Create a project memory subdir under tmp_path/projects/<project>/memory/
    (tmp_path / "projects" / "neuro_core_ops" / "memory").mkdir(parents=True)

    handler = _make_handler(tmp_path)
    result = await _call_process(handler)

    assert result["success"] is True
    assert result["count"] == 1
    subdirs = result["subdirs"]
    assert len(subdirs) == 1
    entry = subdirs[0]
    assert entry["name"] == "neuro_core_ops"
    assert entry["type"] == "project"
    assert entry["path"].endswith("/neuro_core_ops/memory/")


# ---------------------------------------------------------------------------
# Test 3: Neither exists → returns empty list (no exception)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_subdirs_returns_empty_list(tmp_path: Path) -> None:
    # tmp_path exists but has no memory/ or projects/ subdirs
    handler = _make_handler(tmp_path)
    result = await _call_process(handler)

    assert result["success"] is True
    assert result["count"] == 0
    assert result["subdirs"] == []
