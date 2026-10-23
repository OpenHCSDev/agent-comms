from __future__ import annotations

from dataclasses import dataclass
from typing_extensions import TypeForm

from agent_comms.installer.carry import Carry, CarryOperation
from agent_comms.installer.interpreter_operation import RuntimeProbe


@dataclass(frozen=True)
class CarryProbe[Answer](RuntimeProbe[tuple[Answer, ...]]):
    """One pass of steps, in order, answered by the staged runtime's worker; the
    operation owns traversal and declares the answer."""

    operation: CarryOperation
    steps: tuple[Carry, ...]

    def answer_type(self) -> TypeForm[tuple[Answer, ...]]:
        return self.operation.answer_type()

    def run(self) -> tuple[Answer, ...]:
        return self.operation.run(self.steps)

