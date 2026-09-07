"""Agent Zero tool for capturing scoped Neuro Core memories."""
from helpers.tool import Tool

from neuro_core import Memory, Scope
from neuro_service import NeuroCoreService
from sqlite_store import SQLiteStore


class NeuroCapture(Tool):
    async def execute(self, text="", source="agent_zero", project="default", agent="", importance=0.5, confidence=0.5, **kwargs):
        if not text or not project:
            raise ValueError("text and project are required")
        store = SQLiteStore("/a0/usr/plugins/neuro_core/neuro_core.db")
        try:
            service = NeuroCoreService(store)
            memory = service.capture(Memory(text, source, Scope(project, agent or None), float(importance), float(confidence)))
            return {"memory_id": memory.memory_id, "outcome": "stored", "scope": project}
        finally:
            store.close()
