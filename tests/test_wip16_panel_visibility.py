"""WI-P16-PANEL-VISIBILITY-BATCH: tests pinning the three panel-visibility fixes.

Covers KI-018-AQ, KI-018-AE, KI-018-AR remediation in the Context Graph panel
content copy (webui/right-canvas-panels/graph-panel.html):

* AQ — the inspector content paragraph's x-text attribute must be properly
  quote-terminated. The defect (introduced in 903dd3c) was an unterminated
  attribute quote that made the browser HTML parser swallow the score block,
  the relationships section (WI-P9 delete affordances) and the Add edge block
  (WI-P13) at parse time. Pinned two ways: exact source pin AND a parse-level
  DOM test (html.parser) asserting the downstream affordance markup survives
  parsing — the parse-level check is the regression class catcher.
* AE — the x-init prefill must not use `this.subdirs[0]` (Alpine v3 x-init
  has no component `this`; the throw aborted the sub prefill AND the trailing
  search()), and the subdirs initializer must fall back to the default chip
  for null, empty-array, and invalid-JSON localStorage values.
* AR — activeFilterCount must align with the WI-P15 both-bounds date_range
  gate (a single bound is NOT an active filter), and the Date Range group
  must carry a visible hint that both bounds are required.
* Shell/content copy roles preserved per the WI-P5B verdict: the extensions
  shell stays the untouched pointer copy; all edits land in the content copy.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PANEL_CONTENT = PLUGIN_ROOT / "webui/right-canvas-panels/graph-panel.html"
PANEL_SHELL = PLUGIN_ROOT / "extensions/webui/right-canvas-panels/graph-panel.html"


# ---------------------------------------------------------------- AQ pins


def test_aq_content_paragraph_attribute_terminated():
    """KI-018-AQ: the x-text attribute on the inspector content paragraph
    must be closed with a double quote before the tag ends."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    # The broken form (unterminated quote) must be gone.
    assert (
        'x-text="inspectNode.content || \'(no content)\'></p>' not in src
    ), "unterminated x-text attribute quote is present (KI-018-AQ regression)"
    # The fixed form must be present exactly once.
    assert src.count(
        'x-text="inspectNode.content || \'(no content)\'"></p>'
    ) == 1


class _InspectorDOMProbe(HTMLParser):
    """Parse-level probe: records whether the WI-P9 delete affordance and the
    WI-P13 Add edge button survive HTML parsing as real elements with their
    binding attributes intact."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.delete_btn_attrs: list[dict[str, str]] = []
        self.add_edge_btn_attrs: list[dict[str, str]] = []
        self.content_p_attrs: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "")
        if tag == "button" and "nc-details__rel-delete" in cls:
            self.delete_btn_attrs.append(a)
        if tag == "button" and a.get("title") == "Add edge from this memory":
            self.add_edge_btn_attrs.append(a)
        if tag == "p" and "nc-details__content" in cls:
            self.content_p_attrs.append(a)


def test_aq_affordance_markup_survives_html_parsing():
    """KI-018-AQ regression-class catcher: parse the panel with a real HTML
    parser and require the delete-edge button, the Add edge button, and the
    content paragraph to exist as parsed elements with their Alpine bindings
    attached. Under the unterminated-quote defect the parser consumed this
    markup into an attribute value and none of these elements existed."""
    probe = _InspectorDOMProbe()
    probe.feed(PANEL_CONTENT.read_text(encoding="utf-8"))
    assert len(probe.content_p_attrs) == 1, "inspector content paragraph not parsed as an element"
    assert "x-text" in probe.content_p_attrs[0], "content paragraph lost its x-text binding at parse time"
    assert len(probe.delete_btn_attrs) == 1, "WI-P9 delete-edge affordance destroyed at parse time"
    assert "@click.stop" in probe.delete_btn_attrs[0], "delete affordance lost its click.stop binding"
    assert len(probe.add_edge_btn_attrs) == 1, "WI-P13 Add edge affordance destroyed at parse time"
    assert "openAddForm()" in probe.add_edge_btn_attrs[0].get("@click", ""), (
        "Add edge button lost its openAddForm binding"
    )


# ---------------------------------------------------------------- AE pins


def test_ae_x_init_prefill_has_no_this_binding():
    """KI-018-AE (1): x-init must read subdirs[0] directly — `this.subdirs[0]`
    throws in Alpine v3 x-init (no component `this`), which aborted the sub
    prefill and the trailing search()."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "this.subdirs[0]" not in src, "x-init still uses this.subdirs[0] (KI-018-AE regression)"
    # WI-P18 AU: the prefill no longer reads subdirs[0] at all — it now uses
    # the active-project store with a 'default' fallback (pinned in detail in
    # test_wip18_panel_ux.py). Here we pin only that the prefill expression
    # remains a direct store read with no component-`this` binding.
    assert "sub = ($store.chats" in src


def test_ae_subdirs_fallback_covers_empty_array_and_invalid_json():
    """KI-018-AE (2): the subdirs initializer must fall back to the default
    chip for null, empty-array, and invalid-JSON localStorage values — not
    only for null."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "localStorage.getItem('nc_memory_subdirs')" in src
    # The initializer must be the guarded IIFE form.
    assert "Array.isArray(v) && v.length" in src, "empty-array fallback missing (KI-018-AE regression)"
    assert "catch (e)" in src, "invalid-JSON fallback missing (KI-018-AE regression)"
    # The old null-only fallback must be gone.
    assert (
        "JSON.parse(localStorage.getItem('nc_memory_subdirs') || '[&quot;projects/neuro_core&quot;]')"
        not in src
    ), "old null-only fallback still present"


# ---------------------------------------------------------------- AR pins


def test_ar_active_filter_count_matches_both_bounds_gate():
    """KI-018-AR (1): activeFilterCount must count date_range as active only
    when BOTH bounds are set, matching the WI-P15 both-bounds gating in
    applyAdvancedFilters."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "(f.date_range_start || f.date_range_end) ? 1 : 0" not in src, (
        "activeFilterCount still counts a single bound as active (KI-018-AR regression)"
    )
    assert "(f.date_range_start && f.date_range_end) ? 1 : 0" in src
    # The apply-side both-bounds gate must remain intact (P15 behavior kept).
    assert "if (this.advancedFilters.date_range_start && this.advancedFilters.date_range_end) params.set('date_range'" in src


def test_ar_date_range_hint_present():
    """KI-018-AR (2): the Date Range filter group must carry a visible hint
    that both bounds are required."""
    src = PANEL_CONTENT.read_text(encoding="utf-8")
    assert "Both start and end are required for the date filter to apply." in src
    # The hint must sit inside the Date Range filter group.
    group = re.search(
        r'<label class="nc-filter-group__title">Date Range</label>.*?nc-filter-hint',
        src,
        re.DOTALL,
    )
    assert group is not None, "date-range hint not located inside the Date Range filter group"


# ------------------------------------------------- copy-role preservation


def test_shell_copy_role_preserved():
    """WI-P5B verdict: the extensions shell stays the small pointer copy; all
    WI-P16 edits land in the content copy only."""
    shell = PANEL_SHELL.read_text(encoding="utf-8")
    content = PANEL_CONTENT.read_text(encoding="utf-8")
    assert len(shell) < 1024, "extensions shell grew beyond pointer-copy size"
    assert "x-component" in shell, "shell lost its x-component pointer"
    # The fixes must be in the content copy, never duplicated into the shell.
    assert "Both start and end are required" in content
    assert "Both start and end are required" not in shell
    assert "sub = ($store.chats" in content
    assert "sub = ($store.chats" not in shell