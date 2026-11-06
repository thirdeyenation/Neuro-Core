# Contributing to Neuro Core
Thank you for your interest in contributing to Neuro Core — a temporal knowledge graph
and memory enhancement plugin for [Agent Zero](https://github.com/frdel/agent-zero).
---
## Development Environment
Neuro Core runs inside a live Agent Zero container. All plugin code lives at:

`/a0/usr/plugins/neuro_core/`

### Prerequisites
- Agent Zero instance (Docker or local)
- `memory` plugin enabled (required dependency)
- Python 3.12+
- `networkx>=3.0` (installed by `hooks.py` on plugin enable)
### Running Tests
The standing suite entry point is the committed wrapper `tests/run_suite.sh` —
it sets `PYTHONPATH=/a0` itself and always runs from the plugin root:

```bash
/a0/usr/plugins/neuro_core/tests/run_suite.sh
# extra pytest args are forwarded, e.g.:
/a0/usr/plugins/neuro_core/tests/run_suite.sh tests/test_api.py -k graph
```

The full test suite must pass before submitting a PR. No exceptions. The current test count is reported at run time by pytest.
### Plugin Review
```bash
cd /a0
a0-review-plugin usr/plugins/neuro_core
```
Must return 0 FAILs and 0 blocking WARNs.

---

## Project Structure

```text
neuro_core/
├── helpers/ # Core logic — graph_store, metadata, scores, retrieval, reflection
├── tools/ # Agent-facing tools — memory_relate, memory_score, memory_reflect
├── api/ # REST API handlers — context_graph endpoint
├── extensions/ # Agent Zero extension hooks and WebUI injection points
├── webui/ # Panel HTML (inline styles), CSS source, Alpine store
├── tests/ # pytest test suite
└── prompts/ # Tool prompt .md files for agent discovery
```

---

## Contribution Guidelines

### Code Style
- Python only — no TypeScript, no Node.js
- Follow existing import conventions: `from usr.plugins.neuro_core.helpers.X import ...`
- All sidecar file writes must use atomic pattern: `tempfile.mkstemp` + `os.replace`
- No hardcoded colors in WebUI — use Agent Zero CSS variables (`var(--color-*)`) only
- WebUI fragments must use inline `<style>` blocks — external `<link>` stylesheets are not loaded by `x-component`

### Testing
- Every new helper function needs a corresponding test in `tests/`
- Every new API endpoint needs coverage in `test_api.py`
- Tests must not touch live FAISS or Agent Zero internals — use fixtures and mocks
- Run the full suite before every PR: all tests passing is the gate

### Plugin Architecture Rules
- Never write to `/a0/plugins/` — core framework is read-only
- Never modify `agent.py` or `initialize.py`
- API handler subclasses must implement `requires_auth` as a `@classmethod`, not a plain attribute
- Container restart is required after any `.py` file change

### WebUI extension changes
- The WebUI extension manifest is computed once and cached in the WebUI backend
  process (framework cache area `webui_extension_manifest(extensions)(plugins)`;
  see `helpers/extension.py`). It is injected server-side into the rendered page
  and there is no file-watcher or invalidation path for plugin file changes on
  disk. After adding, renaming, or moving plugin extension files under
  `extensions/webui/`, a **WebUI backend restart is required** before the change
  can be observed — a browser hard refresh re-fetches the page but receives the
  same cached manifest.
- Extension-point directory names under `extensions/webui/` must match the
  host's expected point names exactly, including underscores. For example, the
  host calls `right_canvas_register_surfaces` (underscore); a directory named
  `right-canvas-register-surfaces` (hyphen) is silently never invoked — the
  mismatch produces no error.

### Submitting a PR
1. Fork `thirdeyenation/Neuro-Core`
2. Create a feature branch: `git checkout -b feature/your-feature-name`
3. Run tests (all passing) and audit (`0 FAILs`) locally
4. Update `CHANGELOG.md` under `[Unreleased]`
5. Open a PR with a clear description of the change and motivation

---

## Credential Hygiene and Publication Rules

**General principle:** no credential material — tokens, passwords, API keys, or
any other secret value — may appear in any container-external action or artifact.
This applies to **every** path by which repository-derived content leaves the
container. Common examples include (non-exhaustive):

- `git push` / `git commit` to any remote
- Pull requests and their contents (branch commits, PR body, comments)
- Plugin Index or other external submissions (listing text, screenshots, files)
- Any other container-external transfer: attachments, uploads, pasted logs or
  reports derived from repository state

The list above is illustrative, not exhaustive. When in doubt, treat the action
as governed by the general principle.

### Rules

1. **No credentials in remote URLs, ever.** Authentication must flow through
   HITL-managed credential storage outside the repository. A remote URL of the
   form `https://<credential>@host/...` persists the credential in plaintext in
   `.git/config` — this is how the KI-059 exposure occurred. The clean remote
   URL is the required steady state.
2. **Every container-external action requires the pre-publication check to have
   passed** for the exact content being externalized. The check is the
   secret-exclusion scanner at `tests/secret_safeguard_scanner.py`, enforced on
   every run of the test suite (see [Running Tests](#running-tests)). It scans
   `.git/config` remote URLs, tracked repository content, and accepts arbitrary
   text (so PR bodies, submission files, and other payloads can be scanned by
   the same check).
3. **Externalized content is derived from tracked files only** — never from
   runtime state, config files, or chat-captured command output.

### If a credential is exposed

Follow this order strictly:

1. **Rotate or revoke the credential FIRST.** Rotation invalidates the exposure
   regardless of where the credential was visible; scrubbing without rotation
   leaves the credential live.
2. **Scrub the exposed location** (e.g., remove the credential from the remote
   URL in `.git/config`).
3. **Record the incident** in the known-items ledger and the decision log.
4. **Never reproduce the credential value in any artifact** — refer to it in
   redacted form only (location and pattern class, never the value).

### What this safeguard is not

The secret-exclusion scanner is a test-backed exclusion check and policy. It is
**not** a security boundary and **not** a complete data-leak prevention
mechanism: pattern-based detection has inherent false negatives (novel
credential formats, non-GitHub tokens). The recovery procedure above is the
backstop. Do not describe the safeguard as leak prevention on any surface.

---

## Reporting Issues

Open an issue at [github.com/thirdeyenation/Neuro-Core/issues](https://github.com/thirdeyenation/Neuro-Core/issues).
Please include:

- Agent Zero version
- Neuro Core version
- Steps to reproduce
- Expected vs. actual behavior
- Relevant Docker logs or browser console output

---

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE) that covers this project.