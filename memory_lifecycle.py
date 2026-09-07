"""Validation lifecycle rules for memory governance."""
from enum import StrEnum


class ValidationState(StrEnum):
    UNREVIEWED = "unreviewed"
    VALIDATED = "validated"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"


_ALLOWED = {
    ValidationState.UNREVIEWED: {ValidationState.VALIDATED, ValidationState.DISPUTED, ValidationState.SUPERSEDED},
    ValidationState.VALIDATED: {ValidationState.DISPUTED, ValidationState.SUPERSEDED},
    ValidationState.DISPUTED: {ValidationState.VALIDATED, ValidationState.SUPERSEDED},
    ValidationState.SUPERSEDED: set(),
}


def transition(current: ValidationState, target: ValidationState) -> ValidationState:
    if target not in _ALLOWED[current]:
        raise ValueError(f"invalid lifecycle transition: {current} -> {target}")
    return target


def retrievable(state: ValidationState) -> bool:
    return state is not ValidationState.SUPERSEDED
