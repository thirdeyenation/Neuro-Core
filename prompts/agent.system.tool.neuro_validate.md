## neuro_validate
use to apply a lifecycle validation transition to a captured memory — record that a memory was reviewed and judged validated, disputed, or superseded
- `neuro_validate`: args `memory_id` (required), `state` (required)
  - `memory_id` is the id returned by `neuro_capture`
  - state values: `unreviewed`, `validated`, `disputed`, `superseded`
  - marking a memory `validated` records that it was reviewed and judged accurate; `disputed` marks it contested; `superseded` retires it in favor of a newer memory
  - validation status is an annotation: it adjusts recall ordering slightly and never blocks a memory from being retrieved
