## memory_reflect
use to trigger a reflection pass over a memory episode, producing a consolidated episode summary
- `memory_reflect:` args `episode_id`, optional `limit` (max memories to include, default 20)
  * `episode_id` is the shared `episode_id` metadata field on a group of related memories
  * the reflection produces a new memory of type "concept" summarizing the episode
  * use after completing a complex multi-step task to consolidate what was learned
