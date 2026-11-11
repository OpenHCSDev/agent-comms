"""The queue revision and publication operations its input owner supplies."""

from abc import ABC, abstractmethod

from agent_comms.execution.controller import SessionUpdateSink
from agent_comms.nominal.field_codec import AcpSessionId


class QueuePublication(ABC):
    """Queue consumers use the original input owner's revision and publisher."""

    @abstractmethod
    def next_revision(self, session_id: AcpSessionId) -> int: ...

    @abstractmethod
    async def emit_queue_state(
        self, session_id: AcpSessionId, *, client: SessionUpdateSink | None = None,
    ) -> None: ...
