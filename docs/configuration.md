# Neuro Core Configuration

Neuro Core reads every setting from a single YAML file shipped with the
plugin. The file is `default_config.yaml` (note: it is **not** named
`defaultconfig.yaml`). At runtime the value of every key can be
overridden per project and per agent — `plugin.yaml` declares
`per_project_config: true` and `per_agent_config: true` for that reason.

The key-details section below groups keys by territory — the same
grouping used by the Settings window's Advanced view. (The reference
table below lists all keys with their defaults; it does not follow
`default_config.yaml` line order or the territory grouping.)
Defaults below are quoted verbatim from the file. Keys whose type is
`bool` are toggles; numeric keys are read as `int` or `float` depending
on the consumer (most are read as `float` and clamped to `[0.0, 1.0]`
where appropriate).

The plugin's Settings window (WebUI) exposes all 21 keys: five
operator-level controls in Basic view and every key grouped by territory
under Advanced Settings. A plugin-native, key-level help surface with the
same territory anchors is served at `webui/help/configuration.html` and
linked from the Settings window. Changes made in the Settings window
apply to future operations after saving.

## Configuration key table

| Key | Type | Default | Effect |
|---|---|---|---|
| `database_path` | str | `neuro_core.db` | Path to the plugin's SQLite domain-store database. Relative values resolve plugin-relative; absolute values are honored verbatim (existing deployments unaffected). Resolved through the framework's plugin settings chain (`get_plugin_config`). |
| `decay_enabled` | bool | `true` | Master switch for the importance-decay job loop extension (`_10_access_decay.py`). |
| `decay_interval_hours` | int | `24` | Minimum hours between decay runs. Gated through `lifecycle.should_run()`. |
| `importance_decay_rate` | float | `0.02` | Per-run multiplier subtracted from importance: `importance *= (1 - importance_decay_rate)`. |
| `contradiction_detection_enabled` | bool | `true` | Master switch for the contradiction detection job loop extension (`_30_contradiction_detection.py`). |
| `contradiction_llm_enabled` | bool | `false` | Intended to enable LLM-assisted pairwise NLI in the contradiction detector. **Off by default in v0.1.0; the LLM-assisted path is not yet implemented or verified in v0.1.0** — see the key-details note below. |
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
| `graph_analytics_enabled` | bool | `true` | Intended master switch for the graph-analytics pass in `helpers/lifecycle.py`. **Not yet wired in v0.1.0** — see the key-details note below. |
| `graph_analytics_top_pct` | float | `0.10` | Fraction of highest-degree nodes boosted by the graph-analytics pass (pass not yet wired in v0.1.0). |
| `graph_analytics_boost` | float | `0.05` | Importance increment applied to boosted nodes (pass not yet wired in v0.1.0). |
| `episode_boundary_hours` | int | `4` | Maximum gap between adjacent memories before a new episode starts in the episode-grouping job. |
| `episode_min_memories` | int | `3` | Minimum number of memories required to form an episode. Groups below this size are not assigned an `episode_id`. |

## Key details

The subsections below give key-level guidance for every key, grouped by
territory — the same grouping the Settings window and the plugin-native
help surface (`webui/help/configuration.html`) use.

### Lifecycle internals

#### `decay_enabled`

Master switch for the importance-decay job loop. When `false`, the
`_10_access_decay.py` job loop extension short-circuits before reading
any document. When `true`, the extension calls
`helpers.lifecycle.run_importance_decay(...)` every
`decay_interval_hours`.

#### `decay_interval_hours`

Minimum hours between decay runs (gated through the lifecycle
scheduler). The Settings Basic selector "Freshness speed" writes this
key together with `importance_decay_rate`; set it directly only for a
custom cadence.

#### `importance_decay_rate`

Per-run multiplier. The actual operation is

```
new_importance = clamp01(current_importance * (1.0 - decay_rate))
```

so `0.02` means each decay run multiplies the current value by `0.98`.
The result is always clamped to `[0.0, 1.0]` before being written back
to the `scores.json` sidecar.

#### `episode_boundary_hours`

Maximum time gap between adjacent memories before a new episode starts
in the episode-grouping job (`_20_episode_grouping.py`).

#### `episode_min_memories`

The episode-grouping job groups memories by time-window. Groups whose
size is **strictly less than** `episode_min_memories` are not assigned
an `episode_id` and are therefore not visible to the reflection tool.

### Contradiction internals

#### `contradiction_detection_enabled`

Master switch for the contradiction-detection job loop. One of the five
Settings Basic controls.

#### `contradiction_batch_size`

Hard cap on the number of fact memories inspected per pass. The
contradiction detector uses `Memory.search_similarity_threshold(...)`
to find candidates for each fact, and the result list is also capped at
`contradiction_batch_size`.

#### `contradiction_similarity_threshold`

Minimum cosine similarity for a pair of fact memories to be considered
for opposition (read by `run_contradiction_detection()`).

#### `contradiction_llm_enabled`

**Heuristic-only in v0.1.0.** When this key is `false` (the v0.1.0
default), `run_contradiction_detection()` uses the lexical heuristic
defined in `helpers/lifecycle.py` (`_NEGATION_TOKENS`,
`_OPPOSITE_PAIRS`, `_semantically_oppose`).

**Not yet implemented in v0.1.0:** when this key is `true`, the
LLM-assisted pairwise-NLI path described here is planned but is not
implemented and not verified in the v0.1.0 code. Its current scheduled
behavior is a defect (KI-032): the `_30` job-loop extension calls
`run_contradiction_detection(..., docs=docs)` per subdirectory, but the
function's signature accepts `facts=` — each per-subdirectory call
raises a `TypeError` (unexpected keyword argument `docs`), which is
caught and reported as a warning, so **no contradiction check runs at
all while the key is enabled**. A remediation proposal exists; until it
lands, treat this key as currently non-functional.

**Cost warning (precautionary, for when the LLM path ships):**
LLM-based NLI makes LLM usage grow with high-similarity candidate-pair
volume. Enable only in a controlled setting.

#### `contradiction_interval_hours`

Minimum hours between contradiction sweeps (one week).

### Graph analytics internals

#### `graph_analytics_enabled`

Intended master switch for the graph-analytics pass in
`helpers/lifecycle.py` (`run_graph_analytics()`).
One of the five Settings Basic controls ("Graph insights").

**Not yet wired in v0.1.0:** `run_graph_analytics()` is implemented as a
function (defined in `helpers/lifecycle.py`) but has no invocation site
in any scheduled job loop — no `_40` job-loop extension exists yet. As
a result, toggling `graph_analytics_enabled` (and the related
`graph_analytics_top_pct` / `graph_analytics_boost` values) has no
observable effect until the wiring lands.

#### `graph_analytics_top_pct`

Fraction of highest-degree nodes boosted by the graph-analytics pass.

#### `graph_analytics_boost`

Importance increment applied to boosted nodes.

### Retrieval internals

#### `similarity_weight`, `importance_weight`, `recency_weight`

These three weights are combined during the rerank step of
`search_context_graph()`. The final score is roughly

```
score = similarity_weight * cosine_similarity
      + importance_weight * importance
      + recency_weight * recency
```

The retrieval helper does not enforce that the three weights sum to
`1.0` — callers are expected to configure them as a normalized triple.
The Settings Basic preset selector writes these as named points
(Balanced 0.5/0.3/0.2, Freshest-first 0.2/0.3/0.5, Importance-first
0.3/0.5/0.2); editing any raw weight flips the preset display to
**Custom**.

#### `graph_max_hops`

Maximum BFS depth from each seed node in `search_context_graph()`
(0 = seeds only, 1 = one hop, 2 = two hops). Larger values give richer
context but increase retrieval latency.

#### `graph_neighbors_max`

Upper bound on the number of neighbors visited per seed at each hop of
the BFS expansion.

#### `semantic_limit`

Maximum number of semantic seed memories retrieved before graph
expansion.

#### `semantic_threshold`

Minimum similarity for a candidate semantic seed.

### Storage & recovery

#### `database_path`

Path to the plugin's SQLite domain-store database. The bundled default
is the relative value `neuro_core.db`, which resolves plugin-relative.
An absolute configured path is honored verbatim, so existing deployments
that configured an absolute location keep working unchanged. The value
is resolved through the framework's plugin settings chain
(`get_plugin_config`), so per-project and per-agent overrides apply like
any other plugin config key.

**Relocation warning:** changing this path relocates where the plugin
reads and writes its memory database. Existing data is **not** moved
automatically — memories at the old location stop being served, and a
fresh database starts empty. To relocate deliberately, stop memory
activity, move the database file to the new location, then update this
key.

### Manual reboot failsafe

After a framework server restart, graph-panel recovery is normally
automatic. A deeper manual reboot control lives on the Neuro Core graph
panel (beside the Refresh button in the panel header): it deliberately
resets the panel's cached CSRF token, rebuilds the graph canvas, and
re-runs the current search. It lives on the graph panel rather than in
the Settings window because the panel's own script scope is the only
place where its cached token and graph instance can be reset together.

## Scope note

This table reflects the keys implemented and read at runtime as of
v0.1.0 (WI-P3-MANIFEST-CONFIG). The key details above mirror the
plugin-native help surface at `webui/help/configuration.html` (WI-P38).
Behavior documented here is implemented behavior; planned and
unverified behavior remain distinct surfaces and are not claimed here.

## Internal default overrides

`helpers/lifecycle.py` defines a module-level `DEFAULT_CONFIG` dict that
is used when the plugin-level config is missing keys. As of v0.1.0, all
of its keys are bundled in `default_config.yaml` and documented in the
main table above, so no internal-only overrides remain.
