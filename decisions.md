# Neuro Core — Decision Log

Resolved design decisions for the Neuro Core memory enhancement plugin.
Each entry is authoritative for the corresponding phase of work and supersedes
any conflicting text in `NEURO_CORE_SPEC.md` or `extras.md`.

---

## 2026-06-06 — graph_neighbors_max semantics

**Decision:** `graph_neighbors_max` (in `default_config.yaml`) defines the
**maximum number of *additional* graph neighbors** to add to the result
context **beyond the seed set** retrieved by the initial semantic search.

- Seed nodes from `Memory.search_similarity_threshold()` are **always
  retained** in the result, regardless of the cap value.
- The cap limits only the BFS-expanded neighbors (hop 1, hop 2, ...).
- Example: `graph_neighbors_max=2` with one seed returned by semantic search
  yields up to `1 + 2 = 3` total nodes in the `ContextGraph`.
- This matches the test assertion `len(g.nodes) == 3` for that scenario.

**Implementation location:** `helpers/retrieval.py`, function
`search_context_graph()`, around the `remaining_capacity` calculation in the
graph-expansion step.

**Supersedes:** Any "max total nodes" interpretation in
`NEURO_CORE_SPEC.md §6.3 RetrievalConstraints.max_nodes`.

---

## 2026-06-06 — MemoryType enum (Flag 1)

**Decision:** `MemoryType` enum is trimmed to **8 values**, aligned with
`VALID_MEMORY_TYPES` in §4.3 of the spec:

`fact`, `concept`, `task`, `event`, `decision`, `skill`, `preference`, `note`

The 13-value enum in §6.1 of `NEURO_CORE_SPEC.md` is **superseded**.

---

## 2026-06-06 — Episode grouping defaults

**Decision:** `default_config.yaml` includes the two episode-grouping keys
required by Phase 4:

- `episode_boundary_hours: 4`
- `episode_min_memories: 3`

---

## 2026-06-06 — GraphEdge.weight default (Phase 2 Step 2)

**Decision:** `GraphEdge.weight: float = 1.0` was added in Phase 2 Step 2.

The default preserves backwards compatibility with existing edges in
`relationships.json` that were written without a `weight` field; they
deserialize to `weight=1.0` on load and are treated equivalently to
explicitly-weighted edges by `GraphStore.neighbors()` re-ranking.

---

## 2026-06-06 — EpisodeGroupingJob toggle (Phase 3)

**Decision:** `EpisodeGroupingJob` currently uses the `decay_enabled` flag
as its lifecycle toggle (i.e., the decay toggle gates both the decay and
the episode-grouping jobs).

If independent control is needed in the future, introduce a dedicated
`episode_grouping_enabled` config key and have
`EpisodeGroupingJob.should_run()` consult it instead of `decay_enabled`.

---

## 2026-06-06 — memory_reflect outcome paths (Phase 4)

**Decision:** `memory_reflect` tool has **4 outcome paths**:

1. **Empty episode** — `collect_episode_memories` returns `[]`; respond
   with `"No memories found for episode_id {episode_id}"`. `reflect_memories`
   and `write_reflection` are NOT called.
2. **LLM failure** — `reflect_memories` returns `""`; respond with
   `"Reflection failed — LLM did not return content"`. `write_reflection`
   is NOT called.
3. **Persist failure** — `write_reflection` returns `""` after a successful
   LLM call; respond with `"Reflection failed — could not persist reflection
   for episode '{episode_id}'"`. This guards against a successful LLM call
   followed by a failed persist, returning an informative message rather
   than silently succeeding.
4. **Happy path** — `write_reflection` returns a non-empty id; respond
   with `"Reflection written as memory {new_id} (episode: {episode_id}, {N} source memories)"`.

All four paths are covered by `tests/test_memory_reflect_tool.py`.

---

## 2026-06-09 — Stability audit: job_loop extensions (Fix 1–3)

**Trigger:** A crash loop was observed in the live Agent Zero instance when
the job_loop extension `_10_access_decay.py` raised an unhandled
`KeyError: '_10_access_decay'` on every 60s tick. Root cause: the
extension used `sys.modules[__name__]` to persist its throttle timestamp,
but Agent Zero's dynamic extension loader does not guarantee the module
is registered in `sys.modules` under that name. A full stability audit
was completed and nine fixes were applied across the plugin.

**Decision — Fix 1–3 (job_loop extensions):** All three job_loop
extensions (`_10_access_decay.py`, `_20_episode_grouping.py`,
`_30_contradiction_detection.py`) follow the **same template**:

1. **No `sys.modules` anywhere.** The throttle timestamp lives in a
   module-level `_STATE = {"last_run": 0.0}` dict, not in a module
   attribute accessed via `sys.modules[__name__]`.
2. **Two-layer error guard in `execute()`.** The body is wrapped in
   `asyncio.wait_for(self._run(**kwargs), timeout=30.0)` (inner
   timeout) and a broad `except Exception` (outer guard). The method
   NEVER re-raises — under any circumstances — and logs a warning or
   error via `PrintStyle()` instead.
3. **Plugin-local imports live inside the function.** All
   `from usr.plugins.neuro_core...` imports are inside `_run()`, not
   at the module top level. Module-level imports of plugin-local code
   are NOT permitted in job_loop extension files (the loader
   may not have finished bootstrapping the plugin package when the
   module is first imported).

**Decision — Fix 3 specific (contradiction LLM off by default):**
`_30_contradiction_detection.py` adds a `contradiction_llm_enabled`
config key (default `false`). The job is a no-op until the LLM path is
validated in a controlled setting. The check is at the top of `_run()`:

```python
if not config.get("contradiction_llm_enabled", False):
    return  # LLM-based contradiction disabled by default in v1
```

**Tests added:** `TestJobLoopExtensionSafety` class in
`tests/test_lifecycle_jobs.py` (6 tests total):

- 3 exception-safety tests — patch `_run` to raise, assert
  `execute()` returns `None` without re-raising.
- 3 timeout-safety tests — patch `_run` to `await asyncio.sleep(60)`,
  assert `execute()` returns within ~35s (the 30s timeout plus 5s
  scheduling margin).

---

## 2026-06-09 — Stability audit: hook files (Fix 4–6)

**Problem:** The three `_functions` hooks
(`_10_neuro_metadata.py`, `_10_graph_cascade.py`,
`_10_access_tracking.py`) live in a dotted directory tree and are
invoked on every `Memory.insert_documents()`,
`Memory.delete_documents_by_ids()`, and `Memory.search_similarity_threshold()`
call system-wide. If any of them raised, it would break the
corresponding memory operation for every agent, not just Neuro Core
operations.

**Decision — Fix 4–6 (hook files):** All three hook files follow the
**same template**:

1. **Plugin-local import inside `execute()`.** The
   `from usr.plugins.neuro_core.helpers.X import Y` import is on the
   first line of `execute()`, not at the module top level. The dotted
   directory path means the module is loaded via
   `importlib.util.spec_from_file_location` and module-level
   plugin-local imports are unreliable.
2. **Full body wrapped in `try/except Exception`.** A warning is
   logged via `PrintStyle().warning(...)` and the method returns
   without re-raising. The memory operation that triggered the hook
   must NOT be blocked by a Neuro Core failure.
3. **No `from usr.plugins.neuro_core...` at module top level.** Only
   `from helpers.extension import Extension` and
   `from helpers.print_style import PrintStyle` are allowed at the
   module top level.

---

## 2026-06-09 — Stability audit: RLock timeout pattern (Fix 7–8)

**Problem:** All `with self._lock:` usages in `graph_store.py` and
`scores.py` use a bare context manager. If a caller already holds the
lock and an exception prevents release, a subsequent re-acquire in
the same call stack blocks forever. The two helpers manage shared
sidecar files (`relationships.json` and `scores.json`) that are
written by both agent tools and background lifecycle jobs, so
deadlock would freeze the whole memory subsystem.

**Decision — Fix 7–8 (RLock timeout):** All `with self._lock:`
usages in `helpers/graph_store.py` and `helpers/scores.py` are
replaced with a `_locked()` context manager that acquires the
`threading.RLock` with a 5-second timeout deadline:

```python
@contextlib.contextmanager
def _locked(self, timeout: float = _LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    if not self._lock.acquire(timeout=timeout):
        raise TimeoutError(
            f"[neuro_core] <ClassName> lock timed out after {timeout}s"
        )
    try:
        yield
    finally:
        self._lock.release()
```

Every critical section in both classes MUST use `with self._locked():`
instead of `with self._lock:`. A bare `with` is forbidden and a
pattern-scan grep is run as part of the audit checklist to enforce
this.

---

## 2026-06-09 — Stability audit: execute.py path bootstrap (Fix 9)

**Decision — Fix 9 (execute.py):** The `execute.py` migration entry
point computes `_A0_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))`
at the **very top of the file** (before all other imports) and
inserts it into `sys.path[0]`. This is the ONLY acceptable
`sys.path` mutation in the plugin, and it is explicitly exempted from
the general "no sys.path hacks" rule because the framework's
`execute.py` is invoked from a context where `/a0` is not always on
`sys.path`. The `_import_memory()` helper is called first by `main()`;
if it returns `(None, None)`, `main()` prints a clear FATAL error and
exits with code 1. On success, `main()` runs the migration and exits
with code 0. This pattern is documented in
`tests/test_lifecycle_jobs.py::TestExecuteImportFallback` (5 tests).

---

## 2026-06-09 — `contradiction_detection_enabled` config key (typo fix)

**Decision:** The lifecycle job toggle in
`extensions/python/job_loop/_30_contradiction_detection.py` reads the
config key **`contradiction_detection_enabled`**, not the duplicated
form `contradiction_detection_detection_enabled`.

- A previous version of the job had a doubled-`detection` typo that
  silently disabled the job even when operators thought they had
  enabled it via `default_config.yaml`.
- The key is also exposed in `helpers/lifecycle.DEFAULT_CONFIG` and is
  asserted by `TestDefaultConfigContradictionKey::test_default_config_has_correct_key`
  in `tests/test_lifecycle_jobs.py`. The test additionally asserts the
  duplicated form is **NOT** present and that the value is a `bool`.
- Any future config schema audit MUST treat this key as the canonical
  name; the duplicated form is forbidden.

---

## 2026-06-09 — `memory_cls` parameter pass-through in `execute.py`

**Decision:** The migration entry point
`usr/plugins/neuro_core/execute.py` threads the resolved `Memory`
class through `_run_migration()` → `_migrate_subdir()` as a parameter
named **`memory_cls`**, rather than referencing a module-level `Memory`
name inside `_migrate_subdir()`.

- A previous version bound `Memory` inside `_import_memory()` and then
  referenced the bare name `Memory.get_by_subdir(...)` inside
  `_migrate_subdir()`, which raised `NameError: name 'Memory' is not
  defined` at runtime because the function was not in the same scope
  as the import.
- The fix is verified by four regression tests in
  `tests/test_lifecycle_jobs.py::TestExecuteMigrationMemoryParam`:
  1. `test_migrate_subdir_signature_accepts_memory_cls` — inspects
     the signature of `_migrate_subdir` and asserts the parameter
     exists.
  2. `test_run_migration_passes_memory_cls_to_migrate_subdir` —
     inspects the source of `_run_migration` and asserts the call
     pattern `_migrate_subdir(subdir, memory_cls)` is present and the
     broken pattern `_migrate_subdir(subdir)` is not.
  3. `test_migrate_subdir_uses_memory_cls_not_module_name` —
     inspects the source of `_migrate_subdir` and asserts the call
     uses `memory_cls.get_by_subdir(...)` and never the bare
     `Memory.get_by_subdir(...)` (including in comments).
  4. `test_run_migration_completes_without_name_error` — end-to-end
     run with a stub `Memory` class confirms the migration completes
     without `NameError`.
- The stub `Memory` class in the end-to-end test uses
  `@staticmethod` for `get_by_subdir` to match the real `Memory`
  class in `plugins/_memory/helpers/memory.py` (line 96–97), which
  is also a `@staticmethod`. Calling the method on the class
  directly (without instantiation) is therefore the correct call
  pattern.

---

## 2026-06-09 — `abs_db_dir()` API correction (replacing `Memory._get_abs_db_dir`)

**Decision:** The plugin uses the **module-level function**
`abs_db_dir(memory_subdir)` from `plugins._memory.helpers.memory` to
resolve the on-disk sidecar directory. The non-existent
`Memory._get_abs_db_dir(...)` class method is **not** used anywhere in
the plugin.

- A previous version of the plugin called `Memory._get_abs_db_dir(subdir)`,
  which raised `AttributeError: type object 'Memory' has no attribute
  '_get_abs_db_dir'` at runtime.
- The replacement `abs_db_dir()` is imported alongside `Memory` from
  `plugins._memory.helpers.memory` in:
  - `helpers/scores.py`
  - `helpers/graph_store.py`
- Test fixtures in `tests/conftest.py` and `tests/test_graph_store.py`
  patch the **module-level** `abs_db_dir` (and where applicable the
  `helpers.scores.abs_db_dir` / `helpers.graph_store.abs_db_dir`
  re-exports) to return a per-test temporary directory, so the
  sidecar files (`relationships.json`, `scores.json`) live in a
  pytest-managed sandbox.
- `test_file_created_on_add` and the `TestAbsDbDirMigration` class
  in `tests/test_lifecycle_jobs.py` enforce this contract.

---

## 2026-06-09 — `_10_access_tracking.py` in-place metadata mutation removed

**Decision:** The `_functions` hook at
`extensions/python/_functions/plugins._memory.helpers.memory/Memory/search_similarity_threshold/end/_10_access_tracking.py`
**NEVER mutates `doc.metadata` in place** on the documents returned by
`Memory.search_similarity_threshold()`.

- The previous version of the hook set
  `doc.metadata["access_count"]` and `doc.metadata["last_accessed_at"]`
  directly on the framework's `Document` objects. This caused
  Agent Zero's own context retrieval to break when the plugin was
  enabled, because the returned documents are shared with other
  framework callers that did not expect their `Document.metadata`
  dicts to be mutated out from under them.
- The fixed hook persists access metadata **only** to the
  `scores.json` sidecar via `ScoreStore.update_access(...)`. The
  returned `Document` objects are left untouched.
- The caller does NOT need updated access metadata injected into the
  returned documents; persistence to the sidecar is sufficient for
  the lifecycle jobs and the Memory Dashboard, which read scores
  from `scores.json` rather than from per-document metadata.
- The six tests in `tests/test_access_tracking.py` were updated to
  assert the new contract: `update_access` is called on the
  `ScoreStore`, and the returned `Document` objects are NOT mutated.

**Generalised rule:** The Neuro Core plugin MUST NOT mutate
`Document` objects returned by framework memory calls. Framework
`Document` instances are shared across the call graph and any
in-place mutation of `.metadata` (or any other attribute) is a
silent contract violation that can break unrelated subsystems.
All Neuro Core state lives either in FAISS document metadata
(written at insert time) or in the `relationships.json` /
`scores.json` sidecar files — never on shared, borrowed
`Document` instances.

---

## 2026-06-10 — Unified `memory_subdir` resolution chain for Neuro Core tools (D20)

**Decision:** All Neuro Core tools **MUST** resolve `memory_subdir`
using the same chain as `memory_save` / `memory_load` — namely,
read `db.memory_subdir` from the `Memory` instance returned by
`Memory.get(agent)`. The bare `agent.config.memory_subdir`
fallback-to-`"default"` pattern is insufficient in project
contexts.

**Resolution chain (canonical, all tools must use):**
```python
db = await Memory.get(self.agent)            # framework resolves subdir
subdir = getattr(db, "memory_subdir", None) or "default"
```

**Why:** `Memory.get(agent)` is the framework's project-aware
entry point — it inspects the agent's context (including the
active project subdir) and returns a `Memory` instance whose
`memory_subdir` attribute is already correctly resolved. The
plugin must trust this resolution and never reimplement it via
`agent.config.memory_subdir` lookups, because `agent.config` does
not always expose `memory_subdir` directly in project contexts
(where the active subdir is set on the project, not on the
agent's own config dict).

**Bugs fixed in this session:**

- `usr/plugins/neuro_core/tools/memory_reflect.py` — the tool
  had a local `_resolve_memory_subdir(agent)` helper that fell
  back to `"default"` when `agent.config.memory_subdir` was not
  directly exposed. In a project context, this caused
  `collect_episode_memories()` to read the global `default`
  FAISS index instead of the project subdir, producing false
  `"No memories found for episode_id …"` errors. **Fixed** by
  removing the local helper and reading `db.memory_subdir`
  directly. The empty-result message was also updated to
  include the resolved subdir for diagnosability
  (`"No memories found for episode_id {X} in subdir {Y}"`).

- `usr/plugins/neuro_core/tools/memory_relate.py` — the tool
  had a local `_lookup_memory_subdir(agent)` helper with the
  same `"default"` fallback. The tool worked in practice
  because the call site used
  `getattr(db, "memory_subdir", None) or _lookup_memory_subdir(...)`,
  but the fallback was a latent bug. **Fixed** by removing the
  local helper and reading `db.memory_subdir` directly (no
  fallback to the buggy local function).

- `usr/plugins/neuro_core/tools/memory_score.py` — already
  correct. Uses `db.memory_subdir` directly on line 160 when
  constructing the `ScoreStore`. No changes required.

- `usr/plugins/_memory/tools/memory_save.py` — the reference
  pattern. Does not implement its own subdir resolution at all;
  it simply calls `Memory.get(self.agent)` and lets the
  framework resolve the subdir internally.

**Generalised rule:** All Neuro Core tools that need to know
the active `memory_subdir` must read it from the `Memory`
instance returned by `Memory.get(agent)`. Reimplementing
subdir resolution via `agent.config.memory_subdir` lookups is
forbidden — it duplicates the framework's project-aware
resolution logic and is known to fail in project contexts.

**Identified in:** live `memory_reflect` test, June 10, 2026
(failed with `"No memories found for episode_id
execute-test-001"` despite two matching memories existing in
the project subdir).

---

## 2026-06-10 — `collect_episode_memories` retrieval path (D21)

**Decision:** `collect_episode_memories()` in `helpers/reflection.py`
MUST use `Memory.search_similarity_threshold()` (with `query=""`,
`threshold=0.0`) for document retrieval, NOT `memory.db.get_all_docs()`.

- `MyFaiss.get_all_docs()` returns `self.docstore._dict` (a dict of
  `id -> Document`), whose iteration semantics are not guaranteed to
  produce `_is_document_like`-compatible objects. The previous
  implementation filtered the dict through `_is_document_like()` and
  silently found zero matches, so the `episode_id` filter never had a
  chance to match.
- `Memory.search_similarity_threshold()` is the framework's validated
  retrieval path. It is the same path used by the `memory_load` tool,
  which is the confirmed-working access path in the codebase. The
  empty-string query and `threshold=0.0` return the broadest possible
  result set; the client-side `metadata.episode_id` filter does the
  real episode scoping.
- All document retrieval in Neuro Core helpers must go through the
  framework's validated search path. Direct `get_all_docs()` access is
  not supported.

**Implementation:** `helpers/reflection.py`, function
`collect_episode_memories()`, body replaced with a single
`await memory.search_similarity_threshold(query="", limit=n, threshold=0.0)`
call followed by a client-side `_matches_episode` filter, timestamp
sort, and limit cap. The `_is_document_like()` helper has been removed
(no longer reachable from any caller). The `memory_subdir` parameter
is kept in the signature for API stability with the `memory_reflect`
tool caller but is no longer explicitly discarded with `del`.

**Confirmed by live test failure, June 10, 2026,** `memory_reflect`
Test 4.

**Addendum (2026-06-10, Test 5):** Empty-string query confirmed to
return 0 results from FAISS regardless of threshold (zero-vector has
no cosine similarity to any document). Fix: use `episode_id` as the
query string with an expanded internal limit of `max(limit * 10, 200)`
to ensure all episode members are in the candidate pool before
client-side filtering. Confirmed June 10, 2026, `memory_reflect` Test 5.

**Addendum (2026-06-11, Test 6 — final):** `search_similarity_threshold`
approach abandoned entirely (Tests 3–6, June 10–11 2026). Episode
collection requires exhaustive enumeration, not semantic ranking.
Final implementation: enumerate all doc IDs via
`memory.db.index_to_docstore_id.values()`, fetch via
`memory.db.get_by_ids()` (sync, pure dict lookup), filter client-side
by `episode_id`. Matches the path used by `memory_score` and
`memory_relate`. `memory.db.get_by_ids` confirmed sync — no `await`.

---

## 2026-06-11 — reflect_memories coroutine handling (D22)

**Decision:** D22 — reflect_memories coroutine handling:
`asyncio.get_event_loop().run_until_complete()` raises `RuntimeError`
when called from inside an already-running event loop (which is always
the case since `reflect_memories` is `async`). Fixed by replacing
`run_until_complete` with `await`. Confirmed June 11, 2026,
`memory_reflect` Test 7.

**Location:** `helpers/reflection.py`, function `reflect_memories()`,
the `if hasattr(result, "__await__"):` block (post-fix lines 282–286).

**Before:**

```python
if hasattr(result, "__await__"):
    try:
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(result)
    except Exception:
        return ""
```

**After:**

```python
if hasattr(result, "__await__"):
    try:
        result = await result
    except Exception:
        return ""
```

This is safe because `reflect_memories` is already declared `async`;
`await` works correctly inside an `async` function regardless of
whether the event loop is already running.

---

## 2026-06-11 — reflect_memories LLM call: method name + kwarg (D23)

**Decision:** D23 — reflect_memories LLM call: wrong method name and
kwarg. The `getattr` loop in `reflect_memories()` probed for
`call_llm`, `llm_call`, `chat`, `complete` — none of which exist as
methods on `Agent`. The canonical Agent Zero plugin LLM call method
is `call_utility_model(system, message, background)`. Confirmed by 8
call sites across 6 files in `plugins/_memory/`:

- `helpers/memory_consolidation.py` (lines 407, 464)
- `tools/behaviour_adjustment.py` (line 37)
- `extensions/python/monologue_end/_50_memorize_fragments.py` (line 63)
- `extensions/python/monologue_end/_51_memorize_solutions.py` (line 64)
- `extensions/python/message_loop_prompts_after/_50_recall_memories.py` (lines 97, 161)

Secondary bug: the helper called `call(system=prompt, user=user_msg)`;
the canonical kwarg is `message=`, not `user=`. Both fixed in one edit
by replacing the probe loop with a direct `getattr(agent,
"call_utility_model", None)` lookup and rewriting the call site to use
`system=`, `message=`, `background=False`. Confirmed June 11, 2026,
`memory_reflect` Tests 7–8.

**Location:** `helpers/reflection.py`, function `reflect_memories()`,
the LLM-call block (post-D23 lines 265–278).

**Before:**

```python
call = None
for name in ("call_llm", "llm_call", "chat", "complete"):
    call = getattr(agent, name, None)
    if callable(call):
        break
if call is None:
    return ""

try:
    result = call(system=prompt, user=user_msg)
except Exception:
    return ""
```

**After:**

```python
call = getattr(agent, "call_utility_model", None)
if call is None or not callable(call):
    return ""

try:
    result = call(
        system=prompt,
        message=user_msg,
        background=False,
    )
except Exception:
    return ""
```

The `await result` coroutine-handling block added in D22 (lines
282–286) is kept as a defensive fallback in case a future test stub
or alternative agent implementation returns an awaitable from
`call_utility_model`.

---


---

## 2026-06-11 — memory_relate related_to symmetry (D24)

**Decision:** D24 — `memory_relate` must create a symmetric back-edge
when `rel_type == "related_to"`. The Neuro Core spec states that
`related_to` is the only symmetric relationship type — all other
rel_types (`supports`, `contradicts`, `depends_on`, `derived_from`,
`precedes`, `follows`, `part_of`) are strictly directional and must
NOT create back-edges. Confirmed June 11, 2026, Test 10
(`relationships.json` verification).

**Location:** `usr/plugins/neuro_core/tools/memory_relate.py`,
`MemoryRelate.execute()`, the add-edge path (post-D24 lines 249–269)
and the remove path (post-D24 lines 224–232).

**Bug observed:** Test 10 confirmed that calling
`memory_relate(from_id="gjlgVO7Ztw", to_id="qpagFRPr7V",
rel_type="related_to", weight=0.9)` wrote only the forward edge
`gjlgVO7Ztw → qpagFRPr7V`. The reverse edge `qpagFRPr7V → gjlgVO7Ztw`
was not present in `relationships.json` — `qpagFRPr7V` was not even a
top-level key. This violated the spec's symmetry requirement for
`related_to` and would have caused BFS traversal in
`GraphStore.neighbors()` to be non-traversable in the reverse direction
without an explicit re-query.

**Fix applied (add path):**

```python
# --- add_edge path ---
edge = GraphEdge(
    from_id=from_id,
    to_id=to_id,
    type=rel_type,
    weight=safe_weight,
    confidence=safe_weight,
    source="agent",
    created_at=_now_iso(),
)
store.add_edge(edge)

# --- D24: symmetric back-edge for RELATED_TO ---
if rel_type == RelationshipType.RELATED_TO.value:
    reverse_edge = GraphEdge(
        from_id=to_id,
        to_id=from_id,
        type=rel_type,
        weight=safe_weight,
        confidence=safe_weight,
        source="agent",
        created_at=_now_iso(),
    )
    store.add_edge(reverse_edge)
```

**Fix applied (remove path):**

```python
# After forward removal succeeds:
if rel_type == RelationshipType.RELATED_TO.value:
    try:
        _remove_specific_edge(store, to_id, from_id, rel_type)
    except Exception:
        pass  # non-fatal: forward removal already succeeded
```

**Import added:** `RelationshipType` was added to the existing import
from `usr.plugins.neuro_core.helpers.graph_store` (line 43). The
symmetry check uses `RelationshipType.RELATED_TO.value` to avoid
mismatches between the enum and the wire-format string.

**Test cases added (2) in
`usr/plugins/neuro_core/tests/test_memory_relate_tool.py`:**

1. `test_related_to_writes_forward_and_reverse_edges` — calls the tool
   with `rel_type="related_to"` and asserts that exactly **2** edges
   are written to the stub `GraphStore`: the forward edge
   `a → b` and the reverse edge `b → a`, both with
   `type=RelationshipType.RELATED_TO.value` and `weight=0.9`.

2. `test_directional_type_does_not_write_reverse_edge` — calls the
   tool with `rel_type="supports"` and asserts that exactly **1** edge
   is written: the forward edge `a → b` only, with
   `type=RelationshipType.SUPPORTS.value`. Explicitly asserts that no
   edge with `from_id="b"` and `to_id="a"` exists in the saved list.

**Test suite result:**

```
python -m pytest usr/plugins/neuro_core/tests/ -x -q
........................................................................ [ 43%]
........................................................................ [ 86%]
......................                                                   [100%]
166 passed in 90.84s (0:01:30)
```

166 tests pass (was 164 before D24; the 2 new test cases are
`test_related_to_writes_forward_and_reverse_edges` and
`test_directional_type_does_not_write_reverse_edge`).

**Operational note:** A container restart is required before the live
verification can be run, because the in-process Agent Zero Python
runtime caches the old `memory_relate.py` module. The container
restart will re-import the patched module. The live verification
(Step 5 of the D24 session) will call
`memory_relate(from_id="gjlgVO7Ztw", to_id="qpagFRPr7V",
rel_type="related_to", weight=0.9)` and verify that
`relationships.json` now contains the `qpagFRPr7V` key with the
reverse edge entry. This is held pending container restart
confirmation from the user.

**Pre-existing test compatibility:** The 5 original required test
cases (Tests 1–5 in `test_memory_relate_tool.py`) and the 2 bonus
tests (weight clamping, missing args) all continue to pass. The D24
fix only adds behavior; it does not change the behavior of any
existing call site.

---

## 2026-06-11 — `GraphStore.add_edge()` deduplication guard (D25)

**Decision:** `GraphStore.add_edge()` MUST deduplicate by the tuple
`(from_id, to_id, type)`. If an edge with the same `(from_id,
to_id, type)` already exists in the adjacency list, the existing
dict is **updated in place** (weight, confidence, source,
created_at) via `dict.update(edge_dict)` rather than appending a
duplicate. Different `(to_id, type)` combinations remain separate
edges (e.g., a `supports` and a `contradicts` edge from `a` to `b`
are two distinct entries).

**Where:** `usr/plugins/neuro_core/helpers/graph_store.py`,
`add_edge()` method, lines 256–280. The previous implementation
used a `for ... else` pattern that replaced the entire edge dict
(`edges[i] = edge.to_dict()`); the D25 refactor uses the explicit
list-comprehension dedup pattern from the spec:
```python
data = self._adj
bucket = data.setdefault(edge.from_id, [])
edge_dict = edge.to_dict()
# D25: deduplicate by (to_id, type) — update in place if found
existing = [
    e for e in bucket
    if e.get("to_id") == edge.to_id and e.get("type") == edge.type
]
if existing:
    existing[0].update(edge_dict)
else:
    bucket.append(edge_dict)
self._atomic_write(data)
```

**Collateral fix — `GraphEdge.from_dict()` `weight` field loss:**
The D25 test `test_add_edge_duplicate_updates_weight` revealed a
latent bug in `GraphEdge.from_dict()`: the classmethod did not
pass `weight` when reconstructing an edge from a raw dict, so
round-tripped edges always had `weight=1.0` (the default) even
when the on-disk value was different. **Fixed** by adding
`weight=float(raw.get("weight", 1.0))` to the `from_dict()`
kwargs, line 128. The existing round-trip test
`test_round_trip_dict` and the new D25 test both pass after the
fix.

**Test added:** `test_add_edge_duplicate_updates_weight` in
`tests/test_graph_store.py::TestGraphStoreAddAndGet` (lines
146–159). Asserts that two `add_edge()` calls with the same
`("a", "b", "related_to")` keys produce exactly one edge in the
adjacency list with `weight=0.9` (from the second call) and
`confidence=0.8` (from the second call).

**Test suite result:** `cd /a0 && python -m pytest
usr/plugins/neuro_core/tests/ -x -q` → **167 passed in 90.84s**
(was 166 before D25; delta +1 matches the new test). No pre-
existing test regressed.

**No live verification required:** The fix is unit-testable in
isolation. `add_edge()` and `from_dict()` are both pure-Python
functions operating on the sidecar JSON; they do not depend on
the in-process Agent Zero Python module cache. The fix takes
effect immediately on the next import.

**Identified in:** Test 12, June 11, 2026.

---

## D26 — Sidebar entry dead: neuro-entry.html was empty, no click handler, no panel-open logic

**Discovered during:** WebUI verification session, June 12, 2026.

**Root cause:** The file at
`/a0/usr/plugins/neuro_core/webui/sidebar/neuro-entry.html`
was empty (`cat` returned zero output). The sidebar entry
visually rendered (the brain emoji and "Neuro Core" label were
visible in the sidebar) because the surrounding layout template
provided a fallback rendering, but clicking it dispatched no
event and opened no panel.

**Secondary finding — the framework's panel system is DOM-based,
not event-based.** The Agent Zero framework's right-canvas system
(confirmed in
`/a0/webui/components/canvas/right-canvas-store.js`) uses Alpine.js
store methods for surface registration and opening:

- Surfaces are registered via
  `$store.rightCanvas.registerSurface({ id, title, icon, order, canOpen, open, close })`
- Surfaces are opened via
  `$store.rightCanvas.open(surfaceId)` or
  `$store.rightCanvas.toggle(surfaceId)`
- The framework calls `callJsExtensions("right_canvas_register_surfaces", this)`
  during init to let plugins register surfaces
- The DOM is updated via
  `document.body.classList.toggle("right-canvas-open", this.isOpen)`
- The width is controlled via the CSS custom property
  `--right-canvas-width`

**The `a0-open-right-panel` custom event does not exist in this
framework version.** A grep across `/a0/webui/`,
`/a0/plugins/_memory/webui/`, and `/a0/usr/plugins/neuro_core/webui/`
returns zero listeners for any `a0-open-*-panel` event. The
correct pattern is the store method, not a custom event.

**Fix applied (in this session):**

1. **Wrote `/a0/usr/plugins/neuro_core/webui/sidebar/neuro-entry.html`**
   (64 lines) with:
   - `<script type="module">` block that imports
     `/usr/plugins/neuro_core/webui/graph-store.js` — this is
     critical because the store is self-registering (it calls
     `window.Alpine.store('neuroGraph', ...)` on DOMContentLoaded
     or `alpine:init`) and was not being imported anywhere in the
     page. Without this import, the store never registers and the
     panel's `<template x-if="$store.neuroGraph">` never renders.
   - `<a>` element with `@click.prevent="(async () => { ... })()"`
     that:
     - Checks `$store.rightCanvas` is available
     - Calls `rightCanvas.registerSurface({ id: 'neuro-core-graph', ... })`
       on first click (idempotent — the framework's
       `registerSurface` is upsert)
     - The `open` callback injects the panel HTML into
       `#right-panel` via `fetch()` + `insertAdjacentHTML()` +
       `Alpine.initTree()` to mount Alpine bindings on the newly
       added DOM
     - Calls `rightCanvas.open('neuro-core-graph')` to show the panel

2. **Wrote `/a0/usr/plugins/neuro_core/webui/right-canvas-panels/graph-panel.html`**
   (88 lines) with:
   - `<div x-data>` wrapper
   - `<template x-if="$store.neuroGraph">` store-gating (panel
     only mounts when store exists)
   - `x-data="{ q: ..., sub: ... }"` local state for the search form
   - `x-init="$store.neuroGraph.onOpen()"` lifecycle hook
   - `x-destroy="$store.neuroGraph.cleanup()"` cleanup hook
   - Search form with `@submit.prevent` → `$store.neuroGraph.fetch(q, sub)`
   - Loading indicator, in-panel error display, empty state
   - Nodes/edges iteration with `<template x-for>`
   - All class names use the `.nc-` plugin namespace

3. **Confirmed `graph-store.js` load order:** the store was not
   being imported anywhere on the page. The fix in (1) adds the
   import to the sidebar entry's `<script type="module">` block,
   guaranteeing the store registers on first sidebar render.

**Test suite result:** `cd /a0 && python -m pytest
usr/plugins/neuro_core/tests/ -x -q` → **191 passed in 91.07s**.
No regression. WebUI files are not Python — no new tests added
for this fix.

**No live verification performed** — the user will restart the
container and verify the sidebar click opens the right panel
with the graph panel content.

---

## D27-env — Environmental: Memory.Area enum serialization error in Memory Dashboard

**Discovered during:** WebUI verification session, June 12, 2026.

**Environmental note — NOT a Neuro Core bug:**

The Memory Dashboard (at `/plugins/_memory/webui/memory-dashboard.html`)
raises `TypeError: Object of type Area is not JSON serializable`
on page load, before any Neuro Core endpoint is called. The error
originates from `helpers/api.py:85` (the framework's generic API
handler) when serializing the response from
`/a0/plugins/_memory/api/memory_dashboard.py:189`:

```python
def _format_memory_for_dashboard(self, m: Document) -> dict:
    metadata = m.metadata
    ...
    return {
        ...
        "metadata": metadata,  # ← FULL RAW METADATA DICT
    }
```

The `_format_memory_for_dashboard` method passes the entire raw
FAISS document metadata dict through to `json.dumps`. If any
document in the FAISS index has `metadata['area']` stored as a
`Memory.Area` enum instance (rather than a string), the serializer
crashes.

**This is a pre-existing bug in the `_memory` plugin's dashboard
handler, not in Neuro Core.** The dashboard's `process()` method
defaults to `action="search"`, and when called with no search
query, it returns ALL documents from the FAISS index via
`memory.db.get_all_docs()` — which includes any document with enum
metadata.

**The Neuro Core Area serialization fix (D24) is correct and
complete** — all four Neuro Core handlers
(`_get_context_graph`, `_get_relationships`,
`_list_all_relationships`, `_post_relationship`) route their
responses through `_enum_safe_value()` via both per-handler wraps
and the centralized `process()` guard. The fix takes effect on
the next container restart.

**Documented for environmental awareness.** This bug does not
affect Neuro Core functionality or submission. The fix would
require modifying `/a0/plugins/_memory/api/memory_dashboard.py`
(outside Neuro Core scope). If the user wishes to address it
upstream, the recommended fix is to add `"area": str(metadata.get("area", "unknown"))`
or use a recursive enum-coercion helper before returning the
dict.

**Test suite result:** unchanged — **191 passed in 91.07s**.
No new tests added (environmental note only).

**No live verification performed** — the user will restart the
container and verify.

---


---

## D28 — RESOLVED — 2026-06-13

**Issue:** Panel content not rendering in right canvas tab
**Root cause:** `extensions/webui/right-canvas-panels/graph-panel.html` used `<template x-if>` with inline Alpine state instead of the framework's `data-surface-id` + `isSurfaceVisible()` + `isSurfaceRendered()` + `<x-component path>` pattern
**Fix:** Rewrote extension file to match `_editor` plugin pattern exactly. Inner panel file (`webui/right-canvas-panels/graph-panel.html`) unchanged — self-contained with inline x-data.
**Confirmed:** `/usr/plugins/neuro_core/webui/` IS HTTP-served by the framework. `<x-component path="/usr/plugins/neuro_core/webui/right-canvas-panels/graph-panel.html" mode="canvas">` resolves correctly.
**Status:** ✅ Resolved. Panel renders with header, search form, and empty state.


---

## D29 — RESOLVED — 2026-06-13

**Issue:** HTTP 500 on `GET /api/plugins/neuro_core/context_graph` after D28 container restart. Docker logs: `TypeError: 'bool' object is not callable at /a0/helpers/api.py:55 inside requires_csrf(), which calls cls.requires_auth()`.

**Root cause:** `ContextGraphApi` in `api/context_graph.py` defined `requires_auth = True` as a plain class attribute. This shadowed the `@classmethod requires_auth() -> bool` inherited from `ApiHandler`. When the framework called `cls.requires_auth()` (a method call), Python found the `True` attribute first and raised `'bool' object is not callable`. The base class contract (`/a0/helpers/api.py:43-55`) requires `requires_auth`, `requires_csrf`, `requires_api_key`, and `get_methods` to be `@classmethod` returning `bool` / `list[str]`.

**Fix:** Rewrote `requires_auth` in `ContextGraphApi` from a plain `True` attribute to a `@classmethod` returning `True`:

```python
@classmethod
def requires_auth(cls) -> bool:
    return True
```

**Files changed:**
- `usr/plugins/neuro_core/api/context_graph.py` (lines 43-49)

**No other handlers affected** — grep for `requires_auth`/`requires_csrf` across `usr/plugins/neuro_core/api/` found only `context_graph.py`.

**Container restart:** Required (Python module reload).

**Tests:** 191 passed in 90.93s. No regression.

---

## D29 — RESOLVED — 2026-06-13

**Issue:** Panel search returned JSON.parse: unexpected character at line 1 column 1 — API returned HTTP 500
**Root cause:** `GraphStore.neighbors()` defined with `max_hops` parameter, but callers in `context_graph.py:155` and `retrieval.py:237` passed `hops=` keyword argument → `TypeError`
**Fix:** Added `hops: Optional[int] = None` as backward-compatible alias in `GraphStore.neighbors()`. `effective_hops = max_hops if max_hops is not None else (hops if hops is not None else 1)`
**File modified:** `helpers/graph_store.py`
**Container restart:** Required (Python file change)
**Tests:** 191 passed
**Status:** ✅ Resolved pending container restart + live verification

---

## D30 — RESOLVED — 2026-06-13

**Issue:** Panel search returns HTTP 500 after D29 fix — `TypeError: 'bool' object is not callable` in framework dispatch
**Root cause:** `ContextGraphApi` defined `requires_auth = True` as a plain class attribute, shadowing the base `@classmethod requires_auth()`. Framework dispatch calls `cls.requires_auth()` then `cls.requires_csrf()` → calling `True()` raises `TypeError`
**Fix:** Replaced plain attribute with `@classmethod def requires_auth(cls) -> bool: return True` in `api/context_graph.py`
**File modified:** `usr/plugins/neuro_core/api/context_graph.py`
**Only handler affected:** Yes — `find` confirmed no other Neuro Core handlers had the same issue
**Container restart:** Required
**Tests:** 191 passed
**Status:** ✅ Resolved pending container restart + live verification

---

## D31 — RESOLVED — 2026-06-13

**Issue:** UI polish rewrite produced correct HTML/CSS but styles did not apply after hard refresh
**Root cause:** x-component injects files as HTML fragments — `<html>`, `<head>`, and `<link rel="stylesheet">` tags are stripped on injection. External CSS file never loaded.
**Fix:** Embedded all CSS as inline `<style>` block at top of fragment, following confirmed pattern from `_editor` and `_browser` core plugins (both use inline styles in their fragment HTML — confirmed via `grep` at lines 264 and 294 respectively)
**File modified:** `webui/right-canvas-panels/graph-panel.html` (177 lines, self-contained with inline `<style>` block; no `<html>`, `<head>`, or `<body>` wrapper tags)
**External CSS:** `webui/graph-panel.css` kept on disk as source reference (449 lines) but no longer referenced by the panel HTML — live styles come from the inline block
**Container restart:** Not required — hard browser refresh (Ctrl+Shift+R) sufficient
**Tests:** 191 passed in 90.78s
**Architectural note:** All future Neuro Core WebUI fragments must use inline `<style>` — never `<link>` or external stylesheets. x-component fragment injection strips `<html>`, `<head>`, and `<link>` tags, so styles must travel with the fragment.
**Status:** ✅ Resolved — panel renders with polished dark-mode UI (hub icon header, search row, status envelope with pulse loading dot, node cards with color-coded score badges, edge pills)

---

## D32 — retrieval.py BFS Multi-Seed from_id Type Error

**Date:** 2026-06-14

**Layer:** Helper — helpers/retrieval.py + helpers/graph_store.py

**Discovered by:** test_retrieval.py (Workstream B)

**Issue:** BFS seed expansion passed list[str] to GraphStore.get_edges(from_id=...), which only accepted str. When called with a list, it silently returned an empty result, causing multi-seed context graph queries to return zero graph-expanded neighbors for all seeds beyond the first. Silent data loss — no exception raised.

**Symptom in production:** search_context_graph() calls with multiple seed nodes behaved as if only one seed was provided. BFS graph expansion was silently skipped for all additional seeds.

**Fix:** graph_store.py — broadened from_id parameter to accept str | list[str]. retrieval.py — added defensive flat normalization of neighbor_lists to handle both single-string and list returns without assuming shape.

**Files changed:** helpers/graph_store.py, helpers/retrieval.py

**Backward compatible:** Yes — all existing call sites pass from_id as a plain string; unaffected.

**Status:** ✅ Fixed

---

## D33 — run_graph_analytics() Always Returned Zeros Due to get_edges() No-Arg Call

**Date:** 2026-06-14

**Layer:** Helper — helpers/lifecycle.py + helpers/graph_store.py

**Discovered by:** test_graph_analytics.py (Workstream B) — 16 xfail(strict=True) tests confirmed the silent zero-return

**Issue:** lifecycle.py:464 called graph_store.get_edges() with no arguments. GraphStore.get_edges(from_id: str) required from_id, so a TypeError was raised and immediately swallowed by the surrounding except Exception guard, which returned {"nodes": 0, "edges": 0, "boosted": 0} for every call.

**Symptom in production:** The graph analytics job loop reported 0 nodes, 0 edges, and 0 boosted on every run regardless of graph state. Score boosting for high-centrality nodes never occurred. Completely silent — no log warning, no exception.

**Fix:** graph_store.py — made from_id: Optional[str] = None with overloaded return: no-arg call returns full adjacency map dict[str, list[GraphEdge]]; call with from_id returns list[GraphEdge] (historical signature unchanged). Added Union to typing imports.

**Contract clarification confirmed:** run_graph_analytics() returns THREE keys: {"nodes": int, "edges": int, "boosted": int}. Any caller asserting only two keys is incomplete.

**Files changed:** helpers/graph_store.py

**Backward compatible:** Yes — 60+ existing call sites pass from_id explicitly; full suite verified passing.

**Status:** ✅ Fixed

---

## D34 — AccessDecayJob Unprotected from helpers import plugins Import

**Date:** 2026-06-14

**Layer:** Test infrastructure / extensions/python/job_loop/_10_access_decay.py

**Discovered by:** test_hooks.py (Workstream B)

**Issue:** _10_access_decay.py._run() contains an unprotected from helpers import plugins at the top of the function body. The test conftest registers helpers with __path__ = [], which blocks submodule discovery, causing ModuleNotFoundError when any test imports AccessDecayJob. _20_episode_grouping.py and _30_contradiction_detection.py handle this correctly — their equivalent import is inside _read_config() wrapped in try/except.

**Symptom in production (latent):** If helpers.plugins is unavailable at runtime during early plugin load (before the framework fully initializes), AccessDecayJob._run() will raise ModuleNotFoundError rather than degrading gracefully. The other two extensions handle this correctly.

**Fix applied (test-only):** test_hooks.py adds a minimal helpers.plugins stub to sys.modules before loading any job_loop extension. No production code was changed.

**Recommended follow-up (v0.2.0):** Wrap the from helpers import plugins in _10_access_decay.py._run() in try/except ImportError consistent with the other two extensions. Tracked as a v0.2.0 cleanup item.

**Files changed:** tests/test_hooks.py (test-only)

**Status:** ✅ Mitigated in tests; production hardening deferred to v0.2.0

---

## Workstream B Operational Observations (2026-06-14)

The following are not formal decisions but are permanent operational notes arising from Workstream B test coverage work.

1. helpers/graph_analytics.py and job_loop/_40_graph_analytics.py do not exist. Phase 5 (Graph analytics & UI) is genuinely unstarted. Any relay prompt that assumes these files exist will fail silently or produce fabricated tests. Always ls before referencing a file in a relay prompt.

2. tests/test_job_loop_extensions.py does not exist. The correct pattern source for job_loop extension tests is tests/test_lifecycle_jobs.py.

3. Contradiction detector word-boundary limitation. run_contradiction_detection() uses \b regex word boundaries for _NEGATION_TOKENS and _OPPOSITE_PAIRS. Inflected forms (enabled, disabled, increasing) do NOT trigger the heuristic — only exact root forms (enable, disable, increase) match. This is by design for v0.1.0 and is documented in the test_contradiction.py module docstring.

4. run_contradiction_detection() does not persist validation_status = "disputed" to disk. The function sets validation_status on the in-memory metadata dict and returns counts — the caller is responsible for writing back to FAISS. Any job loop extension that calls this function without persisting the result is silently dropping the dispute flag. Verify in Workstream C integration test #10 (see below).

---

## Workstream C — Integration Test #10 (added 2026-06-14)

**Contradiction persistence** — Trigger run_contradiction_detection() via the job loop with two opposing memories present. After the job completes, query FAISS metadata for the older memory's validation_status. Confirm it is "disputed" — not "unvalidated". This verifies that the job loop extension correctly writes the result back to FAISS after calling the lifecycle function.
---

D35 — Runtime Monkey-Patch for Memory Metadata Seeding
- Date: 2026-06-16
- Layer: Plugin startup / helpers/_patch.py / extensions/python/startup_migration/
- Discovered by: Workstream C, Scenario 1 — live memory_save verification
- Issue: Memory class (/a0/plugins/_memory/helpers/memory.py) has zero
  @extensible decorators. The extension hook system (the _functions/ directory
  pattern) is permanently non-functional for any Memory method. All previously
  written hook files targeting Memory/insert_documents/start/ and
  Memory/insert_text/start/ are dead code — they are never loaded by
  call_extensions_sync. Fields remained absent from FAISS metadata envelope
  after every hook-based approach.
- Secondary issue: apply_seeding() only seeds 3 of 5 fields from heuristics
  (importance, confidence, stability — only when source/consolidation_action
  are present). A plain memory_save seeds only importance. memory_type and
  validation_status are never seeded by apply_seeding() at all. apply_defaults()
  seeds all 5 fields unconditionally and was the correct function all along.
- Startup gap: hooks.py install() is not auto-called by load_plugins() at
  container startup. Patches installed via hooks.py were lost on every container
  restart until a startup_migration extension was added.
- Fix:
  - helpers/_patch.py: install_patches() installs 3 idempotent wrappers on the
    Memory class: insert_text (calls apply_defaults + validate_neuro_metadata),
    search_similarity_threshold (access tracking), delete_documents_by_ids
    (graph cascade). Idempotent via _neuro_patched attribute. Non-fatal via
    try/except. Deferred imports inside wrappers. uninstall_patches() restores
    originals from _originals dict.
  - extensions/python/startup_migration/_05_neuro_patch.py: synchronous
    Extension subclass that fires install_patches() via call_extensions_sync
    at container startup (initialize.py:78 → migration.startup_migration()).
    Must be synchronous (not async), must extend helpers.extension.Extension,
    must have _NN_ prefix.
  - hooks.py: install()/uninstall() now call install_patches()/uninstall_patches()
    for runtime plugin enable/disable support.
  - apply_seeding() replaced with apply_defaults() in the insert_text wrapper.
- Tests added:
  - tests/test_patch_live_integration.py: 6 tests (install marks methods,
    idempotency, metadata seeding with apply_defaults, validation coercion,
    uninstall restores originals, patch survives reimport)
  - tests/conftest.py: installed_patches fixture added
  - Total suite: 290 passing (was 284)
- Dead code retained:
  - extensions/python/_functions/.../Memory/insert_documents/start/_10_neuro_metadata.py
  - extensions/python/_functions/.../Memory/insert_text/start/_10_neuro_metadata.py
  Both retained as documentation that Memory has no @extensible decorators.
  Neither file fires. Neither should be removed without this note being preserved.
- Architectural constraint confirmed: /a0 is agent0ai/agent-zero framework repo.
  ALL Neuro Core git operations must target /a0/usr/plugins/neuro_core/ (repo:
  thirdeyenation/Neuro-Core). Never commit Neuro Core files to /a0.
- Live verification: memory_save → memory ID Ba8h1N25cn → all 5 fields present
  in FAISS metadata envelope post-container-restart (2026-06-16).
- Commit: c9f3456 on thirdeyenation/Neuro-Core@dev
- Status: Fixed

