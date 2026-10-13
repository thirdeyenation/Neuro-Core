"""WI-P17-SEARCH-JSONPARSE - Context Graph panel search fetch hardening.

Live-defect background (KI-018-AT): the panel's search() used a raw fetch()
without the framework CSRF token and called r.json() before checking r.ok.
In the authenticated live host the framework CSRF layer answered 403
text/html, and the HTML body surfaced to the user as the raw browser
SyntaxError 'JSON.parse: unexpected character at line 1 column 1'.

These tests pin the fixed panel behavior by executing the panel's actual
x-data JavaScript under Node.js with a stubbed fetch:

  1. search() attaches the X-CSRF-Token header obtained from /api/csrf_token.
  2. URL construction stays well-formed for atypical memory_subdir values
     (leading slash, misspelled subdir - the live stale-chip values), which
     are encodeURIComponent-encoded, never spliced raw into the URL.
  3. A non-JSON error response (403 text/html) surfaces a readable,
     action-specific error message - never a raw JSON.parse SyntaxError.
  4. A JSON error body's error field is surfaced on HTTP failure.
  5. Source-level: search() validates the response via parseApiResponse
     before any JSON parsing, and the test_wip13-pinned addEdge fetch
     literal remains intact.
"""

from __future__ import annotations

import json
from pathlib import Path

PLUGIN = Path("/a0/usr/plugins/neuro_core")
PANEL = PLUGIN / "webui" / "right-canvas-panels" / "graph-panel.html"


# ---------------------------------------------------------------------------
# Node harness: execute the panel's real x-data object with stubbed fetch
# ---------------------------------------------------------------------------

NODE_HARNESS = r"""
const fs = require('fs');
const panelPath = process.argv[2];
const scenario = process.argv[3];
const src = fs.readFileSync(panelPath, 'utf8');
const m = src.match(/x-data="\{([\s\S]*)\}" x-init=/);
if (!m) { console.log(JSON.stringify({ ok: false, error: 'x-data not found' })); process.exit(0); }
const factory = new Function('return ({' + m[1] + '});');

function makeResponse(spec) {
  const s = spec || {};
  return {
    ok: (s.ok === undefined) ? true : s.ok,
    status: s.status || 200,
    headers: { get: (name) => (name.toLowerCase() === 'content-type' ? (s.contentType || 'application/json') : null) },
    json: async () => {
      if (s.jsonThrows) throw new SyntaxError('Unexpected token < in JSON at position 0');
      return s.body;
    },
  };
}

async function run() {
  const panel = factory();
  const calls = [];
  let responders;
  const tokenResp = () => makeResponse({ body: { ok: true, token: 'tok-123' } });

  if (scenario === 'csrf-and-url') {
    responders = (url, opts) => {
      calls.push({ url: url, headers: (opts && opts.headers) || {} });
      if (url === '/api/csrf_token') return tokenResp();
      return makeResponse({ body: { success: true, context_graph: { nodes: [], edges: [] } } });
    };
    panel.q = 'recent';
    panel.sub = '/projects/neruo_core';
  } else if (scenario === 'nonjson-403') {
    responders = (url, opts) => {
      calls.push({ url: url, headers: (opts && opts.headers) || {} });
      if (url === '/api/csrf_token') return tokenResp();
      return makeResponse({ ok: false, status: 403, contentType: 'text/html; charset=utf-8', body: '<html>CSRF token missing or invalid</html>' });
    };
    panel.q = 'recent';
    panel.sub = 'projects/neuro_core';
  } else if (scenario === 'json-error-body') {
    responders = (url, opts) => {
      calls.push({ url: url, headers: (opts && opts.headers) || {} });
      if (url === '/api/csrf_token') return tokenResp();
      return makeResponse({ ok: false, status: 400, contentType: 'application/json', body: { error: 'bad query parameter' } });
    };
    panel.q = 'recent';
    panel.sub = 'projects/neuro_core';
  } else if (scenario === 'unreadable-json') {
    responders = (url, opts) => {
      calls.push({ url: url, headers: (opts && opts.headers) || {} });
      if (url === '/api/csrf_token') return tokenResp();
      return makeResponse({ ok: true, status: 200, contentType: 'application/json', jsonThrows: true });
    };
    panel.q = 'recent';
    panel.sub = 'projects/neuro_core';
  } else {
    console.log(JSON.stringify({ ok: false, error: 'unknown scenario ' + scenario }));
    return;
  }

  global.fetch = async (url, opts) => responders(url, opts);
  panel.$nextTick = (fn) => fn();

  await panel.search();
  console.log(JSON.stringify({
    ok: true,
    calls: calls,
    error: panel.error,
    loading: panel.loading,
    nodes: panel.nodes,
    edges: panel.edges,
  }));
}

run().catch((e) => console.log(JSON.stringify({ ok: false, error: String(e && e.stack || e) })));
"""


def _run_node(scenario: str) -> dict:
    import re
    import subprocess
    import tempfile

    src = PANEL.read_text(encoding="utf-8")
    assert re.search(r'x-data="\{[\s\S]*\}" x-init=', src), "panel x-data block missing"

    with tempfile.TemporaryDirectory() as td:
        harness = Path(td) / "harness.js"
        harness.write_text(NODE_HARNESS, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(harness), str(PANEL), scenario],
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert proc.returncode == 0, "node harness failed: " + proc.stderr
    out = proc.stdout.strip().splitlines()[-1]
    result = json.loads(out)
    assert result.get("ok"), "node harness error: " + json.dumps(result)
    return result


# ---------------------------------------------------------------------------
# 1. CSRF header attachment + URL construction with atypical subdir values
# ---------------------------------------------------------------------------


class TestSearchFetchConstruction:
    def test_search_attaches_csrf_token_header(self):
        """search() obtains the CSRF token from /api/csrf_token and sends it
        as X-CSRF-Token on the context_graph request (the live 403 root
        cause)."""
        result = _run_node("csrf-and-url")
        graph_calls = [c for c in result["calls"] if "/api/plugins/neuro_core/context_graph" in c["url"]]
        assert len(graph_calls) == 1
        assert graph_calls[0]["headers"].get("X-CSRF-Token") == "tok-123"

    def test_search_url_encodes_leading_slash_misspelled_subdir(self):
        """The live stale chip value '/projects/neruo_core' (leading slash +
        misspelling) must be encodeURIComponent-encoded into a well-formed
        URL - never spliced raw (which would produce '...subdir=//projects...')."""
        result = _run_node("csrf-and-url")
        graph_calls = [c for c in result["calls"] if "/api/plugins/neuro_core/context_graph" in c["url"]]
        url = graph_calls[0]["url"]
        assert url == (
            "/api/plugins/neuro_core/context_graph"
            "?query=recent&memory_subdir=%2Fprojects%2Fneruo_core"
        )
        assert "//projects" not in url


# ---------------------------------------------------------------------------
# 2. Non-JSON response error surfacing (the live JSON.parse defect)
# ---------------------------------------------------------------------------


class TestNonJsonErrorSurfacing:
    def test_403_html_response_surfaces_readable_error_not_json_parse(self):
        """A 403 text/html CSRF response must surface a readable,
        action-specific error - never the raw browser SyntaxError message
        'JSON.parse: unexpected character at line 1 column 1'."""
        result = _run_node("nonjson-403")
        err = result["error"] or ""
        assert err, "search() must set an error on a 403 HTML response"
        assert "Search failed" in err
        assert "403" in err
        assert "text/html" in err
        assert "JSON.parse" not in err
        assert result["loading"] is False
        assert result["nodes"] == []

    def test_unreadable_json_body_surfaces_readable_error(self):
        """A 200 application/json response whose body cannot be parsed must
        surface a readable error, not a raw JSON.parse SyntaxError."""
        result = _run_node("unreadable-json")
        err = result["error"] or ""
        assert err, "search() must set an error on an unreadable JSON body"
        assert "Search failed" in err
        assert "JSON.parse" not in err

    def test_json_error_body_error_field_is_surfaced(self):
        """A JSON error body's error field is surfaced on HTTP failure."""
        result = _run_node("json-error-body")
        err = result["error"] or ""
        assert err == "bad query parameter"


# ---------------------------------------------------------------------------
# 3. Source-level pins (parse order + preserved wip13 literal)
# ---------------------------------------------------------------------------


class TestPanelSourcePins:
    def _body_between(self, anchor: str, next_anchor: str) -> str:
        text = PANEL.read_text(encoding="utf-8")
        start = text.index(anchor)
        end = text.index(next_anchor, start)
        return text[start:end]

    def test_search_parses_response_via_parseApiResponse(self):
        """search() validates status/content-type via parseApiResponse BEFORE
        any JSON parsing - no raw r.json() in the search body."""
        body = self._body_between("async search() {", "refresh() {")
        assert "await this.parseApiResponse(r, 'Search')" in body
        assert "r.json()" not in body

    def test_search_sends_auth_headers(self):
        body = self._body_between("async search() {", "refresh() {")
        assert "headers: await this.authHeaders()" in body

    def test_panel_defines_csrf_and_parse_helpers(self):
        """The x-data block defines the CSRF token helper and the safe
        response parser used by all panel fetches."""
        text = PANEL.read_text(encoding="utf-8")
        assert "async getCsrfToken()" in text
        assert "async authHeaders(" in text
        assert "async parseApiResponse(r, action)" in text
        assert "'/api/csrf_token'" in text

    def test_wip13_pinned_addedge_literal_preserved(self):
        """The literal pinned by tests/test_wip13_add_edge_ui.py (addEdge
        POST body) must remain intact after the WI-P17 refactor."""
        body = self._body_between("async addEdge() {", "get relatedNodes()")
        assert "fetch('/api/plugins/neuro_core/relationships', { method: 'POST'" in body

    def test_all_four_fetch_sites_use_auth_headers_and_safe_parse(self):
        """search, applyAdvancedFilters, deleteEdge, and addEdge all send the
        CSRF header and parse via parseApiResponse - the parse-order defect
        must not survive in any call site."""
        for anchor, next_anchor in [
            ("async search() {", "refresh() {"),
            ("async applyAdvancedFilters() {", "clearAdvancedFilters() {"),
            ("async deleteEdge(", "async openAddForm() {"),
            ("async addEdge() {", "get relatedNodes()"),
        ]:
            body = self._body_between(anchor, next_anchor)
            assert "await this.authHeaders(" in body, anchor + ": missing authHeaders"
            assert "await this.parseApiResponse(" in body, anchor + ": missing parseApiResponse"
            assert "await r.json()" not in body, anchor + ": raw r.json() before validation"

    def test_xdata_javascript_is_syntactically_valid(self):
        """The full x-data object must remain parseable JavaScript after the
        WI-P17 edits (guards against brace/quote regressions)."""
        import re
        import subprocess
        import tempfile

        src = PANEL.read_text(encoding="utf-8")
        m = re.search(r'x-data="\{([\s\S]*)\}" x-init=', src)
        assert m, "x-data block not found"
        with tempfile.TemporaryDirectory() as td:
            js = Path(td) / "xdata.js"
            js.write_text("async function __wrap(){ return ({" + m.group(1) + "}); }", encoding="utf-8")
            proc = subprocess.run(
                ["node", "--check", str(js)], capture_output=True, text=True, timeout=30
            )
        assert proc.returncode == 0, "x-data JS syntax invalid: " + proc.stderr
