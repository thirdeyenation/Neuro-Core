"""Audit-friendly activity ledger for Neuro Core operations."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from neuro_core import Scope


@dataclass(frozen=True)
class ActivityEvent:
    kind: str
    scope: Scope
    targets: tuple[str, ...]
    outcome: str
    evidence: dict[str, str] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid4()))
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.kind or not self.targets or not self.outcome:
            raise ValueError("kind, targets, and outcome are required")


class ActivityLedger:
    def __init__(self) -> None:
        self._events: list[ActivityEvent] = []

    def append(self, event: ActivityEvent) -> ActivityEvent:
        if any(existing.event_id == event.event_id for existing in self._events):
            raise ValueError("duplicate event id")
        self._events.append(event)
        return event

    def for_scope(self, scope: Scope) -> tuple[ActivityEvent, ...]:
        return tuple(event for event in self._events if event.scope == scope)

    def all(self) -> tuple[ActivityEvent, ...]:
        return tuple(self._events)
