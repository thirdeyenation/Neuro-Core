# ADR-NC1-005 — Framework Compatibility Contract (model-less migration detection and guarded embedding across supported Agent Zero versions)

- **Status:** ACCEPTED — approved under ARC binding conditions C1–C7 of **WI-P80-KI057-COMPAT-CONTRACT** (steward-design-decision rev 1, approved-with-conditions); recorded per **D-NC1-159**. Canonical copy: control plane (WI-P80-KI057-COMPAT-CONTRACT work-item directory); this product-plane file is the released mirror.
- **Supersedes:** nothing. It establishes a durable cross-framework compatibility contract; it does not amend ADR-NC1-001 (native integration mechanics — the `@extensible`/`_patch.py`-adjacent boundary is untouched), ADR-NC1-002 (dual-store boundaries — no store or sidecar surface is changed), ADR-NC1-003 (recall shaping — retrieval semantics untouched), or ADR-NC1-004 (metadata policy — no metadata surface is touched).
- **Work item:** WI-P80-KI057-COMPAT-CONTRACT (cross-framework-compatible migration detection + guarded contradiction-detection embedding)
- **Classification:** S1
- **Depends on:** the fail-closed model-less detector in `execute.py` (`_modelless_subdir_needs_migration`); the WI-P63 contradiction-detection fail-safe contract (condition 4 backstop); the verified framework evidence: current v2.13 at `/a0` (`models.py:852/884` — batch-shaped `embed(inputs)` present) and v2.8 at git commit `5ff106a2` (`memory.py:219` `InMemoryDocstore`, `:44-51` `._dict` access; embedding wrapper exposing only `embed_documents`/`embed_query`).

---

## Context

Neuro Core ships two plugin-side mechanisms whose correctness depends on framework internals that changed between Agent Zero v2.8 (commit `5ff106a2`) and the current version:

1. **Model-less migration detection** (`execute.py`): before constructing any Memory machinery, the detector reads the persisted FAISS store pickles directly to decide whether a model-less skip is safe. The persisted docstore object shape differs across framework generations, and the detector previously recognized only one shape, failing closed (full migration path) on the other — correct but wasteful — and its test fixtures mocked a shape that real persisted stores never carry (KI-057, child KI-057-AA test-fidelity defect).
2. **Contradiction-detection embedding** (`extensions/python/job_loop/_30_contradiction_detection.py`): the scheduled sweep computes embeddings for fact pairs. The embedding wrapper's capability surface changed across versions: current wrappers expose a batch-shaped `embed(inputs)`; v2.8 wrappers expose only `embed_documents`/`embed_query` (with `embed_documents` retained as the batch-shaped LangChain compatibility adapter).

ARC's pre-design review verified both version shapes by direct source read and bound the remedy to conditions C1–C7. This ADR records the resulting durable compatibility contract.

---

## Decision

### 1. Minimum supported framework version: v2.8 (commit 5ff106a2)

The minimum supported Agent Zero version for Neuro Core's model-less migration detection and contradiction-detection embedding is **v2.8, git commit `5ff106a2`**. Behavior on older versions is unsupported and untested. Raising this minimum version requires a **HITL-ratified amendment of this ADR** — it is a durable product-policy value, not an implementation detail.

### 2. The guarded cross-framework API rule

Both mechanisms detect capabilities **structurally, at call time, without version-string gating, without caching, and without changing any resolution mechanism**:

- **Detector (C1):** a persisted docstore is recognized only when **BOTH** hold: (a) the unpickled object's class name is in the verified allowlist (`LocalFileStore`, `InMemoryDocstore` — the framework docstore classes), AND (b) the object exposes a dict-valued `.store` **or** `._dict` attribute (the two verified persisted shapes across v2.8 → current). All other shapes — including allowlisted classes exposing neither attribute, and non-allowlisted classes — return `None` and fall back to the full migration path. **Fail-closed discipline is preserved** (C2): the detector stays read-only and model-less and never writes to the store directory.
- **Embed guard (C3):** capability detection follows the fixed order `embed()` → `embed_documents` → clean empty-mapping skip. A callable batch `embed` is preferred (current shape); a v2.8-shaped wrapper falls back to its batch-shaped `embed_documents` adapter; a wrapper exposing neither callable method cleanly skips the gated detections for the pass (empty mapping, never `None`, never raised). The WI-P63 outer fail-safe remains the backstop.

No code path may branch on a framework version string. Future framework shapes are handled by extending the verified structural checks — never by version sniffing.

### 3. Recorded implementation-detail dependency: langchain-community `_dict`

The v2.8 persisted `InMemoryDocstore` stores its document mapping in the attribute **`_dict`** — an underscore-prefixed attribute of the `langchain_community`-lineage `InMemoryDocstore` class (verified at commit `5ff106a2`, `memory.py:219` and `:44-51`). **This is an explicit, accepted implementation-detail dependency**: `_dict` is not a public API, and a future langchain-community or framework change could rename it. The detector treats its absence as an unrecognized shape (fail-closed), so a rename degrades to the full migration path — never to data corruption. This dependency is recorded here so future maintainers know it is deliberate and monitored, not accidental.

### 4. Maintenance procedure for future framework updates

When a new Agent Zero version is released:

1. **Probe the persisted shape:** create or locate a store written by the new version; unpickle `index.pkl` and record the docstore class name and the attribute carrying the document mapping (and whether it is dict-valued).
2. **Probe the embedding wrapper surface:** read the new version's `models.py` embedding wrapper; record which of `embed` / `embed_documents` / `embed_query` exist and their call shapes.
3. **If the shapes are already covered** by the existing structural checks (allowlist + dict-valued `.store`/`._dict`; `embed` → `embed_documents` order), no code change is needed — add the new version to this ADR's verified list via a minor revision.
4. **If a shape changed:** extend the structural checks to recognize the new shape alongside the old ones (never replace — older supported versions must keep working), update the allowlist only with source-verified structure (C1), and add version-shaped fixtures per §5.
5. **If the change is behavioral** (e.g., the fail-safe contract would be needed differently), file a Design Request; do not widen this ADR's scope silently.

### 5. Version-shaped-fixture commitment (standing regression assets)

The test suite permanently carries **version-shaped fixtures** as standing regression assets:

- **v2.8-shaped** persisted docstore fixtures (`InMemoryDocstore` exposing `_dict`) — positive fast-path and migration-needed cases;
- **current-shaped** persisted docstore fixtures (the same `_dict` family, verified against the live `.a0proj/memory/index.pkl`: attrs `['_dict']`, no `.store`);
- **`.store`-shaped** fixtures (the `LocalFileStore` structural family) — recognition preserved;
- **unknown-shape negative fixtures** (allowlisted class exposing neither attribute; non-allowlisted classes) — fail-closed pinned;
- **embed-guard fixtures** for both wrapper shapes (current `embed()`; v2.8 `embed_documents`-only) plus the clean-skip and fail-safe backstop tiers (`tests/test_wip80_embed_guard.py`).

Earlier `.store`-shaped mocks that diverged from real persisted data were corrected to the real shapes (KI-057-AA fidelity fix, C4) — a fidelity fix, not test weakening. These fixtures run in every full-suite regression; a framework update that changes persisted shapes fails here first.

---

## Verified version coverage

| Framework version | Docstore persisted shape | Embedding wrapper surface | Verified by |
|---|---|---|---|
| v2.8 (commit `5ff106a2`) | `InMemoryDocstore` with `_dict` | `embed_documents`/`embed_query` only | Direct source read, `memory.py:219`, `:44-51`; `models.py:831/863/870` |
| current (v2.13) | `InMemoryDocstore` with `_dict` (live probe: `.a0proj/memory/index.pkl` attrs `['_dict']`, no `.store`) | batch-shaped `embed(inputs)` (`models.py:852/884`) | Direct source read + live persisted-store probe |

## Consequences

- Model-less detection works across v2.8 → current without version gating; unrecognized future shapes fail closed to the full migration path.
- Contradiction-detection embedding works on both wrapper shapes; unsupported wrappers degrade to a clean per-pass skip with the fail-safe backstop intact.
- The `_dict` dependency is explicit, monitored, and fail-closed on drift.
- Fixture fidelity is enforced as a standing regression asset, preventing recurrence of the KI-057-AA mock-shape defect.
