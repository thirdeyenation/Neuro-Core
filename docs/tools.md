# Neuro Core Agent Tools

Neuro Core adds three agent tools to the Agent Zero tool registry. All
three live in `usr/plugins/neuro_core/tools/` and each has a matching
prompt fragment in `usr/plugins/neuro_core/prompts/agent.system.tool.<name>.md`.

Like every Agent Zero tool, each one is a Python class that inherits
from `helpers.tool.Tool` and implements `async def execute(**kwargs) -> Response`.
The tool is registered automatically by the framework based on its
filename. The three tools are:

| Class | Filename | Prompt fragment |
|---|---|---|
| `MemoryScore` | `tools/memory_score.py` | `prompts/agent.system.tool.memory_score.md` |
| `MemoryRelate` | `tools/memory_relate.py` | `prompts/agent.system.tool.memory_relate.md` |
| `MemoryReflect` | `tools/memory_reflect.py` | `prompts/agent.system.tool.memory_reflect.md` |

Each tool's `execute()` method returns a `Response` object whose
`message` field is the string that becomes the tool result appended to
the agent's history. Errors are returned as `"ERROR: <message>"`
strings — not raised — so the agent can see them and recover.

---

## `memory_score`

Update `importance`, `confidence`, `stability`, and `validation_status`
of a single memory document, plus the optional `task_status` for
`task`-typed memories.

### Purpose

Adjust the per-memory scoring sidecar and FAISS metadata in a single
atomic call, allowing the agent to mark memories as validated,
deprecate them, or update their importance mid-conversation.

### Arguments

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `id` | str | yes | — | The memory ID to update. The tool calls `Memory.get_by_subdir(agent.config.memory_subdir, ...)` and then `mem.db.get_all_docs()[id]` to find the document. |
| `importance` | float | no | unchanged | New importance in `[0.0, 1.0]`. Clamped if out of range. Written to `scores.json` via `ScoreStore.set()`. |
| `confidence` | float | no | unchanged | New confidence in `[0.0, 1.0]`. Same path as `importance`. |
| `stability` | float | no | unchanged | New stability in `[0.0, 1.0]`. Same path as `importance`. |
| `validation_status` | str | no | unchanged | One of `"unvalidated"`, `"validated"`, `"disputed"`, `"deprecated"`. Written to the FAISS metadata. `deprecated` memories are excluded from recall results. |
| `task_status` | str | no | unchanged | One of `"pending"`, `"active"`, `"done"`, `"cancelled"`. **Only valid when the document's `memory_type` is `"task"`.** |

Omit any field to leave its value unchanged.

### Return value

The tool returns a JSON-stringified success message:

```json
{"success": true, "id": "<memory_id>", "updated": ["importance", "validation_status"]}
```

where `updated` lists the names of the fields that were actually
changed in this call.

### Error conditions

| Condition | Returned error |
|---|---|
| `id` not found in the active `memory_subdir` | `"ERROR: memory <id> not found"` |
| `validation_status` not in the allowed set | `"ERROR: invalid validation_status: <value>"` |
| `task_status` not in the allowed set | `"ERROR: invalid task_status: <value>"` |
| `task_status` set on a non-`task` document | `"ERROR: task_status only valid for memory_type=task"` |
| `Memory.get_by_subdir` raises (e.g., `_memory` plugin disabled) | `"ERROR: <exception message>"` |

### Example invocation

```python
await tool.execute(
    id="mem_abc123",
    importance=0.85,
    validation_status="validated",
)
```

---

## `memory_relate`

Create or remove a typed relationship (edge) between two memory
documents.

### Purpose

Maintain the `relationships.json` sidecar that backs graph-aware
retrieval. Allows the agent to express "A supports B", "A contradicts
B", "A depends on B", etc., and to delete edges that turn out to be
wrong.

### Arguments

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `from_id` | str | yes | — | Source memory ID. |
| `to_id` | str | yes | — | Target memory ID. Must differ from `from_id` — self-edges are rejected. |
| `rel_type` | str | yes | — | One of `VALID_RELATIONSHIP_TYPES` from `helpers.graph_store`: `supports`, `contradicts`, `depends_on`, `derived_from`, `related_to`, `precedes`, `follows`. |
| `weight` | float | no | `1.0` | Edge weight in `[0.0, 1.0]`. Out-of-range values are clamped. |
| `remove` | bool | no | `false` | When `true`, delete the matching edge instead of creating one. |

### Symmetric back-edges

When `rel_type == "related_to"`, the tool creates a symmetric back-edge
(`to_id` → `from_id` with the same type and weight). This is the
**D24 fix** in the decisions log — graph traversal from either node
will find the other.

### Return value

On create (success):

```json
{"success": true, "from_id": "<id>", "to_id": "<id>", "rel_type": "<type>", "weight": <float>}
```

On remove (success):

```json
{"success": true, "from_id": "<id>", "to_id": "<id>", "rel_type": "<type>", "removed": true}
```

### Error conditions

| Condition | Returned error |
|---|---|
| `from_id == to_id` | `"ERROR: self-referential edges are not allowed"` |
| `rel_type` not in `VALID_RELATIONSHIP_TYPES` | `"ERROR: unknown rel_type '<value>'. Valid: [...]”` |
| `GraphStore` raises on disk I/O | `"ERROR: <exception message>"` |

### Example invocation

```python
# Create
await tool.execute(
    from_id="mem_abc123",
    to_id="mem_def456",
    rel_type="supports",
    weight=0.8,
)

# Remove the same edge
await tool.execute(
    from_id="mem_abc123",
    to_id="mem_def456",
    rel_type="supports",
    remove=True,
)
```

---

## `memory_reflect`

Trigger an LLM-driven reflection pass over a memory episode. Produces
a new `concept`-type memory summarizing the episode.

### Purpose

When the agent has accumulated enough `episode_id`-tagged memories
about a multi-step task, `memory_reflect` synthesizes them into a
single higher-level concept memory that the agent (or a future
retrieval) can use as a condensed summary.

### Arguments

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `episode_id` | str | yes | — | The shared `episode_id` metadata field identifying the group of related memories. Set by the episode-grouping job loop extension or by prior calls. |
| `limit` | int | no | `20` | Maximum number of episode memories to include in the reflection prompt. Hard-capped at `reflection_max_memories` from the plugin config (default `50`). |

### Pipeline

The tool executes three steps in order:

1. **Collect** — `helpers.reflection.collect_episode_memories(memory, episode_id, limit)`
   reads the FAISS index, filters documents whose metadata has the
   matching `episode_id`, and returns a list sorted by timestamp.
2. **Reflect** — `helpers.reflection.reflect_memories(memories)` calls
   the LLM with `prompts/neuro.reflection.sys.md` (the system prompt)
   and the joined episode content (the user prompt). Returns a
   reflection text string.
3. **Write** — `helpers.reflection.write_reflection(memory, episode_id, text)`
   inserts a new `memory_type="concept"` document into FAISS carrying
   the reflection text and a copy of the original `episode_id`.

The whole pipeline is gated on `reflection_enabled: true` in the
plugin config. If the key is `false` (the v0.1.0 default), the tool
returns an error string instead of running.

### Return value

On success:

```json
{"success": true, "episode_id": "<id>", "new_memory_id": "<id>", "reflected_count": <int>}
```

where `reflected_count` is the number of episode memories that were
included in the reflection prompt (capped by `limit`).

### Error conditions

| Condition | Returned error |
|---|---|
| `reflection_enabled` is `false` in config | `"ERROR: reflection is disabled in plugin config"` |
| `episode_id` matches zero documents | `"ERROR: no memories found for episode <id>"` |
| LLM call fails (network, timeout, refusal) | `"ERROR: reflection LLM call failed: <message>"` |
| `Memory.get_by_subdir` raises | `"ERROR: <exception message>"` |

### Example invocation

```python
await tool.execute(
    episode_id="ep_2026-06-15_001",
    limit=20,
)
```

The tool then writes a new document like:

```
memory_type: concept
episode_id:  ep_2026-06-15_001
content:     "Reflection summary synthesized from the 12 episode memories..."
importance:  0.7    (seeded by reflection helper)
```

---

## Notes common to all three tools

- **Auth**: Tools do not perform their own authentication — they run
  inside the agent loop, so the agent must already be authenticated
  with the framework.
- **`memory_subdir`**: All three tools resolve the `Memory` instance
  from `agent.config.memory_subdir` (the same subdir the rest of the
  agent uses). There is no per-call subdir override — the agent is
  always operating in its own subdir.
- **Sidecar writes**: All three tools use `ScoreStore` /
  `GraphStore` (the same classes that back the HTTP API). Writes are
  atomic (`tempfile.mkstemp` + `os.replace`) and serialized by a
  per-subdir `RLock`, so concurrent calls are safe.
- **No exceptions propagated to the agent**: All tool errors are
  returned as `"ERROR: <message>"` strings in the `Response.message`
  field. Agents are expected to read the string and decide what to do
  next; an unhandled Python exception in a tool would crash the agent
  loop.
