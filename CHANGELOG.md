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
*Nothing yet — see roadmap in README.md*
