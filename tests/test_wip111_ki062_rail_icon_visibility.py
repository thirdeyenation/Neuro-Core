"""WI-P111-KI062-RAIL-ICON-VISIBILITY: tests pinning the repaired canvas rail
surface-registration module (KI-062 / KI-062-AA).

Root cause (ARC-verified, steward-design-decision.yaml rev 1): the plugin's
extensions/webui/right_canvas_register_surfaces/neuro_register.js called
this.registerSurface({...}) at ES-module top level with no default export.
Top-level `this` is undefined in an ES module, so the module threw at import;
the host loader (loadJsExtensions) catches the error and returns [], silently
suppressing the ENTIRE right_canvas_register_surfaces extension point -
including the host's own Files surface (order 5). The rail icon appeared only
via the sidebar quick-action's imperative registerSurface in its @click
handler (neuro-entry.html) - the observed missing-until-activated behavior.

Fix (ARC approved-with-conditions, all conditions binding): neuro_register.js
rewritten to the host built-in pattern - `export default async function(store)`
calling store.registerSurface({ id: 'neuro-core-graph', title: 'Neuro Core',
icon: 'hub', order: 50, ... }) - matching the host's register-files.js.

Pin inventory (every added pin enumerated - no silent removals):

* test_module_imports_and_exports_default_async_function - the module imports
  cleanly (the original defect was an import-time throw) and exports a default
  async function (host contract: callJsExtensions invokes
  extension.module.default(...data)).
* test_default_export_registers_expected_surface - executing the default
  export against a recording store registers exactly one surface with
  id 'neuro-core-graph', title 'Neuro Core', icon 'hub', order 50.
* test_registered_surface_open_close_canopen_contract - canOpen() is true;
  open() and close() are awaitable no-ops; the module itself never calls
  store.open (registration alone never opens the canvas).
* test_no_top_level_this_register_surface - source pin: no top-level
  `this.registerSurface` remains; registration happens only inside the
  default export.
* test_no_auto_open_in_module - source pin: the module never calls open(),
  never touches isOpen or activeSurfaceId - eager registration must not
  auto-open the canvas at session start or after reload.
* test_order_places_files_before_neuro_core - order 50 is pinned so the
  host Files surface (order 5, restored by this fix) precedes Neuro Core
  in railSurfaces ordering.
* test_sidebar_fallback_retained_unchanged - neuro-entry.html still contains
  its defensive idempotent registerSurface fallback (id 'neuro-core-graph',
  icon 'hub', order 50) and rc.open('neuro-core-graph') in the click handler
  (ARC binding condition 2: retained UNCHANGED).
* test_module_discoverable_under_host_extension_point - the module is
  catalogued by the host's own extension-manifest builder under the
  UNDERSCORE point right_canvas_register_surfaces (the one
  rightCanvasStore.init() actually calls) and is NOT catalogued under the
  hyphen-named point (rev-3 scope revision: directory renamed from
  right-canvas-register-surfaces to right_canvas_register_surfaces;
  impact-discovery.yaml rev 1, ORC-authorized).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = (
    PLUGIN_ROOT / "extensions/webui/right_canvas_register_surfaces/neuro_register.js"
)
SIDEBAR_ENTRY_PATH = (
    PLUGIN_ROOT / "extensions/webui/sidebar-quick-actions-main-start/neuro-entry.html"
)


@pytest.fixture(scope="module")
def module_src() -> str:
    return MODULE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sidebar_src() -> str:
    return SIDEBAR_ENTRY_PATH.read_text(encoding="utf-8")


def _run_module_default() -> dict:
    """Import the actual ES module under Node and invoke its default export
    with a recording store, asserting the host contract end-to-end."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available for ES-module execution test")
    harness = MODULE_PATH.parent / "_wip111_harness.mjs"
    harness.write_text(
        "import register from " + json.dumps(str(MODULE_PATH)) + ";\n"
        "const recording = { calls: [] };\n"
        "const store = {\n"
        "  registerSurface(surface) { recording.calls.push(['registerSurface', surface]); },\n"
        "  open(id) { recording.calls.push(['open', id]); },\n"
        "};\n"
        "await register(store);\n"
        "// JSON.stringify drops function values, so exercise the surface's\n"
        "// callable contract inside Node and record plain-data results.\n"
        "const registrations = recording.calls.filter(c => c[0] === 'registerSurface');\n"
        "if (registrations.length === 1) {\n"
        "  const surface = registrations[0][1];\n"
        "  recording.canOpenResult = typeof surface.canOpen === 'function' ? surface.canOpen() : null;\n"
        "  recording.openAwaited = (typeof surface.open === 'function' ? await surface.open() : null) ?? null;\n"
        "  recording.closeAwaited = (typeof surface.close === 'function' ? await surface.close() : null) ?? null;\n"
        "  recording.hasCanOpen = typeof surface.canOpen === 'function';\n"
        "  recording.hasOpen = typeof surface.open === 'function';\n"
        "  recording.hasClose = typeof surface.close === 'function';\n"
        "}\n"
        "console.log(JSON.stringify(recording));\n",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [node, str(harness)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    finally:
        harness.unlink(missing_ok=True)
    return json.loads(proc.stdout.strip().splitlines()[-1])


# --------------------- default-export contract (host integration)


def test_module_imports_and_exports_default_async_function():
    """Host contract: callJsExtensions invokes extension.module.default(...data).
    The module must (a) import cleanly - the original defect was an import-time
    TypeError from top-level `this` - and (b) export a callable default."""
    recording = _run_module_default()
    assert recording.get("calls") is not None


def test_default_export_registers_expected_surface():
    """Executing the default export against a recording store registers exactly
    one surface with the pinned identity: id 'neuro-core-graph', title
    'Neuro Core', icon 'hub', order 50."""
    recording = _run_module_default()
    registrations = [c for c in recording["calls"] if c[0] == "registerSurface"]
    assert len(registrations) == 1, (
        f"expected exactly one registerSurface call, got {recording['calls']}"
    )
    surface = registrations[0][1]
    assert surface["id"] == "neuro-core-graph"
    assert surface["title"] == "Neuro Core"
    assert surface["icon"] == "hub"
    assert surface["order"] == 50


def test_registered_surface_open_close_canopen_contract():
    """canOpen() is true; open() and close() are awaitable no-ops; the module
    itself never calls store.open (registration alone never opens the canvas).
    The harness exercises the callables inside Node (JSON cannot carry
    functions) and records their plain-data results."""
    recording = _run_module_default()
    assert recording["hasCanOpen"] is True
    assert recording["hasOpen"] is True
    assert recording["hasClose"] is True
    assert recording["canOpenResult"] is True
    assert recording["openAwaited"] is None
    assert recording["closeAwaited"] is None
    assert not [c for c in recording["calls"] if c[0] == "open"]


# --------------------- source pins


def test_no_top_level_this_register_surface(module_src):
    """The original defect pattern - top-level `this.registerSurface({...})` -
    must never return; registration happens only inside the default export."""
    assert "this.registerSurface" not in module_src
    assert "export default" in module_src


def test_no_auto_open_in_module(module_src):
    """The module must not open the canvas or touch visibility state: no
    open() call, no isOpen write, no activeSurfaceId write. Eager registration
    must not auto-open at session start or after reload."""
    assert ".open(" not in module_src
    assert "isOpen" not in module_src
    assert "activeSurfaceId" not in module_src


def test_order_places_files_before_neuro_core(module_src):
    """Order 50 is pinned: the host Files surface (order 5, restored by this
    fix) precedes Neuro Core in railSurfaces ordering."""
    assert "order: 50" in module_src


# --------------------- sidebar fallback (ARC binding condition 2)


def test_sidebar_fallback_retained_unchanged(sidebar_src):
    """ARC binding condition 2: the sidebar quick-action entry is retained
    UNCHANGED as a defensive idempotent fallback (registerSurface is an
    idempotent upsert). Its imperative registration and open call must remain."""
    assert "rc.getSurface('neuro-core-graph')" in sidebar_src
    assert "rc.registerSurface({" in sidebar_src
    assert "id: 'neuro-core-graph'" in sidebar_src
    assert "icon: 'hub'" in sidebar_src
    assert "order: 50" in sidebar_src
    assert "rc.open('neuro-core-graph')" in sidebar_src


# --------------------- host discovery (rev-3 scope revision)


def test_module_discoverable_under_host_extension_point():
    """Host-discovery contract pin (rev-3 scope revision; impact-discovery.yaml
    rev 1, ORC-authorized). WebUI JS extension discovery is by literal
    directory name: the host store calls
    callJsExtensions("right_canvas_register_surfaces", ...) at init
    (right-canvas-store.js:86), and helpers/extension.py:258-284 discovers
    modules in extensions/webui/<point_name>/ directories. The module must
    therefore live in a directory whose name EXACTLY equals the point name
    the host calls. The rev-1 defect was a hyphenated directory
    (right-canvas-register-surfaces) catalogued under a point no host code
    ever calls. This pin mirrors that contract without importing framework
    helpers (the suite runtime stubs helpers.extension and lacks the
    'regex' dependency of helpers.plugins):
      1. the host store source names the underscore point at init;
      2. the module exists under the underscore-named directory;
      3. the hyphen-named directory no longer exists."""
    import re

    underscore_dir = (
        PLUGIN_ROOT / "extensions/webui/right_canvas_register_surfaces"
    )
    hyphen_dir = PLUGIN_ROOT / "extensions/webui/right-canvas-register-surfaces"

    assert (underscore_dir / "neuro_register.js").is_file(), (
        "neuro_register.js must live under the underscore-named extension "
        "point directory right_canvas_register_surfaces"
    )
    assert not hyphen_dir.exists(), (
        "the hyphen-named directory (never called by the host) must not exist"
    )

    host_store = Path("/a0/webui/components/canvas/right-canvas-store.js")
    if not host_store.is_file():
        pytest.skip("host right-canvas-store.js not available in this environment")
    called_points = re.findall(
        r'callJsExtensions\(\s*"([^"]+)"', host_store.read_text(encoding="utf-8")
    )
    assert "right_canvas_register_surfaces" in called_points, (
        f"host store no longer calls the underscore point; calls found: {called_points}"
    )
    assert underscore_dir.name in called_points, (
        "directory name must exactly equal the host-called extension point "
        "name (discovery is by literal directory name)"
    )
