# Neuro Core plugin shell

This directory is an Agent Zero plugin shell. Agent Zero discovers local plugins from `/a0/usr/plugins/<plugin_name>/` when a root `plugin.yaml` is present.

## Installation

1. Copy this `neuro_core` directory to `/a0/usr/plugins/neuro_core/`.
2. Copy the repository's domain modules (`neuro_core.py`, `memory_lifecycle.py`, `memory_store.py`, `sqlite_store.py`, `activity_ledger.py`, and `neuro_service.py`) into the plugin directory or package them with the plugin.
3. Configure `database_path` in `default_config.yaml` for a plugin-owned persistent SQLite database.
4. Add host tool registration only after verifying the target Agent Zero release's tool callback contract.

The shell deliberately contains no host-specific tool registration. The core remains independently testable; the adapter should only translate Agent Zero tool calls into `NeuroCoreService.capture`, `retrieve`, and `validate`.
