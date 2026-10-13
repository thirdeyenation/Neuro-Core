"""Minimal, framework-independent Neuro Core foundation."""
from dataclasses import dataclass, field
from uuid import uuid4
from memory_lifecycle import ValidationState, retrievable

@dataclass(frozen=True)
class Scope:
    project: str
    agent: str | None = None

@dataclass(frozen=True)
class Memory:
    text: str
    source: str
    scope: Scope
    importance: float = 0.5
    confidence: float = 0.5
    validation: ValidationState = ValidationState.UNREVIEWED
    memory_id: str = field(default_factory=lambda: str(uuid4()))

def retrieve(query: str, scope: Scope, memories: list[Memory]) -> list[dict]:
    terms = set(query.lower().split())
    results = []
    for memory in memories:
        if memory.scope != scope or not retrievable(memory.validation):
            continue
        overlap = len(terms & set(memory.text.lower().split())) / max(len(terms), 1)
        score = round(.5 * overlap + .25 * memory.importance + .25 * memory.confidence, 6)
        results.append({"memory": memory, "score": score, "factors": {"overlap": overlap, "importance": memory.importance, "confidence": memory.confidence, "validation": memory.validation}})
    return sorted(results, key=lambda item: item["score"], reverse=True)
