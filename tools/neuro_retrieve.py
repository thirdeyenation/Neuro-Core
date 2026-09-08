"""Agent Zero tool for explainable Neuro Core retrieval."""
from helpers.tool import Tool

import os

from neuro_core import Scope
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



class NeuroRetrieve(Tool):
    async def execute(self, query="", project="default", agent="", **kwargs):
        if not query or not project:
            raise ValueError("query and project are required")
        store = SQLiteStore(_resolve_db_path())
        try:
            results = NeuroCoreService(store).retrieve(query, Scope(project, agent or None))
            return [{"memory_id": item["memory"].memory_id, "text": item["memory"].text, "source": item["memory"].source, "score": item["score"], "factors": item["factors"]} for item in results]
        finally:
            store.close()
