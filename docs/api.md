# Neuro Core HTTP API

Neuro Core exposes its hybrid-retrieval and graph-relationship
endpoints under a single HTTP handler,
`usr.plugins.neuro_core.api.context_graph.ContextGraphApi`. The handler
is registered with the Agent Zero API server alongside the existing
`_memory` plugin's `MemoryDashboardApi`, so it is reachable under the
path prefix

```
/api/plugins/neuro_core/
```

All routes are declared on a single class and dispatched by HTTP method
and suffix matching on `request.path` inside the `process()` method.
The handler returns JSON in every case.

## Auth

Every route requires an authenticated session. The base
`helpers.api.ApiHandler` method `requires_auth()` is overridden to
return `True`:

```python
@classmethod
def requires_auth(cls) -> bool:
    return True
```

A valid cookie (browser session) **or** a valid API key must be
present on the request. Anonymous requests are rejected by the
framework before the `process()` method is entered.

The handler also accepts the standard Agent Zero request body shape —
everything is read from the parsed `input` dict, with query string
parameters merged in by the framework.

## Routes

### `GET /api/plugins/neuro_core/context_graph`

Run hybrid retrieval and return a serialized `ContextGraph`.

**Query parameters**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | str | yes | — | The natural-language query to search for. Empty/whitespace-only is rejected. |
| `memory_subdir` | str | yes | — | The `Memory` subdir to search in. Empty is rejected. |

**Response schema**

| Field | Type | Description |
|---|---|---|
| `success` | bool | `true` on success, `false` on error. |
| `context_graph.query` | str | The echo of the input query. |
| `context_graph.seed_ids` | list[str] | Memory IDs identified as semantic seeds. |
| `context_graph.nodes` | list[dict] | One entry per `GraphNode` — fields: `doc_id`, `content`, `metadata`, `score`, `hop`. |
| `context_graph.edges` | list[dict] | One entry per `GraphEdge` — fields: `from_id`, `to_id`, `type`, `weight`, `confidence`, `source`, `created_at`. |
| `context_graph.prompt_text` | str | Pre-rendered LLM prompt fragment produced by `ContextGraph.to_prompt_text()`. |

On error the response is `{"success": false, "error": "<message>"}`.

**Example**

```bash
curl -b cookies.txt \
  'https://agent-zero.local/api/plugins/neuro_core/context_graph?query=authentication+best+practices&memory_subdir=main'
```

```json
{
  "success": true,
  "context_graph": {
    "query": "authentication best practices",
    "seed_ids": ["mem_abc123", "mem_def456"],
    "nodes": [
      {
        "doc_id": "mem_abc123",
        "content": "Use bcrypt or argon2 for password hashing...",
        "metadata": {"memory_type": "concept", "importance": 0.8},
        "score": 0.92,
        "hop": 0
      }
    ],
    "edges": [
      {
        "from_id": "mem_abc123",
        "to_id": "mem_def456",
        "type": "related_to",
        "weight": 1.0,
        "confidence": 1.0,
        "source": "agent",
        "created_at": "2026-06-15T12:00:00+00:00"
      }
    ],
    "prompt_text": "## Retrieved context\n\n[mem_abc123 | concept | hop 0]\nUse bcrypt or argon2 for password hashing...\n\n"
  }
}
```

### `GET /api/plugins/neuro_core/relationships/<memory_id>`

List all edges (inbound and outbound) that touch a given memory ID.

**Path parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `memory_id` | str | yes | The memory ID to query. Extracted from the URL suffix after `/relationships/`. |

**Query parameters**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `memory_subdir` | str | yes | — | The `Memory` subdir to read the `GraphStore` from. |

**Response schema**

| Field | Type | Description |
|---|---|---|
| `success` | bool | `true` on success, `false` on error. |
| `memory_id` | str | Echo of the input path parameter. |
| `memory_subdir` | str | Echo of the input query parameter. |
| `edges` | list[dict] | Deduplicated union of outbound and inbound edges. Each entry: `from_id`, `to_id`, `type`, `weight`, `confidence`, `source`, `created_at`. |

**Example**

```bash
curl -b cookies.txt \
  'https://agent-zero.local/api/plugins/neuro_core/relationships/mem_abc123?memory_subdir=main'
```

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
      "weight": 0.8,
      "confidence": 0.8,
      "source": "agent",
      "created_at": "2026-06-15T12:00:00+00:00"
    }
  ]
}
```

### `POST /api/plugins/neuro_core/relationships`

Create a new graph edge between two memories. The request body is
JSON-decoded by the framework and exposed as `input`.

**Body parameters** (all read from the parsed `input` dict)

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `memory_subdir` | str | yes | — | The `Memory` subdir to write to. |
| `from_id` | str | yes | — | Source memory ID. |
| `to_id` | str | yes | — | Target memory ID. Must differ from `from_id` — self-referential edges are rejected. |
| `rel_type` | str | yes | — | One of `VALID_RELATIONSHIP_TYPES` from `helpers.graph_store` — `supports`, `contradicts`, `depends_on`, `derived_from`, `related_to`, `precedes`, `follows` (and `part_of` if present in your build). |
| `weight` | float | no | `1.0` | Edge weight in `[0.0, 1.0]`. Out-of-range values are clamped. Non-numeric values fall back to `1.0`. |

The new edge is written to `relationships.json` via
`GraphStore.add_edge(...)`. The `source` field is set to the string
`"api"` and `created_at` is set to the current UTC ISO-8601 timestamp.

**Response schema**

| Field | Type | Description |
|---|---|---|
| `success` | bool | `true` on success. |
| `status` | str | Always `"ok"` on success. |
| `from_id` | str | Echo of the input. |
| `to_id` | str | Echo of the input. |
| `rel_type` | str | Echo of the (validated) input. |
| `weight` | float | The clamped weight actually persisted. |

**Example**

```bash
curl -b cookies.txt -X POST \
  -H 'Content-Type: application/json' \
  -d '{
        "memory_subdir": "main",
        "from_id": "mem_abc123",
        "to_id": "mem_def456",
        "rel_type": "supports",
        "weight": 0.8
      }' \
  'https://agent-zero.local/api/plugins/neuro_core/relationships'
```

```json
{
  "success": true,
  "status": "ok",
  "from_id": "mem_abc123",
  "to_id": "mem_def456",
  "rel_type": "supports",
  "weight": 0.8
}
```

### `GET /api/plugins/neuro_core/relationships`

Dump every edge in the `GraphStore` for a given subdir. Reads the
sidecar directly via `GraphStore._data.values()`.

**Query parameters**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `memory_subdir` | str | yes | — | The `Memory` subdir to read from. |

**Response schema**

| Field | Type | Description |
|---|---|---|
| `success` | bool | `true` on success. |
| `memory_subdir` | str | Echo of the input. |
| `edges` | list[dict] | Flat list of every edge. Each entry: `from_id`, `to_id`, `type`, `weight`, `confidence`, `source`, `created_at`. |
| `count` | int | Length of `edges`. |

**Example**

```bash
curl -b cookies.txt \
  'https://agent-zero.local/api/plugins/neuro_core/relationships?memory_subdir=main'
```

```json
{
  "success": true,
  "memory_subdir": "main",
  "edges": [
    {
      "from_id": "mem_abc123",
      "to_id": "mem_def456",
      "type": "supports",
      "weight": 0.8,
      "confidence": 0.8,
      "source": "agent",
      "created_at": "2026-06-15T12:00:00+00:00"
    }
  ],
  "count": 1
}
```

## Error responses

All errors are returned as `{"success": false, "error": "<message>"}`.
Common messages:

- `"`query` is required"` — `query` was empty or missing.
- `"`memory_subdir` is required"` — `memory_subdir` was empty or missing.
- `"self-referential edges are not allowed"` — `from_id == to_id` on POST.
- `"unknown rel_type '<value>'. Valid: [...]”` — POST `rel_type` is not in `VALID_RELATIONSHIP_TYPES`.
- `"memory_id is required in the URL"` — GET on `/relationships/` with no ID suffix.
- `"Unknown route: <METHOD> <path>"` — request hit a path/method combination the handler does not implement.

## Serialization notes

The handler applies two enum-safe serialization helpers to every dict
response before returning it:

- `_enum_safe_value` recursively walks dicts, lists, and tuples and
  converts any `enum.Enum` instance to its `.value` string.
- `_enum_safe_asdict` is a `dataclasses.asdict` equivalent that also
  walks nested metadata dicts and lists.

This is necessary because FAISS metadata can contain `Memory.Area`
enums and Neuro Core `MemoryType` / `ValidationStatus` / `RelationshipType`
enums that the upstream code can mutate freely. Without these
helpers, `json.dumps` would fail with
`TypeError: Object of type <EnumName> is not JSON serializable`.
