# Neuro Core

Neuro Core is an Agent Zero memory enhancement plugin. It extends the
existing `_memory` plugin with typed memory metadata, a typed
relationship graph, importance/confidence/stability scoring, lifecycle
maintenance jobs, and structured ContextGraph retrieval with
explainable scoring factors. It is a Python-only, in-process plugin —
no external service or database daemon is required.

- Plugin identity: `neuro_core` (version 1.1.0)
- Install location: `/a0/usr/plugins/neuro_core/`
- Status: optional plugin — it must be explicitly enabled
  (`always_enabled: false` in `plugin.yaml`) and is configured per
  project and per agent (`per_project_config: true`,
  `per_agent_config: true` in `plugin.yaml`).

## What it provides

**Six agent tools** (declared under `tools/`):

- `neuro_capture` — capture a memory with typed metadata
- `neuro_retrieve` — scoped hybrid retrieval
- `neuro_validate` — validate or dispute a memory
- `memory_score` — update importance/confidence/stability and
  validation status of a memory
- `memory_relate` — create or remove typed graph relationships
- `memory_reflect` — reflect over an episode of memories

See `docs/tools.md` for per-tool behavior and arguments.

**HTTP API** — eight handler modules under `api/`, served under
`/api/plugins/neuro_core/`:

- `context_graph` — hybrid retrieval returning a serialized ContextGraph
- `relationships` — list, create, and delete graph edges. Edge deletion
  is implemented and covered by the test suite; the panel's add-edge UI
  is implemented and covered by integration tests. As of this release,
  these flows have not yet been confirmed end-to-end in a live
  host/browser session.
- `advanced_filters` — filtered graph queries
- `episode_audit` — episode listing and per-episode audit
- `reflection_audit` — reflection-memory listing and audit
- `memory_subdirs` — memory subdir discovery
- `memory_names` — user-assigned Memory Name aliases and custom Cluster names
- `memory_edit` — safe-mode editing of one memory's contents, scores,
  and memory types. Edits are validated before saving; scores are stored
  in the plugin's scores sidecar. Memory types support one primary type
  plus a set of additional types, including user-defined custom type
  names. Validation status (unvalidated / validated / disputed) can also
  be edited, with server-side rules governing which transitions are
  allowed; marking a memory "validated" records that a person reviewed
  it — it is not an automated content check. There is no permanent
  suppression mechanism: if a contradiction between memories persists,
  the periodic contradiction sweep may flag the memory as disputed
  again. Every accepted status change is recorded in the plugin's
  activity ledger.

See `docs/api.md` for routes, parameters, and response schemas.

**WebUI assets** — a right-canvas graph panel
(`extensions/webui/right-canvas-panels/graph-panel.html`), its local
vendored Cytoscape library (`webui/vendor/cytoscape-3.30.2.min.js`, served
via the plugin-asset route with no external CDN dependency), a sidebar
quick-action entry (`extensions/webui/sidebar-quick-actions-main-start/neuro-entry.html`),
and a Settings window (`webui/config.html`) that opens the standard Agent
Zero plugin-settings modal with Basic operator-level controls and an
Advanced section covering every configuration key, plus a plugin-native
configuration help surface (`webui/help/configuration.html`) linked from it.

**Lifecycle jobs** — three `job_loop` extensions
(`_10_access_decay.py`, `_20_episode_grouping.py`,
`_30_contradiction_detection.py`) plus memory-creation/search hook
extensions and graph cascade-delete cleanup. See
`docs/architecture.md` for the full wiring.

**Agent profile** — an opt-in agent profile, `agents/neuro-core-memory-operator/`
(publicly titled "Neuro Core Memory Operator"), that gives an agent coherent
operating discipline for Neuro Core memory work: scope semantics, tool error
contracts, the validation lifecycle, ranking-factor interpretation, and the
boundary between what the plugin maintains automatically and what the agent
operates itself (assisted-manual curation — never end-to-end automatic memory
management). Selecting the profile is opt-in; when it is not selected, agent
behavior and tool availability are unchanged. If a user-level profile of the
same name exists, it overrides the plugin profile's prompt keys per-key, and
metadata keys it leaves unset are inherited from the plugin profile. See
`docs/decisions/ADR-NC1-006-memory-operator-profile.md`.

## Requirements

- Agent Zero **v2.13** (commit `e3051fb`) — the version this release was
  validated against.
- Python **3.12+** (3.12 is the tested baseline; the packaging metadata
  declares a lower floor, but 3.12+ is what is tested).
- The framework's built-in **`_memory` plugin** — Neuro Core extends it
  and must have it available.
- `networkx>=3.0` — installed automatically by the plugin's `hooks.py`
  the first time the plugin is enabled (a one-time `pip install` side
  effect at enable time).
- No other dependencies beyond the Agent Zero framework runtime itself
  (framework-provided libraries such as `langchain_core` come with
  Agent Zero; there is no separate requirements file to install).

## Installation

1. **Prerequisites** — a running Agent Zero instance (Docker or local)
   with the built-in `_memory` plugin available, and Python 3.12+.
2. **Place the plugin** — put this directory at
   `/a0/usr/plugins/neuro_core/`.
3. **Enable the plugin** — open Agent Zero's settings and switch the
   Neuro Core plugin on in the plugin settings UI. Enabling runs the
   plugin's install hook, which installs `networkx` (one-time, printed
   in the logs). Note: plugin enablement has been verified at the
   manifest/discovery level (the plugin is discovered, its manifest and
   settings surface load, and the toggle is present in the settings
   UI); a full live enable-cycle confirmation in a running host is
   still pending.
4. **Verify the installation** — after enabling, confirm that:
   - the Neuro Core entry appears in the sidebar quick actions, and
   - the graph panel opens from the right canvas, and
   - the plugin's settings window opens from the plugin settings UI.
   If any of these fail, check the Agent Zero logs for
   `[neuro_core]` messages.
5. **Configure** — configuration lives in `default_config.yaml` and can
   be overridden per project and per agent; see `docs/configuration.md`.

## The execute.py migration script

The plugin ships a one-shot migration script, `execute.py`, at the
plugin root. It does not run by itself: it runs when **you** trigger
it — either through the plugin manager's **execute** action for Neuro
Core in the Agent Zero WebUI, or manually from the Agent Zero root
with:

    python -m usr.plugins.neuro_core.execute

**When do you need to run it?** After installing or updating the
plugin (for example, when a plugin update tells you its data format
changed, or when support asks you to run the migration), run it once
so that memories created before Neuro Core was installed (or before
the update) are brought up to the current schema. Running it again is
always safe.

**What it does:** it brings any pre-existing Agent Zero memories up to
the Neuro Core schema — seeding the typed `memory_type` field (default
`"note"`), the three score fields (`importance` / `confidence` /
`stability`), and the `validation_status` field (default
`"unvalidated"`). The migration is **non-destructive**: existing values
are never overwritten, and only documents missing one or more Neuro
Core fields are touched.

**Fresh vs. existing stores:**

- **Fresh (empty) store:** the script detects that there is nothing to
  migrate and exits within seconds — it never loads the heavy memory
  machinery at all.
- **Existing store:** the script first inspects the persisted memory
  store directly (without loading the heavy memory module) to determine
  which memory subdirectories need migration. Subdirectories that are
  already fully migrated are skipped; only those that need work are
  migrated through the full path.

**Fail-closed safety:** the pre-migration inspection is strictly
read-only — it never writes to your memory store. If it cannot
confidently determine a subdirectory's state (for example, an
unrecognized internal store format from an unsupported framework
version), it does not guess: it falls back to the full, authoritative
migration path for that subdirectory, which produces the same correct
result. In no case does an unrecognized store format cause data loss or
corruption. The supported framework range and this safety behavior are
recorded in
`docs/decisions/ADR-NC1-005-framework-compatibility-contract.md`.

## Data and privacy

Neuro Core stores data in three places:

- **`neuro_core.db`** — the plugin's SQLite domain database. By default
  it lives in the plugin directory; the `database_path` configuration
  key controls the location (relative paths resolve plugin-relative,
  absolute paths are honored verbatim).
- **Sidecar files** — `scores.json` (importance/confidence/stability
  and access counts) and `relationships.json` (the typed relationship
  graph), stored alongside the plugin's data.
- **The host memory store** — memory contents and their metadata live
  in Agent Zero's own FAISS-based memory store, exactly like ordinary
  Agent Zero memories. Neuro Core reads and enriches that store; it
  does not duplicate memory content into a second database.

**Egress disclosure:** memory content is sent to whatever **embedding
provider** your Agent Zero configuration uses (memory storage and
retrieval rely on embeddings). Neuro Core adds no additional network
egress of its own, but using it means your memory content is processed
by that embedding provider, exactly as with plain Agent Zero memory.

**Deleting your data:**

- To delete everything Neuro Core persists, stop Agent Zero and remove
  the plugin's data files: the SQLite database at the configured
  `database_path` (by default `neuro_core.db` in the plugin directory)
  and the `scores.json` / `relationships.json` sidecars next to it.
- Memories themselves (including Neuro Core's metadata on them) live in
  the host memory store; delete them through Agent Zero's own memory
  management, or remove the host memory directory, depending on how
  much you want to erase.

## Uninstalling / removing the plugin

- The plugin's `uninstall()` hook is intentionally a **no-op**: it does
  not remove pip packages (other plugins may share them) and does not
  delete your data.
- **Removing the plugin directory** (`/a0/usr/plugins/neuro_core/`)
  is the uninstall. Agent Zero's plugin manager removes the plugin
  files when you uninstall it through the UI; deleting the directory
  manually achieves the same result.
- **What remains on disk after removal:**
  - the SQLite database (`neuro_core.db` at the configured
    `database_path`) — contains the plugin's domain data;
  - the `scores.json` and `relationships.json` sidecars;
  - the `networkx` pip package installed at enable time (harmless to
    leave; remove with `pip uninstall networkx` if you want a fully
    clean environment).
- **Safe-removal guidance:** back up or explicitly delete the database
  and sidecar files listed above if you do not want residual data left
  behind. Do not delete files inside the Agent Zero framework itself.

## Known limitations

- Plugin enablement has been verified at the manifest/discovery level
  (the plugin is discovered and its settings toggle appears); a full
  live enable-cycle confirmation in a running host is still pending.
- The plugin's live HTTP endpoints have not been exercised in
  end-to-end validation for this release; they are covered by the
  plugin's test suite at the implementation level.
- The live FAISS similarity query path has not been run against a live
  host index in this release's validation; index loadability is
  verified.
- The plugin's pre-release documentation guard is textual only — it
  checks committed documents for internal identifiers and paths; it is
  not a runtime data-leak prevention mechanism.
- The relationships edge-deletion flow and the panel's add-edge UI are
  implemented and test-covered but not yet confirmed end-to-end in a
  live host/browser session (see the `relationships` API entry above).

## Documentation

- `docs/tools.md` — agent tools
- `docs/api.md` — HTTP API
- `docs/architecture.md` — architecture and data flow
- `docs/configuration.md` — configuration keys
- `docs/data-model.md` — storage and data model
- `docs/decisions/README.md` — architecture decision records (index)
- `packaging/export-manifest.md` — what a public distribution artifact
  contains and how to produce one
- `CHANGELOG.md` — change history

## Support

Issues and feature requests: [github.com/thirdeyenation/Neuro-Core/issues](https://github.com/thirdeyenation/Neuro-Core/issues).
Please include your Agent Zero version, the Neuro Core version, steps
to reproduce, and expected vs. actual behavior.

## Tests

Run the full suite with the committed wrapper (it sets `PYTHONPATH=/a0` and
runs from the plugin root automatically; extra pytest args are forwarded):

```bash
/a0/usr/plugins/neuro_core/tests/run_suite.sh
```

See `CONTRIBUTING.md` for contribution requirements.
