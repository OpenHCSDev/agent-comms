"""Original input state and operations consumed by backend input events."""

from abc import ABC, abstractmethod
from collections.abc import Mapping

from agent_comms.execution.started_input import StartedInput
from agent_comms.nominal.field_codec import AcpSessionId, InputId


class InputEventState(ABC):
    """The event reads the existing started sources, without owning their map."""

    following: Mapping[InputId, StartedInput]

    @abstractmethod
    def require_first_source(self) -> StartedInput: ...


class InputEventSource(ABC):
    """The original input owner applies its captured event."""

    @abstractmethod
    def state(self, session_id: AcpSessionId) -> InputEventState: ...

    @abstractmethod
    async def input_refused(self, session_id: AcpSessionId, input_id: InputId) -> None: ...

    @abstractmethod
    async def input_cleared(
        self, session_id: AcpSessionId, input_id: InputId, native_id: InputId, text: str,
    ) -> None: ...
