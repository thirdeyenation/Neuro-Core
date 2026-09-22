"""WI-P29-403-RECOVERY - Stale-CSRF-token self-healing for the graph panel.

Live-defect background (KI-019): webui/right-canvas-panels/graph-panel.html
cached the CSRF token indefinitely in `_csrfToken` (getCsrfToken/authHeaders)
with no invalidation and no retry on 403. After a framework server restart the
cached token went stale and every authenticated panel call answered 403
(text/html CSRF body) until a full browser hard refresh.

Fix pinned here (panel content copy only; the extensions/ loader shell stays
byte-identical):

  1. Retry-once auto-recovery: a shared withCsrfRetry(doCall) wrapper detects
     HTTP 403 on an authenticated call, resets `_csrfToken = null`, lets the
     thunk re-evaluate authHeaders() (pulling a FRESH token via /api/csrf_token),
     and retries the original call exactly once.
  2. No infinite loop: a second 403 surfaces normally through parseApiResponse
     (with the Refresh-button guidance); no third attempt is made.
  3. Refresh button (@click=refresh()) always resets the cached token before
     re-searching - the manual user fallback for a stale token.
  4. Persistent-403 error addendum instructs the user to click the Refresh
     button (top-right of the panel header, left of the Settings gear).

Methodology (WI-P17 convention): the panel is browser-side HTML/JS, so behavior
is tested by executing the panel's actual x-data JavaScript under Node.js with
a stubbed fetch. This proves the panel's recovery logic (call ordering, token
reset, retry count, error text) - it does NOT prove live host CSRF serving,
which stays under the standing host/browser limitation and is VAL's scope.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

PLUGIN = Path("/a0/usr/plugins/neuro_core")
PANEL = PLUGIN / "webui" / "right-canvas-panels" / "graph-panel.html"
SHELL = PLUGIN / "extensions" / "webui" / "right-canvas-panels" / "graph-panel.html"


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
    json: async () => s.body,
  };
}

async function run() {
  const panel = factory();
  // Stub Alpine magic properties used on success paths ($nextTick -> renderGraph returns early when this.cy is null).
  panel.$nextTick = (fn) => fn();
  const calls = [];
  let tokenCounter = 0;
  const tokenResp = () => { tokenCounter += 1; return makeResponse({ body: { ok: true, token: 'tok-' + tokenCounter } }); };

  if (scenario === 'retry-once-success') {
    let graphCalls = 0;
    const responders = (url, opts) => {
      calls.push({ url: url, token: ((opts && opts.headers) || {})['X-CSRF-Token'] || null });
      if (url === '/api/csrf_token') return tokenResp();
      graphCalls += 1;
      if (graphCalls === 1) return makeResponse({ ok: false, status: 403, contentType: 'text/html; charset=utf-8', body: '<html>CSRF token missing or invalid</html>' });
      return makeResponse({ body: { success: true, context_graph: { nodes: [{ doc_id: 'n1', content: 'hello', score: 0.9 }], edges: [] } } });
    };
    global.fetch = responders;
    panel.q = 'recent';
    panel.sub = 'projects/neuro_core';
    await panel.search();
    return { ok: true, error: panel.error, nodes: panel.nodes, calls: calls };
  }

  if (scenario === 'double-403-no-loop') {
    const responders = (url, opts) => {
      calls.push({ url: url, token: ((opts && opts.headers) || {})['X-CSRF-Token'] || null });
      if (url === '/api/csrf_token') return tokenResp();
      return makeResponse({ ok: false, status: 403, contentType: 'text/html; charset=utf-8', body: '<html>CSRF token missing or invalid</html>' });
    };
    global.fetch = responders;
    panel.q = 'recent';
    panel.sub = 'projects/neuro_core';
    await panel.search();
    return { ok: true, error: panel.error, calls: calls };
  }

  if (scenario === 'refresh-resets-token') {
    const responders = (url, opts) => {
      calls.push({ url: url, token: ((opts && opts.headers) || {})['X-CSRF-Token'] || null });
      if (url === '/api/csrf_token') return tokenResp();
      return makeResponse({ body: { success: true, context_graph: { nodes: [], edges: [] } } });
    };
    global.fetch = responders;
    panel.q = 'recent';
    panel.sub = 'projects/neuro_core';
    await panel.search();
    const tokenBefore = panel._csrfToken;
    panel.refresh();
    const tokenAfterReset = panel._csrfToken;
    await new Promise((resolve) => setTimeout(resolve, 0));
    return { ok: true, tokenBefore: tokenBefore, tokenAfterReset: tokenAfterReset, calls: calls };
  }

  return { ok: false, error: 'unknown scenario: ' + scenario };
}

run().then((r) => console.log(JSON.stringify(r))).catch((e) => console.log(JSON.stringify({ ok: false, error: String(e && e.message || e) })));
"""

SYNTAX_GUARD = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const m = src.match(/x-data="\{([\s\S]*)\}" x-init=/);
if (!m) { console.error('x-data not found'); process.exit(1); }
new Function('return ({' + m[1] + '});');
console.log('JS syntax OK');
"""

_HARNESS_PATH = Path("/tmp/wip29_recovery_harness.js")
_GUARD_PATH = Path("/tmp/wip29_syntax_guard.js")


def _run_scenario(scenario: str) -> dict:
    _HARNESS_PATH.write_text(NODE_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(_HARNESS_PATH), str(PANEL), scenario],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, "node harness failed: " + proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result.get("ok"), "node harness error: " + json.dumps(result)
    return result


def _graph_calls(calls: list) -> list:
    return [c for c in calls if c["url"].startswith("/api/plugins/neuro_core/")]


def _token_calls(calls: list) -> list:
    return [c for c in calls if c["url"] == "/api/csrf_token"]


# ---------------------------------------------------------------------------
# 1. Behavior: retry-once auto-recovery
# ---------------------------------------------------------------------------

class TestRetryOnceRecovery:
    def test_first_403_then_success_recovers_automatically(self):
        """A stale token (first call 403) recovers: token reset, fresh token
        fetched, original call retried exactly once and succeeds."""
        r = _run_scenario("retry-once-success")
        assert r["error"] is None
        assert r["nodes"] and r["nodes"][0]["doc_id"] == "n1"
        graph = _graph_calls(r["calls"])
        assert len(graph) == 2, "exactly one retry expected, got %d graph calls" % len(graph)
        # Stale cached token on first call; FRESH token on the retried call.
        assert graph[0]["token"] == "tok-1"
        assert graph[1]["token"] == "tok-2"
        # Two token fetches: initial + recovery.
        assert len(_token_calls(r["calls"])) == 2

    def test_retry_uses_fresh_token_not_the_stale_cached_one(self):
        r = _run_scenario("retry-once-success")
        graph = _graph_calls(r["calls"])
        assert graph[0]["token"] != graph[1]["token"]


# ---------------------------------------------------------------------------
# 2. Behavior: no infinite loop on persistent 403
# ---------------------------------------------------------------------------

class TestNoInfiniteLoop:
    def test_double_403_surfaces_error_and_stops(self):
        """Two 403s (original + single retry) surface the error and stop -
        no third attempt, no cascading retries."""
        r = _run_scenario("double-403-no-loop")
        graph = _graph_calls(r["calls"])
        assert len(graph) == 2, "retry must happen exactly once, got %d graph calls" % len(graph)
        assert r["error"], "persistent 403 must surface an error"
        assert "403" in r["error"]

    def test_persistent_403_error_names_refresh_fallback(self):
        r = _run_scenario("double-403-no-loop")
        err = r["error"]
        assert "Refresh button" in err
        assert "Settings gear" in err
        assert "server restart" in err


# ---------------------------------------------------------------------------
# 3. Behavior: Refresh button resets the cached token
# ---------------------------------------------------------------------------

class TestRefreshTokenReset:
    def test_refresh_clears_cached_csrf_token(self):
        """refresh() must always reset the cached token before re-searching,
        so the next authenticated call fetches a fresh token."""
        r = _run_scenario("refresh-resets-token")
        assert r["tokenBefore"] == "tok-1"
        assert r["tokenAfterReset"] is None

    def test_search_after_refresh_fetches_fresh_token(self):
        """After the refresh-driven reset the re-run search pulls a new token
        from /api/csrf_token (not the stale cached value)."""
        r = _run_scenario("refresh-resets-token")
        tokens = _token_calls(r["calls"])
        assert len(tokens) >= 2, "token must be re-fetched after refresh"


# ---------------------------------------------------------------------------
# 4. Source-level pins (shared path, scope discipline)
# ---------------------------------------------------------------------------

class TestPanelSourcePins:
    def test_shared_retry_helper_defined(self):
        text = PANEL.read_text(encoding="utf-8")
        assert "async withCsrfRetry(doCall)" in text
        assert "this._csrfToken = null" in text
        assert "r.status === 403" in text

    def test_all_six_authenticated_fetch_sites_use_shared_retry(self):
        """The recovery lives in the shared path: every authenticated fetch in
        the panel goes through withCsrfRetry (search, advanced_filters,
        deleteEdge, addEdge, and both subdir-discovery calls)."""
        text = PANEL.read_text(encoding="utf-8")
        assert text.count("withCsrfRetry(async () => fetch") == 6

    def test_refresh_button_resets_token_before_research(self):
        text = PANEL.read_text(encoding="utf-8")
        body = text[text.index("refresh() {") : text.index("initCytoscape()")]
        assert "this._csrfToken = null" in body
        assert "this.search()" in body

    def test_parseApiResponse_403_addendum_present(self):
        text = PANEL.read_text(encoding="utf-8")
        assert "click the Refresh button" in text
        assert "left of the Settings gear" in text

    def test_extensions_shell_byte_identical(self):
        """Scope discipline: the loader shell must remain untouched."""
        shell_now = hashlib.sha256(SHELL.read_bytes()).hexdigest()
        proc = subprocess.run(
            ["git", "-C", str(PLUGIN), "show", "HEAD:extensions/webui/right-canvas-panels/graph-panel.html"],
            capture_output=True,
            timeout=30,
        )
        shell_head = hashlib.sha256(proc.stdout).hexdigest()
        assert shell_now == shell_head, "extensions loader shell must stay byte-identical"

    def test_pinned_wip17_literals_survive(self):
        """Existing WI-P17 pins remain intact after the WI-P29 refactor."""
        text = PANEL.read_text(encoding="utf-8")
        assert "async getCsrfToken()" in text
        assert "async authHeaders(" in text
        assert "async parseApiResponse(r, action)" in text
        assert "fetch('/api/plugins/neuro_core/relationships', { method: 'POST'" in text
        assert "headers: await this.authHeaders()" in text

    def test_xdata_javascript_is_syntactically_valid(self):
        _GUARD_PATH.write_text(SYNTAX_GUARD, encoding="utf-8")
        check = subprocess.run(
            ["node", str(_GUARD_PATH), str(PANEL)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert check.returncode == 0, "x-data JS syntax invalid: " + check.stderr
