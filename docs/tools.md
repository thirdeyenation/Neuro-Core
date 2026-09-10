# Neuro Core Agent Tools

Neuro Core ships **six** agent tools under `usr/plugins/neuro_core/tools/`.
They fall into two groups with different error contracts:

| Group | Tool files | Error contract |
|---|---|---|
| `neuro_*` (SQLite domain store) | `neuro_capture.py`, `neuro_retrieve.py`, `neuro_validate.py` | **Raise `ValueError`** on invalid/missing required arguments. Missing/invalid arguments are **not** caught — the exception propagates to the caller. |
| `memory_*` (FAISS + sidecars) | `memory_score.py`, `memory_relate.py`, `memory_reflect.py` | **Defensive** — every error path returns an error string in `Response.message` (`"Error: ..."`, `break_loop=False`); no exceptions are raised to the caller. |

All six are Python classes inheriting from `helpers.tool.Tool` and are
registered automatically by the framework from their filenames.
Prompt fragments exist for the three `memory_*` tools
(`prompts/agent.system.tool.<name>.md`); the `neuro_*` tools have no
prompt fragments.

Common operational notes:

- **No tool performs its own authentication** — tools run inside the
  agent loop.
- The `memory_*` tools resolve the `Memory` instance for the agent's
  own `memory_subdir`; there is no per-call subdir override.
- The `neuro_*` tools resolve their SQLite database path from the
  plugin config chain (`database_path`): relative values resolve
  plugin-relative, absolute values are honored verbatim; the bundled
  default is `neuro_core.db` in the plugin directory.
- The `memory_*` tools write sidecars (`scores.json`,
  `relationships.json`) via `ScoreStore` / `GraphStore` with atomic
  writes (`tempfile.mkstemp` + `os.replace`) serialized by a
  per-subdir lock.

---

## Group 1 — `neuro_*` tools (SQLite domain store)

These three tools operate on the plugin's own SQLite domain store
(see `docs/data-model.md`) via `NeuroCoreService`.

### `neuro_capture`

Capture a memory into the scoped SQLite domain store.

**Arguments**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `text` | str | yes | — | Memory text. Must be non-empty. |
| `source` | str | no | `"agent_zero"` | Capture source label. |
| `project` | str | yes (non-empty default) | `"default"` | Project scope component. |
| `agent` | str | no | `""` | Agent scope component; empty means project-scoped only. |
| `importance` | float | no | `0.5` | Importance score. |
| `confidence` | float | no | `0.5` | Confidence score. |

**Behavior**

- Raises `ValueError("text and project are required")` when `text` or
  `project` is empty/missing.
- Persists via `NeuroCoreService.capture(Memory(...))` and returns a
  plain dict: `{"memory_id": ..., "outcome": "stored", "scope": <project>}`.

### `neuro_retrieve`

Explainable retrieval from the scoped SQLite domain store.

**Arguments**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | str | yes | — | Natural-language query. Must be non-empty. |
| `project` | str | yes (non-empty default) | `"default"` | Project scope component. |
| `agent` | str | no | `""` | Agent scope component. |

**Behavior**

- Raises `ValueError("query and project are required")` when `query`
  or `project` is empty/missing.
- Calls `NeuroCoreService.retrieve(query, Scope(project, agent or None))`
  and returns a list of dicts with `memory_id`, `text`, `source`,
  `score`, and `factors` (the explainable scoring factors).

**Agent-scoped exactness (important)**

Retrieval filters on the **exact** scope provided — the filter matches
project AND agent exactly. Callers must pass the scope the memory was
captured under: omitting `agent` silently misses agent-scoped memories,
because an empty `agent` becomes a project-scoped-only `Scope`.

### `neuro_validate`

Apply a lifecycle validation transition to a captured memory.

**Arguments**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `memory_id` | str | yes | — | The memory to transition. |
| `state` | str | yes | — | One of `unreviewed`, `validated`, `disputed`, `superseded`. |

**Behavior**

- Raises `ValueError("memory_id and state are required")` when either
  is empty.
- Raises `ValueError("state must be unreviewed, validated, disputed, or superseded")`
  for an unknown state.
- Returns `{"memory_id": ..., "validation": <state>, "outcome": "updated"}`.

---

## Group 2 — `memory_*` tools (FAISS + sidecars)

These three tools operate on the host `_memory` plugin's FAISS store
and Neuro Core's JSON sidecars. All error paths return
`Response(message="Error: ...", break_loop=False)` — the tools never
raise.

### `memory_score`

Update the mutable score layer and typed metadata fields for a single
memory document.

**Arguments**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `id` | str | yes | — | The memory ID to update. |
| `memory_type` | str | no | unchanged | One of the `MemoryType` enum values. Invalid values are coerced to `"note"` by metadata validation. Written to FAISS metadata. |
| `validation_status` | str | no | unchanged | One of `unvalidated`, `validated`, `disputed`, `deprecated`. Invalid values are coerced to `"unvalidated"`. Written to FAISS metadata. |
| `task_status` | str | no | unchanged | One of `pending`, `active`, `done`, `cancelled`. **Only valid when the document's `memory_type` is `task`.** Written to FAISS metadata. |
| `importance` | float | no | unchanged | Clamped to `[0.0, 1.0]`. Written **only** to `scores.json` via `ScoreStore.set()`. |
| `confidence` | float | no | unchanged | Same path as `importance`. |
| `stability` | float | no | unchanged | Same path as `importance`. |

**Persistence model**

- `memory_type`, `validation_status`, `task_status` are written to the
  FAISS document metadata via `Memory.update_documents()`.
- `importance`, `confidence`, `stability` are written **only** to the
  `scores.json` sidecar — the single authoritative write path for
  mutable score fields (per ADR-NC1-002; the removed FAISS mirror was
  the KI-009 drift source). Retrieval's metadata fallback is read-only,
  never authoritative.

**Return / errors**

Success returns a confirmation `Response` listing each changed field
with its new value (plus a `neuro_core_ack` entry in
`additional`). Errors (all `"Error: ..."` strings, never raised):

- `id` missing/empty → ``Error: `id` is required...``
- Memory backend init failure → `Error: could not initialize Memory backend: ...`
- ID not found → `Error: no memory found with id '<id>'. Use memory_load to find valid ids.`
- No updatable fields supplied → `Error: no updatable fields supplied for memory '<id>'...`
- `task_status` on a non-`task` document → `Error: task_status is only valid for memories of type 'task'...`
- Persist failures → `Error: failed to persist metadata/score changes...`
- If values were supplied but unchanged: returns
  `"No changes to memory '<id>' (values were unchanged)."`

### `memory_relate`

Create or remove a typed relationship (edge) between two memory
documents.

**Arguments**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `from_id` | str | yes | — | Source memory ID. |
| `to_id` | str | yes | — | Target memory ID. Must differ from `from_id`. |
| `rel_type` | str | yes | — | One of the 8 `VALID_RELATIONSHIP_TYPES`: `supports`, `contradicts`, `depends_on`, `derived_from`, `related_to`, `precedes`, `follows`, `part_of`. |
| `weight` | float | no | `1.0` | Edge weight in `[0.0, 1.0]`; out-of-range clamped, non-numeric falls back to `1.0`. |
| `remove` | bool | no | `false` | When `true`, remove the matching edge instead of creating one. |

**Behavior**

- Validates both IDs exist in the FAISS store before any write.
- Writes the edge to `relationships.json` via `GraphStore.add_edge()`
  with `source="agent"` and the current UTC timestamp. Never touches
  FAISS directly.
- **Symmetric back-edge (D24)**: when `rel_type == "related_to"`, a
  reverse edge (`to_id -> from_id`) is also written (and removed on
  `remove=True`), so the adjacency is traversable in both directions.
- **Surgical removal**: `remove=True` targets only the specific
  `(from_id, to_id, rel_type)` edge — in either direction — preserving
  all unrelated edges (WI-P2-DEFECT-BATCH fix).

**Return / errors**

Success messages confirm the added or removed edge (plus
`neuro_core_ack`). Errors (all `"Error: ..."` strings, never raised):

- Missing required args, self-referential edge, unknown `rel_type`
  (with the valid list), memory-backend failures, ID-not-found
  (listing the missing IDs), and edge read/write failures.
- If no matching edge exists on remove: a non-error
  `"No matching edge found to remove..."` message; the store is
  unchanged.

### `memory_reflect`

Trigger an LLM-driven reflection pass over a memory episode and persist
the synthesized insight as a new `concept`-type memory.

**Arguments**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `episode_id` | str | yes | — | The shared `episode_id` metadata field identifying the episode. |
| `limit` | int | no | `20` | Maximum episode memories included. Clamped to `[1, 100]` — 100 is the hard ceiling; there is **no** `reflection_enabled` or `reflection_max_memories` config key. |

**Pipeline**

1. **Collect** — `helpers.reflection.collect_episode_memories()`
   exhaustively enumerates the subdir's documents (via the FAISS
   docstore ID mapping), filters by `metadata.episode_id`, sorts by
   timestamp ascending, and applies the limit. (The earlier
   semantic-search approach was abandoned — episode IDs are not
   semantically similar to their members.)
2. **Reflect** — `helpers.reflection.reflect_memories()` calls the
   agent's **utility model** (`agent.call_utility_model`) with the
   embedded reflection system prompt (also available for human editing
   at `prompts/neuro.reflection.sys.md`). An empty result is treated as
   failure.
3. **Write** — `helpers.reflection.write_reflection()` inserts the new
   `memory_type="concept"` document with `importance=0.8`,
   `stability=0.9`, `source="neuro_reflect"`, and the source
   `episode_id`, then writes the matching `scores.json` sidecar entry
   (`importance=0.8`, `stability=0.9`, `confidence=0.9`,
   `source="neuro_reflect"`) — the D50 fix so every sidecar-reading
   subsystem sees the new reflection memory. A sidecar write failure
   propagates (never silently discarded) and the tool converts it into
   an error `Response`.

The tool has **no config gate** — it always runs when called.

**Return / errors**

Success: `"Reflection written as memory <id> (episode: <id>, N source
memories)"` plus a `neuro_core_ack`. Non-error short-circuits:

- No matching memories → `"No memories found for episode_id <id> in
  subdir <subdir>"`
- Empty LLM output → `"Reflection failed — LLM did not return content"`

Errors (all `"Error: ..."` strings, never raised): missing
`episode_id`, memory-backend init failure, collection failure,
`"Error: reflection LLM call failed: <message>"`, and reflection
persistence failure (including the KI-014 sidecar-failure
propagation).

---

## Notes common to the `memory_*` tools

- **Auth**: Tools do not perform their own authentication — they run
  inside the agent loop.
- **`memory_subdir`**: All three resolve the `Memory` instance from the
  agent's active subdir; there is no per-call subdir override.
- **Sidecar writes**: atomic and per-subdir-locked, so concurrent tool
  and job-loop calls cannot tear the files.
- **Exception contract differs from `neuro_*`**: the `memory_*` tools
  return error strings; the `neuro_*` tools raise `ValueError` on
  invalid required arguments. Both behaviors are intentional; do not
  assume one contract for all six tools.
