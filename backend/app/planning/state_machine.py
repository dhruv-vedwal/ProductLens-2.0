"""Provider-neutral state transitions for validated workflow execution.

The browser adapter performs operations; this module owns the generic lifecycle
invariant that a workflow may only advance after its postconditions are proven.
It deliberately has no product or route names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.contracts.models import SemanticOperation


class InvalidTransition(ValueError):
    """Raised when a workflow attempts to skip an unverified state."""


@dataclass(frozen=True)
class StateTransition:
    source: str
    event: str
    target: str
    required_postconditions: tuple[str, ...] = ()


@dataclass
class WorkflowStateMachine:
    initial: str = "NEW"
    transitions: list[StateTransition] = field(default_factory=list)
    current: str = field(init=False)
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.current = self.initial

    def transition(self, event: str, *, verified: bool = False) -> str:
        matches = [
            item for item in self.transitions if item.source == self.current and item.event == event
        ]
        if not matches:
            raise InvalidTransition(f"No transition from {self.current!r} for {event!r}")
        rule = matches[0]
        if rule.required_postconditions and not verified:
            raise InvalidTransition(f"Postconditions required before {event!r}")
        previous = self.current
        self.current = rule.target
        self.history.append(
            {"source": previous, "event": event, "target": self.current, "verified": verified}
        )
        return self.current

    def artifact(self) -> dict[str, object]:
        """Return the immutable state contract persisted beside a DemoPlan."""
        return {
            "initial_state": self.initial,
            "transitions": [
                {
                    "source": item.source,
                    "event": item.event,
                    "target": item.target,
                    "required_postconditions": list(item.required_postconditions),
                }
                for item in self.transitions
            ],
            "invariant": "a workflow transition advances only after its required postconditions verify",
        }

    @classmethod
    def from_operations(
        cls, operations: list[SemanticOperation], *, initial: str = "NEW"
    ) -> WorkflowStateMachine:
        """Compile a generic operation sequence into a deterministic state graph."""
        transitions: list[StateTransition] = []
        source = initial
        for index, operation in enumerate(operations):
            target = f"STEP_{index + 1}"
            transitions.append(
                StateTransition(
                    source=source,
                    event=operation.id,
                    target=target,
                    required_postconditions=tuple(
                        condition.kind for condition in operation.postconditions
                    ),
                )
            )
            source = target
        return cls(initial=initial, transitions=transitions)
