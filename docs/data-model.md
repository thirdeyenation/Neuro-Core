# Neuro Core Data Model

Neuro Core stores all persistent data in two places:

1. **FAISS document metadata** — the `metadata` dict attached to each
   `Document` already managed by the `_memory` plugin's `Memory.db`.
2. **JSON sidecar files** alongside the FAISS index in the
   `abs_db_dir(memory_subdir)` directory.

This document describes every field that Neuro Core reads or writes on
both substrates. There is **no SQL database, no separate process, and
no migration path** for existing data other than the one-shot
`execute.py` migration.

---

## Section 1 — FAISS Memory Metadata Fields

Each field below is a key in the `Document.metadata` dict. Fields are
read and written through `helpers.metadata` (validation, defaults)
and individual tools (`memory_score`, `memory_relate`) and lifecycle
helpers (`run_importance_decay`, `run_contradiction_detection`,
`run_episode_grouping`).

### `memory_type`

- **Type**: `str`
- **Default**: `"note"` (from `apply_defaults()` when missing)
- **Set by**: `execute.py` migration (legacy), `helpers.metadata.apply_defaults()`
- **Description**: The category of this memory. One of `MemoryType`
  enum values: `fact`, `concept`, `task`, `event`, `decision`,
  `preference`, `skill`, `episode`, `note`. Stored as the enum's string
  value, not the `Enum` instance.

### `importance`

- **Type**: `float`
- **Default**: `0.5` (from `apply_defaults()`)
- **Set by**: `apply_defaults()`, `memory_score` tool, `run_importance_decay()`
  (decreased each decay pass), `run_graph_analytics()` (boosted for
  high-degree nodes — reserved for v0.2.0)
- **Description**: User/agent-assigned importance in `[0.0, 1.0]`.
  Read by the rerank step of `search_context_graph()` via the
  `importance_weight` config key.

### `confidence`

- **Type**: `float`
- **Default**: `0.5` (from `apply_defaults()`)
- **Set by**: `apply_defaults()`, `memory_score` tool, `consolidation`
  (high-confidence merges promote confidence upward)
- **Description**: Confidence in the truth of this memory in `[0.0, 1.0]`.
  Used to weight edges in `GraphStore` and to bias ranking.

### `stability`

- **Type**: `float`
- **Default**: `0.5` (from `apply_defaults()`)
- **Set by**: `apply_defaults()`, `memory_score` tool
- **Description**: Memory consolidation stability in `[0.0, 1.0]`. High
  values indicate a memory is unlikely to change; the decay job
  applies a smaller multiplier to high-stability entries.

### `validation_status`

- **Type**: `str`
- **Default**: `"unvalidated"` (from `apply_defaults()`)
- **Set by**: `apply_defaults()`, `memory_score` tool,
  `run_contradiction_detection()` (marks the older of a contradicting
  pair as `"disputed"`)
- **Description**: One of `ValidationStatus` enum values:
  `unvalidated`, `validated`, `disputed`, `deprecated`. `deprecated`
  memories are excluded from recall results by
  `Memory.search_similarity_threshold()`.

### `read_only`

- **Type**: `bool`
- **Default**: `false`
- **Set by**: Agent or user — not set by Neuro Core itself
- **Description**: When `true`, prevents agent deletion of the
  document. The existing `_memory` plugin's `delete_documents_by_ids()`
  refuses to delete documents with this flag set.

### `episode_id`

- **Type**: `str | None`
- **Default**: `None` (from `apply_defaults()`)
- **Set by**: `run_episode_grouping()` job loop extension,
  `write_reflection()` (copied from source episode)
- **Description**: The episode this memory belongs to. Memories
  sharing the same `episode_id` are grouped by the time-window logic
  in `run_episode_grouping()` and can be summarized by the
  `memory_reflect` tool.

### `tags`

- **Type**: `list[str]`
- **Default**: `[]`
- **Set by**: Agent or user — not modified by Neuro Core
- **Description**: Free-form tag list, preserved from the existing
  `_memory` plugin. Neuro Core does not currently write to this field
  but reads it during retrieval filtering when present.

### `area`

- **Type**: `str`
- **Default**: `"main"`
- **Set by**: `_memory` plugin (Agent Zero core)
- **Description**: The `Memory.Area` enum value as its string name
  (e.g., `"main"`, `"fragments"`, `"solutions"`). Set by the upstream
  Memory plugin; Neuro Core only reads it for display and
  filtering. The `_enum_safe_value` helper in the API layer ensures
  enum values are serialized correctly.

### `consolidated_from`

- **Type**: `list[str]`
- **Default**: `[]`
- **Set by**: `_memory` plugin consolidation pipeline
- **Description**: The list of source memory IDs that this document
  was consolidated from. Read (not written) by the `execute.py`
  migration: when the `relationships.json` sidecar is first
  introduced, any `consolidated_from` lists are converted into
  `derived_from` edges in the sidecar.

### Fields read but not written by Neuro Core

The retrieval helper (`search_context_graph()`) also reads the
following standard FAISS metadata fields, all of which are managed
by the `_memory` plugin and are not modified by Neuro Core:

- `timestamp` — the memory creation time (ISO-8601 string)
- `last_accessed_at` — last read time (ISO-8601 string)
- `access_count` — integer read counter
- `source` — string indicating the memory origin (e.g., `"user"`, `"agent"`, `"knowledge_tool"`)

These power the `recency_weight` term in the rerank step.

### Validation contract: `_NEURO_CORE_FIELDS`

The module `helpers/metadata.py` defines the tuple of fields that
Neuro Core owns and validates on write:

```python
_NEURO_CORE_FIELDS = (
    "memory_type",
    "importance",
    "confidence",
    "stability",
    "validation_status",
    "read_only",
    "episode_id",
)
```

`validate_neuro_metadata(metadata: dict) -> dict` accepts a metadata
dict, applies `apply_defaults()` (which fills in missing keys with
their default values), validates the `memory_type` against the
`MemoryType` enum and `validation_status` against the
`ValidationStatus` enum, coerces all floats into `[0.0, 1.0]`
(clamped), and returns the same dict back. Validation is strict: an
invalid `memory_type` or `validation_status` raises `ValueError`.

---

## Section 2 — `scores.json` Schema

`scores.json` is the sidecar file written by `helpers/scores.py`
(`ScoreStore` class). It lives in
`abs_db_dir(memory_subdir) / "scores.json"` alongside the FAISS index
and the other sidecars.

### Top-level shape

A flat dict from memory ID (str) to per-memory score record:

```json
{
  "mem_abc123": { "importance": 0.85, "confidence": 0.7, "stability": 0.6, "access_count": 4, "last_accessed_at": "2026-06-15T08:00:00+00:00" },
  "mem_def456": { "importance": 0.4,  "confidence": 0.9, "stability": 0.8, "access_count": 1, "last_accessed_at": "2026-06-14T12:30:00+00:00" }
}
```

### Per-entry fields

| Field | Type | Default | Written by | Description |
|---|---|---|---|---|
| `importance` | float | `0.5` | `memory_score` tool, `run_importance_decay()` | Per-memory importance in `[0.0, 1.0]`. |
| `confidence` | float | `0.5` | `memory_score` tool | Per-memory confidence in `[0.0, 1.0]`. |
| `stability` | float | `0.5` | `memory_score` tool | Per-memory stability in `[0.0, 1.0]`. |
| `access_count` | int | `0` | `update_access()` | Read counter, incremented by `search_context_graph()` and by the `_memory` plugin's auto-recall. |
| `last_accessed_at` | str (ISO-8601) | `"1970-01-01T00:00:00+00:00"` | `update_access()` | UTC timestamp of last read. Updated alongside `access_count`. |

The dataclass `MemoryScores` in `helpers/scores.py` declares exactly
these five fields and is the canonical in-memory representation. A
`MemoryScores` instance is serialized to a flat dict when written to
`scores.json`.

### Read/write API

`ScoreStore` exposes:

- `get(memory_id) -> MemoryScores | None`
- `set(memory_id, scores: MemoryScores) -> None` — atomic write
- `update_access(memory_id) -> None` — increments `access_count` and
  updates `last_accessed_at` to the current UTC time
- `load() -> dict[str, MemoryScores]`
- `save() -> None` — atomic write of the full dict

### Atomic write guarantees

All writes go through `tempfile.mkstemp` + `os.replace`, exactly as
in the framework's `helpers/kvp.py` pattern. A per-subdir
`threading.RLock` (held in `ScoreStore._lock`) serializes reads and
writes so a concurrent `memory_score` call cannot tear the file.

### Missing file behavior

If `scores.json` does not exist when the first `ScoreStore` operation
runs, it is created with an empty dict (`{}`). The file is created
lazily on first write, not on plugin load.

---

## Section 3 — `relationships.json` Schema

`relationships.json` is the sidecar file written by
`helpers/graph_store.py` (`GraphStore` class). It lives in
`abs_db_dir(memory_subdir) / "relationships.json"` alongside the FAISS
index.

### Top-level shape

A flat dict from source memory ID (str) to a list of outgoing edges:

```json
{
  "mem_abc123": [
    {
      "from_id": "mem_abc123",
      "to_id": "mem_def456",
      "type": "supports",
      "weight": 0.8,
      "confidence": 0.8,
      "source": "agent",
      "created_at": "2026-06-15T08:00:00+00:00"
    }
  ],
  "mem_xyz789": [
    {
      "from_id": "mem_xyz789",
      "to_id": "mem_def456",
      "type": "contradicts",
      "weight": 1.0,
      "confidence": 0.9,
      "source": "lifecycle",
      "created_at": "2026-06-14T12:00:00+00:00"
    }
  ]
}
```

### Per-edge fields

| Field | Type | Default | Description |
|---|---|---|---|
| `from_id` | str | required | Source memory ID (matches the key in the top-level dict). |
| `to_id` | str | required | Target memory ID. |
| `type` | str | required | One of `VALID_RELATIONSHIP_TYPES` from `helpers.graph_store`. |
| `weight` | float | `1.0` | Edge weight in `[0.0, 1.0]`. Clamped on write. |
| `confidence` | float | `1.0` | Confidence in the relationship in `[0.0, 1.0]`. |
| `source` | str | `"agent"` | Origin tag — `"agent"` for `memory_relate` calls, `"api"` for the HTTP API, `"lifecycle"` for the contradiction detector, `"migration"` for the `execute.py` one-shot import. |
| `created_at` | str (ISO-8601) | current UTC | Timestamp of edge creation. |

### `VALID_RELATIONSHIP_TYPES`

```python
VALID_RELATIONSHIP_TYPES = {
    "supports",
    "contradicts",
    "depends_on",
    "derived_from",
    "related_to",
    "precedes",
    "follows",
}
```

(Older builds may also include `"part_of"`; the API rejects unknown
types with a `400`-equivalent error message.)

### Read/write API

`GraphStore` exposes:

- `add_edge(from_id, to_id, rel_type, weight=1.0, ...) -> GraphEdge`
- `remove_edges_for_id(memory_id) -> int` — returns count removed
- `neighbors(memory_id, hops: int = 1) -> set[str]` — set of memory IDs
  reachable within `hops` (1 = direct neighbors, 2 = neighbors of
  neighbors, ...)
- `get_edges(from_id: str | None = None) -> Union[list[GraphEdge], dict[str, list[GraphEdge]]]` —
  with a `from_id`, returns that source's outgoing edges (backward
  compatible); with no argument, returns the full adjacency map
  (used by `run_graph_analytics()`)
- `load() -> dict[str, list[GraphEdge]]`
- `save() -> None`

### Symmetric back-edges (D24)

When the `memory_relate` tool or the HTTP API creates a `related_to`
edge, a second edge is created in the reverse direction with the
same type and weight. This is a v0.1.0 design fix — without it, the
graph traversal from the target node would miss the source. The
duplicate is stored explicitly in the file (not computed on read),
so the file size is roughly 2× the unique-edge count for any
`related_to` graph.

### Components that read/write `relationships.json`

| Component | Operation | Notes |
|---|---|---|
| `tools/memory_relate.py` (`MemoryRelate` tool) | add, remove | Source tag is `"agent"`. Creates symmetric back-edge for `related_to`. |
| `api/context_graph.py` (`ContextGraphApi`) | add | Source tag is `"api"`. Same symmetric-back-edge behavior. |
| `execute.py` migration | add | Source tag is `"migration"`. Imports `consolidated_from` lists from FAISS metadata into `derived_from` edges. |
| `extensions/python/_functions/.../Memory/delete_documents_by_ids/end/_01_*.py` | remove | Hook fires after `_memory.Memory.delete_documents_by_ids()` succeeds; removes all edges touching the deleted IDs. |
| `extensions/python/_30_contradiction_detection.py` | add | Source tag is `"lifecycle"`. When a contradiction pair is found, an edge of type `contradicts` is added between the two memories, and the older one is marked `validation_status = "disputed"` in FAISS metadata. |
| `helpers/retrieval.py` | read | Calls `GraphStore.neighbors(seed_id, hops=...)` to expand the seed set. |
| `helpers/lifecycle.run_graph_analytics()` | read | Calls `GraphStore.get_edges()` (no argument) to dump the full adjacency map for analytics. |

### Atomic write guarantees

Identical to `scores.json`: `tempfile.mkstemp` + `os.replace`, with a
per-subdir `threading.RLock` in `GraphStore._lock`.

### Missing file behavior

`GraphStore` creates the file lazily with `{}` on first write, same
as `ScoreStore`. Reads of a missing file return an empty adjacency
map; `neighbors()` and `get_edges()` return empty results without
raising.
