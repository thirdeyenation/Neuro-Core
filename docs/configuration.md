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
| `database_path` | str | `neuro_core.db` | Path to the plugin's SQLite domain-store database. Relative values resolve plugin-relative; absolute values are honored verbatim (existing deployments unaffected). Resolved through the framework's plugin settings chain (`get_plugin_config`). |
| `decay_enabled` | bool | `true` | Master switch for the importance-decay job loop extension (`_10_access_decay.py`). |
| `decay_interval_hours` | int | `24` | Minimum hours between decay runs. Gated through `lifecycle.should_run()`. |
| `importance_decay_rate` | float | `0.02` | Per-run multiplier subtracted from importance: `importance *= (1 - importance_decay_rate)`. |
| `contradiction_detection_enabled` | bool | `true` | Master switch for the contradiction detection job loop extension (`_30_contradiction_detection.py`). |
| `contradiction_llm_enabled` | bool | `false` | When `true`, the contradiction detector may call an LLM for pairwise NLI. **Off by default in v0.1.0** — the job loop is a no-op until enabled in a controlled setting. |
| `contradiction_batch_size` | int | `100` | Maximum number of fact memories considered per pass of the contradiction sweep. |
| `contradiction_interval_hours` | int | `168` | Minimum hours between contradiction sweeps (1 week). |
| `graph_neighbors_max` | int | `10` | Upper bound on the number of neighbors retrieved per seed during BFS graph expansion. |
| `graph_max_hops` | int | `2` | Maximum BFS depth from each seed node. |
| `importance_weight` | float | `0.3` | Weight applied to the `importance` score during the rerank step of `search_context_graph()`. |
| `recency_weight` | float | `0.2` | Weight applied to the recency term during the rerank step. |
| `similarity_weight` | float | `0.5` | Weight applied to the cosine similarity term during the rerank step. The three rerank weights are expected to sum to `1.0`. |
| `semantic_limit` | int | `5` | Maximum number of semantic seed memories retrieved before graph expansion. |
| `semantic_threshold` | float | `0.6` | Minimum similarity for a candidate semantic seed. |
| `contradiction_similarity_threshold` | float | `0.85` | Minimum cosine similarity for a pair of fact memories to be considered for opposition (read by `run_contradiction_detection()`). |
| `graph_analytics_enabled` | bool | `true` | Master switch for the graph-analytics pass in `helpers/lifecycle.py`. |
| `graph_analytics_top_pct` | float | `0.10` | Fraction of highest-degree nodes boosted by the graph-analytics pass. |
| `graph_analytics_boost` | float | `0.05` | Importance increment applied to boosted nodes. |
| `episode_boundary_hours` | int | `4` | Maximum gap between adjacent memories before a new episode starts in the episode-grouping job. |
| `episode_min_memories` | int | `3` | Minimum number of memories required to form an episode. Groups below this size are not assigned an `episode_id`. |

## Key details

### `database_path`

Path to the plugin's SQLite domain-store database. The bundled default
is the relative value `neuro_core.db`, which resolves plugin-relative.
An absolute configured path is honored verbatim, so existing deployments
that configured an absolute location keep working unchanged. The value
is resolved through the framework's plugin settings chain
(`get_plugin_config`), so per-project and per-agent overrides apply like
any other plugin config key.

### Scope note

This table reflects the keys implemented and read at runtime as of
v0.1.0 (WI-P3-MANIFEST-CONFIG). A full documentation reconciliation of
all configuration surfaces is owned by the Phase E docs work item.

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

`helpers/lifecycle.py` defines a module-level `DEFAULT_CONFIG` dict that
is used when the plugin-level config is missing keys. As of v0.1.0, all
of its keys are bundled in `default_config.yaml` and documented in the
main table above, so no internal-only overrides remain.
