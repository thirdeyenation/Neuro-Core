# ADR-NC1-002: Dual-Store Architecture Verdict — Documented Split with Explicit Single-Writer Ownership Boundaries (NC1)

> **Mirror notice:** this is a product-plane MIRROR, not the authority. The canonical copy lives at
> `.a0proj/team/work-items/WI-P1-OA1-CHARTER-ADR/ADR-NC1-002-dual-store-verdict.md` (control plane).
> Mirrored 2026-10-10 per D-NC1-150, following the ADR-NC1-003 mirror precedent. On divergence, the control-plane copy governs.

- **Status:** Accepted (2026-09-08) — promoted to canonical 2026-09-08 following ARC charter-review pass (steward-design-decision.yaml rev 1, approved-as-proposed, zero conditions; ORC-verified).
- **Resolves:** Charter open items **OA-1** (dual-store split intent) and **OA-3** (score authority direction), per the OA-1 verdict RATIFIED by HITL in **D-NC1-026** (2026-09-08T10:11:00-04:00). Resolves KI-009 score double-write drift **by assignment, not unification** — KI-009 itself remains OPEN until the downstream FAISS mirror-write removal work item closes it with validation evidence.
- **Work item:** WI-P1-OA1-CHARTER-ADR (this ADR's creation); verdict chain: WI-P1-OA1-STACK-VERDICT (design-request.yaml rev 2 → ARC steward-design-decision.yaml rev 2 approved-as-proposed → PRD narrative-report rev 1 narrative-approved → HITL ratification D-NC1-026).
- **Classification:** S2 (durable NC1 storage-contract decision)
- **Precedent format:** ADR-NC1-001 (WI-2026-09-04-PHASE0-PATCH-ARCH)

## Context

NC1's persistence is a dual-store architecture: a SQLite domain core (via `NeuroCoreService`/`SQLiteStore`, serving the `neuro_*` tool family) operating alongside the host FAISS+sidecar stack (via the `memory_*` tool family and host `Memory` operations). The plugin's documentation never acknowledged this split, and ownership of several data classes was ambiguous or contradictory — most concretely KI-009: `docs` and `helpers/scores.py:15-17` declare the scores.json sidecar authoritative, while `tools/memory_score.py:157-165` double-writes mutable scores to both the sidecar and FAISS metadata, producing drift.

The MVP Execution Plan (D-NC1-023) set the OA-1 starting position as **documented-split-with-explicit-ownership-boundaries** pending the S2 process. ADDENDUM 3 §M of the plan's assessment made four de-facto ownership claims; INT's design request (rev 2) agreed with the verdict shape but rebutted two of them on evidence, and ARC independently confirmed both rebuttals:

- §M's claim that SQLite already owns relationships is **FALSE** on dev — the dev `SQLiteStore` has exactly one table and **no edge table**; relationships are sidecar-only (`helpers/graph_store.py:153-158, 217-234`).
- §M's claim that SQLite owns audit is **FALSE** — audit is in-memory only (`activity_ledger.py:24-38`; default wiring `neuro_service.py:10`, append path `:33-34`; no persistence path, lost on restart).

§M's claims that host FAISS owns semantic vectors and that single-writer assignment resolves score drift **are** supported by evidence (`helpers/retrieval.py:192`; `tools/memory_score.py:157-165` vs `helpers/scores.py:15-17`). The plan document remains reference, not authority.

Unification alternatives were evaluated and rejected on evidence (see Alternatives Considered). The verdict was ratified by HITL per S2 policy (D-NC1-026), with all gates ORC-verified on disk: ARC steward-design-decision rev 2 **approved-as-proposed** (zero conditions; 18/18 grounding citations independently verified, `verification_scope: all`; all 5 rev 1 revision conditions satisfied), PRD narrative-report rev 1 **narrative-approved** (`required_level: charter-reconciliation`; `drift_detected: false`; no difficulty-only scope rationales). Gates per confirmed plan: no ARC conformance, no VAL (decision-only work item); HITL ratification per S2 policy.

## Decision Basis and Applicability

Decision basis: **dev @ cde1e0c** (canonical per D-NC1-025). Domain-store clauses apply to the live plugin upon the **HITL-gated dev→main merge**; until then, live main lacks the SQLite domain stack and the verdict's domain-store clauses are **forward-binding only**.

Per D-NC1-025, main (6318dc2) is a direct ancestor of dev (cde1e0c); dev = main + exactly one commit (Phase 0 closure). The verdict describes a strict superset of live main.

**Standing rule (carried from D-NC1-025):** ancestry claims must be verified with `git merge-base --is-ancestor` before being reported as decision-critical facts.

## Decision

NC1 adopts a **documented split with explicit single-writer ownership per data class** — the following eight-boundary set — plus the config-relative DB path target state. The eight boundaries, both §M rebuttals, and the KI-009 single-writer assignment constitute the complete boundary set.

**(1) Domain memory text + core fields (neuro_* family):** SQLite domain store, single writer (`SQLiteStore` via the `neuro_*` tools). The host family's parallel writes to FAISS metadata remain host-stack behavior, documented as a separate family, not as drift.

**(2) Host-stack memory text/metadata:** FAISS metadata via host `Memory` operations, single writer (host `Memory`).

**(3) Mutable scores (importance/confidence/stability):** the **scores.json sidecar is the single writer of record**. The FAISS metadata mirror write in `tools/memory_score.py:157-165` is **assigned for removal in a downstream implementation work item** — this is the single-writer assignment that resolves KI-009 by assignment, not unification. The read-side metadata fallback (`helpers/retrieval.py:124-137`) is **RETAINED as a documented read-only compatibility path** for legacy records lacking sidecar entries; it reads potentially stale mirror data and is **never authoritative**. The downstream mirror-removal work item must add a test asserting **sidecar precedence** and preserve the **non-destructive, non-activation-blocking** properties (`docs/architecture.md:117,146`).

**(4) Access tracking (access_count, last_accessed_at):** scores.json sidecar, single writer. The `access_count` mirror at `helpers/native_access.py:94` is assigned for removal in the same downstream work item.

**(5) Relationships/edges:** relationships.json sidecar, single writer (host-coupled `GraphStore`). **SQLite gains NO edge table under this verdict.**

**(6) Activity/audit events:** **durable activity events in the SQLite domain store — ADOPTED** (INT position upheld; NC2 precedent PK-004, register status verified-adapted with `usable_in: decision`, legitimate under the six-point rule), replacing the volatile in-memory `ActivityLedger` (`activity_ledger.py:24-38`). Conditions: (i) the in-memory `ActivityLedger`'s API surface is preserved behind the service boundary so the domain family's contract does not break; (ii) implementation is a **separate downstream work item** with its own classification and gates — the verdict is decision-only; (iii) the migration-runner precedent (PK-004: exclusive `user_version` ownership, downgrade refusal, `BEGIN IMMEDIATE` per migration) is recorded as **design-space precedent for that implementation, not a transferable NC2 layout**.

**(7) Lifecycle validation state:** documented intentional split — SQLite `ValidationState` column for the domain family (`sqlite_store.py:10,13`; `memory_lifecycle.py:5-27`), FAISS metadata `validation_status` for the host family (`tools/memory_score.py:44`) — recorded as **designed behavior, not drift**.

**(8) Semantic vectors:** host FAISS exclusively; **no SQLite vector storage**.

**Config-relative DB path — REQUIRED TARGET STATE:** the config-relative DB path (resolved through the plugin settings chain per `/a0/plugins/AGENTS.md:26-27`: defaults in `default_config.yaml`, resolution order project/profile → project → user/profile → user plugin config → bundled default) is the **REQUIRED TARGET STATE** for the domain store, citing the framework negative constraint (`/a0/helpers/AGENTS.md:19` — no hardcoded local absolute paths) and KI-004. Implementation remains a **separate downstream remediation work item**; this ADR records the target, not the change.

**NO-SCOPE-REDUCTION GUARD:** the eight per-data-class ownership boundaries above, both ADDENDUM 3 §M rebuttals, and the KI-009 single-writer assignment constitute the complete boundary set and **must not be reduced**. Difficulty, defect burden, or schedule pressure are **NEVER** valid rationales for narrowing or removing any boundary in this set; any such proposal is a scope decision requiring a new S2 design request naming the affected boundary and **HITL approval** — never a silent implementation-time reduction.

## Honest Framing (binding — implemented / planned / unverified remain distinct)

**Implemented (on dev @ cde1e0c):** the native extensible-decoration host-integration mechanism (ADR-NC1-001) and the SQLite domain store serving the `neuro_*` family. The dual-store split itself exists in code; what this ADR adds is its **ratified documentation and ownership assignment**.

**Planned (adopted by this verdict, not yet implemented):** durable SQLite `activity_events` (boundary 6); FAISS mirror-write removal for scores and access tracking (boundaries 3–4); the config-relative DB path (target state). Each is a separate downstream work item with its own classification and gates.

**Unverified:** post-restart persistence of the new behavior (durable activity_events, post-removal sidecar precedence) is **unverified until validated** by the downstream work items' validation evidence. Nothing in this ADR claims it.

**KI-009 status:** this ADR records the ratified authority assignment. **KI-009 remains OPEN** until the FAISS mirror-write removal work item closes it with validation evidence (including the sidecar-precedence test). No sentence of this ADR, the charter amendment, or the verdict document may word KI-009 as resolved.

**No overclaiming.** This ADR does not claim proven concurrency, performance, security, or benchmark outcomes. Domain-store clauses are forward-binding until the HITL-gated dev→main merge.

## Consequences

- **Downstream work items created by ratification (each with its own classification + gates):** (a) durable `activity_events` implementation; (b) FAISS mirror-write removal (with sidecar-precedence test; non-destructive, non-activation-blocking preservation); (c) config-relative DB path remediation; (d) this ADR as the durable-policy record for the verdict.
- **Charter amendment:** OA-1/OA-3 closure + CAP-05/13/29 annotations, applied via the gated charter path (PRD proposes, ARC reviews, HITL ratifies as part of D-NC1-026) — executed under WI-P1-OA1-CHARTER-ADR.
- **Documentation:** the dual-store split and the eight ownership boundaries must be reflected in NC1's documentation surfaces through the normal docs-reconciliation gates; until then, docs remain a known-unreconciled claim surface (KI-005/KI-007 et al.).
- **Rollback:** the verdict is a documentation/policy decision with zero code changes in this work item; rollback is a HITL decision superseding this ADR via a new S2 design request.

## Alternatives Considered

1. **Unify-on-SQLite** (fold sidecars and host metadata into the SQLite domain store) — **Rejected.** Not achievable without breaking host contracts: semantic vectors must remain in host FAISS regardless (`helpers/retrieval.py:192` — host `Memory` delegation inside `search_context_graph`), `GraphStore` is host-coupled (`helpers/graph_store.py:153-158`), and the dev `SQLiteStore` has no migration infrastructure (`sqlite_store.py:7-30`, plugin root: inline `CREATE TABLE IF NOT EXISTS`, no `PRAGMA user_version`, no busy_timeout, no migration runner).
2. **Unify-on-host-stack** (retire the SQLite domain store and the `neuro_*` family) — **Rejected.** Prohibited as a completion mechanism (D-NC1-002): it would retire the `neuro_*` family's durable structured storage and discard the validated Phase 0 baseline (336/336 at cde1e0c).
3. **Re-document the double-write as intended** (make FAISS metadata co-authoritative) — **Rejected.** Contradicts the documented sidecar-authority contract (`helpers/scores.py:15-17`), leaves the KI-009 drift class alive, and was superseded by the ratified single-writer assignment.

## References

- D-NC1-023 (MVP Execution Plan; OA-1 starting position), D-NC1-025 (dev @ cde1e0c canonical; merge-base standing rule), D-NC1-026 (verdict RATIFIED; ambiguity resolutions; prohibited-claim guards) — `/a0/usr/projects/nc1/.a0proj/decision_log/decisions.md`
- Verdict chain: `WI-P1-OA1-STACK-VERDICT/design-request.yaml` rev 2 (eight-boundary set, §M rebuttals, guard), `steward-design-decision.yaml` rev 2 (approved-as-proposed; 18/18 citations verified, scope: all), `narrative-report.yaml` rev 1 (narrative-approved)
- Charter: `.a0proj/team/product-purpose/charter.md` rev 4 → rev 5 amendment (OA-1/OA-3 closure; CAP-05/13/29 annotations)
- Precedent: ADR-NC1-001 (`WI-2026-09-04-PHASE0-PATCH-ARCH/ADR-NC1-001-native-memory-integration.md`)
- Prior knowledge: PK-004 (NC2 MemoryStore/migration pattern — register status verified-adapted, `usable_in: decision`)
- Framework grounding: `/a0/helpers/AGENTS.md:19` (no hardcoded local absolute paths); `/a0/plugins/AGENTS.md:26-27` (settings defaults and resolution order)
