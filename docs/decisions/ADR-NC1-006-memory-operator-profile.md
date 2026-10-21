# ADR-NC1-006 — Neuro Core Memory Operator Agent Profile (plugin-distributed profile decision)

- **Status:** ACCEPTED (decision ratified) — the capability clause was ratified by HITL 2026-10-17 as charter revision 11 (CAP-31), on ARC charter-review approved-with-conditions (C-R1..C-R5); recorded per **D-NC1-165** (ADR placement: this amendment path, not the implementation work item) alongside **D-NC1-164** (version-identity truth). Implementation status of the capability itself: **delivered and validated** (2026-10-17) — implemented under WI-P84-PROFILE-IMPLEMENTATION (W2-2): implementation report complete; ARC conformance **conformant**; VAL integration validation **pass** (live discovery, prompt composition, user-overrides-plugin merge semantics, non-selection neutrality; full suite 1062 passed / 2 pre-existing skips, no drift). Canonical copy: control plane (WI-P73-AGENT-PROFILE-DESIGN work-item directory); this product-plane file is the released mirror.
- **Supersedes:** nothing. It creates a new capability decision; it does not amend ADR-NC1-001 (native integration mechanics — untouched), ADR-NC1-002 (dual-store boundaries — no store or sidecar surface is changed), ADR-NC1-003 (recall shaping — retrieval semantics untouched), ADR-NC1-004 (metadata policy — no metadata surface is touched), or ADR-NC1-005 (framework compatibility contract — untouched).
- **Work item:** WI-P73-AGENT-PROFILE-DESIGN (S2 charter amendment: CAP-31 added + folded descriptive maintenance)
- **Classification:** S2 (charter scope addition; ratified by HITL 2026-10-17)
- **Depends on:** charter rev 11 CAP-31 clause (`.a0proj/team/product-purpose/charter.md`); the design decision **WI-P73-AGENT-PROFILE-DESIGN/steward-design-decision.yaml rev 5 (approved-with-conditions)**; the charter review **WI-P73-AGENT-PROFILE-DESIGN/steward-design-decision-charter-review.yaml rev 1 (approved-with-conditions, C-R1..C-R5)**; the standing public-naming rule (**D-NC1-146**: public-facing text uses "Neuro Core", never "nc1"/"NC1"); the KI-053 closure (2026-10-13, WI-P82-KI053-REMEDIATION).

---

## Context

NC1's agent-facing memory operations are today disciplined only by plugin-level tool fragments (`prompts/agent.system.tool.*.md`, complete for all six tools since KI-055 closed) and by whatever an agent infers from tool descriptions. There is no plugin-distributed agent profile that gives an agent coherent operating discipline for scoped, explainable Neuro Core memory work — scope semantics, error contracts, the validation lifecycle, ranking-factor interpretation, and the boundary between what the plugin maintains automatically and what the agent operates itself.

WI-P73 designed such a profile and routed it through the gated path: Design Request (rev 5) → ARC pre-design review (approved-with-conditions, including the binding automaticity and negative-claims verification against lifecycle source) → charter amendment (CAP-31) → ARC charter review (approved-with-conditions, C-R1..C-R5) → HITL ratification (2026-10-17). This ADR records the resulting durable decision, per D-NC1-165 and ARC condition C-R5.

---

## Decision

### 1. Profile identity and location

NC1 ships a **plugin-distributed agent profile** at `agents/neuro-core-memory-operator/` (`agent.yaml` + a single-concern, non-duplicating `prompts/agent.system.main.specifics.md`), publicly titled **"Neuro Core Memory Operator"** (standing public-naming rule, D-NC1-146). The profile's specifics content provides operating discipline **without duplicating plugin-level tool-fragment content**.

### 2. Scope semantics the profile provides

Operating discipline for scoped, explainable Neuro Core memory work:

- `Scope(project, agent)` isolation semantics;
- the two tool error contracts;
- the validation lifecycle — **`validation_status` is annotation-only and never blocks retrieval**;
- reading ranking factors as data (similarity, importance, confidence, recency, validation factor) — the judgment remains the agent's;
- the memory-operations coverage matrix;
- the tool-invocation policy — **neuro-first for covered operations; native memory tools for uncovered operations**;
- degradation semantics;
- the two-store topology note (SQLite domain core vs host FAISS+sidecar stack);
- the hygiene operating loop.

### 3. Automaticity boundary (binding)

The profile delivers **assisted-manual curation over an automatically maintained store**. It does **NOT** provide, and must never claim, end-to-end automatic memory management. Automatic store maintenance (recall shaping, access tracking, importance decay, episode grouping, contradiction detection with dispute persistence) is delivered by the plugin's lifecycle jobs and the host integration layer — not by the profile; capture, relate, reflect, score, validate, and delete remain **agent-operated**. This boundary was independently verified against lifecycle source during ARC pre-design review (`helpers/lifecycle.py:61`, `_10_access_decay.py:204-219`, `_20_episode_grouping.py`, `_30_contradiction_detection.py:385-398`, `_51_memorize_solutions.py:134-151`).

### 4. KI-053 resolution note

The profile capability's prompt content was conditional on the terminal state of KI-053 (inert insert-path metadata-seeding hooks: native `memory_save` bypassed Neuro Core bookkeeping). **KI-053 is CLOSED (2026-10-13, WI-P82-KI053-REMEDIATION): native `memory_save` now seeds Neuro Core bookkeeping, verified at host level with restart-cycle persistence** (four-part closure evidence bar: framework-runtime probe; end-to-end native `memory_save` seeding; restart-cycle persistence; full regression run no-drift — 1054 passed / 2 pre-existing skips). The profile's automaticity claims are therefore no longer conditioned on an open defect. The implementation work item (W2-2, WI-P84) executed both duties at its design-freeze: the KI-053 ledger re-check (closed, ledger rev 130) and the prompt-text functional re-evaluation (ARC conditions C14 and C12, per WI-P82's PRD deferred item), recorded in WI-P84's implementation contract.

### 5. Selection posture

The profile is **purely opt-in** via profile selection; non-selection is behaviorally neutral. Profile origins merge default → plugin → user → project; a user-origin profile of the same name overrides plugin prompt keys (**user-overrides-plugin merge semantics**, to be documented in the implementation design per ARC condition C6).

### 6. Candidate follow-on capabilities firewall (ARC condition C13)

Annex tool proposals — inspection, deletion, maintenance-visibility, dedup-detection, cross-store bridge, and existing-tool revisions — are **NOT part of this capability and NOT ratified by this decision**. Each requires its own ORC-created work item with default classification **S2**, routed PRD → ARC → HITL before any implementation. KI-053 remediation was a defect work item, not an annex proposal.

---

## Review evidence (mandatory per D-NC1-165 and ARC condition C-R5)

- `.a0proj/team/work-items/WI-P73-AGENT-PROFILE-DESIGN/steward-design-decision.yaml` — rev 5, **approved-with-conditions** (conditions C3/C5/C6/C7/C8/C12/C13/C14; automaticity and negative claims independently verified against source).
- `.a0proj/team/work-items/WI-P73-AGENT-PROFILE-DESIGN/steward-design-decision-charter-review.yaml` — rev 1, **approved-with-conditions** (C-R1..C-R5).
- Charter revision 11, CAP-31 clause (`.a0proj/team/product-purpose/charter.md`), ratified by HITL 2026-10-17.
- Decision-log entries **D-NC1-164** (version-identity truth) and **D-NC1-165** (ADR placement on this amendment path).

---

## Consequences

- NC1 gains a ratified, bounded capability clause (CAP-31) for a plugin-distributed operator profile; the profile itself is **delivered and validated** (WI-P84, 2026-10-17: ARC conformant, VAL integration pass) — implemented, planned, and unverified remain distinct on every surface.
- The automaticity boundary is durable product policy: no surface may describe the profile as end-to-end automatic memory management.
- The C13 firewall prevents capability creep: annex proposals cannot ride on this decision.
- W2-2 (WI-P84-PROFILE-IMPLEMENTATION, completed 2026-10-17) delivered the implementation, the C6 merge-semantics documentation, the C14 ledger re-check, and the C12 prompt-text re-evaluation, with ARC conformance and VAL integration evidence attached.
- The public title "Neuro Core Memory Operator" is fixed by the D-NC1-146 naming rule; public-facing text never uses "nc1"/"NC1".