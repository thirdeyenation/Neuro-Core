"""Agent Zero tool for Neuro Core memory lifecycle transitions."""
from helpers.tool import Tool

from memory_lifecycle import ValidationState
from neuro_service import NeuroCoreService
from sqlite_store import SQLiteStore


class NeuroValidate(Tool):
    async def execute(self, memory_id="", state="", **kwargs):
        if not memory_id or not state:
            raise ValueError("memory_id and state are required")
        try:
            target = ValidationState(state)
        except ValueError as error:
            raise ValueError("state must be unreviewed, validated, disputed, or superseded") from error
        store = SQLiteStore("/a0/usr/plugins/neuro_core/neuro_core.db")
        try:
            memory = NeuroCoreService(store).validate(memory_id, target)
            return {"memory_id": memory.memory_id, "validation": memory.validation.value, "outcome": "updated"}
        finally:
            store.close()
