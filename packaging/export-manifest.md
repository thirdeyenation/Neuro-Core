# Clean-Export Manifest — Public Distribution Artifact

This document defines exactly what a public distribution artifact of
Neuro Core contains, how to produce one, and how to verify it before
publishing.

## What the artifact contains

A public distribution artifact contains **tracked files only** — the
files under git version control in this repository at the release tag:

- plugin source (`*.py` at the plugin root, `helpers/`, `tools/`,
  `api/`, `extensions/`)
- WebUI assets (`webui/`, `extensions/webui/`)
- agent tool prompts (`prompts/`)
- configuration defaults (`default_config.yaml`)
- plugin identity files (`plugin.yaml`, `index.yaml`, `pyproject.toml`)
- documentation (`README.md`, `docs/`, `CONTRIBUTING.md`,
  `CHANGELOG.md`, `LICENSE`, `packaging/`)
- the test suite (`tests/`)

## What the artifact must NOT contain

The export procedure below excludes all of the following. None of them
may appear in a published artifact:

- **No `.git/`** — repository internals, including full history.
- **No `neuro_core.db`** — the live SQLite database (user data).
- **No sidecar files** — `scores.json`, `relationships.json` (user
  data).
- **No `__pycache__/`** directories (anywhere in the tree).
- **No `.pytest_cache/`**.
- **No `neuro_core.egg-info/`** or other build metadata.
- **No `execute_record.json`** — runtime execution record.
- **No `.toggle-*` files** — runtime toggle state.
- **No `config.json`** — live runtime configuration (may contain
  instance-specific values).

## Export procedure

Produce the artifact from the release tag using `git archive`, so only
tracked files at that tag are included:

```bash
cd /path/to/neuro_core   # a clean clone at the release tag
git archive --format=tar.gz --prefix=neuro_core-1.1.0/ \
    -o neuro_core-1.1.0.tar.gz v1.1.0
```

(Adjust the tag name to the actual release tag.)

## Post-export verification checklist

Before publishing the artifact, verify:

1. **No credentials** — no API keys, tokens, passwords, `.env` files,
   or secret material anywhere in the extracted tree.
2. **No live data** — no `neuro_core.db`, no `scores.json`, no
   `relationships.json`, no `config.json`, no `execute_record.json`.
3. **No repository internals** — no `.git/` directory.
4. **No caches or build residue** — no `__pycache__/`,
   `.pytest_cache/`, `neuro_core.egg-info/`, `.toggle-*` files.
5. **Extracts cleanly** — the archive unpacks and the plugin's file
   layout matches the repository's tracked tree.

A quick automated check after extracting:

```bash
tar -xzf neuro_core-1.1.0.tar.gz -C /tmp/export-check
cd /tmp/export-check/neuro_core-1.1.0
find . -name '.git' -o -name '__pycache__' -o -name '.pytest_cache' \
    -o -name '*.db' -o -name 'scores.json' -o -name 'relationships.json' \
    -o -name 'config.json' -o -name 'execute_record.json' \
    -o -name '.toggle-*' -o -name '*.egg-info'
# expected output: (empty)
```

If any path is printed, do not publish the artifact — fix the export
and re-verify.
