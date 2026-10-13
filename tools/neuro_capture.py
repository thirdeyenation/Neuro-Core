"""Agent Zero tool for capturing scoped Neuro Core memories."""
from helpers.tool import Tool

import os

from neuro_core import Memory, Scope
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



class NeuroCapture(Tool):
    async def execute(self, text="", source="agent_zero", project="default", agent="", importance=0.5, confidence=0.5, **kwargs):
        if not text or not project:
            raise ValueError("text and project are required")
        store = SQLiteStore(_resolve_db_path())
        try:
            service = NeuroCoreService(store)
            memory = service.capture(Memory(text, source, Scope(project, agent or None), float(importance), float(confidence)))
            return {"memory_id": memory.memory_id, "outcome": "stored", "scope": project}
        finally:
            store.close()
