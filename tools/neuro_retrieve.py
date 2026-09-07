"""Agent Zero tool for explainable Neuro Core retrieval."""
from helpers.tool import Tool

from neuro_core import Scope
from neuro_service import NeuroCoreService
from sqlite_store import SQLiteStore


class NeuroRetrieve(Tool):
    async def execute(self, query="", project="default", agent="", **kwargs):
        if not query or not project:
            raise ValueError("query and project are required")
        store = SQLiteStore("/a0/usr/plugins/neuro_core/neuro_core.db")
        try:
            results = NeuroCoreService(store).retrieve(query, Scope(project, agent or None))
            return [{"memory_id": item["memory"].memory_id, "text": item["memory"].text, "source": item["memory"].source, "score": item["score"], "factors": item["factors"]} for item in results]
        finally:
            store.close()
