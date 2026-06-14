"""Neuro Core scores sidecar (scores.json).

This module manages the mutable score layer for every memory document:
``importance``, ``confidence``, ``stability``, ``access_count`` and
``last_accessed_at``. The values are stored in a flat JSON map keyed by
memory ID, alongside the FAISS index.

Why a sidecar file?
    The FAISS docstore serializes ``Document.metadata`` for every record.
    Updating one score requires rewriting the entire index. The sidecar
    gives the decay / contradiction / reflection jobs (and the future
    ``memory_score`` tool) a cheap, lock-protected write target that does
    not touch the index.

The FAISS metadata still carries a copy of the same fields so that the
``MemoryObject`` and the Memory Dashboard can read them without opening
the sidecar; the sidecar is the authoritative write target.

Concurrency:
    All reads and writes go through a per-subdir ``threading.RLock`` so
    concurrent agents and the background lifecycle jobs do not corrupt
    the file. The lock is acquired with an explicit ``timeout=5.0``
    deadline; a bare ``with self._lock:`` can deadlock if the calling
    thread already holds the lock and an exception prevents release.
    The ``_locked(timeout=...)`` context manager below centralises the
    acquire/try/finally/release pattern so every critical section has
    consistent deadlock protection.

Persistence:
    Atomic writes — the new JSON is written to a temp file in the same
    directory and renamed with ``os.replace``.

Schema (flat map keyed by memory ID):

    {
        "memory_id_1": {
            "importance": 0.8,
            "confidence": 0.7,
            "stability": 0.5,
            "access_count": 3,
            "last_accessed_at": "2026-06-05T12:34:56+00:00"
        },
        ...
    }
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional


# ---------------------------------------------------------------------------
# Lock timeout configuration
# ---------------------------------------------------------------------------

# All ScoreStore RLocks are acquired with this deadline. If a thread cannot
# acquire the lock within 5 seconds we raise TimeoutError rather than
# blocking indefinitely, which prevents deadlock when a caller already
# holds the lock and an exception prevents release.
_LOCK_TIMEOUT_SECONDS = 5.0


# ---------------------------------------------------------------------------
# MemoryScores dataclass
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MemoryScores:
    """Mutable score record for a single memory document."""

    importance: float = 0.5
    confidence: float = 0.7
    stability: float = 0.5
    access_count: int = 0
    last_accessed_at: Optional[str] = None

    def __post_init__(self) -> None:
        self.importance = _clamp01(self.importance)
        self.confidence = _clamp01(self.confidence)
        self.stability = _clamp01(self.stability)
        try:
            self.access_count = max(0, int(self.access_count))
        except (TypeError, ValueError):
            self.access_count = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "MemoryScores":
        return cls(
            importance=raw.get("importance", 0.5),
            confidence=raw.get("confidence", 0.7),
            stability=raw.get("stability", 0.5),
            access_count=raw.get("access_count", 0),
            last_accessed_at=raw.get("last_accessed_at"),
        )


def _clamp01(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))


# ---------------------------------------------------------------------------
# ScoreStore — flat JSON map with atomic persistence
# ---------------------------------------------------------------------------


# Per-subdir lock registry (separate from GraphStore but the same pattern).
_LOCKS: dict[str, "threading.RLock"] = {}
_LOCKS_GUARD = threading.Lock()


def _get_lock(memory_subdir: str) -> "threading.RLock":
    if memory_subdir not in _LOCKS:
        with _LOCKS_GUARD:
            if memory_subdir not in _LOCKS:
                _LOCKS[memory_subdir] = threading.RLock()
    return _LOCKS[memory_subdir]


def _scores_path(memory_subdir: str) -> str:
    """Return the on-disk path to ``scores.json`` for a subdir."""
    from plugins._memory.helpers.memory import Memory, abs_db_dir

    return os.path.join(abs_db_dir(memory_subdir), "scores.json")


class ScoreStore:
    """Mutable score record store for a single ``memory_subdir``.

    Stores ``MemoryScores`` instances keyed by memory ID. Reads are cheap
    (full in-memory dict); writes are atomic and serialized through the
    per-subdir ``RLock``.
    """

    def __init__(self, memory_subdir: str) -> None:
        self.memory_subdir = memory_subdir
        self._lock = _get_lock(memory_subdir)
        self._path = _scores_path(memory_subdir)
        self._data: dict[str, dict] = {}
        self._loaded = False

    # ---- Lock helper ------------------------------------------------------

    @contextlib.contextmanager
    def _locked(
        self, timeout: float = _LOCK_TIMEOUT_SECONDS
    ) -> Iterator[None]:
        """Acquire ``self._lock`` with an explicit timeout deadline.

        All critical sections in this class MUST use this helper
        instead of ``with self._lock:``. A bare ``with`` can deadlock
        if the caller already holds the lock and an exception
        prevents release (the finally clause is reached, but a
        subsequent re-acquire in the same call stack blocks
        forever). The timeout acquire raises ``TimeoutError``
        after 5 seconds so the caller can fail fast and the
        scheduler can keep ticking.
        """
        if not self._lock.acquire(timeout=timeout):
            raise TimeoutError(
                f"[neuro_core] ScoreStore lock timed out after {timeout}s"
            )
        try:
            yield
        finally:
            self._lock.release()

    # ---- I/O --------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._locked():
            if self._loaded:
                return
            self._data = self._read_file()
            self._loaded = True

    def _read_file(self) -> dict[str, dict]:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(raw, dict):
            return {}
        # Defensive: drop non-dict entries.
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _atomic_write(self, data: dict[str, dict]) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(self._path) or ".",
            prefix=".scores.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def save(self) -> None:
        """Persist the current dict to disk atomically."""
        with self._locked():
            self._atomic_write(self._data)

    def load(self) -> dict[str, dict]:
        """Re-read the on-disk file (discarding the in-memory cache)."""
        with self._locked():
            self._data = self._read_file()
            self._loaded = True
            return dict(self._data)

    # ---- Accessors --------------------------------------------------------

    def get(self, memory_id: str) -> MemoryScores:
        """Return the ``MemoryScores`` for ``memory_id`` (defaults if absent)."""
        if not memory_id:
            return MemoryScores()
        self._ensure_loaded()
        with self._locked():
            raw = self._data.get(memory_id)
        if raw is None:
            return MemoryScores()
        return MemoryScores.from_dict(raw)

    def set(
        self,
        memory_id: str,
        importance: Optional[float] = None,
        confidence: Optional[float] = None,
        stability: Optional[float] = None,
        access_count: Optional[int] = None,
        last_accessed_at: Optional[str] = None,
    ) -> MemoryScores:
        """Update one or more score fields for ``memory_id``.

        Fields passed as ``None`` are left untouched on the existing record.
        Returns the full updated ``MemoryScores``.
        """
        if not memory_id:
            raise ValueError("memory_id is required")
        self._ensure_loaded()
        with self._locked():
            current = MemoryScores.from_dict(
                self._data.get(memory_id, {})
            )
            if importance is not None:
                current.importance = float(importance)
            if confidence is not None:
                current.confidence = float(confidence)
            if stability is not None:
                current.stability = float(stability)
            if access_count is not None:
                current.access_count = max(0, int(access_count))
            if last_accessed_at is not None:
                current.last_accessed_at = str(last_accessed_at)
            # Re-clamp in __post_init__ semantics.
            current = MemoryScores(
                importance=current.importance,
                confidence=current.confidence,
                stability=current.stability,
                access_count=current.access_count,
                last_accessed_at=current.last_accessed_at,
            )
            self._data[memory_id] = current.to_dict()
            self._atomic_write(self._data)
            return current

    def update_access(self, memory_id: str) -> MemoryScores:
        """Increment the access counter and stamp ``last_accessed_at``."""
        if not memory_id:
            raise ValueError("memory_id is required")
        self._ensure_loaded()
        with self._locked():
            current = MemoryScores.from_dict(
                self._data.get(memory_id, {})
            )
            current.access_count = current.access_count + 1
            current.last_accessed_at = _now_iso()
            self._data[memory_id] = current.to_dict()
            self._atomic_write(self._data)
            return current

    def forget(self, memory_id: str) -> bool:
        """Remove the score record for ``memory_id`` (used on cascade delete).

        Returns ``True`` if a record was removed, ``False`` otherwise.
        """
        if not memory_id:
            return False
        self._ensure_loaded()
        with self._locked():
            if memory_id in self._data:
                del self._data[memory_id]
                self._atomic_write(self._data)
                return True
            return False

    def all_ids(self) -> list[str]:
        """Return every memory ID currently tracked by this store."""
        self._ensure_loaded()
        with self._locked():
            return list(self._data.keys())

    def __len__(self) -> int:
        self._ensure_loaded()
        with self._locked():
            return len(self._data)
