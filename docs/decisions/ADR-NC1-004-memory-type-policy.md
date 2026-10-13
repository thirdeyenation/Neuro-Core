# ADR-NC1-004 — Durable Memory-Type Metadata Policy (scalar enum primary + additive memory_types collection)

- **Status:** RATIFIED (ACCEPTED) — ratified by HITL per **D-NC1-134** (2026-10-05); product-plane stamp applied 2026-10-10 per D-NC1-150 (re-confirmed D-NC1-149). This ADR is the C6 checkpoint artifact of **WI-P59-KI029-MEMTYPE-EDIT**. It records the durable metadata policy implemented in that work item; the ratification condition ("must not be treated as accepted until HITL decides") was SATISFIED by D-NC1-134. Canonical copy: control plane (WI-P59-KI029-MEMTYPE-EDIT work-item directory); this product-plane file is the released mirror.
- **Supersedes:** nothing. It creates new durable metadata policy; it does not amend ADR-NC1-002 (dual-store boundaries and sidecar authority — no boundary is touched: no sidecar is read or written by the type-edit path) and does not amend ADR-NC1-003 (recall-shaping — retrieval semantics are explicitly out of scope here).
- **Work item:** WI-P59-KI029-MEMTYPE-EDIT (user-editable memory_type in the graph-UI inspector)
- **Classification:** S2 (durable metadata-policy decision; new policy no existing ADR authorized — classification adjusted S1 → S2 by ARC)
- **Depends on:** ADR-NC1-002 sidecar/single-write authority (scores remain sidecar-authoritative; this policy adds no sidecar surface); the 8-value `MemoryType` enum contract in `helpers/metadata.py` (unchanged); ARC steward-design-decision rev 1 (approved-with-conditions, C1–C8) and design-request rev 1 (17 grounding citations, all ARC-verified); implementation-report rev 1 (canonical suite 861 passed / 0 failed, zero drift vs the 827-passing baseline).

---

## Context

The graph-UI inspector exposed no way for a user to set or edit a memory's type. The scalar `memory_type` field has always been enum-locked to the 8 implemented `MemoryType` values (`fact`, `concept`, `task`, `event`, `decision`, `skill`, `preference`, `note`), with silent coercion to `note` for invalid values at write time (`helpers/metadata.py`). Users could not correct a wrong type, assign multiple types to a node, or define their own categories. ARC's pre-design review (steward-design-decision rev 1) confirmed this is **new durable metadata policy** — no existing ADR authorized a second type surface — and required this ADR as condition C6 before work-item closure, with HITL ratification of the ADR outcome.

Implementation (WI-P59 sub-delegation 1) is complete and verified on disk: contract + implementation-report rev 1 present, canonical suite 861/0 zero drift, `tools/memory_score.py` untouched.

---

## Decision

### 1. Scalar primary remains enum-locked (C6a)

The scalar metadata field `memory_type` remains the **primary type** and stays **enum-locked to the 8 implemented `MemoryType` values**. Its meaning, membership, and all existing consumers are unchanged. User-defined types **never** enter the scalar primary, and the enum is **never extended** by user data. A legacy scalar value outside the 8 values is tolerated on read (see §4) but can never be re-asserted as a valid primary through an edit.

### 2. Additive `memory_types` collection with invariant (C6a)

A new additive metadata field `memory_types` (a JSON list of strings) lives **inside the existing FAISS document metadata** — no new store, no new sidecar, no persisted-shape change to existing records. It carries the **full type set, primary included**, under the invariant:

```text
memory_type ∈ memory_types
```

The invariant is enforced at every types write (the scalar and the collection are written **together**, in one standard-metadata-path update). Legacy records that carry only the scalar are **read-derived** as `[memory_type]` without any persisted mutation — read-compat derivation only; there is no bulk FAISS rewrite and no data migration.

### 3. Single normalization authority — `normalize_memory_types` (C6a)

`helpers/metadata.py::normalize_memory_types()` is the **single normalization authority** for the type set. All reads and all validation of the collection go through it. Two modes:

- **strict mode** (edit validation): validates the full-set-replace payload — primary must be one of the 8 enum values; every additional token must match the grammar below; duplicates, enum collisions, and cap violations are rejected loudly with no partial write.
- **lenient read mode** (all reads): derives the set for scalar-only records (`[memory_type]`), never mutates persisted state, and flags a scalar/collection mismatch as `inconsistent` (see §4).

No other code path may read, write, or normalize the collection shape.

### 4. C2 inconsistent-collection read behavior

When `memory_types` exists but the scalar `memory_type` was rewritten outside the collection (e.g. by the `memory_score` tool, which writes the scalar through `_FAISS_FIELDS` and is behaviorally unchanged by this policy), reads **flag the set as `inconsistent`** and **tolerate it without mutation** — reads never repair persisted state. The inconsistency is repaired only by the **next full-set-replace types edit**, which rewrites scalar and collection together and restores the invariant. This behavior is pinned by test (`tests/test_wip59_ki029_memtype_edit.py`).

### 5. Write-path authority (C6b)

The **only** write path for the type set is the `memory_edit` handler's **full-set-replace** `types` payload (the 8th handler, `api/memory_edit.py`), writing through the **standard framework metadata path** (`Memory.get_by_subdir` + `Memory.update_documents`):

- the framework **Memory ID is immutable** and never written;
- **no sidecar write** — the type set is not a score; the KI-009 single-write discipline is untouched;
- server-side validation happens at **one point** (`normalize_memory_types`, strict mode);
- the handler **re-reads current document state immediately before mutation** (last-writer-wins concurrency posture, Q5): a concurrent write between load and save wins on the fields it owns last; types edits are whole-set replacements, not deltas, so a stale payload simply overwrites with the user's confirmed full set after re-read;
- validation failures reject loudly with **no partial write** (edit-time rejection, not clamping — the established WI-P53 validation precedent).

### 6. Custom user-defined types — grammar and caps as durable policy values (C6c)

Custom types are **free-form tokens confined to the additive collection** (never the primary; the enum is never extended). The bounded grammar and caps are **durable policy values of this ADR**:

| Policy value | Value |
|---|---|
| Custom-token grammar | `^[a-z0-9][a-z0-9_-]{0,39}$` (1–40 chars, lowercase, no whitespace) |
| Primary types per memory | exactly 1 (enum-locked) |
| Additional (custom or non-primary enum) types per memory | at most **7** |
| Enum-name collision | rejected |
| Duplicate tokens | rejected |

Changing any of these values in the future is a **durable-policy change requiring the gated S2 path** (new/updated ADR + ARC + HITL as applicable) — **never a silent code edit**. (Q6 confirmed by ARC: the parameters are reasonable and bounded; the gate is the point.)

### 7. UI contract (safe mode)

The graph-UI inspector edits types in **safe mode per the WI-P53 pattern**: a two-step Edit → Save/Confirm flow with cancel discarding the draft; an **Add-Type affordance** mirroring the Add-Edge UX; chip removal **limited to non-primary types** (the primary type changes only through the primary-type select); the type-unknown fallback preserved; custom chips visually distinct from enum chips via a CSS-only modifier class (`.nc-details__chip--custom`). No new SVG icon was added; the icon inventory is unchanged (the `material-symbols-outlined` usage count is test-pinned against HEAD).

---

## Explicit non-goals

- No retrieval-scoring semantic changes; no recall-order influence from `memory_types` in this policy (filtering behavior is additive and compatibility-aware; scoring weights untouched).
- No changes to the 8 enum values' meaning or membership.
- No validation/dispute-status editing (KI-034 — remains deferred, unchanged).
- No sidecar schema change, no new store, no new single-writer boundary (ADR-NC1-002 respected).
- No bulk migration of persisted records; no persisted-shape mutation on read.

## Honest limitations

1. **Ratification status:** RATIFIED (ACCEPTED) by HITL per D-NC1-134 (2026-10-05); product-plane stamp applied 2026-10-10 per D-NC1-150. If HITL amends or rejects a clause in future, remediation follows the normal gated path.
2. **Concurrency is last-writer-wins, not optimistic:** the pre-mutation re-read narrows the window but does not detect divergence; two simultaneous editors can still race (Q5 posture as approved).
3. **Scalar rewrites outside the collection** (currently the `memory_score` tool's `_FAISS_FIELDS` write) produce `inconsistent` sets until the next types edit; the plugin tolerates this by design rather than constraining the tool.
4. **No performance, concurrency-safety, or security claims** are made or supported by this policy.
5. **HITL live-UX verification** of the inspector flow has not been performed in the implementing sub-delegation; a framework restart is required before any live re-verification (standing NC1 discipline). *Update 2026-10-10: subsequent HITL live verification was performed across WI-P59/P61/P65/P70 (per D-NC1-149), including restart-survival checks; see RV-001 host-validation evidence for the candidate-specific restart pass.*

---

## Consequences

- **Positive:** users can correct a memory's primary type, assign multiple types per node, and define bounded custom categories — all through the safe-mode inspector with loud, no-partial-write validation. Legacy scalar-only data needs no migration. The scalar contract and every existing consumer are preserved (zero-drift suite evidence).
- **Negative / accepted costs:** a second type surface now exists on one document, contained by the single-normalization-authority rule and the primary∈collection invariant; transient `inconsistent` states are possible when non-`memory_edit` writers touch the scalar.
- **Rollback:** revert the WI-P59 commit (single work item). Records edited under this policy retain their collection after rollback; rolled-back code simply ignores it — no data-loss path.

## Verification and gates (per the confirmed impact assessment — recorded, not decided, by INT)

ARC conformance (required, mode both), VAL integration (required), PRD consistency (required), HITL ratification of this ADR outcome (required before closure). Implementation evidence: canonical suite via `run_suite.sh` — 861 passed / 0 failed, zero drift vs the 827 baseline (827 + 34 new WI-P59 tests).

## References

- Work item: `.a0proj/team/work-items/WI-P59-KI029-MEMTYPE-EDIT/` — design-request rev 1, steward-design-decision rev 1 (C6 wording, Q5/Q6 responses), implementation-contract rev 1, implementation-report rev 1
- Decision log: `.a0proj/decision_log/decisions.md` (classification adjustment S1→S2 and gate plan recorded by ORC)
- Implemented source: `helpers/metadata.py` (`normalize_memory_types`), `api/memory_edit.py` (types payload), `webui/right-canvas-panels/graph-panel.html` (safe-mode type editing), `tests/test_wip59_ki029_memtype_edit.py`
- Related ADRs: ADR-NC1-001 (native integration/decoration precedent), ADR-NC1-002 (sidecar authority — respected, untouched), ADR-NC1-003 (recall-shaping — untouched)
