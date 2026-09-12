# Neuro Core

Neuro Core is an Agent Zero memory enhancement plugin. It extends the
existing `_memory` plugin with typed memory metadata, a typed
relationship graph, importance/confidence/stability scoring, lifecycle
maintenance jobs, and structured ContextGraph retrieval with
explainable scoring factors. It is a Python-only, in-process plugin —
no external service or database daemon is required.

- Plugin identity: `neuro_core` (version 0.1.0)
- Install location: `/a0/usr/plugins/neuro_core/`
- Status: optional plugin — it must be explicitly enabled
  (`always_enabled: false` in `plugin.yaml`) and is configured per
  project and per agent (`per_project_config: true`,
  `per_agent_config: true` in `plugin.yaml`).

## What it provides

**Six agent tools** (declared under `tools/`):

- `neuro_capture` — capture a memory with typed metadata
- `neuro_retrieve` — scoped hybrid retrieval
- `neuro_validate` — validate or dispute a memory
- `memory_score` — update importance/confidence/stability and
  validation status of a memory
- `memory_relate` — create or remove typed graph relationships
- `memory_reflect` — reflect over an episode of memories

See `docs/tools.md` for per-tool behavior and arguments.

**HTTP API** — six handler modules under `api/`, served under
`/api/plugins/neuro_core/`:

- `context_graph` — hybrid retrieval returning a serialized ContextGraph
- `relationships` — list/create/delete graph edges (edge deletion implemented and validated at implementation level; live end-to-end host/browser confirmation pending)
- `advanced_filters` — filtered graph queries
- `episode_audit` — episode listing and per-episode audit
- `reflection_audit` — reflection-memory listing and audit
- `memory_subdirs` — memory subdir discovery

See `docs/api.md` for routes, parameters, and response schemas.

**WebUI assets** — a right-canvas graph panel
(`extensions/webui/right-canvas-panels/graph-panel.html`), its local
vendored Cytoscape library (`webui/vendor/cytoscape-3.30.2.min.js`, served
via the plugin-asset route with no external CDN dependency), and a sidebar
quick-action entry (`extensions/webui/sidebar-quick-actions-main-start/neuro-entry.html`).

**Lifecycle jobs** — three `job_loop` extensions
(`_10_access_decay.py`, `_20_episode_grouping.py`,
`_30_contradiction_detection.py`) plus memory-creation/search hook
extensions and graph cascade-delete cleanup. See
`docs/architecture.md` for the full wiring.

## Installation

1. Place this directory at `/a0/usr/plugins/neuro_core/`.
2. Enable the plugin in Agent Zero's plugin settings.
3. Configuration lives in `default_config.yaml` and can be overridden
   per project and per agent; see `docs/configuration.md`.

## Documentation

- `docs/tools.md` — agent tools
- `docs/api.md` — HTTP API
- `docs/architecture.md` — architecture and data flow
- `docs/configuration.md` — configuration keys
- `docs/data-model.md` — storage and data model
- `CHANGELOG.md` — change history
