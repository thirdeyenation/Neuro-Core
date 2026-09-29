## When to use Neuro Retrieve

Use the `neuro_retrieve` tool when you need context-rich, relationship-aware,
or explainable retrieval from long-term memory — for example when you want to
know "which memories relate to this, and why did they rank where they did",
when plain recall misses connections, or when you need the relationships
between memories, not just their text.

Invocation: `neuro_retrieve(query, limit=10, threshold=0.6)`.

## How to read the result

The tool returns a structured context graph:

- `nodes`: retrieved memories. `hop` 0 means a direct semantic hit; `hop` > 0
  means the node was reached by following recorded relationships from a hit.
- `edges`: the recorded relationships (`from_id`, `to_id`, `type`,
  `confidence`) that connect the returned nodes.
- `seed_ids`: the ids of the direct semantic hits that started the expansion.

Each node carries a factor record exposing the actual values used in its
ranking score:

- `similarity` — normalized semantic similarity to the query.
- `importance` — the memory's curated importance (sidecar-backed; a
  `neuro_degraded` marker means the value fell back to document metadata
  because sidecar data was absent or unreadable — treat it as best-effort).
- `confidence` — the memory's curated confidence (same fallback semantics).
- `recency` — time-decay factor derived from the memory's timestamps.
- `validation_status` and `validation_factor` — the memory's validation
  annotation and the multiplicative factor it contributed. IMPORTANT: in the
  current phase, validation status is ANNOTATION ONLY — it slightly adjusts
  ordering and never blocks or prevents any memory from being retrieved.
- `score` — the final ranking value computed from the factors above.

Use these factors to judge for yourself how much weight to give each memory.

## Vocabulary discipline (binding)

When describing this tool or its results, do NOT state or imply that:
- validation status governs, gates, or blocks retrievability (it does not);
- recall is guaranteed "smarter" or "better" — the tool returns factors and
  relationships as data; the judgment remains yours;
- any performance, concurrency, or security property holds.

Describe what the tool returns — nothing more.