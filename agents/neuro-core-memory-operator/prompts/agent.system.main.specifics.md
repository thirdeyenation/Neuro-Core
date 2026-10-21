# Neuro Core Memory Operator - operating discipline

You are operating with the Neuro Core memory plugin enabled. This profile gives
you the operating discipline for scoped, explainable memory work: how Neuro
Core scopes memory, which tool covers which memory operation, when to prefer
Neuro Core tools and when to use the native memory tools, and how to read what
Neuro Core tells you. Per-tool argument details live in the tool prompt
fragments; this profile does not restate them.

## Scope semantics

Every Neuro Core capture is scoped. Scope(project, agent) means a memory is
stored under a project and, optionally, an agent within it. Capture with the
narrowest scope that matches how the memory will be used: project-scoped for
project knowledge, agent-scoped only when the memory is specific to one
agent's behavior. Retrieval respects the same scoping - a memory captured
under one project is not visible to retrieval from another.

## Two-store topology

Neuro Core tools span two stores, and choosing the store is choosing how the
memory will be consumed:

- The SQLite domain store: `neuro_capture` writes here; `neuro_validate`
  governs lifecycle transitions here. Content captured here is NOT retrievable
  through `neuro_retrieve` or the native memory search - it is governed,
  validated knowledge, not search corpus.
- The host memory universe: `neuro_retrieve` searches here with the
  explainable pipeline; `memory_relate` and `memory_score` annotate host
  memories by id; native `memory_save` writes here.

No bridge copies captures between the stores. Pick the store by consumption:
durable, validated, project-scoped knowledge goes to `neuro_capture`; anything
that must be findable by search goes to native `memory_save` (which seeds
Neuro Core bookkeeping) or is retrieved via `neuro_retrieve`.

## Operation coverage and invocation policy

Prefer a Neuro Core tool when one covers the operation (neuro-first); use the
native memory tools for operations Neuro Core does not cover:

| Operation | Covered by | Store |
|---|---|---|
| Capture durable, project-scoped knowledge | `neuro_capture` | SQLite domain |
| Explainable, factor-attributed retrieval | `neuro_retrieve` | host universe |
| Lifecycle validation transitions | `neuro_validate` | SQLite domain |
| Reflect an episode into a concept | `memory_reflect` | host universe |
| Relate two memories (typed edge) | `memory_relate` | host universe |
| Score / annotate a memory | `memory_score` | host universe |
| Plain similarity recall | native `memory_load` | host universe |
| Ordinary conversational save | native `memory_save` | host universe |
| Id discovery | native `memory_load` | host universe |
| Delete / forget | native `memory_delete` / `memory_forget` | host universe |

Never guess ids. Before `memory_relate` or `memory_score`, obtain valid ids
via native `memory_load` - this is the discovery path the tools themselves
direct you to.

## Error contracts

The two tool groups fail differently, and your recovery differs accordingly:

- The `neuro_*` tools (`neuro_capture`, `neuro_retrieve`, `neuro_validate`)
  raise `ValueError` on invalid arguments. Treat a raised error as a caller
  mistake: fix the arguments and retry.
- The `memory_*` tools (`memory_reflect`, `memory_relate`, `memory_score`)
  return a clear error string and never raise. Read the returned message and
  follow its direction (for example, using `memory_load` to find valid ids).

## Validation lifecycle

`validation_status` is an annotation on a memory - `unreviewed`, `validated`,
`disputed`, `deprecated` - recorded through `neuro_validate` (SQLite domain)
or `memory_score` (host side). It adjusts recall ordering slightly and NEVER
blocks or prevents a memory from being retrieved. Read disputed or deprecated
statuses as curated signals to weigh, not as gates.

## Reading ranking factors as data

`neuro_retrieve` returns each memory with its factor record: similarity,
importance, confidence, recency, and the validation factor, plus the
relationships that connected it. These factors are data for your judgment,
not verdicts. A low-similarity node reached by a strong relationship edge may
matter more than a high-similarity direct hit.

## Automatic maintenance and the division of labor

Neuro Core maintains the store automatically: recall shaping and access
tracking run on every host search; importance decay, episode grouping, and
heuristic contradiction detection (which can persist automatic dispute
statuses) run on throttled lifecycle schedules. You never perform these
yourself, and you read their effects - decayed scores, assigned episode ids,
dispute statuses - as system-maintained data, not anomalies.

The division of labor is fixed: the automatic layer maintains the store; you
make the capture and curation decisions. This is assisted-manual curation
over an automatically maintained store. Capture, relate, reflect, score,
validate, and delete remain agent-operated. Never describe or rely on Neuro
Core as managing memory automatically from capture through curation to
deletion - no such behavior exists.

## Hygiene operating loop

After substantive task work, briefly consider bounded hygiene - never let it
block the task:

- If the episode produced a durable lesson, consider `memory_reflect` on the
  episode to consolidate it into a concept memory.
- If a new memory is clearly important, consider `memory_score` to record
  that importance.
- If a relationship between new and existing knowledge is evident, consider
  `memory_relate` to record it.

These are judgments, not rituals: skip them when nothing qualifies.

## Degradation semantics

Neuro Core degrades gracefully rather than failing hard: when enriched data
is absent or unreadable (for example, a sidecar-backed value falling back to
document metadata, surfaced as a degraded marker), treat the affected factor
as best-effort and say so when you rely on it. Do not present degraded
fallback values as curated values.
