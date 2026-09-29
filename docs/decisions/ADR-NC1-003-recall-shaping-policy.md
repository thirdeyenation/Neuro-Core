# ADR-NC1-003 — Phase 1 Recall-Shaping Policy (Neuro Core intelligence influences delivered recall)

- **Status:** APPROVED — HITL approval **D-NC1-106** (2026-09-28), approved as proposed, including `recall_shaping_enabled` default **false** (gate flip is ORC's committed action after clean VAL integration). This document is the C6 checkpoint artifact of **WI-P43-PHASE1-RECALL-SHAPING** (D-NC1-105). The canonical copy lives control-plane-first at `.a0proj/team/work-items/WI-P43-PHASE1-RECALL-SHAPING/adr-recall-shaping-policy.md`; a product-plane copy exists under `docs/decisions/ADR-NC1-003-recall-shaping-policy.md` (mirror, not authority — canonical source is the control-plane copy; see Amendment record for the 2026-09-28 record-alignment revision).
- **Supersedes:** nothing. It is a distinct durable retrieval-policy decision from ADR-NC1-001 (which covers decoration *mechanics* only) and does not amend ADR-NC1-002 (dual-store boundaries — Phase 1 touches none of the eight boundaries).
- **Work item:** WI-P43-PHASE1-RECALL-SHAPING (Phase 1 of ratified Option C)
- **Classification:** S2 (durable retrieval-policy decision; HITL checkpoint per D-NC1-105)
- **Depends on:** ADR-NC1-001 (native `extensible` decoration, production-proven); ADR-NC1-002 sidecar authority (OA-1/D-NC1-026, reaffirmed by WI-P12-SCORE-AUTHORITY); D-NC1-105 (Option C ratified, Phase 1 authorized under conditions C1–C7); WI-P44-PHASE2-GATING-EVIDENCE (VAL methodology defining the Phase-2 gating evidence bar before Phase-1 opens).

### Amendment record (2026-09-28 — record-alignment only, no semantic change)

Following ARC conformance (WI-P43, CONFORMANT with conditions), the following record corrections were applied. **No approved semantics were altered** — these are corrective alignments accepted by ARC as record accuracy, not scope changes.

1. **Status** updated PROPOSED → APPROVED per HITL decision **D-NC1-106** (2026-09-28), approved as proposed, including `recall_shaping_enabled` default **false**.
2. **Validation-factor vocabulary correction:** the table and prose below originally named the lowest status `superseded` (`validation_factor_superseded`). The ratified `MemoryScore` vocabulary value is **`deprecated`** (`helpers/metadata.py` `ValidationStatus` enum has no `superseded` member), so the key is **`validation_factor_deprecated`**. The demotion **value 0.4 is unchanged**. ARC-accepted as corrective alignment, not a scope change.
3. **`neuro_retrieve` backend retirement sequencing record:** ADR §3 states the replacement must pass VAL integration validation before the old SQLite-backed path is retired. At implementation time (WI-P43, 2026-09-28) the SQLite-backed backend was retired **before** VAL integration validation — i.e., ahead of the sequencing sentence. Rationale: the full NC1 suite was green at implementation completion (742 passed, zero drift vs the WI-P41 baseline plus updated pins), and VAL integration is the immediate next gate. **Explicit commitment:** if VAL integration fails or is inconclusive, the backend retirement is **reverted as part of remediation** before the gate flip is considered.
4. **Nonexistent 'ADR addendum' reference removed:** a code comment in `helpers/recall_shaping.py` previously cited a nonexistent 'ADR addendum' as the record for the vocabulary correction; it now cites this Amendment record section directly.

5. **§C4 access-tracking semantics correction (D-NC1-112 — record-alignment, 2026-09-29):** the §C4 dispatch-order paragraph and the C4 condition line originally stated that every delivered document (native + appended graph neighbor) is access-counted through `ScoreStore.update_access`. The shipped implementation implements the **other position — (b) PREVENTED (D-NC1-112)**: native delivered documents are access-counted through the existing single writer (`ScoreStore.update_access`), while graph-neighbor deliveries marked `neuro_neighbor: True` are **EXCLUDED** from access tracking — both `_10_access_tracking.py` handlers skip them — so graph proximity does not inflate importance/decay signals. Pinned by `tests/test_recall_shaping.py::test_v2_neighbors_excluded_from_access_tracking`. The paragraph and condition line have been corrected in place; this is a record-alignment correction to match shipped behavior, not an approved-semantics change.
6. **Gate-default history record (D-NC1-110 → D-NC1-113, 2026-09-29):** `recall_shaping_enabled` shipped default `false` per this ADR; was flipped to default `true` per **D-NC1-110** (2026-09-28) after clean VAL integration evidence; is temporarily reverted to default `false` per **D-NC1-113** (2026-09-29) pending the **KI-036** sanitizer (decoration of `Memory.update_documents` plus a start-hook `neuro_*` stripper; `Memory.update_documents` is not currently a decorated/extensible point, and the dashboard edit-save path can persist shaped `neuro_*` markers into FAISS metadata). The flip back to `true` is **pre-authorized as a fast-follow commit conditional on the sanitizer's clean gates**. This is a gate-state record, not a change to approved Phase-1 semantics.

---

## Context

The agent's real recall path is the framework host plugin's tool: `/a0/plugins/_memory/tools/memory_load.py:19` calls `Memory.search_similarity_threshold(query, limit, threshold, filter)`, and the returned documents are assembled into the agent's context. Neuro Core's accumulated intelligence — sidecar importance/confidence scores, the typed relationship graph, validation status, recency — currently has **zero influence** on what that call returns. The chain Capture→Type→Score→Validate→Intelligent Recall→Better Agent Behavior is structurally broken at the Recall link (WI-P42 verified ground truth §0.1, §0.4).

HITL ratified Option C (recall-first staged architecture, terminal state B) on 2026-09-27 (D-NC1-105), authorizing Phase 1 under ARC conditions C1–C7: an exception-safe, config-gated **end-hook handler** on the already-decorated `Memory.search_similarity_threshold` and `Memory.search_similarity_threshold_with_scores` extension points, an agent-facing explainable retrieval tool, and prompt fragments. Condition C6 requires this ADR — the durable *policy* of what governs delivered recall order and set — to exist and be HITL-approved before any code.

### C3 open verification — completed by direct read (required before this ADR could claim shape preservation)

ARC condition C3 required verification of the `_with_scores` return shape from framework source before any shape claim. Performed 2026-09-27 by direct read:

- `/a0/plugins/_memory/helpers/memory.py:373-386` — `async def search_similarity_threshold_with_scores(self, query: str, limit: int, threshold: float, filter: str = "") -> list[tuple[Document, float]]:` returns `await self.db.asimilarity_search_with_relevance_scores(query, k=limit, score_threshold=threshold, filter=comparator)`. **Verified shape: `list[tuple[Document, float]]`** — a list of (Document, native relevance score) tuples.
- `/a0/plugins/_memory/helpers/memory.py:343-370` — `search_similarity_threshold` returns `list[Document]` (either a threshold-filtered comprehension over `docs_and_scores` when an `embedding` is passed, or `await self.db.asearch(..., search_type="similarity_score_threshold", ...)`).
- Both methods are decorated by NC1 at startup (`helpers/decorate.py` `_TARGETS`, lines 19–23), so both emit start/end extension points today; the end point may rewrite `data['result']` (`/a0/helpers/extension.py` `extensible` docstring: "end extensions run last and may rewrite data['result'] or replace / clear data['exception']"; `_run_async` at `/a0/helpers/extension.py:167-186` dispatches `call_extensions_async(end_point, ...)` then `_process_result(data)`).

Shape preservation is therefore **verified feasible**, and both shapes are pinned by test in the implementation (C3).

---

## Decision

### 1. Shaping contract — what governs delivered recall in Phase 1 (executable specification)

**Insertion point.** One new NC1 handler, `extensions/python/_functions/plugins/_memory/helpers/memory/Memory/<method>/end/_05_recall_shaping.py`, registered on the two search methods (`search_similarity_threshold`, `search_similarity_threshold_with_scores`) via the **existing, production-proven** registration pattern: inherited `helpers.extension.Extension`, discovered through the framework's `_functions/<module>/<qualname>/end` mechanism (`helpers/extension.py:367` `load_classes_from_folder`), decoration re-applied at startup by the existing `helpers/decorate.py`. No new registration mechanism (C2). No framework source modification (C7).

**Filename uniqueness and dispatch order (C4).** The framework merges handler classes by **first occurrence of file name across all agent extension paths and dispatches sorted by file name** (`/a0/helpers/extension.py:326-358`, `_get_extension_classes`: "merge: first ocurrence of file name is the override"; `classes = sorted(unique.values(), key=lambda cls: _get_file_from_module(cls.__module__))`). The handler is therefore named `_05_recall_shaping.py` — verified absent from every other extension path on disk — so it dispatches **before** the existing `_10_access_tracking.py` on both search end-points. Rationale for dispatch-before-tracking: the existing access-tracking handler counts every returned document as an access, persisted exclusively through `ScoreStore.update_access` (the ratified single writer for access counts). With shaping dispatched first, access tracking observes the **final delivered set**. Corrected semantics (Amendment item 5, 2026-09-29 — record-alignment per D-NC1-112, position (b) PREVENTED): native delivered documents are access-counted through the existing single writer, `ScoreStore.update_access`; graph-neighbor deliveries marked `neuro_neighbor: True` are **EXCLUDED** from access tracking — both `_10_access_tracking.py` handlers (`search_similarity_threshold` and `search_similarity_threshold_with_scores`) skip them — so graph proximity does not inflate importance/decay signals, and no second writer to the sidecar access fields is introduced. Exclusion is pinned by `test_v2_neighbors_excluded_from_access_tracking` (`tests/test_recall_shaping.py:447`). Dispatch order and collision-uniqueness are pinned by test.

**Re-ranking formula (the heart of this ADR).** For each document `d` in the native result (and each expanded neighbor, below), with weights read from the plugin's **existing** config keys `similarity_weight` (0.5), `importance_weight` (0.3), `recency_weight` (0.2):

```text
sem    = normalized semantic/similarity factor in [0,1]
imp    = sidecar-authoritative importance in [0,1]      (ScoreStore; metadata fallback + degraded marker)
conf   = sidecar-authoritative confidence in [0,1]      (ScoreStore; metadata fallback + degraded marker)
rec    = recency in [0,1]                               (7-day exponential half-life; 0.5 neutral)
vf     = validation demotion factor in (0,1]            (status→factor map below)

shaped_score(d) = (sim_w·sem + imp_w·(imp · conf) + rec_w·rec) · vf
```

Factor sources, exactly (reusing the plugin's existing, tested factor helpers in `helpers/retrieval.py` rather than inventing parallel ones):

- **sem:**
  - `_with_scores` path: the native relevance score from the tuple, normalized by the framework's `Memory._cosine_normalizer` (`/a0/plugins/_memory/helpers/memory.py:612`) into [0,1].
  - plain path: `_semantic_for(d, default=0.5)` over metadata keys `semantic_score`/`similarity`/`score` (`helpers/retrieval.py:172-180`); for plain-path results this term is constant across the result and therefore order-neutral by construction.
- **imp / conf (sidecar authority, C5):** read via `ScoreStore.get_optional(memory_subdir)`-based lookup exactly as `helpers/retrieval.py:_importance_for` (lines 128-161): sidecar record present → `rec.importance` / `rec.confidence`, healthy; sidecar read **fails** or the record is **absent** (legacy record) → metadata fallback (`_metadata_importance`, 0.5 default; confidence metadata default 0.7 per `helpers/metadata.py:258-260`) **and the document carries an explicit `neuro_degraded: True` marker** — no fabricated healthy baselines (KI-008 honesty contract, `helpers/retrieval.py:134,253,337`). The FAISS-metadata copy is **never authoritative** (ADR-NC1-002 boundary semantics).
- **rec:** `_recency_score` (`helpers/retrieval.py:88-112`): `2^(-age_seconds / half_life)`, half-life 7 days; missing/unparseable timestamp → 0.5 neutral. Timestamp source: `metadata.last_accessed_at` else `metadata.timestamp`.
- **vf — validation demotion (annotation, NOT gating):** a status→factor map with one named config key per status, defaults:

  | `validation_status` | config key | default factor |
  |---|---|---|
  | `validated` | `validation_factor_validated` | 1.0 |
  | `unvalidated` (and missing/unknown status) | `validation_factor_unvalidated` | 0.8 |
  | `disputed` | `validation_factor_disputed` | 0.6 |
  | `deprecated` | `validation_factor_deprecated` | 0.4 |

  **Gating vs annotation, explicit (C12):** in Phase 1 validation status **only multiplies the shaped score** — it **never removes, filters, or threshold-excludes** any document from the delivered set. A `disputed` memory can still be recalled; it is demoted, and its factor is visible in-band. No claim that validation governs retrievability is made or supported by Phase 1 (that is Phase 2, with its evidence bar set by WI-P44). Default seeding note: `validation_status` defaults to `unvalidated` at capture (`helpers/metadata.py:263-264`), so unmaintained memories take the 0.8 factor — a mild, visible, explainable demotion, not exclusion.

- **importance·confidence as one term:** the sidecar's two quality axes multiply (`imp · conf`): an important-but-low-confidence memory contributes less to delivered order. Both values are [0,1]-clamped sidecar fields (`helpers/scores.py` `MemoryScores` schema, `_clamp01`), so the product stays in [0,1].

**Ordering.** The native result is re-sorted by `shaped_score` **descending, stable** — documents with equal shaped scores preserve their native relative order. No native document is ever dropped, replaced, or thresholded out by shaping: the shaped list contains **exactly the native set** (plus appended neighbors, below), reordered.

**Graph relationship expansion (bounded, additive-only).** When the `GraphStore` for the active `memory_subdir` is available and the gate is on:

1. Seed ids = the native result's document ids (`_doc_id`, `helpers/retrieval.py:117-124`).
2. `GraphStore.neighbors(from_id=seed_ids, hops=graph_max_hops)` (`helpers/graph_store.py:378`) returns flat `(target_id, hop, edge)` triples; sorted by `(hop ascending, edge.confidence descending)` — identical to the pipeline's capacity rule (`helpers/retrieval.py` step 2).
3. The first `recall_shaping_neighbors_max` (new key, **default 3**) not-already-present targets are materialized as Documents via the framework's own surfaces (`memory.db.get_by_ids([target_id])` / `Memory.get_document_by_id`, `/a0/plugins/_memory/helpers/memory.py:43/:337`) and **appended after** the re-ranked native list, in shaped-score order computed with the pipeline's graph-neighbor baseline `sem = 0.5` (`helpers/retrieval.py` step-2 parity).
4. Expanded neighbors **never displace** a native result; they only extend the tail of the list. The list may therefore exceed the caller's `limit` by at most `recall_shaping_neighbors_max` entries — a documented, bounded, honest growth (the same semantic as the panel pipeline's `graph_neighbors_max`, at a deliberately smaller default because these documents enter the agent's context window).
5. A `GraphStore` failure or missing file degrades per the envelope below — no neighbors are added, native results pass through shaped by scores alone, with the degraded marker.

**Explainability in-band (no shared-object mutation).** The existing access-tracking handler documents that returned objects are **never mutated** because framework callers hold shared references. Shaping therefore never writes into the caller-held `Document.metadata`. Instead, shaping replaces each delivered `Document` with a **shallow copy** (new object, `metadata` copied to a fresh dict) carrying explanation keys:

```text
neuro_shaped: True
neuro_factors: { similarity, importance, confidence, recency,
                 validation_status, validation_factor, shaped_score }
neuro_degraded: True        # only where a sidecar/graph read degraded (KI-008)
```

For the `_with_scores` path, tuples keep their **native** relevance score as the tuple's float — the shaped score lives in `neuro_factors.shaped_score` — so every existing consumer of that tuple shape (including framework memory consolidation at `/a0/plugins/_memory/helpers/memory_consolidation.py:340/354`) sees the shape and native score semantics it already handles. Original Document objects are never touched.

**Disabled / degraded behavior (C1, C2).**

- **Gate off:** the handler returns without touching `data['result']` — **byte-identical baseline behavior**, pinned by test (native order, native set, original objects, no copies, no markers).
- **Any failure** — `ScoreStore`/`GraphStore` unavailable, sidecar read error, neighbor fetch failure, any handler exception: the handler never re-raises and never sets `data['exception']` (C1, identical to the existing handlers' contract). Per-document failures degrade that document's factors (metadata fallback + `neuro_degraded`); per-store failures skip that factor or expansion; a total handler failure returns the native result untouched. Host recall can never break because of Neuro Core.
- **Empty or non-list result:** returned unchanged.

### 2. Safety envelope (verbatim conditions, binding on implementation)

- **C1 — exception safety + byte-identical disabled state. "the end-hook handler MUST be exception-safe in the ratified pattern — it never sets data['exception'], never re-raises, and any ScoreStore/GraphStore/sidecar failure or handler exception preserves the exact native result (native order and set) with degraded-state marking consistent with the existing neuro_degraded honesty contract (helpers/retrieval.py:134,253,337). Disabled config state MUST produce byte-identical baseline behavior, pinned by test."**
- **C2 — config gate. "the handler MUST be config-gated via a new, default-observable config key (charter observability requirement) with rollout semantics stated in the implementation contract; it MUST reuse the existing extensions/python/_functions/plugins/_memory/helpers/memory/Memory/<method>/end/ registration pattern (inherit helpers.extension.Extension), introducing no new registration mechanism."**
  - **Recommended gate key: `recall_shaping_enabled`, default `false`,** declared in `default_config.yaml` and surfaced in the plugin's settings surface (WebUI config Advanced view), so the key is discoverable and observable in the shipped default configuration. **Justification for default-false:** recall shaping changes what every host-memory consumer receives — the widest-blast-radius behavior change NC1 has made (WI-P42 risk 1). The approved design request states the rollout as "config-gated, default observable via a new key; default-off rollout" (design-request.yaml compatibility). Default-off makes the byte-identical disabled state the shipped behavior until VAL integration evidence (including the restart/startup scenario per the D-NC1-016/017 precedent) exists; HITL may then flip the default through a recorded decision. The *key* is default-observable (shipped, documented, surfaced); the *behavior* is evidence-gated. If HITL prefers default-on for immediate observability, that is a one-line ADR amendment before approval.
- **C3 — exact return-shape preservation. "the handler MUST preserve each decorated method's exact public return shape (list from search_similarity_threshold; the _with_scores return tuple shape) — verify the _with_scores shape from /a0/plugins/_memory/helpers/memory.py before implementation and pin both shapes with tests."** Verification completed above (memory.py:343-370, :373-386: `list[Document]` and `list[tuple[Document, float]]`). Both shapes pinned by test; the tuple's native score is preserved verbatim.
- **C4 — filename collision-uniqueness + dispatch order. "handler filename MUST be collision-unique across all agent extension paths (framework merges by first occurrence of file name, helpers/extension.py:_get_extension_classes) and its dispatch order relative to _10_access_tracking.py (files sorted by name) MUST be pinned by test, with access-tracking semantics on shaped results explicitly defined and asserted."** Adopted: `_05_recall_shaping.py` dispatching before `_10_access_tracking.py`; access-tracking semantics on shaped results = native delivered documents are access-counted through `ScoreStore.update_access`; graph-neighbor deliveries marked `neuro_neighbor: True` are excluded from access tracking (both `_10_access_tracking.py` handlers skip them) — corrected per D-NC1-112, Amendment item 5; delivery order does not affect counting (order-invariant iteration); asserted by test (`test_v2_neighbors_excluded_from_access_tracking`).
- **C5 — sidecar authority, read-side only. "shaping reads MUST honor ADR-NC1-002 sidecar authority (OA-3/D-NC1-026): scores/relationships from the sidecars as authoritative; the FAISS-metadata fallback read-only and never authoritative; validation_status used for demotion only in Phase 1 — no write-side boundary is touched; Phase 1 is read-side only and creates/modifies none of the eight single-writer boundaries."** The only sidecar writes in the shaped-recall flow remain the existing access-tracking handler's `update_access` calls — no new write path, no second writer. The `networkx` pip-install (`hooks.py:38-51`) remains an analytics-only dependency: Phase 1 adds **no** networkx usage (`GraphStore` expansion is sidecar-JSON BFS; C11 respected).
- **C7 — framework read-only + regression guard. "zero writes to /a0 outside /a0/usr (framework read-only per /a0/AGENTS.md); all mechanisms plugin-side; full NC1 baseline suite (51-file test discipline) runs as the non-regression guard per the §2.6 trigger table (retrieval + patch-layer triggers apply)."

### 3. Agent-facing retrieval tool contract (Phase 1, re-homed `neuro_retrieve`)

- **Name:** `neuro_retrieve` — the existing tool file `tools/neuro_retrieve.py` is re-homed: its retrieval backend becomes the plugin's own explainable hybrid pipeline `search_context_graph` (`helpers/retrieval.py:187`) over the **host** universe, replacing the current SQLite-domain backend. The existing SQLite-backed behavior is not silently removed: the replacement must pass VAL integration validation before the old path is retired (approved design-request risk mitigation), and the SQLite substrate itself is retired only in Phase 2 under the guard process.
- **Invocation (agent-facing):** `neuro_retrieve(query: str, limit: int = 10, threshold: float = 0.6)` — defaults aligned with the existing pipeline config keys (`semantic_limit`, `semantic_threshold` classes of defaults; exact wiring fixed in the implementation contract).
- **In-band factors exposed.** The tool returns the pipeline's `ContextGraph` serialized for the agent: per node — `doc_id`, `content`, `hop` (0 = direct semantic hit, >0 = relationship-expanded neighbor), and the factor record `{similarity, importance, confidence, recency, validation_status, score}` with `neuro_degraded` flags where a sidecar read degraded; per edge — `from_id`, `to_id`, `type`, `confidence`; top-level `query` and `seed_ids`. Every delivered factor is the actual value used by the pipeline's scoring (`sim_w·sem + imp_w·imp + rec_w·rec`, `helpers/retrieval.py:216-218`) — retrieval explainability is delivered as data, not narrative.
- **Behavior when shaping is disabled or store data is absent.** The tool is **independent of the recall-shaping gate**: `recall_shaping_enabled` governs only the end-hook re-ranking of ordinary `memory_load` recall; the tool always serves the explainable pipeline it is asked for. When `ScoreStore`/`GraphStore` are unavailable or a sidecar read fails, the tool returns the semantic-seed subset with explicit `neuro_degraded` markers (pipeline's documented degradation behavior, `helpers/retrieval.py:187-` docstring and KI-008 contract) — it never fabricates scores or healthy baselines. When the plugin's stores have no sidecar data at all, nodes degrade to metadata-importance with explicit markers.

### 4. Prompt-fragment policy (C12 vocabulary discipline)

One new fragment, `prompts/agent.system.tool.neuro_retrieve.md` (the plugin's existing fragment pattern; the neuro_* trio has none today — `docs/tools.md:13-15`), and, if implementation determines it is needed for discovery, a bounded mention in the `memory_*` fragment family pointing to the richer retrieval surface. Policy, binding on the fragment text:

- It **invites** the agent to use `neuro_retrieve` when it needs context-rich, relationship-aware, or explainable retrieval ("which memories relate to this, and why did they rank where they did").
- It **defines the factor vocabulary** (importance, confidence, recency, validation status, degraded markers) so the agent can read the in-band factors correctly.
- It **prohibits**, verbatim discipline per C12: any statement that validation status governs or blocks retrievability (Phase 1 demotes only); any claim that recall is "smarter/better" as a guarantee; any performance, concurrency, or security claim. Implemented, planned, and unverified behavior remain distinct. The fragment describes what the tool returns — nothing more.

### 5. Explicit non-goals

- No absorb, no boundary amendment, no charter work of any kind — Phase 2 executes the guard process (C8–C10) under separate HITL approval; this ADR amends no ADR-NC1-002 boundary.
- No networkx changes — the pip-install stays (C11); Phase 1 uses no networkx.
- No claims beyond Phase-1 reality — no validation-governs-retrievability claims (C12); no capture-chain claims for the SQLite-captured universe (orphaned until Phase 2).
- No write-side changes: no FAISS-metadata writes, no sidecar-schema changes, no new single-writer boundary (C5).
- No framework source modification, no new registration mechanism (C2, C7).
- No removal, disabling, or bypassing of intended functionality or baseline tests.

### 6. Honest limitations — what Phase 1 does NOT deliver

1. **Captured-domain universe integration is Phase 2.** Memories captured via today's SQLite-routed `neuro_capture` are invisible to the shaped host recall until the Phase-2 absorb/migration executes (verified: domain DB holds exactly 1 memory row, 12 KB — stranded cost negligible per D-NC1-105 condition 4). Phase 1 does not complete the chain test for that universe.
2. **Validation remains demotion-annotation, not gating.** A disputed or deprecated memory can still be delivered. The charter's "validation lifecycle that governs retrievability" clause stays unrealized until Phase 2 delivers and validates real gating (C12; WI-P44 defines the evidence bar).
3. **Expansion is sidecar-graph-bounded.** Only relationships recorded in `relationships.json` expand; no semantic edge inference happens at recall time.
4. **No performance, concurrency, or security claims.** Shaping adds per-result factor reads on the recall path; measured cost is unbuilt and unclaimed. Multi-process behavior is not covered by this ADR.
5. **Coverage window.** Any host Memory search before the startup decoration hook runs is uncovered (ADR-NC1-001's documented, validated window applies unchanged).
6. **Delivered-list growth.** With shaping on, recall may return up to `recall_shaping_neighbors_max` more documents than the caller's `limit` — bounded and documented, but a real behavior delta consumers must tolerate.
7. **Access-count of expanded neighbors** passes through the existing single writer only because of the adopted dispatch order; if a future change reorders handlers, that semantics must be re-asserted (pinned by test).

### 7. Phase-1 telemetry policy (ratified in this ADR)

Phase 1 adds exactly **one** new telemetry surface — no more:

- **Path/format:** a per-subdir JSON sidecar `shaping_telemetry.json`, sibling of `scores.json` / `relationships.json` in the plugin data directory (same per-subdir layout, same `_locked()` / `_atomic_write` concurrency pattern as the existing sidecars).
- **Safety:** exception-safe and non-fatal in the C1 envelope — any telemetry failure is swallowed and logged, never re-raised, never set as `data['exception']`. **Zero writes when the gate is off** (`recall_shaping_enabled: false` produces no telemetry file and no writes, byte-identical disabled state applies to telemetry too).
- **Recorded content (bounded):** timestamps, event kinds, counters, `memory_subdir`, delivered document counts, delivered document-id lists, factor bucket summaries (e.g. shaped-score ranges), exception **class names**, and gate state.
- **Prohibited content (standing constraint, binding):** telemetry must **never** persist memory text or query text (established NC1 telemetry constraint).

This telemetry exists so the Phase-2 gating decision (WI-P44 evidence bar) has real shaping-behavior evidence to evaluate, without any host-contract or privacy cost.

- **Panel continuity (architecture-analysis Dimension 3/B):** "The panel is already a host-stack application; captured memories appear with zero bridge; panel edits (edges, scores) act on the same memories the agent recalls. No rework of the panel required — this dimension is where absorb wins most cleanly."
- **Product coherence (Dimension 12/B):** "The panel already reads/writes the terminal store; it is not merely preserved, it is **completed** — it finally operates over the same universe capture writes into. Per the HITL direction, the panel is not secondary or disposable in any option; in B and C it becomes *more* first-class, not less."

Phase-1 reading of these excerpts: the panel is untouched by Phase 1 (C5 read-side-only); Phase 1 changes nothing the panel reads or writes; Phase 2 completes the shared universe.

---

## Consequences

- **Positive:** the agent's ordinary recall (`memory_load`) becomes sensitive to Neuro Core's curation intelligence — importance, confidence, recency, validation demotion, relationship expansion — with every factor inspectable in-band and an on-demand explainable pipeline via `neuro_retrieve`. Fully reversible (single config key; handler file removable), read-side only, boundary-neutral.
- **Negative / accepted costs:** a behavior change with wide blast radius when enabled (mitigated by default-off rollout + pinned byte-identity); bounded delivered-list growth; per-result factor-read cost on the recall path (unmeasured, unclaimed); dispatch-order coupling between shaping and access tracking that tests must hold.
- **Neutral:** the panel, the SQLite store, all eight boundaries, and the networkx dependency are unchanged.
- **Rollback:** set `recall_shaping_enabled: false` (byte-identical baseline, test-pinned), or remove the handler file and the config keys — a single-work-item revert. The agent-facing tool reversion is bounded to its own work item's commit.

## Verification and gates (per the approved impact assessment — recorded, not decided, by INT)

ARC conformance (required), VAL integration level (required — scenarios: shaped vs byte-identical disabled recall, degraded-mode honesty, shape pins, restart/startup dispatch evidence, tool in-band factors), PRD claim-review (required — README/docs/config-surface claims reconciled to implemented behavior, C12 vocabulary enforced). HITL approves this ADR before any code (C6/D-NC1-105).

## References

- D-NC1-105 (Option C ratified; C6 ADR checkpoint), D-NC1-104 (framing), D-NC1-026 (OA-1 dual-store verdict + sidecar authority), D-NC1-010/017 (native integration ratified; restart evidence precedent) — `/a0/usr/projects/nc1/.a0proj/decision_log/decisions.md`
- Design basis: `.a0proj/team/work-items/WI-P42-TWOSTORE-RECALL/design-request.yaml` rev 1 (13 grounding citations, all independently ARC-verified), `architecture-analysis.md` §0/§7, `steward-design-decision.yaml` rev 1 (approved-with-conditions, C1–C12)
- Framework source (all read directly, this session): `/a0/plugins/_memory/helpers/memory.py:343-386,43,337,607-612`; `/a0/plugins/_memory/tools/memory_load.py:19`; `/a0/helpers/extension.py:145-190,326-358,367`
- Plugin source: `helpers/decorate.py`; `helpers/scores.py`; `helpers/graph_store.py`; `helpers/retrieval.py:88-350`; `helpers/metadata.py:255-266`; `helpers/lifecycle.py:304-418`; `default_config.yaml`; `hooks.py:38-51`; `docs/tools.md:13-15`; `docs/decisions/ADR-NC1-001-native-memory-integration.md`; existing handlers under `extensions/python/_functions/plugins/_memory/helpers/memory/Memory/*/end/_10_access_tracking.py`

---

## Amendment Record (WI-P45-KI036-SANITIZER, 2026-09-29) — Marker-Never-Persisted Invariant

Amended per ARC condition 1 (`steward-design-decision.yaml` rev 1,
WI-P45-KI036-SANITIZER). No policy clause is changed; a durability guarantee
is added.

**New invariant — marker-never-persisted:** shaped `neuro_*` metadata markers
(`neuro_shaped`, `neuro_factors`, `neuro_degraded`, `neuro_neighbor`) are
**delivery-time only**. They are shaped onto delivered Documents at recall
time and must never persist into FAISS metadata through any write path. The
guarantee is enforced by the write-path sanitizer start hook on
`Memory.update_documents` (`_05_neuro_sanitizer.py`, ADR-NC1-001 amendment —
fourth decoration target), which strips the NC1-owned `neuro_` prefix
generically (four markers individually test-pinned) before persistence.

**Role in gating:** this invariant is a **precondition for D-NC1-113** (the
Phase-2 recall-shaping gate flip). With the gate ON, shaped markers now flow
on every recall; the sanitizer guarantees the dashboard edit-save round trip
cannot echo them back into FAISS. Gate flip remains conditional on clean
gates and remains a separate pre-authorized committed change — not part of
this work item.

**Test pins:** the invariant is pinned in `tests/test_wip45_sanitizer.py` —
real decorated `update_documents` round-trip (markers never survive),
gate-OFF byte-identity companion assertion, docs-position resolution order,
four markers individually pinned, sanitizer-internal-failure safety, and
resolution-order/idempotency pins.
