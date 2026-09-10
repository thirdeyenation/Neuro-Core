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
   `related_to`, `precedes`, `follows`, `part_of`). The graph lives in
   two new JSON sidecar files (`relationships.json` and `scores.json`)
   placed in the same `abs_db_dir(memory_subdir)` directory as the
   FAISS index.
3. **Hybrid retrieval** via `search_context_graph()`. A single
   function combines FAISS semantic search, BFS graph expansion,
   and importance/recency-weighted reranking, returning a structured
   `ContextGraph` (nodes + edges) that the agent can serialize to a
   prompt fragment.
4. **Six agent tools** — `memory_score`, `memory_relate`,
   `memory_reflect`, plus `neuro_capture`, `neuro_retrieve`, and
   `neuro_validate` — that let the agent capture, retrieve, and
   maintain the graph and the score sidecar.
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
that the framework itself uses in `/a0/helpers/kvp.py`:

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

At plugin init (`hooks.py:install()`), Neuro Core re-applies the framework's public `@extensible` decorator to the three host-called `Memory` methods at full identity (`helpers/decorate.py`), so the `_functions` handler tree fires for host-initiated operations (D-NC1-010; ADR-NC1-001). NC1-originated operations go through the native access layer (`helpers/native_access.py`) instead of any runtime monkey-patching. Decoration is applied at two points: plugin install (`hooks.py:install()`) and every framework startup via the `startup_migration` extension `usr/plugins/neuro_core/extensions/python/startup_migration/_10_neuro_decoration.py`, whose `execute()` calls `helpers/decorate.py:decorate_memory()` (exception-safe and idempotent, so decoration survives container restarts where install-time wiring does not re-run).

### Job loop extensions

Agent Zero's `helpers/job_loop.py` calls
`call_extensions_async("job_loop")` every 60 seconds. Neuro Core
hooks into this loop with three extensions, each throttled
independently by the `should_run(...)` helper in
`helpers/lifecycle.py`:

Job execution is deferred for the first 300 seconds of process
uptime after framework boot; a skipped tick performs no work and
records no throttle state, so the first post-grace tick runs the
real job.

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

The framework's `plugins/_memory` Memory methods carry **no**
`@extensible` decorators — verified against framework source
(zero `@extensible` occurrences in
`/a0/plugins/_memory/helpers/memory.py`; see ADR-NC1-001, which
resolves KI-022/OA-9). Hook points for
`Memory.delete_documents_by_ids(...)` are provided by Neuro Core's
startup re-decoration instead: at plugin install (`hooks.py`) and at
every framework startup (`extensions/python/startup_migration/_10_neuro_decoration.py`),
`helpers/decorate.py` re-applies
`helpers.extension.extensible` to the host-called Memory methods at
full identity, generating the hook points
`_functions/plugins/_memory/helpers/memory/Memory/delete_documents_by_ids/start`
and `.../end`. Neuro Core installs a single extension at the
`end` hook point:

- **Path**: `extensions/python/_functions/plugins/_memory/helpers/memory/Memory/delete_documents_by_ids/end/_01_graph_cleanup.py`
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

Retrieval through `NeuroCoreService.retrieve()` (exposed to agents as
the `neuro_retrieve` tool) filters on the **exact** scope provided —
`Scope(project, agent)`. Callers must pass the scope the memory was
captured under: omitting `agent` silently misses agent-scoped memories,
because the filter matches project AND agent exactly.

### Stage 1 — Semantic seed retrieval

```
seed_docs = await memory.search_similarity_threshold(
    query=query, limit=semantic_limit, threshold=semantic_threshold)
```

This calls the existing `_memory` plugin's FAISS-based semantic
search. `semantic_limit` (default `10`) and `semantic_threshold`
(default `0.5`) are config-resolved inside `search_context_graph`
(`helpers/retrieval.py`); the final node list is re-ranked by the
composite score computed during seed-node construction (Stage 1) and
neighbor registration (Stage 2) — there is no separate top_k cut at
assembly time.

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

This stage is **skipped entirely** when no graph store is available
(`graph_store is None`) or when `graph_max_hops` is `0` — in that
mode the returned `ContextGraph` has the seed nodes and an empty
`edges` list. There is no separate `graph_enabled` config key; the
pipeline is bounded by `graph_max_hops` (default `2`) and
`graph_neighbors_max` (default `40` additional graph neighbors beyond
the seed set, allocated hop-ascending then edge-confidence-descending).

### Stage 3 — Importance/recency-weighted scoring (folded into Stages 1–2)

```
importance = score_store.get(doc_id) or metadata importance (0.5 fallback)
recency    = _recency_score(last_accessed_at or timestamp)
node.score = config["similarity_weight"] * semantic
           + config["importance_weight"] * importance
           + config["recency_weight"]   * recency
```

The three weights are configurable (defaults: 0.5, 0.3, 0.2). There
is no separate rerank pass — the composite score is computed as each
node is registered, and graph-only neighbors use a moderate semantic
baseline of `0.5` so importance and recency differentiate them. When
the score sidecar is unreadable, importance falls back to metadata
and the node's metadata is flagged `neuro_degraded: true` (KI-008).

### Stage 4 — Assembly and serialization

```
context_graph = ContextGraph(
    nodes=sorted(nodes_by_id.values(), key=lambda n: (-n.score, n.doc_id)),
    edges=list(edges_by_key.values()),
    query=query,
    seed_ids=seed_ids,
)
prompt_text = context_graph.to_prompt_text()
```

Nodes are sorted by descending composite score (ties broken by
`doc_id`); the full node set is returned — no top_k truncation.
Edges are deduplicated by `(from_id, to_id, type)`.

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

All six Neuro Core tools follow the same general pattern:

```
agent calls tool.execute(**kwargs)
        │
        ▼
Tool class (helpers/tool.py subclass)
        │ resolves memory_subdir from the agent's memory db/config
        │ validates arguments
        ▼
Helper class (ScoreStore / GraphStore / reflection helper)
        │ acquires per-subdir RLock
        │ reads / mutates JSON sidecar (atomic write)
        ▼
FAISS metadata update (only for fields routed to FAISS)
        │ calls mem.update_documents([doc])
        ▼
Returns Response(message=json.dumps({...}))
```

### `memory_score` flow

1. Agent calls `memory_score(id="mem_abc", importance=0.85,
   validation_status="validated")`.
2. `MemoryScore.execute()` resolves the memory db from the agent
   context and derives the subdir from `db.memory_subdir`.
3. The tool splits the kwargs into
   `_FAISS_FIELDS = ("memory_type", "validation_status", "task_status")`
   and `_SCORECAR_FIELDS = ("importance", "confidence", "stability")`
   (`tools/memory_score.py:44-47`).
4. `_SCORECAR_FIELDS` go to the `ScoreStore` (atomic write of
   `scores.json` — the single authoritative score write path per
   ADR-NC1-002).
5. `_FAISS_FIELDS` go to FAISS metadata via
   `mem.update_documents([doc])`.
6. Tool returns `{"success": true, "id": "mem_abc", "updated":
   ["importance", "validation_status"]}`.

### `memory_relate` flow

1. Agent calls `memory_relate(from_id="A", to_id="B", rel_type="supports", weight=0.8)`.
2. `MemoryRelate.execute()` validates `rel_type` against
   `VALID_RELATIONSHIP_TYPES` (8 values incl. `part_of`).
3. `GraphStore.add_edge(A, B, "supports", weight=0.8, source="agent")`
   writes the edge to `relationships.json` (atomic).
4. If `rel_type == "related_to"`, a second `add_edge(B, A,
   "related_to", ...)` creates the symmetric back-edge — `related_to`
   is the only symmetric type (D24).
5. Tool returns the JSON success message.

On `remove=True` the tool performs a **surgical single-edge removal**
(WI-P2-DEFECT-BATCH): it locates the specific `(from_id, to_id,
rel_type)` edge in either direction, snapshots every edge touching
that edge's anchor bucket, calls the public bulk
`remove_edges_for_id(anchor)`, then re-adds everything except the
one target edge — preserving all unrelated edges.

### `memory_reflect` flow

1. Agent calls `memory_reflect(episode_id="ep_001", limit=20)`.
2. `MemoryReflect.execute()` collects episode documents via
   `helpers/reflection.collect_episode_memories(subdir, episode_id,
   db, limit)`, returning an error Response if the episode is empty
   or uncollectable.
3. `helpers/reflection.reflect_memories(docs, agent)` calls the LLM
   with `prompts/neuro.reflection.sys.md` (via
   `DEFAULT_REFLECTION_PROMPT`, editable in-repo) as the system
   prompt and the joined episode content as the user prompt. Returns
   the reflection text; an LLM failure or empty content returns an
   error Response.
4. `helpers/reflection.write_reflection(subdir, content, episode_id,
   db)` persists the reflection as a new memory carrying the source
   `episode_id` and writes its ScoreStore sidecar entry (D50).
5. Tool returns an acknowledgement message naming the new memory ID
   and source-memory count.

---

## 7. WebUI panel

The Neuro Core graph lives in the framework's right-canvas system.
Four plugin assets make it up:

- `extensions/webui/right-canvas-register-surfaces/neuro_register.js`
  - registers the surface at framework init time via the framework's
  `callJsExtensions("right_canvas_register_surfaces", ...)` hook:
  `registerSurface({ id: 'neuro-core-graph', title: 'Neuro Core', icon: 'hub', order: 50, ... })`.
  Registration is idempotent (upsert by surface id).
- `extensions/webui/right-canvas-panels/graph-panel.html` - a
  `data-surface-id="neuro-core-graph"` wrapper whose visibility is
  gated by `$store.rightCanvas.isSurfaceVisible(...)`; it mounts the
  actual panel via `<x-component path=".../graph-panel.html" mode="canvas">`.
- `extensions/webui/sidebar-quick-actions-main-start/neuro-entry.html`
  - a sidebar quick-action link that opens the surface with
  `$store.rightCanvas.open('neuro-core-graph')`.
- `webui/graph-store.js` - an Alpine store registered as
  `Alpine.store('neuroGraph')`; `webui/graph-panel.css` holds the
  panel styling, keyed to framework CSS custom properties.

The panel body (`webui/right-canvas-panels/graph-panel.html`) is a
self-contained Alpine.js component (`x-data` scope) rather than a
thin client of the store: it owns its search state, Cytoscape
instance, and rendering.

### Panel capabilities

- Query search via `GET
  /api/plugins/neuro_core/context_graph?query=...&memory_subdir=...`.
- Memory-subdir chips (add/remove/persisted in
  `localStorage['nc_memory_subdirs']`).
- Advanced filters via `GET
  /api/plugins/neuro_core/advanced_filters?...` with `memory_type`,
  `validation_status`, `relationship_type`, `date_range_start/end`,
  `importance_min`, `confidence_min`, `stability_min`, and
  `episode_id` parameters.
- Cytoscape rendering: nodes sized by importance, edges labeled by
  `rel_type`, score-bucket styling (high >= 0.7, mid >= 0.4, low).
- Node inspector: tapping a node opens a details card with content,
  score badges, and its relationship list; clicking a related node
  re-runs the search centered on that node.
- Theme observation: a `MutationObserver` on the document element
  re-applies Cytoscape styles when the framework theme changes.

### End-to-end search flow

1. User types a query in the panel search input and clicks
   "Search" (the panel auto-issues an initial `recent` search on
   mount).
2. The panel's `search()` fetches
   `/api/plugins/neuro_core/context_graph?query=...&memory_subdir=...`.
3. The server-side handler resolves the memory db for the subdir,
   calls `search_context_graph(...)`, and returns the serialized
   `ContextGraph` as JSON.
4. The panel assigns `nodes`/`edges` and calls `renderGraph()`,
   mapping each node to a Cytoscape node (id, label, content,
   importance) and each edge to `source/target/rel_type`, then runs
   the selected layout (`cose` default).

The WebUI panel is the only Neuro Core surface that gives the user
a visual graph; all other interaction is via the API and the agent
tools. The panel is read-only: it renders and inspects the graph
but does not expose relationship add/delete controls.
