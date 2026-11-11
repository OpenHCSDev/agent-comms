"""The original owner facts read while assembling a turn's context."""

from abc import abstractmethod

from agent_comms.planning.task_scope import TaskScope
from agent_comms.registry.task_scope_owner import TaskScopeOwner
from agent_comms.registry.thread_identity_source import ThreadIdentitySource
from agent_comms.registry.thread_text import ThreadParent


class ContextOwner(TaskScopeOwner[TaskScope], ThreadIdentitySource):
    parent: ThreadParent

    @abstractmethod
    def to_wire(self) -> dict[str, object]: ...
