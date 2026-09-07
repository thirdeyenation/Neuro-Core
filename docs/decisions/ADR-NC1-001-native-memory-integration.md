# ADR-NC1-001: Native Memory Integration for Neuro Core (NC1)

- **Status:** Accepted (2026-09-05)
- **Supersedes:** The `_patch.py` monkey-patch host-integration mechanism. Resolves the intentionally open architecture question recorded in **D-NC1-004**; ratified by HITL in **D-NC1-010** (WI-2026-09-04-PHASE0-PATCH-ARCH, design-request.yaml rev 1, ARC steward-design-decision.yaml rev 1 — approved-with-conditions, all 10 grounding citations independently verified).
- **Work item:** WI-2026-09-04-PHASE0-PATCH-ARCH
- **Classification:** S2 (durable host-contract decision)

## Context

NC1 (`/a0/usr/plugins/neuro_core/`) currently integrates with the Agent Zero memory host through `helpers/_patch.py`, a runtime monkey-patch installed at plugin init (`hooks.py → install()`) that wraps three `Memory` methods (`insert_text`, `search_similarity_threshold`, `delete_documents_by_ids`) with hand-written signature replicas. The framework's own `plugins/_memory` `Memory` class carries no `@extensible` decorators (verified: zero occurrences of `extensible` in `/a0/plugins/_memory/helpers/memory.py`), so host-called Memory methods emit no extension points today.

Three HIGH defects in this mechanism are **empirically confirmed** (validation-report.yaml rev 3, decision: pass; research-findings-report.md Finding 1):

1. **KI-001** — the `search_similarity_threshold` wrapper raises `TypeError` when the framework passes `embedding`.
2. **KI-002** — the `delete_documents_by_ids` wrapper raises `TypeError` on `cascade=True` and on `filter`, while the framework itself calls delete with `cascade=True` in memory consolidation and other flows.
3. **KI-021** — `search_similarity_threshold_with_scores` is not wrapped at all, so access tracking silently misses that retrieval path; the framework's memory consolidation calls it directly at `/a0/plugins/_memory/helpers/memory_consolidation.py:340` and `:354`.

The patch is therefore **not a working baseline**. Separately, the restart-verification record (rev 1, findings F2–F4) proved that in-process `@extensible` decoration is lost on container restart and nothing re-applies it at framework startup — but that startup-time re-decoration at **full identity** (`__module__=plugins._memory.helpers.memory`, `__qualname__=Memory.<method>`) via `helpers.extension.extensible` makes the handler fire post-restart (FINAL probe: pass, after a real HITL-executed container restart).

D-NC1-004 deliberately left the `_patch.py` final architecture open; D-NC1-008 set the decision criterion (working, reliable, NO regression; replacement only with a proven replacement); D-NC1-009 approved the native path as the recommended approach after VAL's probes (native access layer 8/8 checks; extensible self-decoration handler firing) and the HITL-executed restart verification.

## Decision

NC1 adopts a native, framework-sanctioned host-integration mechanism with four bounded parts and **zero framework source modification**:

**(a) NC1-owned native access layer for NC1-originated operations.** All NC1-originated Memory operations — capture, search, with-scores search, delete (plain and cascade) — go through an NC1 module that calls the real framework `Memory` methods directly with full signature fidelity and performs NC1 bookkeeping (metadata seeding, access tracking, graph cascade) itself. The delete path (plain and cascade) performs the NC1 sidecar cascade **BEFORE** the underlying FAISS/framework delete, preserving the orphan-sidecar-window elimination contract (D39-A closure contract, D53).

**(b) Startup hook re-applying the framework's `@extensible` decoration at full identity for host-initiated operations.** A NC1 startup hook, at the plugin's existing init path (where `hooks.py install()` runs today) or the earliest framework-sanctioned startup extension NC1 already occupies, applies `helpers.extension.extensible` to the three host-called Memory methods — `Memory.search_similarity_threshold`, `Memory.search_similarity_threshold_with_scores`, `Memory.delete_documents_by_ids` — at **full identity** (`__module__=plugins._memory.helpers.memory`, `__qualname__=Memory.<method>`), so framework flows (consolidation reads at `memory_consolidation.py:340/354`, host deletes with cascade) emit extension points that NC1 handlers in the user extensions area implement. The hook derives `__module__` and `__qualname__` from the live method objects at decoration time (not hardcoded strings where avoidable), detects already-decorated methods (idempotency, mirroring the `_neuro_patched` pattern, to prevent double-wrapping if the framework later adds `@extensible` itself), and asserts the derived identity before applying. Qualname-only derivation is prohibited (empirically failed: restart-verification-record, discriminating-qualname-only-redecoration step).

**(c) Full `_patch.py` deletion.** The monkey-patch module is fully deleted in this work item — not reduced to a no-op shim — and all referencing code is updated in the same change set: `hooks.py` (install_patches call at lines 59–60), `tests/test_patch_live_integration.py`, `tests/test_hooks.py`, `tests/conftest.py`, `tests/test_memory_score_tool.py`, `tests/test_lifecycle_jobs.py`, and `extensions/python/startup_migration/_05_neuro_patch.py`. No dangling imports may remain; the full NC1 suite must pass after removal.

**(d) Exception-safe handlers.** Every NC1 extensible handler wraps its entire body in try/except, logs, and **never sets `data['exception']`** and never allows an exception to escape the handler. Grounding: `/a0/helpers/extension.py` `call_extensions_async`/`call_extensions_sync` execute `cls.execute` with no exception containment, so a handler exception propagates to the Memory caller (independently verified from source, confirming the Probe B caveat); an exception present in `data['exception']` is raised by the decorator. A handler failure must degrade NC1 bookkeeping only — never the underlying Memory operation.

## Honest Framing (binding, per ARC condition 8)

Startup re-decoration **is runtime method wrapping**. It is applied via the framework's own sanctioned public decorator (`helpers.extension.extensible`) and the documented `_functions/<module>/<qualname>/start|end` discovery mechanism, without any framework source modification — categorically different from the raw attribute-replacement monkey-patch it replaces, which duplicated framework behavior with fragile hand-written signature replicas. ARC has weighed this framing and judged the mechanism architecturally acceptable as the durable host-integration contract.

The tradeoffs are stated plainly and are **not eliminable**:

- The decoration is **re-applied at every startup**; it is not a one-time change to the framework.
- **Framework-upgrade drift risk is mitigated but not eliminable**: if the framework later adds `@extensible` to Memory methods or changes signatures, NC1's idempotency detection and identity assertion (decision part b) guard against double-wrapping and drift, but continued compatibility depends on the framework retaining `helpers.extension.extensible` and the documented `_functions` discovery.
- Any host Memory call **before the startup hook runs is uncovered**; the coverage window is documented honestly and validated as a mandatory integration scenario.

**No overclaiming.** This ADR does **not** claim proven concurrency, performance, security, authorization, or multi-process coverage. Multi-process coverage must be verified per process context or recorded as a known limitation — never claimed without evidence. Known maturity limits (performance, concurrency, security, observability, benchmark outcomes) remain distinct from implemented and verified behavior in all documentation derived from this decision.

## Consequences

- **Cascade-ordering preservation (ARC condition 2):** the native access layer's delete path (plain and cascade) must perform NC1 sidecar cascade before the underlying FAISS/framework delete, preserving the D39-A/D53 orphan-sidecar-window contract. Verified with a dedicated test in the implementation.
- **Idempotency + identity assertion (ARC condition 4):** the startup hook derives identity from live method objects, detects existing decoration, and asserts derived identity before applying. Qualname-only derivation is prohibited.
- **Reference-cleanup obligations (ARC condition 5):** full `_patch.py` deletion with all referencing files updated in the same work item (`hooks.py`, five test files, `extensions/python/startup_migration/_05_neuro_patch.py`); no dangling imports; full NC1 suite passes after removal.
- **Startup-ordering as mandatory validated scenario (ARC condition 6):** integration validation includes an explicit startup-ordering scenario verifying the decoration hook runs before the first Memory use in the framework process, with the coverage window documented honestly. Container restarts are performed by HITL per D-NC1-009 — agents must not restart the container.
- **Hybrid as contingency-only via impact discovery (ARC condition 7):** the hybrid fallback (minimal guarded patch for any method whose extensible coverage proves infeasible) is **not** designed-in or implemented preemptively. If implementation uncovers an infeasibility, INT reports an impact discovery to ORC rather than silently introducing patch code.
- **Documentation consistency:** NC1's docs describing the patch mechanism are updated to describe the native integration with this honest framing (PRD consistency gate).
- **Rollback:** restore `_patch.py` and its `hooks.py install()` call (git-revertable, single mechanism), or disable the plugin. The plugin is currently disabled by default (3-line manifest; tools do not register until enabled), so there is no user-facing regression risk during the transition window.

## Provenance Note (added post-checkpoint B)

The NC1 native access layer module (`helpers/native_access.py`) is a behavior-identical reconstruction from the compiled bytecode artifact (`helpers/__pycache__/native_access.cpython-312.pyc`) of the previously validated prototype, re-validated by the probe5b oracle with all 17 stable output keys identical to the original VAL record. This note restates provenance only; no behavior, scope, gate, or architecture claim in this ADR is changed. Full details: `impact-discovery.yaml` (this work item).

## Alternatives Considered

1. **Repair the patch layer** (restore signature parity, add with-scores wrapping) — **Rejected.** Preserves the fragile monkey-patch mechanism whose residual fragility (framework API drift) is inherent and cannot be resolved once-and-for-all (fails the D-NC1-008 repair criterion). Covering with-scores would require *more* patching, expanding the defect surface.
2. **Hybrid transition** (native-first + minimal guarded patch for uncovered methods) — **Contingency only, not adopted.** Both remaining probes succeeded and the restart verification passed, so the patch's remaining role shrinks to zero; retaining any patch keeps the defect class alive. Recorded as the documented fallback, reachable only via an impact discovery to ORC (ARC condition 7).
3. **Defer / contain** (plugin disabled by default) — **Rejected.** Three live, empirically reproducible defects; incompatible with the zero-known-issues bar and D-NC1-009's approved direction.

## References

- D-NC1-004 (open `_patch.py` architecture question — resolved by this ADR), D-NC1-008 (decision criteria), D-NC1-009 (native path approved as recommended approach; HITL-executed restarts), D-NC1-010 (HITL ratification of this design with ARC's 8 conditions) — `/a0/usr/projects/nc1/.a0proj/decision_log/decisions.md`
- Design: `design-request.yaml` rev 1 (this work item)
- ARC decision: `steward-design-decision.yaml` rev 1 (approved-with-conditions; grounding_review verification_scope: all)
- Evidence: `research-findings-report.md`, `validation-report.yaml` rev 3 (pass, 296/296 suite, 8/8 native checks), `restart-verification-record.yaml` rev 1 (F2–F4, FINAL probe pass)
- Framework grounding: `/a0/helpers/extension.py` (extensible decorator, `_functions` discovery, data contract, no exception containment in dispatch); `/a0/plugins/_memory/helpers/memory.py` (no `@extensible` decorators); `/a0/plugins/_memory/helpers/memory_consolidation.py:340/354, 588/666/715`