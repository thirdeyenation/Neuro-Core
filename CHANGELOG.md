# Changelog
All notable changes to Neuro Core will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
---
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
