# NEURO CORE CHANGELOG

*CURRENT DATE:* 2026-09-09
*UPDATE STATUS:* reconciled 2026-09-09 (Phase E documentation
reconciliation, WI-P5-DOCS-RECON)

## [1.1.0] — 2026-10-10

### Publication Release

Neuro Core 1.1.0 is the first public release of the context graph and
memory enhancement plugin for Agent Zero.

**What Neuro Core does:** it extends Agent Zero's built-in memory
plugin with typed memory categories, a typed relationship graph,
importance/confidence/stability scoring, periodic maintenance jobs
(importance decay, episode grouping, contradiction detection), and
structured ContextGraph retrieval that explains why each result ranked
where it did. It also ships a graph panel for the Agent Zero WebUI, a
settings window, and an HTTP API for graph queries, filtering, audits,
and safe-mode memory editing.

**Requirements:** Agent Zero v2.13 (validated against commit
`e3051fb`), Python 3.12+, and the framework's built-in `_memory`
plugin. The plugin installs `networkx` automatically the first time it
is enabled. No other dependencies beyond the Agent Zero framework
runtime.

**Known limitations in this release:**

- Plugin enablement has been verified at the manifest/discovery level
  (the plugin is discovered and its settings toggle appears); a full
  live enable-cycle confirmation in a running host is still pending.
- The plugin's live HTTP endpoints have not been exercised in
  end-to-end validation for this release; they are covered by the
  plugin's test suite at the implementation level.
- The live FAISS similarity query path has not been run against a live
  host index in this release's validation; index loadability is
  verified.
- The relationships edge-deletion flow and the panel's add-edge UI are
  implemented and test-covered but not yet confirmed end-to-end in a
  live host/browser session.
- The plugin's pre-release documentation guard is textual only; it is
  not a runtime data-leak prevention mechanism.

See the README for installation, data-and-privacy disclosures, and
uninstall guidance.

## [1.0.0] — 2026-06-25

### The First Stable Release

Neuro Core v1.0.0 is the first stable release of the context graph and
memory enhancement plugin for Agent Zero. It ships with all five core
subsystems verified, with the full test suite passing (419 tests at the
1.0.0 release; full-suite count verified 2026-09-11 against the plugin
test suite — see the count-lineage continuation below for the complete
suite history), and with 10 live integration scenarios confirmed on a
real Agent Zero instance. Workstream D — instrumentation and
measurement engineering across the context graph retrieval pipeline,
graph store, score store, decay job, and episode grouping against
internal engineering targets — was completed as part of this release.

Count-lineage continuation (reconciled 2026-09-16, WI-P21): 470 adds 24
new WI-P13-ADD-EDGE-UI inspector add-edge tests (suite 446->470);
482 adds 12 new WI-P14-GRAPH-FILTER-DEFECTS advanced-filters
real-interface tests (470->482); 492 adds 10 new WI-P15-AP-AO docs/
panel date_range contract tests (482->492); 499 adds 7 new
WI-P16-PANEL-VISIBILITY inspector/visibility tests (492->499); 510
adds 11 new WI-P17-SEARCH-JSONPARSE CSRF/response-validation tests
(499->510); 525 adds 15 new WI-P18-PANEL-UX tests (510->525); 539
adds 14 new WI-P19-PANEL-INTERACTION direction-aware labeling,
post-create refresh, form-reset, and subdir-composition tests
(525->539); 552 adds 13 new WI-P20-APISIDE-REMEDIATION
memory_subdirs scan-location, delete-refresh, and multi-seed
visited-set tests (539->552, confirmed 2026-09-15); 570 adds 18 new
WI-P21-LEDGER-ZERO tests (AL query-args consistency pins, BH
per-type/multi-edge behavior pins, AA dormancy pins, CHANGELOG
lineage pin, api.md doc pin, shell-copy pointer; suite count
552->570 confirmed 2026-09-16).

### What Neuro Core Does

Neuro Core gives Agent Zero a persistent, structured memory layer that
goes beyond raw memory storage. Every saved memory is enriched with
importance, confidence, stability, and validation scores. Memories are
connected through a typed relationship graph. The context graph retrieval
system expands any query beyond direct similarity matches — surfacing
semantically related memories, graph-connected neighbors, and
importance-boosted candidates that a flat vector search would miss.
Periodic lifecycle jobs decay stale memories, group temporally-clustered
memories into episodes, and synthesize recurring patterns into durable
concept memories via reflection. The result is an agent that builds
genuinely structured knowledge over time.

### New Since v0.2.0

**Features**
- Reflection enabled by default (`reflection_enabled: true`) — the agent
  now synthesizes concept memories from recurring patterns automatically
- Startup sidecar reconciliation — orphan sidecar entries detected and
  cleaned on every restart (D55)
- Sidecar-authoritative memory scores (supersedes D41 write-back) —
  per ADR-NC1-002 (WI-P12-SCORE-AUTHORITY, KI-009 resolution), score and
  access-count writes go only to the `scores.json` sidecar (single
  authoritative writer); the FAISS metadata score copy is read-only and
  never authoritative — retrieval surfaces `neuro_degraded` for legacy
  records without sidecar entries instead of presenting the mirror as
  healthy
- API relationships routing corrected — list, create, and targeted delete of graph edges via
  `/api/plugins/neuro_core/relationships` (D42, D45; delete implemented and validated at
  implementation level, WI-P9; live host/browser confirmation pending)

**Reliability Fixes**
- EpisodeGroupingJob silent failure resolved — `_iter_docs` and
  `_persist_assignments` now correctly process all docs (D48, D49)
- AccessDecayJob and ContradictionDetectionJob async calls corrected —
  replaced non-existent `Memory.get_by_subdir_sync` with
  `_get_memory_sync` + ThreadPoolExecutor pattern (D51, D52)
- `write_reflection` ScoreStore sidecar write added (D50)
- Episode grouping datetime sort TypeError fixed — offset-naive/aware
  mismatch resolved in `run_episode_grouping` (D57)
- `concurrent_futures` NameError in cold-cache fallback fixed (D58)

**Verification**
- Workstream C: 10/10 live integration scenarios confirmed on a real
  Agent Zero instance
- Workstream D: instrumentation and measurement engineering completed
  across five subsystem areas (context graph retrieval pipeline, graph
  store, score store, decay job, episode grouping) against internal
  engineering targets

**Documentation**
- `docs/` — architecture, API, tools, data model, configuration (5 docs)

# Changelog
All notable changes to Neuro Core will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
---
## [0.2.0] — 2026-06-15
### Added
- `tests/test_isolation.py` — memory subdir isolation contract (7 tests)
- `tests/test_execute.py` — execute.py idempotency (7 tests)
- `tests/test_retrieval.py` — BFS multi-seed context graph retrieval (14 tests)
- `tests/test_contradiction.py` — contradiction detection heuristic (25 tests)
- `tests/test_graph_analytics.py` — run_graph_analytics() contract (22 tests)
- `tests/test_hooks.py` — job_loop extension hooks (16 tests)
- `.github/workflows/test.yml` — GitHub Actions CI on push/PR to main (Python 3.12)
- `pyproject.toml` — build config, dev dependencies, ruff lint config
- `docs/configuration.md` — all config keys with types, defaults, ranges, effects
- `docs/api.md` — HTTP endpoints, params, response schemas, curl examples
- `docs/tools.md` — all 3 agent tools with args, return values, error conditions
- `docs/data-model.md` — FAISS metadata fields, scores.json, relationships.json schemas
- `docs/architecture.md` — storage substrate, hook wiring, ContextGraph pipeline, WebUI panel
### Fixed
- D32: `graph_store.py` — `from_id` now accepts `str | list[str]`; BFS multi-seed expansion was silently returning zero neighbors for all seeds beyond the first
- D33: `graph_store.get_edges()` — made `from_id` optional; no-arg call now returns full adjacency map; `run_graph_analytics()` was always returning `{"nodes": 0, "edges": 0, "boosted": 0}` for all non-empty graphs
- D34: `test_hooks.py` — added `helpers.plugins` stub to unblock `AccessDecayJob` test isolation
### Changed
- Test suite: 191 → 284 passing (+93 tests)

## [0.1.0] — 2026-06-13
### Added
- **Memory metadata extension** — `MemoryType`, `ValidationStatus`, and `validate_neuro_metadata()` enforce typed categories and validation status on all memory insertions
- **Memory scoring tool** (`memory_score`) — agent tool to update `importance`, `confidence`, `stability`, and `validation_status` on any memory
- **Graph store** (`GraphStore`) — typed, atomic relationship graph persisted as `relationships.json` sidecar; supports `add_edge`, `remove_edge`, `neighbors()` with BFS hop traversal
- **Memory relate tool** (`memory_relate`) — agent tool to create and remove typed graph relationships between memories
- **Context graph API** (`GET/POST /api/plugins/neuro_core/context_graph`) — BFS retrieval from seed memories, returning ranked nodes and edges as JSON
- **Access tracking** — `access_count` and `last_accessed_at` updated on every memory recall via extension hook
- **Importance decay job** — background job (`extensions/python/job_loop/`) that decays importance scores over time
- **Contradiction detection job** — background job that flags semantically contradictory memories
- **Episode reflection** — `memory_reflect` tool triggers LLM-powered reflection over memory episodes, writing structured summaries back to the memory store
- **System prompt injection** — Neuro Core context graph summary auto-injected into agent system prompt via extension hook
- **WebUI panel** — right-canvas panel with search input, score-badged node cards, edge pills, and polished dark-mode UI using Agent Zero CSS variables
- **Sidebar entry** — "Neuro Core" entry in Agent Zero sidebar with `hub` Material icon
- **Plugin Index manifest** (`index.yaml`) — ready for `a0-plugins` community index submission
### Architecture
- All persistent state stored as atomic JSON sidecars (`scores.json`, `relationships.json`) — no SQL, no external services
- Plugin-local imports use `usr.plugins.neuro_core.helpers.*` convention
- All sidecar writes use `tempfile.mkstemp` + `os.replace` for atomicity
- Per-project and per-agent config isolation via `memory_subdir`
### Known Limitations (v0.1.0)
- Graph analytics (networkx centrality/clustering) deferred to v0.2.0
- Test coverage for retrieval edge cases (`test_retrieval.py`) deferred to v0.2.0
- Live integration test suite (9-step) deferred to v0.2.0
---
## [Unreleased]
*TO BE FILLED*
