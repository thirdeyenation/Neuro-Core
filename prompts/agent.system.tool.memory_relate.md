## memory_relate
use to create or remove a typed relationship between two memory entries
- `memory_relate`: args `from_id`, `to_id`, `rel_type`, optional `weight` (0.0–1.0), `remove` (bool)
  - rel_type values: `supports`, `contradicts`, `depends_on`, `derived_from`, `related_to`, `precedes`, `follows`
  - omit `weight` to use default (1.0)
  - set `remove: true` to delete an existing relationship
