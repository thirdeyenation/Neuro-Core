# Neuro Core HTTP API

Neuro Core ships **six** API handler files under
`usr/plugins/neuro_core/api/`, all registered with the Agent Zero API
server and reachable under the path prefix:

```
/api/plugins/neuro_core/
```

| Handler file | Class | Methods | Routes |
|---|---|---|---|
| `api/context_graph.py` | `ContextGraphApi` | GET | `GET /context_graph` |
| `api/relationships.py` | `RelationshipsApi` | GET, POST, DELETE | `GET /relationships?id=<memory_id>`, `GET /relationships`, `POST /relationships`, `DELETE /relationships` |
| `api/advanced_filters.py` | `AdvancedFiltersApi` | GET | `GET /advanced_filters` |
| `api/episode_audit.py` | `EpisodeAuditApi` | GET | `GET /episode_audit`, `GET /episode_audit?id=<episode_id>` |
| `api/reflection_audit.py` | `ReflectionAuditApi` | GET | `GET /reflection_audit`, `GET /reflection_audit?id=<memory_id>` |
| `api/memory_subdirs.py` | `MemorySubdirsApi` | GET | `GET /memory_subdirs` |

Framework routing maps one handler file to exactly one routable URL
prefix (the framework splits paths on `"/", 2`), so a single `.py`
file maps to exactly one URL prefix and deeper path segments are **not
supported** — per-memory relationship lookups therefore use the `?id=`
query parameter, not a `/relationships/<id>` path segment.

**Live-verified reachability:** in the Phase D host battery
(WI-P4-HOST-BATTERY, Journey 4), all six handlers responded HTTP 200
with structured payloads through the authenticated live-WebUI CSRF
flow. Per-surface accuracy note: for the relationships list-all route,
the Phase D battery captured only the required-arg error shape — the
success shape was never captured there, before or after the WI-P8
fix. The list-all success path is instead live-verified at the
in-process level against a real `GraphStore` with the real
`ApiHandler` base class (WI-P8 validation evidence:
`live_list_all_output.json` in
`.a0proj/notepad_temp/val/20260910T1810-P8GRAPH-VAL/`); it has NOT
been exercised through the live WebUI CSRF serving flow. The POST
`/relationships` write path remains grounded in code only (not
executed against live data). Phase D raw evidence:
`.a0proj/team/work-items/WI-P4-HOST-BATTERY/validation-report.yaml`
rev 2 and its `notepad_temp/val/20260909T1615-P4BATTERY-VAL/j4-*`
artifacts.

## Auth

Every handler overrides the base `helpers.api.ApiHandler` classmethod:

```python
@classmethod
def requires_auth(cls) -> bool:
    return True
```

A valid cookie (browser session) **or** a valid API key must be
present on the request; anonymous requests are rejected by the
framework before `process()` runs.

Handlers read parameters from the parsed `input` dict **or**, where
noted, from Flask `request.args` (GET query string). Handlers route on
HTTP method plus suffix matching on `request.path` inside `process()`.
All responses are JSON dicts; every dict leaves `process()`
enum-safe-serialized.

---

## `GET /api/plugins/neuro_core/context_graph`

Run hybrid retrieval and return a serialized `ContextGraph`.

**Query parameters** (read from `input` or `request.args`)

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | str | yes | — | Natural-language query. Empty/whitespace-only is rejected. |
| `memory_subdir` | str | yes | — | The `Memory` subdir to search. Empty is rejected. |

Retrieval configuration comes from the plugin settings chain with
fallbacks `graph_max_hops=2`, `graph_neighbors_max=10`,
`semantic_limit=5`, `semantic_threshold=0.6`, `similarity_weight=0.5`,
`importance_weight=0.3`, `recency_weight=0.2` — values resolve through
`get_plugin_config("neuro_core")` per key, so user overrides apply.

**Response schema**

| Field | Type | Description |
|---|---|---|
| `success` | bool | `true` on success, `false` on error. |
| `context_graph.query` | str | Echo of the input query. |
| `context_graph.seed_ids` | list[str] | Memory IDs identified as semantic seeds. |
| `context_graph.nodes` | list[dict] | One entry per `GraphNode`: `doc_id`, `content`, `metadata`, `score`, `hop`. `hop` is the graph distance from the nearest seed (0 = seed). |
| `context_graph.edges` | list[dict] | One entry per edge: `from_id`, `to_id`, `type`, `weight`, `confidence`, `source`, `created_at`. |
| `context_graph.prompt_text` | str | Pre-rendered LLM prompt fragment from `ContextGraph.to_prompt_text()`. |

On error: `{"success": false, "error": "<message>"}` — e.g.
`` "`query` is required" `` or `` "`memory_subdir` is required" ``.

---

## Relationship routes (`api/relationships.py`)

### `GET /api/plugins/neuro_core/relationships?id=<memory_id>&memory_subdir=<subdir>`

List all edges (outbound and inbound) touching a memory ID.

**Parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `id` | str | yes | The memory ID (query parameter — not a path segment). |
| `memory_subdir` | str | yes | The `GraphStore` subdir to read from. Accepted from `input` or `request.args`. |

**Response**

```json
{
  "success": true,
  "memory_id": "mem_abc123",
  "memory_subdir": "main",
  "edges": [
    {
      "from_id": "mem_abc123",
      "to_id": "mem_def456",
      "type": "supports",
      "weight": 0.9,
      "confidence": 0.9,
      "source": "agent",
      "created_at": "2026-09-09T12:00:00+00:00"
    }
  ]
}
```

Edges are the deduplicated union of outbound and inbound edges
(dedup key: `(from_id, to_id, type)`).

### `GET /api/plugins/neuro_core/relationships`

Dump every edge in the `GraphStore` for a subdir. The list-all path
reads the full adjacency map via the public no-arg
`GraphStore.get_edges()` contract (`helpers/graph_store.py`); no
private store internals are accessed (pinned by the test
`test_relationships_api_has_no_private_store_access` in
`tests/test_wip8_graph_blockers.py`).

`memory_subdir` is accepted from the parsed `input` dict (POST/JSON
body) or, for GET requests, from the query string — the same fallback
order (`input` -> `request.args`) used by the `?id=` route and all
sibling handlers. Programmatic clients that populate the parsed
`input` dict keep precedence over the query string.

| Name | Type | Required | Description |
|---|---|---|---|
| `memory_subdir` | str | yes | Accepted from parsed `input` or `request.args` (consistent with the `?id=` route and sibling handlers). |

**Response**: `success`, `memory_subdir` (echo), `edges` (flat list
with the same per-edge fields as above), `count`.

### `POST /api/plugins/neuro_core/relationships`

Create a graph edge. Body is JSON-decoded by the framework into
`input`.

**Body parameters**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `memory_subdir` | str | yes | — | Subdir to write to. |
| `from_id` | str | yes | — | Source memory ID. |
| `to_id` | str | yes | — | Target memory ID; must differ from `from_id`. |
| `rel_type` | str | yes | — | One of the 8 valid types: `supports`, `contradicts`, `depends_on`, `derived_from`, `related_to`, `precedes`, `follows`, `part_of`. |
| `weight` | float | no | `1.0` | Clamped to `[0.0, 1.0]`; non-numeric falls back to `1.0`. |

The edge is written via `GraphStore.add_edge(...)` with `source="api"`
and the current UTC ISO-8601 `created_at`.

**Response**: `{"success": true, "status": "ok", "from_id": ..., "to_id": ..., "rel_type": ..., "weight": <clamped>}`.

**Error responses**: missing `memory_subdir` / `from_id` / `to_id`,
`"self-referential edges are not allowed"` (from_id == to_id), and
`"unknown rel_type '<value>'. Valid: [...]"`.

### `DELETE /api/plugins/neuro_core/relationships`

Remove one graph edge. Implemented in WI-P9-EDGE-DELETE.

**Parameters** (query strings on DELETE — never path segments, as the
framework routes on `path.split("/", 2)`; also accepted from a parsed
`input` dict, query string taking the same fallback order as GET):

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `memory_subdir` | str | yes | — | Subdir to operate in. |
| `from_id` | str | yes | — | Source memory ID of the edge. |
| `to_id` | str | yes | — | Target memory ID; must differ from `from_id`. |
| `rel_type` | str | yes | — | One of the 8 valid types (same set as POST). |

The edge is identified by the exact `(from_id, to_id, rel_type)` triple
and removed via the public `GraphStore.remove_edge(from_id, to_id,
rel_type) -> int` contract — a single targeted removal inside one locked
atomic write. Deletion never bulk-rebuilds the store and does not route
through the framework cascade hook. For `rel_type=related_to`, a
symmetric reverse-direction pass (`to_id → from_id`) runs automatically
as non-fatal best-effort, mirroring the `memory_relate` tool precedent
(D24): its outcome is surfaced in the response, never swallowed.

**Success response**: `{"success": true, "removed": 1, "from_id": ...,
"to_id": ..., "rel_type": ..., "reverse_removed": <0|1|null>,
"reverse_error": <only if the reverse pass failed>}`.

**Error responses** (structured, nothing silently swallowed):
missing `memory_subdir` / `from_id` / `to_id`,
`"self-referential edges are not allowed"`,
`"unknown rel_type '<value>'. Valid: [...]"`, and — for a 0-removal —
a structured not-found error (`"edge not found: no edge matching ..."`)
with `removed: 0` and no store write (idempotent re-DELETE is safe).
Store failures surface as `"store removal failed: <exception>"`.

---

## `GET /api/plugins/neuro_core/advanced_filters`

Run a filtered graph query and return matching nodes and edges.
Backend for the graph panel's advanced filter UI.

**Query parameters** (read from `input` or `request.args`)

| Name | Type | Default | Description |
|---|---|---|---|
| `memory_subdir` | str | required | Subdir to query. |
| `memory_type` | str (CSV) or list | none | Filter by `MemoryType` values; invalid entries dropped. |
| `validation_status` | str (CSV) or list | none | Filter by `ValidationStatus` values. |
| `relationship_type` | str (CSV) or list | none | Filter edges by type (values validated only against the edge set present). |
| `date_range` | JSON `{"start": ISO, "end": ISO}` | none | Timestamp window (numeric epoch or ISO-8601 accepted; unparseable per-doc dates are skipped, not rejected). |
| `importance_min` / `confidence_min` / `stability_min` | float | none | Sidecar score thresholds. |
| `episode_id` | str | none | Exact `episode_id` metadata match. |
| `query` | str | none | Accepted and echoed in `filters_applied`; **no semantic filtering is currently applied to the query value**. |
| `limit` | int | `100` | Max nodes returned. |

**Response**

```json
{
  "success": true,
  "memory_subdir": "main",
  "filters_applied": {"memory_type": [...], "episode_id": "...", "...": null},
  "node_count": 3,
  "edge_count": 2,
  "nodes": [{"id": "...", "content": "...", "metadata": {...}}],
  "edges": [{"from_id": "...", "to_id": "...", "type": "related_to", "weight": 1.0, "created_at": "..."}]
}
```

Notes on current behavior (verified against source):

- Enum filters (`memory_type`, `validation_status`) silently drop
  unknown values; a list with no valid values becomes "no filter".
- Edges returned are only those whose **both** endpoints are in the
  filtered node set.
- Edge serialization includes `from_id`, `to_id`, `type`, `weight`,
  `created_at` (no `confidence`/`source`).

---

## `GET /api/plugins/neuro_core/episode_audit`

Episode listing and detail for the WebUI episode audit view.

**Without `id`** — list all episodes in a subdir:

- `memory_subdir` (required, from `input` or `request.args`).
- Returns `success`, `memory_subdir`, `episode_count`, and `episodes`:
  each entry has `episode_id`, `memory_count`, `memory_types` (sorted
  list), `first_timestamp` / `last_timestamp` (ISO, earliest/latest
  member), `has_reflection`, `reflection_id`, and `avg_importance`
  (sidecar-scored mean). Sorted newest-first by `first_timestamp`.

**With `?id=<episode_id>`** — episode detail:

- Returns `episode_id`, `memory_count`, `has_reflection`, `reflection`
  (the reflection doc, if any), and `memories`: each entry has `id`,
  `content`, `metadata`, and `scores` (importance, confidence,
  stability, access_count, last_accessed_at) from the `scores.json`
  sidecar, sorted chronologically ascending.

Only documents carrying `metadata.episode_id` are considered;
reflections are detected as `memory_type == "episode"` with
`is_reflection == true`.

---

## `GET /api/plugins/neuro_core/reflection_audit`

Reflection-memory audit views.

**Without `id`** — list all reflections in a subdir (`memory_subdir`
required): returns `reflection_count` and `reflections`, each with
`id`, `content_preview` (first 200 chars), `metadata`, `scores`
(from the sidecar, or `None` if no sidecar entry), `source_episode_id`,
`source_memory_count`, `created_at`, `validation_status` (default
`"unvalidated"`). Sorted newest-first.

**With `?id=<memory_id>`** — reflection detail: full content, metadata,
scores, `source_episode_id`, and `source_memories` (all non-reflection
docs in the source episode, each with `id`, `content_preview` (first
100 chars), `memory_type`, `timestamp`). If the ID is not a reflection
memory: `{"success": false, "error": "Reflection with id '<id>' not
found"}`.

A reflection is an `episode`-type document with `is_reflection == true`;
source episode resolution prefers `source_episode_id`, falling back to
`episode_id`.

---

## `GET /api/plugins/neuro_core/memory_subdirs`

Subdir discovery. No parameters.

Discovers two path patterns and returns their union:

- **Standard subdirs** — directories under `/a0/usr/memory/`
  (`type: "standard"`).
- **Project subdirs** — `/a0/usr/projects/<project>/memory/`
  directories (`type: "project"`, `name` = project name).

**Response**

```json
{
  "success": true,
  "subdirs": [
    {"name": "main", "path": "/a0/usr/memory/main/", "type": "standard"},
    {"name": "nc1", "path": "/a0/usr/projects/nc1/memory/", "type": "project"}
  ],
  "count": 2
}
```

Permission errors while listing either root are logged and skipped,
not fatal.

---

## Error responses

All error responses are `{"success": false, "error": "<message>"}`
(HTTP-level auth/CSRF failures are rejected by the framework before
the handler runs). Common handler-level messages:

- `` "`memory_subdir` is required" `` — every route.
- `` "`query` is required" `` — context_graph.
- `` "`id` query param is required" `` — relationships GET-with-id.
- `"self-referential edges are not allowed"` — relationships POST.
- `"unknown rel_type '<value>'. Valid: [...]"` — relationships POST.
- `"Reflection with id '<id>' not found"` — reflection_audit detail.
- `"Unknown route: <METHOD> <path>"` — unmatched path/method.

## Serialization notes

Every handler applies enum-safe serialization before returning a dict:
`_enum_safe_value` recursively converts any `enum.Enum` to its
`.value` string (walking dicts, lists/tuples, and nested dataclasses),
and the context_graph handler additionally applies a dataclass-tree
serializer that walks nested `metadata` dicts. This is required
because FAISS metadata may contain `Memory.Area`, `MemoryType`,
`ValidationStatus`, or `RelationshipType` enum instances, which would
otherwise crash `json.dumps` with
`TypeError: Object of type <EnumName> is not JSON serializable`.
Handlers that only need the recursion-free variant (`episode_audit`,
`reflection_audit`, `advanced_filters`, `memory_subdirs`) apply only
`_enum_safe_value`; the serialization helpers are duplicated per file
by design (private helpers are not a cross-file public API surface).
