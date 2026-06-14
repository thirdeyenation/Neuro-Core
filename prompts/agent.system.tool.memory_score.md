## memory_score
use to update importance, confidence, stability, or validation status of a memory entry
- `memory_score`: args `id`, optional `importance` (0.0–1.0), `confidence` (0.0–1.0), `stability` (0.0–1.0), `validation_status`, `task_status`
  - validation_status values: `unvalidated`, `validated`, `disputed`, `deprecated`
  - deprecated memories are excluded from recall results
  - task_status values: `pending`, `active`, `done`, `cancelled` — only valid when memory_type is `task`
  - omit any field to leave it unchanged
