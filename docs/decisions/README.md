# Architecture Decision Records (ADR Index)

This directory contains product-plane mirrors of Neuro Core's
architecture decision records. Unless noted otherwise, the **canonical
copy of each ADR lives in the project's control plane**; the files here
are released mirrors for contributor reference. On divergence, the
canonical control-plane copy governs.

| ADR | Title | Status | Summary |
|---|---|---|---|
| [ADR-NC1-001](ADR-NC1-001-native-memory-integration.md) | Native Memory Integration | Accepted (2026-09-05) | Replaces the `_patch.py` monkey-patch host integration with the framework's native `@extensible` decoration applied at full identity by a startup hook; ratified by HITL. |
| [ADR-NC1-002](ADR-NC1-002-dual-store-verdict.md) | Dual-Store Architecture Verdict | Accepted (2026-09-08) | Documents the deliberate split between the SQLite domain store and the host FAISS+sidecar stack, with explicit single-writer ownership boundaries; the scores sidecar is the single authoritative writer for mutable scores. Canonical copy: control plane (this file is a mirror). |
| [ADR-NC1-003](ADR-NC1-003-recall-shaping-policy.md) | Phase 1 Recall-Shaping Policy | Approved (2026-09-28) | Defines how Neuro Core intelligence (graph neighbors, validation factors) influences delivered recall, including the `recall_shaping_enabled` gate and access-tracking exclusions for graph-neighbor deliveries. Canonical copy: control plane. |
| [ADR-NC1-004](ADR-NC1-004-memory-type-policy.md) | Durable Memory-Type Metadata Policy | Ratified (2026-10-05) | Scalar `memory_type` enum stays primary and locked to the 8 implemented values; an additive `memory_types` collection carries the full set (primary ∈ collection) plus user-defined custom type tokens. Canonical copy: control plane. |

## Supersession and relationships

- ADR-NC1-001 supersedes the `_patch.py` monkey-patch mechanism (it does
  not amend ADR-NC1-002 or ADR-NC1-003).
- ADR-NC1-003 does not amend ADR-NC1-001 (decoration mechanics) or
  ADR-NC1-002 (dual-store boundaries).
- ADR-NC1-004 creates new durable metadata policy; it amends neither
  ADR-NC1-002 (no sidecar surface is added) nor ADR-NC1-003 (retrieval
  semantics out of scope).
