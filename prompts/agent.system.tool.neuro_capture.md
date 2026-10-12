## neuro_capture
use to capture a new memory into Neuro Core's scoped long-term memory with typed metadata
- `neuro_capture`: args `text` (required), optional `source` (default "agent_zero"), `project` (default "default"), `agent`, `importance` (0.0–1.0, default 0.5), `confidence` (0.0–1.0, default 0.5)
  - `text` is the memory content to store; `project` sets the scope the memory is captured under
  - `agent` optionally scopes the memory to a specific agent within the project
  - returns the new `memory_id` on success
  - use for facts, decisions, and context worth persisting; retrieval is via `neuro_retrieve`
