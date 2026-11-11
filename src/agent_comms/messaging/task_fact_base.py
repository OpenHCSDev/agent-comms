"""The task-fact family root, below both messages and retained_task_facts.

ExactTaskFact declares the family and its abstract interface. Its method
annotations reference Message and StoredInput as strings under
``from __future__ import annotations``; no cycle module is imported.
Concrete members stay in retained_task_facts.py.
"""

from __future__ import annotations

from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.registry.task_owner import TaskOwner
from agent_comms.registry.task_registry import TaskRegistry


class ExactTaskFact(DeclaredFamily, affix="TaskFact"):
    """Membership belongs to each determining source, not the packing policy."""

    def authored_sources(self) -> tuple[messages.Message, ...]:
        return ()

    def wire_sources(self) -> tuple[messages.Message, ...]:
        return ()

    def original_sources(self):
        return tuple((source.reference, source) for source in self.wire_sources())

    def input_sources(self) -> tuple[input_attempt.StoredInput, ...]:
        return ()

    def for_tasks(self, current: frozenset[MessageReference]) -> ExactTaskFact:
        return self

    def for_owner(self, owner: TaskOwner, registry: TaskRegistry[TaskOwner]) -> ExactTaskFact:
        return self
