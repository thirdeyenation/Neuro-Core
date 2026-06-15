# Neuro Core Configuration

Neuro Core reads every setting from a single YAML file shipped with the
plugin. The file is `default_config.yaml` (note: it is **not** named
`defaultconfig.yaml`). At runtime the value of every key can be
overridden per project and per agent — `plugin.yaml` declares
`per_project_config: true` and `per_agent_config: true` for that reason.

All keys are listed in the order they appear in `default_config.yaml`.
Defaults below are quoted verbatim from the file. Keys whose type is
`bool` are toggles; numeric keys are read as `int` or `float` depending
on the consumer (most are read as `float` and clamped to `[0.0, 1.0]`
where appropriate).

## Configuration key table

| Key | Type | Default | Effect |
|---|---|---|---|
| `graph_enabled` | bool | `true` | Master switch for graph-aware retrieval. When `false`, `search_context_graph()` skips the BFS expansion step and returns only semantic seeds. |
| `decay_enabled` | bool | `true` | Master switch for the importance-decay job loop extension (`_10_access_decay.py`). |
| `decay_interval_hours` | int | `24` | Minimum hours between decay runs. Gated through `lifecycle.should_run()`. |
| `importance_decay_rate` | float | `0.02` | Per-run multiplier subtracted from importance: `importance *= (1 - importance_decay_rate)`. |
| `contradiction_detection_enabled` | bool | `true` | Master switch for the contradiction detection job loop extension (`_30_contradiction_detection.py`). |
| `contradiction_llm_enabled` | bool | `false` | When `true`, the contradiction detector may call an LLM for pairwise NLI. **Off by default in v0.1.0** — the job loop is a no-op until enabled in a controlled setting. |
| `contradiction_batch_size` | int | `100` | Maximum number of fact memories considered per pass of the contradiction sweep. |
| `contradiction_interval_hours` | int | `168` | Minimum hours between contradiction sweeps (1 week). |
| `reflection_enabled` | bool | `false` | Master switch for the episode reflection tool (`memory_reflect`). |
| `reflection_max_memories` | int | `50` | Hard cap on the number of episode memories fed into a single reflection prompt. |
| `graph_neighbors_max` | int | `40` | Upper bound on the number of neighbors retrieved per seed during BFS graph expansion. |
| `graph_max_hops` | int | `2` | Maximum BFS depth from each seed node. |
| `importance_weight` | float | `0.3` | Weight applied to the `importance` score during the rerank step of `search_context_graph()`. |
| `recency_weight` | float | `0.2` | Weight applied to the recency term during the rerank step. |
| `similarity_weight` | float | `0.5` | Weight applied to the cosine similarity term during the rerank step. The three rerank weights are expected to sum to `1.0`. |
| `episode_boundary_hours` | int | `4` | Maximum gap between adjacent memories before a new episode starts in the episode-grouping job. |
| `episode_min_memories` | int | `3` | Minimum number of memories required to form an episode. Groups below this size are not assigned an `episode_id`. |

## Key details

### `graph_enabled`

Master switch for the **graph-aware** portion of retrieval. When `false`,
`search_context_graph()` returns a `ContextGraph` whose `nodes` list
contains only the semantic-seed documents and whose `edges` list is
empty. The seed retrieval, importance-weighted rerank, and
`ContextGraph.to_prompt_text()` assembly all still run.

### `decay_enabled`

When `false`, the `_10_access_decay.py` job loop extension short-circuits
before reading any document. When `true`, the extension calls
`helpers.lifecycle.run_importance_decay(...)` every
`decay_interval_hours`.

### `importance_decay_rate`

Per-run multiplier. The actual operation is

```
new_importance = clamp01(current_importance * (1.0 - decay_rate))
```

so `0.02` means each decay run multiplies the current value by `0.98`.
The result is always clamped to `[0.0, 1.0]` before being written back
to the `scores.json` sidecar.

### `contradiction_llm_enabled`

**Heuristic-only by default.** When this key is `false` (the v0.1.0
default), `run_contradiction_detection()` uses the lexical heuristic
defined in `helpers/lifecycle.py` (`_NEGATION_TOKENS`,
`_OPPOSITE_PAIRS`, `_semantically_oppose`). When `true`, the function
may additionally call an LLM for pairwise NLI between high-similarity
candidates.

### `contradiction_batch_size`

Hard cap on the number of fact memories inspected per pass. The
contradiction detector uses `Memory.search_similarity_threshold(...)`
to find candidates for each fact, and the result list is also capped at
`contradiction_batch_size`.

### `graph_max_hops` and `graph_neighbors_max`

Both control the BFS expansion in `search_context_graph()`:

- `graph_max_hops` is the maximum depth (0 = seeds only, 1 = one hop,
  2 = two hops).
- `graph_neighbors_max` is the maximum number of neighbors visited per
  seed at each hop.

Larger values give richer context but increase retrieval latency.

### `importance_weight`, `recency_weight`, `similarity_weight`

These three weights are combined during the rerank step of
`search_context_graph()`. The final score is roughly

```
score = similarity_weight * cosine_similarity
      + importance_weight * importance
      + recency_weight * recency
```

The retrieval helper does not enforce that the three weights sum to
`1.0` — callers are expected to configure them as a normalized triple.

### `episode_min_memories`

The episode-grouping job (`_20_episode_grouping.py`) groups memories by
time-window. Groups whose size is **strictly less than**
`episode_min_memories` are not assigned an `episode_id` and are
therefore not visible to the reflection tool.

## Internal default overrides

`helpers/lifecycle.py` defines a module-level `DEFAULT_CONFIG` dict
that is used when the plugin-level config is missing keys. The
`DEFAULT_CONFIG` dict in code includes the following keys (in addition
to the YAML keys above) that are **not** exposed in
`default_config.yaml` but are read by the lifecycle functions:

| Key | Type | Default | Read by |
|---|---|---|---|
| `contradiction_similarity_threshold` | float | `0.85` | `run_contradiction_detection()` — minimum cosine similarity for a pair to be considered for opposition. |
| `graph_analytics_enabled` | bool | `true` | Reserved for the future `_40_graph_analytics` job loop extension. |
| `graph_analytics_top_pct` | float | `0.10` | Reserved — fraction of highest-degree nodes to boost. |
| `graph_analytics_boost` | float | `0.05` | Reserved — importance increment applied to top-degree nodes. |

These keys are documented here because they appear in
`helpers/lifecycle.py` even though they are not surfaced in
`default_config.yaml`. Production callers should not rely on them being
present in the user-facing config.
