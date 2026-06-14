# Neuro Core

Temporal knowledge graph memory enhancement for Agent Zero. Extends the built-in `_memory` plugin with typed memory categories, graph relationships, importance/confidence/stability scoring, structured `ContextGraph` retrieval, and lifecycle jobs.

## What It Does

Neuro Core layers five capabilities on top of the existing FAISS-backed `Memory` store without replacing it. All new state is stored as FAISS document metadata plus two JSON sidecar files (`relationships.json`, `scores.json`) alongside each `memory_subdir` index. There is no new database, no new service, and no new HTTP layer.

## Installation Prerequisites

- **Agent Zero** version 0.9.0 or newer (per `index.yaml`).
- The built-in **`_memory` plugin must be enabled** before enabling Neuro Core. Neuro Core imports `from plugins._memory.helpers.memory import Memory` and hooks into its `insert_documents`, `delete_documents_by_ids`, and `search_similarity_threshold` methods; it cannot function without the `_memory` plugin loaded.
- Python 3.10+ (matches the Agent Zero runtime).
- No additional pip packages are required for the core feature set. Network graph analytics (networkx) are an optional dependency and the plugin degrades gracefully if absent.

To enable: copy the plugin into `usr/plugins/neuro_core/`, then enable it from the Agent Zero WebUI (Settings → Plugins). The `execute.py` migration runs automatically on first activation and migrates any legacy `consolidated_from` metadata to the `relationships.json` sidecar.

## Main Behavior

### 1. Metadata Scoring

- Adds typed metadata fields to every FAISS document: `memory_type`, `importance`, `confidence`, `stability`, `validation_status`, `read_only`, `episode_id`, and `tags`.
- `memory_type` accepts eight values: `fact`, `concept`, `task`, `event`, `decision`, `skill`, `preference`, `note`.
- `validation_status` accepts four states: `unvalidated`, `validated`, `disputed`, `deprecated`. Deprecated memories are excluded from recall results.
- Numeric scores (`importance`, `confidence`, `stability`) live in the `scores.json` sidecar to avoid full FAISS index rewrites on every update. `validation_status` and `memory_type` live in the FAISS metadata itself.
- All metadata writes go through `validate_neuro_metadata()` which clamps scores to `[0.0, 1.0]` and coerces invalid enum values to safe fallbacks.

### 2. Graph Relationships

- Adds a typed graph layer between memories with seven relationship types: `supports`, `contradicts`, `depends_on`, `derived_from`, `related_to`, `precedes`, `follows`.
- `related_to` is the only symmetric relationship type; the tool writes both directions atomically.
- All edges live in `relationships.json`, keyed by `memory_subdir`, with a per-subdir `RLock` for thread safety and atomic `tempfile.mkstemp` + `os.replace` writes.
- Cascade deletion: when a memory is removed via `Memory.delete_documents_by_ids()`, the `_10_graph_cascade.py` extension hook automatically removes every edge touching that ID.

### 3. ContextGraph Retrieval

- Replaces flat `list[Document]` recall results with a structured `ContextGraph` containing typed nodes and edges.
- The retrieval pipeline (`helpers/retrieval.py`) runs four stages: semantic seed search, lexical/keyword fallback, graph-neighbor expansion up to `graph_max_hops` (default 2), and importance-weighted re-ranking using the configurable `importance_weight` / `recency_weight` / `similarity_weight` blend.
- `ContextGraph.to_prompt_text()` serializes the result into LLM-readable text that can be injected directly into the agent's monologue.
- Access tracking: every successful recall increments `access_count` and updates `last_accessed_at` in the score sidecar (via the `_10_access_tracking.py` hook), feeding back into importance-based ranking.

### 4. Lifecycle Jobs

Three background extensions run on Agent Zero's 60-second `job_loop` scheduler, each with its own throttle interval:

- **`_10_access_decay.py`** — applies `importance *= (1 - importance_decay_rate)` to all non-`validated` memories on a `decay_interval_hours` cadence (default 24h).
- **`_20_episode_grouping.py`** — clusters recent memories into episodes by temporal proximity, using `episode_boundary_hours` (default 4h) and `episode_min_memories` (default 3) thresholds.
- **`_30_contradiction_detection.py`** — sweeps for semantically contradictory memory pairs on a `contradiction_interval_hours` cadence (default 168h / 1 week). Marks the older memory `disputed`. LLM-based detection is opt-in via `contradiction_llm_enabled` (off by default).

Each job uses module-level throttle state to skip runs within the configured interval, so the per-tick cost is near zero.

### 5. Episode Reflection

- The `memory_reflect` tool collects all memories sharing an `episode_id`, sends them to the agent's LLM via `call_utility_model` with a dedicated reflection system prompt, and persists the synthesized insight as a new `concept` memory (importance 0.8, stability 0.9, source `neuro_reflect`).
- Reflections participate in normal recall, so the agent can later retrieve the consolidated summary alongside the original episode fragments.
- Use this after completing a complex multi-step task to consolidate what was learned into a single, high-stability insight memory.

## Agent Tools

All three tools follow the same `Tool` subclass pattern as `plugins/_memory/tools/memory_save.py` and never raise — every error path returns a `Response(message=...)` with `break_loop=False`.

### `memory_relate`

Create or remove a typed relationship between two memory entries.

| Argument   | Type    | Required | Description                                                                 |
|------------|---------|----------|-----------------------------------------------------------------------------|
| `from_id`  | string  | yes      | Source memory ID. Must exist in the active `Memory` instance.               |
| `to_id`    | string  | yes      | Target memory ID. Must exist in the active `Memory` instance.               |
| `rel_type` | string  | yes      | One of: `supports`, `contradicts`, `depends_on`, `derived_from`, `related_to`, `precedes`, `follows`. |
| `weight`   | float   | no       | Edge weight in `[0.0, 1.0]`. Defaults to `1.0`; clamped if out of range.     |
| `remove`   | bool    | no       | If `true`, delete the matching edge instead of adding it.                   |

**Example — add a support edge:**

```
memory_relate(
    from_id="mem_abc123",
    to_id="mem_def456",
    rel_type="supports",
    weight=0.8,
)
```

Returns: `Edge added: mem_abc123 -[supports]-> mem_def456 (weight=0.80).`

**Example — remove a `related_to` edge:**

```
memory_relate(
    from_id="mem_abc123",
    to_id="mem_def456",
    rel_type="related_to",
    remove=true,
)
```

Because `related_to` is symmetric, this also removes the reverse edge. All other relationship types are directional and only the matching `(from_id, to_id, rel_type)` tuple is removed.

### `memory_score`

Update importance, confidence, stability, validation status, memory type, or task status of a memory entry.

| Argument            | Type   | Required | Description                                                                                       |
|---------------------|--------|----------|---------------------------------------------------------------------------------------------------|
| `id`                | string | yes      | Memory ID to update. Must exist in the active `Memory` instance.                                  |
| `importance`        | float  | no       | Importance in `[0.0, 1.0]`. Written to `scores.json`.                                             |
| `confidence`        | float  | no       | Confidence in `[0.0, 1.0]`. Written to `scores.json`.                                             |
| `stability`         | float  | no       | Stability in `[0.0, 1.0]`. Written to `scores.json`.                                              |
| `validation_status` | string | no       | One of: `unvalidated`, `validated`, `disputed`, `deprecated`. Written to FAISS metadata.          |
| `memory_type`       | string | no       | One of: `fact`, `concept`, `task`, `event`, `decision`, `skill`, `preference`, `note`.            |
| `task_status`       | string | no       | One of: `pending`, `active`, `done`, `cancelled`. Only valid when `memory_type == "task"`.        |

Omit any field to leave it unchanged.

**Example — mark a memory as validated and bump its importance:**

```
memory_score(
    id="mem_abc123",
    importance=0.9,
    confidence=0.95,
    stability=0.85,
    validation_status="validated",
)
```

Returns a confirmation listing every field that was changed with its new value.

### `memory_reflect`

Trigger a reflection pass over a memory episode and persist the LLM-synthesized insight as a new `concept` memory.

| Argument     | Type   | Required | Description                                                              |
|--------------|--------|----------|--------------------------------------------------------------------------|
| `episode_id` | string | yes      | The shared `episode_id` metadata field that groups the source memories. |
| `limit`      | int    | no       | Max source memories to include, clamped to `[1, 100]`. Defaults to `20`. |

**Example — reflect on a completed multi-step task:**

```
memory_reflect(episode_id="ep_2026_06_11_research_workflow")
```

Returns: `Reflection written as memory mem_xyz789 (episode: ep_2026_06_11_research_workflow, 7 source memories)`.

## API Endpoints

All endpoints are mounted under `/api/plugins/neuro_core/` via the `ContextGraphApi` handler in `api/context_graph.py`. They require an authenticated session (cookie or API key).

| Method | Path                                | Purpose                                                              |
|--------|-------------------------------------|----------------------------------------------------------------------|
| `GET`  | `/context_graph`                    | Run hybrid retrieval and return the serialized `ContextGraph`.        |
| `GET`  | `/relationships/<memory_id>`        | List all edges (inbound + outbound) touching the given memory ID.     |
| `POST` | `/relationships`                    | Add a new graph edge. Body: `from_id`, `to_id`, `rel_type`, `weight`. |
| `GET`  | `/relationships`                    | List every edge in the active `memory_subdir`.                        |

All requests require a `memory_subdir` parameter. The `GET /context_graph` endpoint additionally requires a `query` string and accepts optional `seed_ids` and `max_hops` overrides.

## Key Files

- **Core data model**
  - `helpers/metadata.py` — `MemoryType` and `ValidationStatus` enums, `validate_neuro_metadata()`.
  - `helpers/graph_store.py` — `GraphStore`, `GraphEdge`, `RelationshipType` enum, atomic JSON writes.
  - `helpers/scores.py` — `ScoreStore`, `MemoryScores` dataclass.
  - `helpers/context_graph.py` — `ContextGraph`, `GraphNode`, `GraphEdge` dataclasses, prompt serialization.
  - `helpers/retrieval.py` — hybrid retrieval pipeline.
  - `helpers/reflection.py` — episode collection, LLM reflection call, persistence.
- **Tools**
  - `tools/memory_relate.py`
  - `tools/memory_score.py`
  - `tools/memory_reflect.py`
- **API**
  - `api/context_graph.py` — `ContextGraphApi` handler exposing the four endpoints above.
- **Extensions**
  - `extensions/python/_functions/plugins._memory.helpers.memory/Memory/insert_documents/start/_10_neuro_metadata.py` — auto-tag new documents with Neuro Core fields.
  - `extensions/python/_functions/plugins._memory.helpers.memory/Memory/delete_documents_by_ids/end/_10_graph_cascade.py` — cascade-delete graph edges.
  - `extensions/python/_functions/plugins._memory.helpers.memory/Memory/search_similarity_threshold/end/_10_access_tracking.py` — increment access counters on recall.
  - `extensions/python/job_loop/_10_access_decay.py` — importance decay sweep.
  - `extensions/python/job_loop/_20_episode_grouping.py` — episode clustering.
  - `extensions/python/job_loop/_30_contradiction_detection.py` — contradiction detection sweep.
- **Prompts**
  - `prompts/agent.system.tool.memory_relate.md`
  - `prompts/agent.system.tool.memory_score.md`
  - `prompts/agent.system.tool.memory_reflect.md`
  - `prompts/neuro.reflection.sys.md` — system prompt for the reflection LLM call.

## Configuration Scope

All settings are read via `plugins.get_plugin_config("neuro_core", agent=agent)`.

- **Settings section**: `agent`
- **Per-project config**: `true`
- **Per-agent config**: `true`

## Configuration Keys

Every key below lives in `default_config.yaml` and is documented with its default value and effect.

| Key                                | Default | Description                                                                                              |
|------------------------------------|---------|----------------------------------------------------------------------------------------------------------|
| `graph_enabled`                    | `true`  | Master switch for the graph layer. When `false`, the `memory_relate` tool and `/relationships` endpoints are disabled. |
| `decay_enabled`                    | `true`  | Master switch for the importance decay job.                                                              |
| `decay_interval_hours`             | `24`    | Minimum hours between decay sweeps. The job loop checks this throttle and skips runs within the window. |
| `importance_decay_rate`            | `0.02`  | Per-day equivalent decay multiplier applied to non-`validated` memories. Example: `importance *= 0.98`.  |
| `contradiction_detection_enabled`  | `true`  | Master switch for the contradiction detection job.                                                       |
| `contradiction_llm_enabled`         | `false` | When `true`, the contradiction sweep uses an LLM call to semantically compare memory pairs. Off by default because the call is expensive. |
| `contradiction_batch_size`         | `100`   | Maximum number of memories scanned per sweep.                                                            |
| `contradiction_interval_hours`     | `168`   | Minimum hours between contradiction sweeps (default 1 week).                                             |
| `reflection_enabled`               | `false` | Master switch for the `memory_reflect` tool and reflection pipeline. Enable per-project when needed.     |
| `reflection_max_memories`          | `50`    | Upper bound on memories included in a single reflection pass.                                            |
| `graph_neighbors_max`              | `40`    | Maximum neighbor count returned per node during graph expansion.                                         |
| `graph_max_hops`                   | `2`     | Maximum hop depth for graph-neighbor expansion in `ContextGraph` retrieval.                              |
| `importance_weight`                | `0.3`   | Weight of the importance score in the final retrieval ranking blend.                                    |
| `recency_weight`                   | `0.2`   | Weight of the recency score in the final retrieval ranking blend.                                       |
| `similarity_weight`                | `0.5`   | Weight of the semantic similarity score in the final retrieval ranking blend.                           |
| `episode_boundary_hours`           | `4`     | Maximum gap between two memories for them to share an `episode_id`. Wider gaps start a new episode.     |
| `episode_min_memories`             | `3`     | Minimum number of memories required for the episode grouping job to form an episode.                    |

The three ranking weights (`importance_weight`, `recency_weight`, `similarity_weight`) are expected to sum to `1.0`; if they do not, the retrieval pipeline normalizes them automatically.

## Known Limitations

- **No dedup-guard on `memory_reflect` episodes (D24).** The reflection tool does not check whether a reflection has already been written for a given `episode_id`. Calling `memory_reflect(episode_id="ep_xyz")` twice will create two separate reflection memories unless the caller deduplicates upstream. A `reflections.json` index or an `episode_id` uniqueness check is on the Phase 5 roadmap; until then, callers should track which episodes have already been reflected on.
- **Module-cache restart requirement after plugin updates.** Agent Zero's runtime caches imported Python modules in `sys.modules`. When you edit a Neuro Core file (e.g., a tool or helper), the running process continues to execute the cached version until it is restarted. The standard `importlib.reload()` workaround works inside the running process but is not safe to leave in production code. Always restart the Agent Zero container (or the affected worker) after updating Neuro Core to pick up the changes.
- **LLM-based contradiction detection is off by default.** `contradiction_llm_enabled` defaults to `false` because the semantic-comparison call is expensive and not yet stability-validated. The contradiction sweep is a no-op until this is enabled explicitly in a controlled setting.
- **Numeric scores live outside FAISS.** `importance`, `confidence`, and `stability` are stored in `scores.json`, not in FAISS metadata. This is intentional (avoids full index rewrites on every update) but means those three values will not appear in `memory_load` results — only `validation_status` and `memory_type` will. Inspect `scores.json` directly or use the Memory Dashboard to view them.
- **Symmetric back-edges only for `related_to`.** Of the seven relationship types, only `related_to` is symmetric. Directional types (`supports`, `contradicts`, `depends_on`, `derived_from`, `precedes`, `follows`) write exactly one edge; traversal in the reverse direction requires a separate call.
- **In-process only.** Neuro Core shares the Agent Zero process and storage paths with `_memory`. It cannot be deployed as a separate service, and it does not provide cross-instance synchronization.

## Plugin Metadata

- **Name**: `neuro_core`
- **Title**: Neuro Core
- **Version**: 0.1.0
- **Tags**: `memory`, `knowledge-graph`, `lifecycle`
- **Min Agent Zero version**: 0.9.0
