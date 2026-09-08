"""Agent Zero tool for Neuro Core memory lifecycle transitions."""
from helpers.tool import Tool

import os

from memory_lifecycle import ValidationState
from neuro_service import NeuroCoreService
from sqlite_store import SQLiteStore


_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DB_PATH = "neuro_core.db"


def _resolve_db_path() -> str:
    """Resolve the SQLite DB path from the plugin config chain.

    KI-004 (WI-P2-DEFECT-BATCH): never a hardcoded absolute path.
    Resolution follows the plugin settings chain (project/profile ->
    project -> user/profile -> user plugin config -> bundled
    default_config.yaml). A relative configured path resolves against
    the plugin root (config-relative target state per ADR-NC1-002).
    If the config chain is unavailable, fall back to the plugin-relative
    default — never a hardcoded absolute path.
    """
    path = None
    try:
        from helpers.plugins import get_plugin_config

        cfg = get_plugin_config("neuro_core")
        if cfg is None:
            cfg = {}
        path = cfg.get("database_path")
    except Exception:
        path = None
    if not path:
        path = _DEFAULT_DB_PATH
    path = str(path)
    if not os.path.isabs(path):
        path = os.path.join(_PLUGIN_ROOT, path)
    return path



class NeuroValidate(Tool):
    async def execute(self, memory_id="", state="", **kwargs):
        if not memory_id or not state:
            raise ValueError("memory_id and state are required")
        try:
            target = ValidationState(state)
        except ValueError as error:
            raise ValueError("state must be unreviewed, validated, disputed, or superseded") from error
        store = SQLiteStore(_resolve_db_path())
        try:
            memory = NeuroCoreService(store).validate(memory_id, target)
            return {"memory_id": memory.memory_id, "validation": memory.validation.value, "outcome": "updated"}
        finally:
            store.close()
