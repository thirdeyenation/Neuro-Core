"""WI-P107-PM020-INJECTED-ORDER-RACE-TEST: standing injected-order race guard.

Mechanizes lessons LL-005/LL-006 (test-context fidelity is part of validation;
panel logic depending on external scripts must use deferred/idempotent
registration and be verified under the framework's real injection semantics).

Background (LL-006, KI-018-BI): the framework component loader
(/a0/webui/js/components.js, loadComponent) re-creates external ``src``
scripts NON-blocking while inline scripts execute synchronously at append
time. A panel inline script that availability-checks an external global
exactly once, synchronously, races the vendor script loads and can permanently
conclude unavailability (the WI-P27 dagre failure: 618/618 green in top-level
harnesses while the live injected panel failed).

This guard is GENERAL (protects future panel code, not just dagre) and is
complementary to the behavioral dagre race coverage in
tests/test_wip27_dagre_vendor.py (test_injection_order_race_deferred_registration_registers_once_and_option_appears
and test_injection_order_race_genuine_load_failure_keeps_cose_fallback_and_hides_option),
whose continued presence is asserted here (coverage preservation).

Mechanism: static anti-pattern scan of panel HTML inline scripts.

A violation is an inline script that BOTH:
  1. performs a synchronous availability check of a non-local, non-builtin
     global (``typeof X``, ``window.X`` reads, ``X in window``), where X is
     not declared in the script itself (var/let/const/function/params) and
     not a self-defined ``window.X =`` global, and
  2. has NO deferred/idempotent semantics: no load/error event wiring, no
     window-load/DOMContentLoaded backstop, no timers.

Escape hatch (registry convention): an inline script may carry the explicit
marker comment ``nc-injected-order-safe`` to declare that its availability
logic is deferred or otherwise race-safe by a mechanism this static scan
cannot recognize. The marker is deliberate and auditable — it must appear in
the script body itself.

Known limitations (documented, not hidden): the scan is regex-based, covers
<script> blocks only (not x-data/x-init attribute logic), and recognizes
deferred semantics structurally, not behaviorally. Behavioral injected-order
evidence for the dagre case remains in test_wip27_dagre_vendor.py; live
injected-panel behavior remains a VAL live-engine step (LL-005).

Fixture discipline: reads plugin-plane files only; violation demonstrations
use tmp_path fixtures — zero product-plane writes.
"""

import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# Panel content sources: the live panel dir and the extensions mount wrapper.
PANEL_DIRS = [
    PLUGIN_ROOT / "webui/right-canvas-panels",
    PLUGIN_ROOT / "extensions/webui/right-canvas-panels",
]

# The behavioral dagre race coverage that must remain in the standing suite.
WIP27_TEST_FILE = PLUGIN_ROOT / "tests/test_wip27_dagre_vendor.py"
WIP27_RACE_TESTS = [
    "test_injection_order_race_deferred_registration_registers_once_and_option_appears",
    "test_injection_order_race_genuine_load_failure_keeps_cose_fallback_and_hides_option",
]

# Explicit, auditable escape-hatch marker (registry convention).
SAFE_MARKER = "nc-injected-order-safe"

# Host builtins that are never "external script dependencies".
_BUILTINS = frozenset(
    {
        "window", "document", "console", "Math", "JSON", "Event", "CustomEvent",
        "Promise", "Object", "Array", "String", "Number", "Boolean", "Date",
        "Error", "RegExp", "Map", "Set", "Symbol", "URL", "Blob", "location",
        "navigator", "localStorage", "sessionStorage", "fetch",
        "requestAnimationFrame", "performance", "history", "alert",
        "setTimeout", "setInterval", "clearTimeout", "clearInterval",
        "undefined", "arguments", "this", "globalThis", "addEventListener",
        "removeEventListener", "dispatchEvent", "alert", "confirm", "prompt",
        "structuredClone", "queueMicrotask", "reportError", "Text", "Node",
        "Element", "HTMLElement", "MutationObserver", "IntersectionObserver",
        "ResizeObserver", "Alpine",
    }
)

# Availability-check shapes. Group 1 captures the checked identifier.
_AVAILABILITY_PATTERNS = [
    re.compile(r"typeof\s+(?:(?:window|globalThis)\.)?([A-Za-z_$][\w$]*)"),
    re.compile(r"(?:window|globalThis)\.([A-Za-z_$][\w$]*)"),
    re.compile(r"([A-Za-z_$][\w$]*)\s+in\s+(?:window|globalThis)\b"),
]

# Deferred/idempotent semantics: retry or backstop wiring of any kind.
_DEFERRED_PATTERNS = [
    re.compile(r"addEventListener\s*\(\s*['\"](?:load|error|DOMContentLoaded)['\"]"),
    re.compile(r"\.(?:onload|onerror)\s*="),
    re.compile(r"\b(?:setTimeout|setInterval)\s*\("),
    re.compile(r"\bDOMContentLoaded\b"),
]

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>([\s\S]*?)</script>", re.IGNORECASE)


def _declared_names(body: str) -> set:
    """Identifiers the script itself binds: declarations, params, self-defined
    window globals. These are never 'external' dependencies."""
    names = set(_BUILTINS)
    for m in re.finditer(r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)", body):
        names.add(m.group(1))
    for m in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)", body):
        names.add(m.group(1))
    # function (named and anonymous) and catch parameter lists
    for m in re.finditer(r"\bfunction\b[^()]*\(([^)]*)\)|\bcatch\s*\(([^)]*)\)", body):
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        for p in raw.split(","):
            p = p.strip().replace("...", "")
            p = re.sub(r"=[\s\S]*$", "", p).strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", p):
                names.add(p)
    # self-defined window globals: window.NAME = ... (assignment, not read)
    for m in re.finditer(r"(?:window|globalThis)\.([A-Za-z_$][\w$]*)\s*=[^=]", body):
        names.add(m.group(1))
    return names


def _external_availability_checks(body: str, declared: set) -> list:
    """External globals whose availability is synchronously checked."""
    checked = []
    for pat in _AVAILABILITY_PATTERNS:
        for m in pat.finditer(body):
            name = m.group(1)
            if name not in declared and name not in checked:
                checked.append(name)
    return checked


def _has_deferred_semantics(body: str) -> bool:
    return any(p.search(body) for p in _DEFERRED_PATTERNS)


def scan_html_for_injected_order_violations(path: Path) -> list:
    """Return a list of violation descriptions for one HTML file.

    Each violation is a string naming the file, inline script index, the
    raced external identifier(s), and the missing deferred semantics.
    External ``src`` scripts are skipped (they ARE the async dependencies).
    """
    violations = []
    text = path.read_text(encoding="utf-8")
    for idx, m in enumerate(_SCRIPT_RE.finditer(text)):
        attrs, body = m.group(1), m.group(2)
        if re.search(r"\bsrc\s*=", attrs):
            continue
        if SAFE_MARKER in body:
            continue  # explicit, auditable escape hatch
        declared = _declared_names(body)
        external = _external_availability_checks(body, declared)
        if not external:
            continue  # no external dependency -> no injected-order race
        if _has_deferred_semantics(body):
            continue  # deferred/idempotent registration present
        violations.append(
            f"{path.name} inline script #{idx}: one-shot synchronous availability "
            f"check(s) of external global(s) {external} with no deferred/idempotent "
            f"semantics (no load/error wiring, no window-load/DOMContentLoaded "
            f"backstop, no timer). Under the framework's injection order "
            f"(non-blocking external scripts + synchronous inline scripts, see "
            f"/a0/webui/js/components.js loadComponent) this races the external "
            f"loads and can permanently conclude unavailability (LL-005/LL-006). "
            f"Use deferred/idempotent registration (inline attempt + load/error "
            f"retry + window-load backstop), or add the explicit marker comment "
            f"'{SAFE_MARKER}' if the logic is race-safe by another auditable "
            f"mechanism."
        )
    return violations


def _panel_html_files():
    files = []
    for d in PANEL_DIRS:
        if d.is_dir():
            files.extend(sorted(d.glob("*.html")))
    return files


# ---------------------------------------------------------------------------
# The standing guard
# ---------------------------------------------------------------------------


def test_panel_inline_scripts_have_no_one_shot_availability_guards():
    """LL-005/LL-006 standing guard: no panel inline script may one-shot
    availability-check an external global without deferred semantics."""
    files = _panel_html_files()
    assert files, "panel HTML directories not found — guard coverage is empty"
    violations = []
    for f in files:
        violations.extend(scan_html_for_injected_order_violations(f))
    assert not violations, (
        "Injected-order race-safety violations in panel code:\n"
        + "\n".join(violations)
    )


def test_guard_coverage_covers_both_panel_content_sources():
    """The guard must actually scan the live panel dir; the extensions copy is
    a mount wrapper but is scanned too if present."""
    live = PANEL_DIRS[0]
    assert live.is_dir(), f"live panel dir missing: {live}"
    assert any(live.glob("*.html")), "no panel HTML found in live panel dir"


# ---------------------------------------------------------------------------
# Coverage preservation: the behavioral dagre race tests must remain
# ---------------------------------------------------------------------------


def test_wip27_behavioral_race_tests_remain_in_suite():
    """This static guard complements — never replaces — the behavioral dagre
    injected-order race tests (WI-P27). Their removal would silently drop the
    only behavioral injected-semantics coverage in the suite."""
    src = WIP27_TEST_FILE.read_text(encoding="utf-8")
    for name in WIP27_RACE_TESTS:
        assert f"def {name}(" in src, (
            f"behavioral injected-order race test '{name}' is missing from "
            f"{WIP27_TEST_FILE.name} — coverage preservation violated"
        )


# ---------------------------------------------------------------------------
# Self-demonstration: the guard must fail on a simulated violation
# ---------------------------------------------------------------------------


def test_scan_demonstrably_flags_one_shot_availability_guard(tmp_path):
    """Simulated violation (LL-006 failure shape): a one-shot inline guard that
    synchronously checks an external vendor global with no retry/backstop.
    The guard MUST fail on this."""
    violating = tmp_path / "violating-panel.html"
    violating.write_text(
        "<div>panel</div>\n"
        '<script src="/vendor/mylib.min.js"></script>\n'
        "<script>\n"
        "if (typeof MyVendorLib === 'undefined') {\n"
        "  window.__myFeatureReady = false; // permanently hides the feature\n"
        "} else {\n"
        "  window.__myFeatureReady = true;\n"
        "}\n"
        "</script>\n",
        encoding="utf-8",
    )
    v = scan_html_for_injected_order_violations(violating)
    assert len(v) == 1, f"violation not detected: {v}"
    assert "MyVendorLib" in v[0]


def test_scan_passes_deferred_registration_fixture(tmp_path):
    """The compliant shape (the WI-P27 hardened pattern): inline attempt,
    load/error retry, window-load backstop. Must NOT be flagged."""
    compliant = tmp_path / "compliant-panel.html"
    compliant.write_text(
        "<div>panel</div>\n"
        '<script src="/vendor/mylib.min.js"></script>\n'
        "<script>\n"
        "window.__myFeatureReady = false;\n"
        "window.__myFeatureRegister = function () {\n"
        "  if (window.__myFeatureReady || typeof window.MyVendorLib !== 'function') return window.__myFeatureReady;\n"
        "  window.__myFeatureReady = true;\n"
        "  return true;\n"
        "};\n"
        "if (!window.__myFeatureRegister()) {\n"
        "  var script = document.querySelector('script[src*=\"mylib\"]');\n"
        "  if (script) {\n"
        "    script.addEventListener('load', function () { window.__myFeatureRegister(); });\n"
        "    script.addEventListener('error', function () {});\n"
        "  }\n"
        "  window.addEventListener('load', function () { window.__myFeatureRegister(); });\n"
        "}\n"
        "</script>\n",
        encoding="utf-8",
    )
    assert scan_html_for_injected_order_violations(compliant) == []


def test_scan_marker_exemption_is_explicit_and_auditable(tmp_path):
    """The registry escape hatch: a script carrying the explicit marker is
    exempt, and the marker must live in the script body itself."""
    marked = tmp_path / "marked-panel.html"
    marked.write_text(
        "<div>panel</div>\n"
        "<script>\n"
        "/* nc-injected-order-safe: availability is re-checked by the host "
        "scheduler on each render tick; no one-shot decision is made here. */\n"
        "if (typeof MyVendorLib === 'undefined') { window.__x = false; }\n"
        "</script>\n",
        encoding="utf-8",
    )
    assert scan_html_for_injected_order_violations(marked) == []

    # marker outside the script body must NOT exempt
    marked_wrong = tmp_path / "marked-wrong.html"
    marked_wrong.write_text(
        "<!-- nc-injected-order-safe -->\n"
        "<script>\n"
        "if (typeof MyVendorLib === 'undefined') { window.__x = false; }\n"
        "</script>\n",
        encoding="utf-8",
    )
    assert len(scan_html_for_injected_order_violations(marked_wrong)) == 1


def test_scan_skips_external_src_scripts_and_local_only_checks(tmp_path):
    """No false positives: external src scripts are the async dependencies
    themselves, and scripts that only check builtins/locals do not race."""
    benign = tmp_path / "benign-panel.html"
    benign.write_text(
        "<div>panel</div>\n"
        '<script src="/vendor/mylib.min.js"></script>\n'
        "<script>\n"
        "var localThing = 1;\n"
        "function helper(x) { return typeof x === 'undefined'; }\n"
        "if (typeof document !== 'undefined') { helper(localThing); }\n"
        "</script>\n",
        encoding="utf-8",
    )
    assert scan_html_for_injected_order_violations(benign) == []
