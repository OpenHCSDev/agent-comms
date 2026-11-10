"""Session lookup and observation contracts below their lifetime owners."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from acp.schema import PermissionOption, RequestPermissionResponse, ToolCallUpdate
from pydantic import JsonValue

from agent_comms.execution.controller import Controller, SessionUpdateSink
from agent_comms.nominal.field_codec import AcpSessionId


class SessionConfigurationSource[Proxy](ABC):
    """The existing session identities and proxy bindings a configuration reads."""

    proxies: dict[AcpSessionId, Proxy]

    @abstractmethod
    def require(self, session_id: AcpSessionId) -> str: ...

    @abstractmethod
    async def sync_identity(self, session_id: AcpSessionId) -> str: ...


class SessionIdentity(ABC):
    """The original ACP identity retained by a session capability."""

    identifier: AcpSessionId


class OwnedObservation(SessionIdentity):
    """An original owner admits and joins observations until its stop begins."""

    @abstractmethod
    async def observe_live(self, observe: Callable[[], Awaitable[object]]) -> None: ...


class SessionClient(Controller, SessionUpdateSink):
    """The attached client keeps its controller, update and permission operations."""

    @abstractmethod
    async def request_permission(
        self, session_id: str, tool_call: ToolCallUpdate,
        options: list[PermissionOption], **metadata: JsonValue,
    ) -> RequestPermissionResponse: ...
