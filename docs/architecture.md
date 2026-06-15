# Neuro Core Architecture

This document describes how Neuro Core fits into Agent Zero and how the
moving parts — FAISS metadata, JSON sidecars, job loop extensions,
agent tools, the HTTP API, and the WebUI panel — interact with the
existing `_memory` plugin.

---

## 1. Overview

Neuro Core is a **memory enhancement plugin** for Agent Zero. It is a
Python-only, in-process plugin installed at
`/a0/usr/plugins/neuro_core/` and discovered automatically by
Agent Zero's plugin loader. It does not introduce any new service,
daemon, container, language, or external database. It extends the
existing `_memory` plugin by adding:

1. **Typed metadata** on every memory document. The
   `memory_type`, `importance`, `confidence`, `stability`,
   `validation_status`, `read_only`, and `episode_id` fields are
   written into the `Document.metadata` dict that FAISS already
   stores alongside each vector. No schema migration of the FAISS
   index is required — adding metadata keys is non-breaking.
2. **A graph layer** of typed, weighted relationships between
   memories (`supports`, `contradicts`, `depends_on`, `derived_from`,
   `related_to`, `precedes`, `follows`). The graph lives in two new
   JSON sidecar files (`relationships.json` and `scores.json`)
   placed in the same `abs_db_dir(memory_subdir)` directory as the
   FAISS index.
3. **Hybrid retrieval** via `search_context_graph()`. A single
   function combines FAISS semantic search, BFS graph expansion,
   and importance/recency-weighted reranking, returning a structured
   `ContextGraph` (nodes + edges) that the agent can serialize to a
   prompt fragment.
4. **Three new agent tools** — `memory_score`, `memory_relate`,
   `memory_reflect` — that let the agent itself maintain the graph
   and the score sidecar.
5. **Three background lifecycle jobs** — importance decay, episode
   grouping, and contradiction detection — that run as `job_loop`
   extensions piggy-backing on Agent Zero's existing job scheduler.
6. **An HTTP API** under `/api/plugins/neuro_core/` for both the
   Memory Dashboard UI and external consumers, plus a right-canvas
   WebUI panel that lets users browse the graph, search for
   memories, and create/delete relationships.

---

## 2. Storage substrate

All Neuro Core state lives in three places, all under
`abs_db_dir(memory_subdir)`:

| Substrate | File | Format | Written by | Read by |
|---|---|---|---|---|
| FAISS document metadata | `index.faiss` + `index.pkl` | FAISS + pickled docstore | `_memory` plugin, Neuro Core (validation_status, memory_type, episode_id) | `_memory` plugin, Neuro Core retrieval |
| Scores sidecar | `scores.json` | JSON | `ScoreStore` (via `memory_score` tool, `run_importance_decay`, `update_access`) | `ScoreStore.get()` (via retrieval) |
| Relationships sidecar | `relationships.json` | JSON | `GraphStore` (via `memory_relate` tool, `ContextGraphApi`, `execute.py` migration, contradiction detector, cascade-delete hook) | `GraphStore.neighbors()` (via retrieval), `GraphStore.get_edges()` (via `run_graph_analytics`) |

The two existing sidecars from `_memory` (`embedding.json` and
`knowledge_import.json`) are not modified by Neuro Core.

### Why sidecars and not a separate database

Three reasons:

1. **Atomicity at the agent-tool level.** Every tool call reads and
   writes the same subdir's files; adding a separate DB would require
   cross-process transactions.
2. **Mobility.** The directory is a self-contained, copyable bundle
   that can be rsynced, backed up, or attached to a new agent
   instance without external dependencies.
3. **Memory_subdir isolation.** Each Agent Zero subdir has its own
   `abs_db_dir`, which automatically scopes the graph and scores —
   no `tenant_id` plumbing needed.

### Sidecar write pattern

Both `ScoreStore` and `GraphStore` use the same atomic write pattern
that the framework itself uses in `helpers/kvp.py`:

```python
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target_path))
try:
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, target_path)
except Exception:
    os.unlink(tmp)
    raise
```

A per-subdir `threading.RLock` (stored on the instance) serializes
all reads and writes, so concurrent tool and job-loop calls cannot
tear the file.

---

## 3. Hook wiring

Neuro Core integrates with Agent Zero's plugin system through two
entry points: `hooks.py` (lifecycle) and `execute.py` (migration).

### `hooks.py` — install / uninstall

`hooks.py` defines:

- `install()` — called once by the plugin loader when the plugin is
  first enabled. It:
  1. Verifies that `plugins._memory.helpers.memory.Memory` is
     importable. If not, prints a fatal error and returns `False`,
     blocking activation.
  2. Installs `networkx>=3.0` via `subprocess.run([sys.executable,
     "-m", "pip", "install", "networkx>=3.0"])`. This is a soft
     dependency — only required for the future `_40_graph_analytics`
     job loop extension. The install is wrapped in try/except and
     never blocks plugin activation if it fails.
  3. Returns `True` on success.
- `uninstall()` — no-op. Removing the plugin directory is the
  uninstall; there is no cleanup script for the sidecar files
  (deliberate — users may want to recover the data after
  uninstalling).

### `execute.py` — one-shot migration

`execute.py` runs **exactly once** when the plugin is upgraded from a
version that did not have Neuro Core metadata, or when a fresh
plugin is first activated on an existing FAISS store. The migration
script:

1. Iterates over all subdirs returned by
   `get_existing_memory_subdirs()`.
2. For each subdir, opens a `Memory` instance, fetches
   `db.get_all_docs()`, and for every document:
   - If the document is missing one or more of the fields in
     `_NEURO_CORE_FIELDS = ("memory_type", "importance",
     "confidence", "stability", "validation_status")`, calls
     `apply_defaults(metadata)` to seed the missing keys without
     overwriting existing values.
   - If the document has a `consolidated_from` list, converts each
     entry into a `derived_from` edge in `GraphStore` with
     `source="migration"`.
3. Calls `mem.update_documents(modified)` to persist the changes.

The migration is idempotent: running it twice has no effect the
second time. It is non-destructive: it never overwrites an existing
value with the default.

### Discovery and activation

The plugin is discovered by the framework's standard plugin loader
(no custom registration code is needed). The `plugin.yaml` manifest
declares:

- `per_project_config: true` and `per_agent_config: true` — every
  key in `default_config.yaml` can be overridden per project and
  per agent.
- `always_enabled: false` — the plugin must be explicitly enabled
  by the user; it is not auto-activated.
- `settings_sections:` — the list of config sections shown in the
  WebUI plugin settings page.

---

## 4. Extension injection points

Neuro Core ships three `job_loop` extensions and one `_functions`
hook, all under
`usr/plugins/neuro_core/extensions/python/`.

### Job loop extensions

Agent Zero's `helpers/job_loop.py` calls
`call_extensions_async("job_loop")` every 60 seconds. Neuro Core
hooks into this loop with three extensions, each throttled
independently by the `should_run(...)` helper in
`helpers/lifecycle.py`:

#### `_10_access_decay.py` — Importance decay

- **Purpose**: Apply `importance *= (1 - importance_decay_rate)` to
  every memory that is not in `validation_status == "validated"`.
- **Throttle**: `decay_interval_hours` (default `24`).
- **Config gate**: `decay_enabled` (default `true`).
- **Implementation**: Calls `run_importance_decay(memory,
  score_store, config)` from `helpers/lifecycle.py`. The function
  reads `scores.json`, applies the multiplier, and writes the new
  values back. Memories with high `stability` (close to `1.0`) are
  decayed by a smaller amount.

#### `_20_episode_grouping.py` — Episode assignment

- **Purpose**: Cluster memories into episodes based on time gaps
  between adjacent timestamps. Each cluster is assigned a unique
  `episode_id` (UUID4 truncated to a readable prefix).
- **Throttle**: `episode_interval_hours` (default `24`). The job
  uses a separate `episode_last_run` timestamp from the decay job
  so they do not block each other.
- **Config gate**: `episode_enabled` (default `true`).
- **Implementation**: Calls `run_episode_grouping(memory, config)`
  from `helpers/lifecycle.py`. The function reads all documents,
  sorts by `timestamp`, scans for time gaps wider than
  `episode_boundary_hours` (default `4`), and assigns a new
  `episode_id` to each group with at least `episode_min_memories`
  (default `3`) members. Smaller groups are left without an
  `episode_id`.

#### `_30_contradiction_detection.py` — Contradiction sweep

- **Purpose**: Find pairs of fact memories that semantically
  contradict each other and mark the older one as
  `validation_status = "disputed"`.
- **Throttle**: `contradiction_interval_hours` (default `168` /
  one week).
- **Config gate**: `contradiction_detection_enabled` (default
  `true`); `contradiction_llm_enabled` (default `false` in v0.1.0).
- **Implementation**: Calls `run_contradiction_detection(memory,
  config)` from `helpers/lifecycle.py`. The function:
  1. Selects up to `contradiction_batch_size` (default `100`)
     `fact`-type memories with
     `validation_status != "deprecated"`.
  2. For each fact, runs `Memory.search_similarity_threshold(...)`
     with `contradiction_similarity_threshold` (default `0.85`) to
     find candidates.
  3. For each candidate pair, applies the lexical heuristic
     (`_NEGATION_TOKENS` and `_OPPOSITE_PAIRS` in
     `helpers/lifecycle.py`) to decide opposition.
  4. **Critical caveat (Workstream B obs #4)**: The function
     updates the in-memory metadata dict and **does not** persist
     the new `validation_status` to FAISS. The caller
     (`_30_contradiction_detection.py`) is responsible for writing
     the change back. This will be verified in Workstream C
     integration test #10.

### `_functions` extension — Cascade delete

`helpers/memory.py:Memory.delete_documents_by_ids(...)` is decorated
by Agent Zero's `@extensible`, which auto-generates the hook points
`_functions/plugins._memory.helpers.memory/Memory/delete_documents_by_ids/start`
and `.../end`. Neuro Core installs a single extension at the
`end` hook point:

- **Path**: `extensions/python/_functions/plugins._memory.helpers.memory/Memory/delete_documents_by_ids/end/_01_graph_cleanup.py`
- **Purpose**: After `_memory` finishes deleting the requested
  documents, iterate over the deleted IDs and call
  `GraphStore.remove_edges_for_id(memory_id)` for each one. This
  prevents stale edges from accumulating in `relationships.json`
  when memories are deleted.

The extension only fires on success — if the upstream
`delete_documents_by_ids()` raises, no cleanup runs (which is
correct, since no documents were actually deleted).

---

## 5. ContextGraph pipeline

The hybrid-retrieval pipeline is the heart of Neuro Core's value-add.
It is invoked by `search_context_graph(memory, query, graph_store,
score_store, config) -> ContextGraph` and runs in four stages.

### Stage 1 — Semantic seed retrieval

```
seeds = memory.search_similarity_threshold(query, threshold=0.5, k=20)
```

This calls the existing `_memory` plugin's FAISS-based semantic
search. The threshold and `k` are hard-coded inside
`search_context_graph`; the rerank step (Stage 3) is what filters
the result list down to the final returned node count.

### Stage 2 — BFS graph expansion

```
for seed_id in seeds:
    expanded = graph_store.neighbors(seed_id, hops=config["graph_max_hops"])
    for neighbor_id in expanded:
        nodes.append(GraphNode(doc_id=neighbor_id, hop=h, ...))
        edges.extend(graph_store.get_edges(seed_id))
```

`graph_max_hops` defaults to `2`, so each seed pulls in its direct
neighbors (hop 1) and their neighbors (hop 2). The BFS is capped at
`graph_neighbors_max` (default `40`) neighbors per seed to bound
latency.

This stage is **skipped entirely** when `graph_enabled` is `false`
in the config — in that mode the returned `ContextGraph` has the
seed nodes and an empty `edges` list.

### Stage 3 — Importance-weighted rerank

```
for node in nodes:
    scores = score_store.get(node.doc_id)
    importance = scores.importance if scores else 0.5
    recency = compute_recency(node.metadata["timestamp"])
    node.score = (
        config["similarity_weight"] * node.cosine_similarity
      + config["importance_weight"] * importance
      + config["recency_weight"]   * recency
    )
```

The three weights are configurable (default: 0.5, 0.3, 0.2) and are
expected to sum to `1.0`, but the helper does not enforce this.

### Stage 4 — Assembly and serialization

```
context_graph = ContextGraph(
    query=query,
    nodes=sorted(nodes, key=lambda n: n.score, reverse=True)[:top_k],
    edges=deduplicated_edges,
)
prompt_text = context_graph.to_prompt_text()
```

`ContextGraph.to_prompt_text()` produces a single LLM-ready string:

```
## Retrieved context

[mem_abc123 | concept | hop 0 | score 0.92]
Use bcrypt or argon2 for password hashing...

[mem_def456 | fact | hop 1 | score 0.81]
Never store passwords in plaintext.
```

This is the string the agent sees in its prompt during a hybrid
retrieval. The edges are not included in `to_prompt_text()` — they
are exposed via the `edges` attribute for callers that want to
inspect the graph structure (e.g., the API and the WebUI panel).

---

## 6. Tool invocation flow

All three Neuro Core tools follow the same pattern:

```
agent calls tool.execute(**kwargs)
        │
        ▼
Tool class (helpers/tool.py subclass)
        │ resolves memory_subdir from agent.config
        │ validates arguments
        ▼
Helper class (ScoreStore / GraphStore / reflection helper)
        │ acquires per-subdir RLock
        │ reads / mutates JSON sidecar (atomic write)
        ▼
FAISS metadata update (only for memory_score / memory_reflect)
        │ calls mem.update_documents([doc])
        ▼
Returns Response(message=json.dumps({...}))
```

### `memory_score` flow

1. Agent calls `memory_score(id="mem_abc", importance=0.85,
   validation_status="validated")`.
2. `MemoryScore.execute()` resolves the `Memory` instance from
   `agent.config.memory_subdir`.
3. The tool looks up the document by ID, splits the kwargs into
   `FAISS_FIELDS = ("validation_status", "task_status")` and
   `SCORECAR_FIELDS = ("importance", "confidence", "stability")`.
4. `SCORECAR_FIELDS` go to `ScoreStore.set(id, MemoryScores(...))`
   — atomic write of `scores.json`.
5. `FAISS_FIELDS` go to `mem.update_documents([doc])` — FAISS
   metadata update.
6. Tool returns `{"success": true, "id": "mem_abc", "updated":
   ["importance", "validation_status"]}`.

### `memory_relate` flow

1. Agent calls `memory_relate(from_id="A", to_id="B", rel_type="supports", weight=0.8)`.
2. `MemoryRelate.execute()` validates `rel_type` against
   `VALID_RELATIONSHIP_TYPES`.
3. `GraphStore.add_edge(A, B, "supports", weight=0.8, source="agent")`
   writes the edge to `relationships.json` (atomic).
4. If `rel_type == "related_to"`, a second `add_edge(B, A,
   "related_to", weight=0.8, source="agent")` creates the symmetric
   back-edge (D24).
5. Tool returns the JSON success message.

On `remove=True`, the same path is followed but `add_edge` is
replaced by `GraphStore.remove_edges_for_id(A)` (which also
removes the back-edge if it exists).

### `memory_reflect` flow

1. Agent calls `memory_reflect(episode_id="ep_001", limit=20)`.
2. `MemoryReflect.execute()` checks `config.reflection_enabled`;
   returns an error string if `false`.
3. `helpers/reflection.collect_episode_memories(memory,
   episode_id, limit)` reads the FAISS index, filters documents
   whose metadata has the matching `episode_id`, and returns the
   list sorted by timestamp.
4. `helpers/reflection.reflect_memories(memories)` calls the LLM
   with `prompts/neuro.reflection.sys.md` as the system prompt and
   the joined episode content as the user prompt. Returns the
   reflection text.
5. `helpers/reflection.write_reflection(memory, episode_id, text)`
   inserts a new `memory_type="concept"` document into FAISS
   carrying the reflection text and the source `episode_id`.
6. Tool returns `{"success": true, "episode_id": "ep_001",
   "new_memory_id": "<id>", "reflected_count": 12}`.

---

## 7. WebUI panel

The right-canvas WebUI panel is registered via the existing
`MemoryDashboardApi` infrastructure. The panel is an Alpine.js
component that loads a reactive store, calls the Neuro Core HTTP
API, and renders the graph + node cards.

### Registration

`api/context_graph.py` exposes a single `ContextGraphApi` class that
the framework picks up automatically by filename convention. It is
registered under the path prefix `/api/plugins/neuro_core/` and
dispatches by HTTP method and path suffix inside the `process()`
method:

- `GET /api/plugins/neuro_core/context_graph?query=...&memory_subdir=...`
- `GET /api/plugins/neuro_core/relationships/<memory_id>?memory_subdir=...`
- `POST /api/plugins/neuro_core/relationships`
- `GET /api/plugins/neuro_core/relationships?memory_subdir=...`

The panel's HTML template is loaded as an extension surface under
`extensions/webui/panel.html` and injects a button in the existing
Memory Dashboard toolbar that opens the right-canvas.

### Alpine.js store wiring

`webui/neuro_core_store.js` exports a singleton Alpine store that
holds the panel's reactive state:

```js
Alpine.store('neuroCore', {
  query: '',
  memorySubdir: 'main',
  contextGraph: null,
  loading: false,
  error: null,
  async search() { ... },
  async addRelationship(fromId, toId, relType, weight) { ... },
  async deleteRelationship(fromId, toId, relType) { ... },
});
```

The store's `search()` method calls the `/context_graph` endpoint
and assigns the result to `contextGraph`, which the panel template
iterates over to render node cards and edge pills.

### End-to-end search flow

1. User types a query in the panel search input and clicks "Search".
2. The panel calls `Alpine.store('neuroCore').search()`.
3. The store makes a `GET` request to
   `/api/plugins/neuro_core/context_graph?query=...&memory_subdir=...`.
4. The server-side handler instantiates `Memory` for the
   subdir, calls `search_context_graph(...)`, and returns the
   serialized `ContextGraph` as JSON.
5. The store assigns the result to `contextGraph`. The panel
   re-renders:
   - The header shows the query echo and a node/edge count.
   - Each `GraphNode` becomes a node card with color-coded score
     badges (green ≥ 0.7, yellow 0.4–0.7, grey < 0.4).
   - Each `GraphEdge` becomes an edge pill below the cards.
6. The user can click an edge pill to delete it — the store calls
   the `POST /relationships` endpoint (currently only adds; the
   delete UI is wired but the endpoint is a no-op in v0.1.0).

The WebUI panel is the only Neuro Core surface that gives the user
a visual graph; all other interaction is via the API and the agent
tools.
