"""Storage boundary for Neuro Core; durable backends implement this port."""
from collections.abc import Iterable
from typing import Protocol
from neuro_core import Memory, Scope

class MemoryStore(Protocol):
    def put(self, memory: Memory) -> Memory: ...
    def get(self, memory_id: str) -> Memory | None: ...
    def list(self, scope: Scope) -> Iterable[Memory]: ...

class InMemoryStore:
    def __init__(self) -> None:
        self._items: dict[str, Memory] = {}

    def put(self, memory: Memory) -> Memory:
        self._items[memory.memory_id] = memory
        return memory

    def get(self, memory_id: str) -> Memory | None:
        return self._items.get(memory_id)

    def list(self, scope: Scope) -> tuple[Memory, ...]:
        return tuple(memory for memory in self._items.values() if memory.scope == scope)
