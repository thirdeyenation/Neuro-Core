"""SQLite implementation of the Neuro Core memory-store port."""
import sqlite3
from memory_lifecycle import ValidationState
from neuro_core import Memory, Scope


class SQLiteStore:
    def __init__(self, path: str = "neuro_core.db") -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, text TEXT, source TEXT, project TEXT, agent TEXT, importance REAL, confidence REAL, validation TEXT)")

    def put(self, memory: Memory) -> Memory:
        self.connection.execute("INSERT OR REPLACE INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (memory.memory_id, memory.text, memory.source, memory.scope.project, memory.scope.agent, memory.importance, memory.confidence, memory.validation.value))
        self.connection.commit()
        return memory

    def get(self, memory_id: str) -> Memory | None:
        row = self.connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._memory(row) if row else None

    def list(self, scope: Scope) -> tuple[Memory, ...]:
        rows = self.connection.execute("SELECT * FROM memories WHERE project = ? AND agent IS ?", (scope.project, scope.agent)).fetchall()
        return tuple(self._memory(row) for row in rows)

    @staticmethod
    def _memory(row: tuple) -> Memory:
        return Memory(row[1], row[2], Scope(row[3], row[4]), row[5], row[6], ValidationState(row[7]), row[0])

    def close(self) -> None:
        self.connection.close()
