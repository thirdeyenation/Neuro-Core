"""Application service composing Neuro Core domain capabilities."""
from activity_ledger import ActivityEvent, ActivityLedger
from memory_lifecycle import ValidationState, transition
from memory_store import MemoryStore
from neuro_core import Memory, Scope, retrieve


class NeuroCoreService:
    def __init__(self, store: MemoryStore, ledger: ActivityLedger | None = None) -> None:
        self.store = store
        self.ledger = ledger or ActivityLedger()

    def capture(self, memory: Memory) -> Memory:
        self.store.put(memory)
        self._event("captured", memory, "stored")
        return memory

    def retrieve(self, query: str, scope: Scope) -> list[dict]:
        results = retrieve(query, scope, list(self.store.list(scope)))
        for item in results:
            self._event("retrieved", item["memory"], "selected")
        return results

    def validate(self, memory_id: str, target: ValidationState) -> Memory:
        current = self.store.get(memory_id)
        if current is None:
            raise KeyError(memory_id)
        updated = Memory(current.text, current.source, current.scope, current.importance, current.confidence, transition(current.validation, target), current.memory_id)
        self.store.put(updated)
        self._event("validation_changed", updated, target.value)
        return updated

    def _event(self, kind: str, memory: Memory, outcome: str) -> None:
        self.ledger.append(ActivityEvent(kind, memory.scope, (memory.memory_id,), outcome, {"source": memory.source}))
