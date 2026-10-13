"""WI-P27-AS-VENDOR-DAGRE-IMPL: vendored cytoscape-dagre 4.0.1 battery.

Covers the five VAL scenarios pinned by the WI-P26 ARC decision (C8):
(a) vendored file identity (SHA-256 pin, exact size, MIT header); (b) layout
dropdown = 7 existing options in original order + dagre (source-verified,
WI-P24/25 precedent); (c) dagre script tag ordered after the cytoscape tag
with guarded registration; (d) graceful degradation - the panel's real guard
snippet is executed in Node for absent/broken/success cases, and the
effectiveLayout cose fallback is exercised; (e) no conflicting version
claims in docs (4.0.1 per package metadata; no 4.0.0 header claim).

Fixture discipline: reads plugin-plane files only; no live db/sidecar access.
"""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL = PLUGIN_ROOT / "webui/right-canvas-panels/graph-panel.html"
PANEL_SHELL = PLUGIN_ROOT / "extensions/webui/right-canvas-panels/graph-panel.html"
DOCS = PLUGIN_ROOT / "docs/architecture.md"
VENDOR_DIR = PLUGIN_ROOT / "webui/vendor"
DAGRE_FILE = VENDOR_DIR / "cytoscape-dagre.min.js"

DAGRE_SHA256 = "ec671a41d2bfcac580352e2997797793949e3489cecaf347fc48670d4bd456b0"
DAGRE_SIZE = 45649
EXISTING_LAYOUTS = ["cose", "concentric", "breadthfirst", "grid", "circle", "random", "preset"]

NODE_AVAILABLE = shutil.which("node") is not None
needs_node = pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")


@pytest.fixture(scope="module")
def panel_text():
    return PANEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def docs_text():
    return DOCS.read_text(encoding="utf-8")


# scenario (a): vendored file identity

def test_vendor_dir_contains_exactly_the_pinned_set():
    # C1: exactly one file was added; vendor dir holds cytoscape + dagre only
    files = sorted(f.name for f in VENDOR_DIR.iterdir() if f.is_file())
    assert files == ["cytoscape-3.30.2.min.js", "cytoscape-dagre.min.js"]


def test_dagre_file_sha256_matches_pin():
    assert hashlib.sha256(DAGRE_FILE.read_bytes()).hexdigest() == DAGRE_SHA256


def test_dagre_file_size_exact():
    assert DAGRE_FILE.stat().st_size == DAGRE_SIZE


def test_dagre_license_header_present():
    head = DAGRE_FILE.read_bytes()[:500].decode("utf-8", errors="replace")
    assert "License: MIT" in head
    assert "cytoscape-dagre" in head


def test_dagre_is_self_contained_umd_global():
    text = DAGRE_FILE.read_text(encoding="utf-8")
    assert "require(" not in text
    assert "cytoscapeDagre" in text


# scenario (b): dropdown composition

def _layout_select_block(text):
    m = re.search(r'<select x-model="layout"[\s\S]*?</select>', text)
    assert m, "layout select not found"
    return m.group(0)


def test_dropdown_has_exactly_8_options_in_order(panel_text):
    values = re.findall(r'<option value="([^"]+)"', _layout_select_block(panel_text))
    assert values[:7] == EXISTING_LAYOUTS
    assert values[7:] == ["dagre"]
    assert len(values) == 8


def test_dagre_option_self_hides_when_not_ready(panel_text):
    dagre_opt = re.search(r'<option value="dagre"[^>]*>', _layout_select_block(panel_text))
    assert dagre_opt
    assert 'x-show="dagreReady"' in dagre_opt.group(0)


# scenario (c): script tag order + guarded registration

def test_script_tag_order_cytoscape_then_dagre(panel_text):
    cyto_idx = panel_text.find('<script src="/usr/plugins/neuro_core/webui/vendor/cytoscape-3.30.2.min.js">')
    dagre_idx = panel_text.find('<script src="/usr/plugins/neuro_core/webui/vendor/cytoscape-dagre.min.js">')
    assert cyto_idx != -1 and dagre_idx != -1
    assert cyto_idx < dagre_idx


def test_guarded_registration_present(panel_text):
    # KI-018-BI (rev 4): registration is now an idempotent deferred hook.
    assert "window.__ncDagreReady" in panel_text
    assert "window.__ncDagreRegister = function ()" in panel_text
    assert "cytoObj.use(dagreFn)" in panel_text
    # deferral wiring: script load listener + window-load backstop
    assert panel_text.count('addEventListener("load"') >= 1
    assert 'window.addEventListener("load"' in panel_text
    # diagnostics on every fallback/deferral path (LL-002)
    assert "console.warn" in panel_text
    assert panel_text.count("name: this.effectiveLayout()") == 2
    assert panel_text.count("name: this.layout") == 0


def test_shell_content_chain_unaffected(panel_text):
    shell = PANEL_SHELL.read_text(encoding="utf-8")
    assert '<x-component path="/usr/plugins/neuro_core/webui/right-canvas-panels/graph-panel.html" mode="canvas">' in shell
    assert "cytoscape-dagre" not in shell
    assert "cytoscape-dagre" in panel_text



# scenario (d): behavioral degradation - run the panel's real guard in Node

def _extract_guard_snippet(text):
    m = re.search(r"window\.__ncDagreReady = false;[\s\S]*?\}\)\(\);", text)
    assert m, "guarded registration block not found in panel"
    return m.group(0)


def _extract_effective_layout(text):
    m = re.search(r"effectiveLayout\(\) \{ return [\s\S]*?\},", text)
    assert m, "effectiveLayout helper not found in panel"
    return m.group(0)


def _run_node_js(js):
    return subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)


@needs_node
def test_degradation_absent_global_guard_stays_false_without_throwing(panel_text):
    snippet = _extract_guard_snippet(panel_text)
    js = (
        "var window = this; var cytoscape = function () {}; "
        "delete this.cytoscapeDagre;\n" + snippet + "\n"
        "if (window.__ncDagreReady !== false) { throw new Error('ready flag must stay false'); }\n"
        "console.log('OK');"
    )
    r = _run_node_js(js)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OK"


@needs_node
def test_degradation_broken_use_throws_inside_guard_caught(panel_text):
    snippet = _extract_guard_snippet(panel_text)
    js = (
        "var window = this; "
        "var cytoscapeDagre = function () {}; "
        "var cytoscape = function () {}; cytoscape.use = function () { throw new Error('boom'); };\n"
        + snippet + "\n"
        "if (window.__ncDagreReady !== false) { throw new Error('ready flag must stay false'); }\n"
        "console.log('OK');"
    )
    r = _run_node_js(js)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OK"


@needs_node
def test_success_path_guard_registers_and_sets_ready(panel_text):
    snippet = _extract_guard_snippet(panel_text)
    js = (
        "var window = this; var registered = []; "
        "var cytoscapeDagre = function () {}; "
        "var cytoscape = function () {}; cytoscape.use = function (x) { registered.push(x); };\n"
        + snippet + "\n"
        "if (window.__ncDagreReady !== true) { throw new Error('ready flag must be true'); }\n"
        "if (registered[0] !== cytoscapeDagre) { throw new Error('dagre not registered'); }\n"
        "console.log('OK');"
    )
    r = _run_node_js(js)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OK"


@needs_node
def test_degradation_cose_fallback_via_effective_layout(panel_text):
    helper = _extract_effective_layout(panel_text)
    js = (
        "var window = { __ncDagreReady: false };\n"
        "var obj = { layout: 'dagre', get dagreReady() { return window.__ncDagreReady === true; } };\n"
        "obj.effectiveLayout = function " + helper.strip().rstrip(",") + ";\n"
        "if (obj.effectiveLayout() !== 'cose') { throw new Error('fallback must be cose, got ' + obj.effectiveLayout()); }\n"
        "window.__ncDagreReady = true;\n"
        "if (obj.effectiveLayout() !== 'dagre') { throw new Error('dagre must pass through when ready'); }\n"
        "obj.layout = 'cose';\n"
        "if (obj.effectiveLayout() !== 'cose') { throw new Error('non-dagre layouts pass through'); }\n"
        "console.log('OK');"
    )
    r = _run_node_js(js)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OK"


def test_both_layout_call_sites_use_guarded_resolution(panel_text):
    # both construction sites resolve via effectiveLayout()
    assert panel_text.count("name: this.effectiveLayout()") == 2
    assert panel_text.count("name: this.layout") == 0


def test_dagre_options_rankdir_lr_at_call_sites_existing_layouts_unchanged(panel_text):
    # WI-P27 rev3 (ARC conformance condition 3): pin the dagre-only options path.
    # The dagre options helper exists and is merged at both call sites.
    assert "layoutExtras()" in panel_text
    assert panel_text.count("this.layoutExtras()") == 2
    m = re.search(r"layoutExtras\(\) \{ return [\s\S]*?\},", panel_text)
    assert m, "layoutExtras helper not found in panel"

    # both call sites keep the original shared options (7 existing layouts unchanged)
    original = "name: this.effectiveLayout(), animate: false, fit: true, padding: 30"
    assert panel_text.count(original) == 2

    # behavioral: dagre-ready -> { rankDir: 'LR' }; anything else -> {} (no extras)
    helper = m.group(0)
    js = (
        "var window = { __ncDagreReady: true };\n"
        "var obj = { layout: 'dagre', get dagreReady() { return window.__ncDagreReady === true; } };\n"
        "obj.layoutExtras = function " + helper.strip().rstrip(",") + ";\n"
        "var e = obj.layoutExtras();\n"
        "if (e.rankDir !== 'LR') { throw new Error('dagre extras must set rankDir LR, got ' + JSON.stringify(e)); }\n"
        "if (Object.keys(e).length !== 1) { throw new Error('dagre extras must contain only rankDir'); }\n"
        "obj.layout = 'cose';\n"
        "if (Object.keys(obj.layoutExtras()).length !== 0) { throw new Error('non-dagre layouts must receive empty extras'); }\n"
        "obj.layout = 'dagre'; window.__ncDagreReady = false;\n"
        "if (Object.keys(obj.layoutExtras()).length !== 0) { throw new Error('unready dagre must receive empty extras'); }\n"
        "console.log('OK');"
    )
    if NODE_AVAILABLE:
        r = _run_node_js(js)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "OK"
    else:  # source-level fallback when node is unavailable
        assert "this.layout === 'dagre' && this.dagreReady" in m.group(0)
        assert "rankDir: 'LR'" in m.group(0)


# scenario (e): docs version claims

def test_docs_state_package_version_not_header_version(docs_text):
    assert "4.0.1" in docs_text
    assert "4.0.0" not in docs_text


def test_docs_describe_dagre_behavior_without_unfounded_claims(docs_text):
    text = docs_text
    dagre_pos = text.find("dagre")
    assert dagre_pos != -1
    bullet = text[dagre_pos:text.find("\n-", dagre_pos) if text.find("\n-", dagre_pos) != -1 else len(text)]
    assert "layered" in bullet or "layer" in bullet
    assert "performance" not in bullet.lower()


# KI-018-BI regression: framework injection order (non-blocking external vendor
# scripts + synchronous inline script). The harness extracts the panel's REAL
# guard block and Alpine mirror snippets and exercises the ordering behaviorally:
# inline guard runs BEFORE the vendor globals exist, then the vendor script's
# load event fires later. Documented limitation: Node executes the real snippet
# logic and the real ordering semantics (check-time vs load-time), but not the
# browser's innerHTML/script-network machinery itself; live injected-panel
# behavior remains a VAL live-engine step.

def _extract_hardened_guard(text):
    m = re.search(r"window\.__ncDagreReady = false;[\s\S]*?\}\)\(\);", text)
    assert m, "hardened guard block not found in panel"
    return m.group(0)


def _extract_dagre_sync(text):
    m = re.search(r"dagreSync\(\) \{ return [\s\S]*?;\},", text)
    # dagreSync is a single-statement helper; match its real form instead
    m = re.search(r"dagreSync\(\) \{ [\s\S]*?\},", text)
    assert m, "dagreSync mirror helper not found in panel"
    return m.group(0)


@needs_node
def test_injection_order_race_deferred_registration_registers_once_and_option_appears(panel_text):
    guard = _extract_hardened_guard(panel_text)
    sync_helper = _extract_dagre_sync(panel_text)
    js = (
        "var warnings = [];\n"
        "var registered = [];\n"
        # fake vendor script element (framework re-creates it non-blocking)
        "var scriptEl = { h: {}, addEventListener: function (t, f) { (scriptEl.h[t] = scriptEl.h[t] || []).push(f); } };\n"
        "var docH = {};\n"
        "var winH = {};\n"
        "global.document = {\n"
        "  querySelector: function () { return scriptEl; },\n"
        "  addEventListener: function (t, f) { (docH[t] = docH[t] || []).push(f); },\n"
        "  dispatchEvent: function (ev) { (docH[ev.type] || []).forEach(function (f) { f(); }); }\n"
        "};\n"
        "global.window = {\n"
        "  addEventListener: function (t, f) { (winH[t] = winH[t] || []).push(f); }\n"
        "};\n"
        "var _olog = console.log.bind(console);\n"
        "global.console = { warn: function (m) { warnings.push(String(m)); }, error: function () {}, log: _olog };\n"
        "global.Event = function (t) { this.type = t; };\n"
        "window.console = global.console;\n"
        # --- framework injection order: inline guard executes SYNCHRONOUSLY here,
        # while the external vendor scripts have NOT finished loading ---
        + guard + "\n"
        "if (window.__ncDagreReady !== false) { throw new Error('inline attempt must not flip ready before vendor load'); }\n"
        "if (warnings.length !== 1) { throw new Error('expected exactly 1 deferral diagnostic, got ' + warnings.length); }\n"
        # --- the vendor scripts eventually finish loading ---
        "var cyto = { use: function (ext) { registered.push(ext); } };\n"
        "var dagreExt = function () {};\n"
        "window.cytoscape = cyto; window.cytoscapeDagre = dagreExt;\n"
        "var fired = [];\n"
        "docH['nc-dagre-ready'] = [function () { fired.push('nc-dagre-ready'); }];\n"
        "(scriptEl.h['load'] || []).forEach(function (f) { f(); });\n"
        "if (window.__ncDagreReady !== true) { throw new Error('deferred path must register dagre on script load'); }\n"
        "if (registered.length !== 1 || registered[0] !== dagreExt) { throw new Error('dagre must be registered exactly once'); }\n"
        "if (fired.length !== 1) { throw new Error('nc-dagre-ready event must dispatch once on deferred registration'); }\n"
        # idempotency: repeated load events and direct calls must not re-register
        "(scriptEl.h['load'] || []).forEach(function (f) { f(); });\n"
        "window.__ncDagreRegister();\n"
        "if (registered.length !== 1) { throw new Error('double registration must not duplicate the extension'); }\n"
        "if (warnings.length !== 1) { throw new Error('diagnostics must not spam on idempotent re-determination'); }\n"
        # Alpine mirror: the reactive prop flips so the Dagre option appears
        "var comp = { layout: 'dagre', dagreReady: false };\n"
        "comp.dagreSync = function " + sync_helper.strip().rstrip(",") + ";\n"
        "(docH['nc-dagre-ready'] || []).length; // listener wiring is done by the component itself\n"
        "comp.dagreSync();\n"
        "if (comp.dagreReady !== true) { throw new Error('Alpine mirror prop must flip true so the option appears'); }\n"
        "console.log('OK');"
    )
    r = _run_node_js(js)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OK"


@needs_node
def test_injection_order_race_genuine_load_failure_keeps_cose_fallback_and_hides_option(panel_text):
    guard = _extract_hardened_guard(panel_text)
    sync_helper = _extract_dagre_sync(panel_text)
    js = (
        "var warnings = [];\n"
        "var registered = [];\n"
        "var scriptEl = { h: {}, addEventListener: function (t, f) { (scriptEl.h[t] = scriptEl.h[t] || []).push(f); } };\n"
        "var docH = {};\n"
        "global.document = {\n"
        "  querySelector: function () { return scriptEl; },\n"
        "  addEventListener: function (t, f) { (docH[t] = docH[t] || []).push(f); },\n"
        "  dispatchEvent: function (ev) { (docH[ev.type] || []).forEach(function (f) { f(); }); }\n"
        "};\n"
        "global.window = { addEventListener: function (t, f) { this._wh = this._wh || []; this._wh.push([t, f]); } };\n"
        "var _olog = console.log.bind(console);\n"
        "global.console = { warn: function (m) { warnings.push(String(m)); }, error: function () {}, log: _olog };\n"
        "global.Event = function (t) { this.type = t; };\n"
        "window.console = global.console;\n"
        "var registered = [];\n"
        + guard + "\n"
        # genuine load failure: script error event, then window load backstop fires
        "(scriptEl.h['error'] || []).forEach(function (f) { f(); });\n"
        "(window._wh || []).filter(function (p) { return p[0] === 'load'; }).forEach(function (p) { p[1](); });\n"
        "if (window.__ncDagreReady !== false) { throw new Error('ready must stay false on genuine load failure'); }\n"
        "if (registered.length !== 0) { throw new Error('nothing may be registered on genuine load failure'); }\n"
        "if (warnings.length < 2) { throw new Error('deferral and final-failure diagnostics are both required (LL-002)'); }\n"
        # cose fallback still resolves for a dagre selection that never became ready
        "var comp = { layout: 'dagre', dagreReady: false };\n"
        "comp.dagreSync = function " + sync_helper.strip().rstrip(",") + ";\n"
        "comp.dagreSync();\n"
        "if (comp.dagreReady !== false) { throw new Error('mirror prop must stay false'); }\n"
        "var eff = (comp.layout === 'dagre' && !comp.dagreReady) ? 'cose' : comp.layout;\n"
        "if (eff !== 'cose') { throw new Error('cose fallback must remain'); }\n"
        "console.log('OK');"
    )
    r = _run_node_js(js)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OK"


# constraints

def test_battery_reads_plugin_files_only():
    src = Path(__file__).read_text(encoding="utf-8")
    # tokens assembled by concatenation so this test does not self-reference
    assert ("neuro_" + "core.db") not in src
    assert ("scores" + ".json") not in src
    assert ("relationships" + ".json") not in src
    assert "sqlite" + "3" not in src


def test_panel_patch_is_bounded_wiring_only(panel_text):
    assert panel_text.count('x-model="layout"') == 1
    assert "window.__ncDagreReady = false;" in panel_text
    assert "<!-- WI-P24 C6: bundled layouts exposed" in panel_text
    assert 'value="preset"' in panel_text
