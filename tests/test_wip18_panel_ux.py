"""WI-P18-PANEL-UX-BATCH: tests pinning the panel-UX batch fixes.

Covers the KI-018-AQ remainder (delete-affordance discoverability), AU, AV,
and AW remediation in the Context Graph panel content copy
(webui/right-canvas-panels/graph-panel.html):

* D1 (AQ remainder) — the Relationships section is always visible when a node
  is inspected (no x-show gate), with an explicit empty-state hint so the
  zero-edges case is discoverable; per-relationship delete affordances
  (WI-P9) remain intact inside the section.
* AU — the fresh-open prefill uses the ACTIVE project from the existing
  Alpine chats store ($store.chats.selectedContext.project.name ->
  'projects/<name>') with a 'default' fallback; a stale localStorage chip is
  never the sole prefill source. Chips remain saved favorites.
* AV — a subdir dropdown populated from the existing memory_subdirs API
  (GET /api/plugins/neuro_core/memory_subdirs), with the manual-entry input
  retained as a fallback; project entries are emitted in the abs_db_dir
  'projects/<name>' format.
* AW — the inspector's Memory ID and Content fields carry labels, and the
  Close button states its purpose ('Close inspector').
* Shell/content copy roles preserved per the WI-P5B verdict: the extensions
  shell stays the untouched pointer copy; all edits land in the content copy.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL_CONTENT = PLUGIN_ROOT / "webui/right-canvas-panels/graph-panel.html"
PANEL_SHELL = PLUGIN_ROOT / "extensions/webui/right-canvas-panels/graph-panel.html"


# ---------------------------------------------------------------- D1 pins


def test_d1_relationships_section_always_visible_when_inspecting():
    """KI-018-AQ remainder: the Relationships section must not be gated by
    x-show="relatedNodes.length > 0" — a zero-edge node must still show the
    section (with its empty-state hint) so delete affordances are
    discoverable once edges exist."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert (
        'x-show="relatedNodes.length > 0"' not in src
    ), "Relationships section still hidden for zero-edge nodes (D1 regression)"
    assert '<div class="nc-details__rels">' in src


def test_d1_empty_state_hint_present():
    """D1: the Relationships section carries the explicit empty-state hint."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "No relationships yet — add one with Add edge" in src
    # The hint must be gated on the zero-edges condition.
    assert 'x-show="relatedNodes.length === 0"' in src


def test_d1_delete_affordance_wiring_intact():
    """D1: the WI-P9 per-relationship delete affordance remains intact inside
    the (now always-visible) Relationships section."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert 'class="nc-details__rel-delete" title="Delete edge" @click.stop="deleteEdge(rel)"' in src


_VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class _RelationshipsDOMProbe(HTMLParser):
    """Parse-level probe: the empty-state hint and the delete button must both
    survive HTML parsing inside the Relationships section. Nesting is tracked
    with a real tag stack (exact container class match, void elements excluded)
    so sibling sections cannot be mistaken for nested containers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_rels = False
        self._stack: list[str] = []
        self.hint_seen = False
        self.delete_seen = False

    def _check(self, attrs) -> None:
        cls = dict(attrs).get("class", "")
        if "nc-filter-hint" in cls:
            self.hint_seen = True
        if "nc-details__rel-delete" in cls:
            self.delete_seen = True

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "")
        if not self.in_rels:
            if cls == "nc-details__rels":
                self.in_rels = True
                self._stack = ["div"]
            return
        self._check(attrs)
        if tag not in _VOID_ELEMENTS:
            self._stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if self.in_rels:
            self._check(attrs)

    def handle_endtag(self, tag):
        if not self.in_rels:
            return
        while self._stack and self._stack.pop() != tag:
            pass
        if not self._stack:
            self.in_rels = False


def test_d1_relationships_section_survives_parsing():
    """D1 parse-level check: hint and delete affordance coexist in the parsed
    Relationships section (regression class catcher for attribute-quote
    defects of the KI-018-AQ kind)."""
    probe = _RelationshipsDOMProbe()
    probe.feed(PANEL_CONTENT.read_text(encoding="utf-8"))
    assert probe.hint_seen, "empty-state hint did not survive parsing"
    assert probe.delete_seen, "delete affordance did not survive parsing"


# ---------------------------------------------------------------- AU pins


def test_au_prefill_uses_active_project_store():
    """AU: the fresh-open prefill must read the active project from the
    existing Alpine chats store and map it to the abs_db_dir
    'projects/<name>' format."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "$store.chats.selectedContext.project.name" in src
    assert "'projects/' + $store.chats.selectedContext.project.name" in src


def test_au_prefill_fallback_is_default_not_stale_chip():
    """AU: with no active project the prefill falls back to 'default' — never
    to a stale localStorage chip as the sole source."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert ": 'default'; search()" in src
    # The old stale-chip prefill must be gone.
    assert "sub = subdirs[0] || 'projects/neuro_core'" not in src


def test_au_chips_remain_saved_favorites():
    """AU: localStorage chips remain as saved favorites (click-to-apply and
    save/remove flows intact) — they are no longer the prefill source, but
    the mechanism is preserved, not removed."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "localStorage.getItem('nc_memory_subdirs')" in src
    assert "localStorage.setItem('nc_memory_subdirs'" in src
    assert '@click="sub = s"' in src


# ---------------------------------------------------------------- AV pins


def test_av_dropdown_consumes_memory_subdirs_api():
    """AV: the dropdown is populated by loadSubdirOptions() from the existing
    memory_subdirs API endpoint."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "async loadSubdirOptions()" in src
    assert "fetch('/api/plugins/neuro_core/memory_subdirs'" in src
    assert "subdirOptions" in src


def test_av_project_entries_use_abs_db_dir_format():
    """AV: project subdir entries are emitted as 'projects/<name>' (the
    abs_db_dir format, plugins/_memory/helpers/memory.py:666-673); standard
    entries keep their bare name."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "s.type === 'project' ? ('projects/' + s.name) : s.name" in src


def test_av_manual_entry_fallback_retained():
    """AV: the manual-entry input remains as a robustness fallback alongside
    the dropdown."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert 'placeholder="memory subdir... (manual entry)"' in src
    assert 'x-model="sub"' in src


def test_av_dropdown_is_a_select_bound_to_sub():
    """AV: the dropdown is a real <select> bound to the same `sub` state."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert '<select x-model="sub" class="nc-input nc-input--sub"' in src


# ---------------------------------------------------------------- AW pins


def test_aw_memory_id_label_present():
    """AW: the inspector's memory ID field carries a visible label."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "Memory ID" in src


def test_aw_content_label_present():
    """AW: the inspector's content field carries a visible label."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert ">Content</div>" in src


def test_aw_close_button_states_purpose():
    """AW/D1: the Close button states its purpose explicitly."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert ">Close inspector</button>" in src
    assert ">Close</button>" not in src


# ------------------------------------------------- WI-P36 fav-star pin


def test_wip36_favstar_affordance():
    """WI-P36-FAVSTAR-ICON: the save-subdir-as-chip button carries a Star
    favorite affordance (inline SVG star, HITL-preferred) instead of the bare
    '+' glyph, with an accurate accessible name/tooltip, and the existing
    click handler binding is intact."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    # The save-subdir button: star SVG inside the button element.
    button_idx = src.find('@click="saveSubdir()" class="nc-btn nc-btn--sm nc-btn--ghost"')
    assert button_idx != -1, "save-subdir button markup not found"
    button_end = src.find("</button>", button_idx)
    button_html = src[button_idx:button_end]
    assert "<svg" in button_html and "viewBox=\"0 0 24 24\"" in button_html, (
        "save-subdir button must carry the inline SVG star"
    )
    assert (
        "M12 2l2.9 6.26L21.5 9.27l-4.75 4.63L17.85 20.5 12 17.27 6.15 20.5l1.1-6.6L2.5 9.27l6.6-1.01z"
        in button_html
    ), "save-subdir button must carry the five-point star path"
    # Accessible name/tooltip reflecting purpose.
    assert 'title="Save subdir as chip"' in button_html
    assert 'aria-label="Save subdir as favorite chip"' in button_html
    # No new material-symbols ligature introduced (WI-P25 guard compliance).
    assert "material-symbols-outlined" not in button_html
    # The old bare '+' glyph is gone from this button element itself (the
    # cluster-legend toggle elsewhere in the panel retains its own '+').
    assert "+" not in button_html, "save-subdir button still carries a plus glyph"


def test_wip36_favstar_click_binding_intact():
    """WI-P36: the click handler binding on the save-subdir button is exactly
    the pre-existing saveSubdir() call — behavior unchanged."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert (
        '<button @click="saveSubdir()" class="nc-btn nc-btn--sm nc-btn--ghost" title="Save subdir as chip" aria-label="Save subdir as favorite chip"><svg'
        in src
    )
    # saveSubdir function definition remains in the panel x-data.
    assert "saveSubdir() {" in src


# ------------------------------------------------- copy-role preservation


def test_shell_copy_role_preserved():
    """WI-P5B verdict: the extensions shell stays the small pointer copy; all
    WI-P18 edits land in the content copy only."""
    shell = PANEL_SHELL.read_text(encoding="utf-8")
    content = PANEL_CONTENT.read_text(encoding="utf-8")
    assert len(shell) < 1024, "extensions shell grew beyond pointer-copy size"
    assert "x-component" in shell, "shell lost its x-component pointer"
    for marker in (
        "No relationships yet — add one with Add edge",
        "$store.chats.selectedContext.project.name",
        "loadSubdirOptions()",
        "Close inspector",
        "Memory ID",
    ):
        assert marker in content, f"marker missing from content copy: {marker}"
        assert marker not in shell, f"marker leaked into shell copy: {marker}"