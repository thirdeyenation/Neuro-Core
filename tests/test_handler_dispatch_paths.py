"""Regression tests for the NC1 extensible-handler dispatch-path layout.

Defect (WI-2026-09-07-DISPATCH-PATH-FIX): the handler tree lived under a
LITERAL DOTTED directory (``_functions/plugins._memory.helpers.memory/``),
but the framework's ``extensible`` decorator derives dispatch paths by
splitting the decorated method's ``__module__`` on ``'.'`` and joining the
parts with ``os.path.join`` (``helpers/extension.py``, ``_prepare_inputs``)
— producing SLASHED segments
(``_functions/plugins/_memory/helpers/memory/...``). The dotted directory
could therefore never match: zero handlers resolved and every decorated
Memory call silently no-oped.

Coverage:
1. Path alignment: for each of the three decorated Memory methods
   (``search_similarity_threshold``,
   ``search_similarity_threshold_with_scores``, ``delete_documents_by_ids``),
   the framework-derived slashed dispatch base path exists on disk.
2. Registered-point existence: every point where NC1 registers a handler
   (three ``end`` points for the decorated methods, two ``start`` points for
   ``insert_documents``/``insert_text``) resolves from the framework-derived
   path.
3. Dotted tree removed: the old literal-dotted directory no longer exists.
4. Discovery: a faithful test-double of the framework's only
   handler-discovery path (``helpers.modules.load_classes_from_folder``,
   invoked from ``helpers/extension.py``'s ``_get_extensions``) resolves at
   least one ``Extension`` subclass at every registered point, loading from
   the framework-derived dispatch directory.

Note: the suite's conftest stubs ``helpers`` (empty ``__path__``) and
``helpers.extension`` (minimal ``Extension`` base) so the plugin imports
without booting the full framework — the real ``helpers.modules`` is
therefore intentionally unreachable under pytest. Like
``test_access_tracking.py``'s discovery test, this file uses the same
faithful test-double against the same ``Extension`` base the handlers
import.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS_PYTHON = PLUGIN_ROOT / "extensions" / "python"

# Full decorated identity (must match helpers/decorate.py: full module path
# of the host Memory class + Memory.<method> qualnames).
MEMORY_MODULE = "plugins._memory.helpers.memory"
MEMORY_CLASS = "Memory"

DECORATED_METHODS = (
    "search_similarity_threshold",
    "search_similarity_threshold_with_scores",
    "delete_documents_by_ids",
)

# Points where NC1 actually registers handler files (preserved exactly by
# the move; no new handler files were created).
REGISTERED_POINTS = (
    ("search_similarity_threshold", "end"),
    ("search_similarity_threshold_with_scores", "end"),
    ("delete_documents_by_ids", "end"),
    ("insert_documents", "start"),
    ("insert_text", "start"),
)


def _dispatch_base(method: str) -> str:
    """Replicate the framework's dispatch-path derivation.

    Mirrors helpers/extension.py ``extensible()._prepare_inputs``:
    os.path.join("_functions", *module_parts, *qual_parts).
    """
    module_parts = [p for p in MEMORY_MODULE.split(".") if p]
    qual_parts = [p for p in f"{MEMORY_CLASS}.{method}".split(".") if p]
    return os.path.join("_functions", *module_parts, *qual_parts)


def _discover(point_dir: Path) -> list[type]:
    """Faithful test-double of helpers.modules.load_classes_from_folder.

    The framework's ONLY handler-discovery path: every .py file in the
    handler directory is imported and inspected, and only classes that
    are subclasses of the Extension base the handler itself imports
    survive. Resolves ZERO classes when the directory path does not match
    the framework-derived dispatch path — the exact regression VAL found.
    """
    from helpers.extension import Extension

    discovered = []
    for py_file in sorted(point_dir.glob("*.py")):
        spec = importlib.util.spec_from_file_location(
            f"_dispatch_probe_{py_file.stem}", py_file
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, Extension) and cls is not Extension:
                discovered.append(cls)
    return discovered


# ---------------------------------------------------------------------------
# 1. Framework-derived base path exists for all three decorated methods
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", DECORATED_METHODS)
def test_slashed_dispatch_base_path_exists(method: str):
    """The framework-derived slashed dispatch base path exists on disk."""
    expected = EXTENSIONS_PYTHON / Path(_dispatch_base(method))
    assert expected.is_dir(), (
        f"framework-derived dispatch path does not exist on disk: {expected} "
        f"(derived via os.path.join over module/qualname parts, exactly as "
        f"helpers/extension.py._prepare_inputs does) — handlers at this "
        f"method would silently never fire"
    )


# ---------------------------------------------------------------------------
# 2. Registered handler points resolve from the derived dispatch path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,point", REGISTERED_POINTS)
def test_registered_point_dir_exists(method: str, point: str):
    """Each registered handler point matches the framework-derived path."""
    point_dir = EXTENSIONS_PYTHON / Path(_dispatch_base(method)) / point
    assert point_dir.is_dir(), f"missing dispatch directory: {point_dir}"


# ---------------------------------------------------------------------------
# 3. The dotted tree is gone
# ---------------------------------------------------------------------------


def test_dotted_tree_removed():
    """The old literal-dotted directory must no longer exist."""
    dotted = EXTENSIONS_PYTHON / "_functions" / MEMORY_MODULE
    assert not dotted.exists(), (
        f"stale dotted handler directory still present: {dotted} — it can "
        f"never match the framework's slashed dispatch derivation"
    )


# ---------------------------------------------------------------------------
# 4. Discovery resolves >=1 handler at every registered point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,point", REGISTERED_POINTS)
def test_discovery_resolves_handlers(method: str, point: str):
    """Discovery (framework's load_classes_from_folder contract) finds >=1."""
    point_dir = EXTENSIONS_PYTHON / Path(_dispatch_base(method)) / point
    classes = _discover(point_dir)
    assert len(classes) >= 1, (
        f"discovery resolved ZERO handlers at {method}/{point} ({point_dir}) "
        f"— the exact silent no-op regression this work item remediates"
    )
    # Classes returned by _discover are already Extension subclasses (they
    # were filtered against the same Extension base the handlers import),
    # but assert the framework's execute() contract explicitly.
    for cls in classes:
        assert hasattr(cls, "execute")
        assert hasattr(cls, "__init__")
