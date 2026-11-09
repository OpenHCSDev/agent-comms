"""The original owner facts read while assembling a turn's context."""

from abc import abstractmethod

from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.thread_text import ThreadParent
from agent_comms.task_scope import TaskScope
from agent_comms.task_scope_owner import TaskScopeOwner


class ContextOwner(TaskScopeOwner[TaskScope]):
    parent: ThreadParent

    @property
    @abstractmethod
    def incarnation(self) -> ThreadIncarnation: ...

    @abstractmethod
    def to_wire(self) -> dict[str, object]: ...
