"""The attached transport owns controller validity and permission delivery."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from acp.agent.connection import AgentSideConnection
from acp.schema import PermissionOption, RequestPermissionResponse, ToolCallUpdate
from pydantic import BaseModel

from .coordination_errors import IdentityConflict

if TYPE_CHECKING:
    from .runtime import RuntimeServer
    from .selected_write_authority import AcpSelectedWriteAuthority
    from .turn_runner import TurnRunner

ACP_PERMISSION_TIMEOUT_SECONDS = 14.0


class SessionUpdateSink(ABC):
    @abstractmethod
    async def publish_update(self, *, session_id: str, update: BaseModel) -> bool:
        """True when someone accepted the update."""


class Controller(ABC):
    @abstractmethod
    def require_current(self, authority: AcpSelectedWriteAuthority, session_id: str) -> None: ...

    @abstractmethod
    async def request_ui_permission(
        self, runner: TurnRunner, session_id: str,
        request: tuple[ToolCallUpdate, list[PermissionOption]],
    ) -> dict[str, object] | None: ...

    @abstractmethod
    def departed(self, runtime: RuntimeServer, session_id: str) -> bool: ...


class AcpController(AgentSideConnection, Controller):
    def require_current(self, authority: AcpSelectedWriteAuthority, session_id: str) -> None:
        if self is not authority.agent.sessions.client:
            raise IdentityConflict("Selected write ACP controller changed")

    async def request_ui_permission(
        self, runner: TurnRunner, session_id: str,
        request: tuple[ToolCallUpdate, list[PermissionOption]],
    ) -> dict[str, object] | None:
        tool_call, options = request
        if self is runner.sessions.client:
            response = await asyncio.wait_for(
                self.request_permission(
                    session_id=session_id, tool_call=tool_call, options=options
                ),
                timeout=ACP_PERMISSION_TIMEOUT_SECONDS,
            )
            return RequestPermissionResponse.model_validate(response).outcome.model_dump(
                by_alias=True, exclude_none=True
            )
        return None

    def departed(self, runtime: RuntimeServer, session_id: str) -> bool:
        return False


class UnboundController(Controller):
    """No prompt has selected a transport; ingress may still bind its ACP client."""

    def require_current(self, authority: AcpSelectedWriteAuthority, session_id: str) -> None:
        raise IdentityConflict("Selected write ACP controller changed")

    async def request_ui_permission(
        self, runner: TurnRunner, session_id: str,
        request: tuple[ToolCallUpdate, list[PermissionOption]],
    ) -> dict[str, object] | None:
        return None

    def departed(self, runtime: RuntimeServer, session_id: str) -> bool:
        return False


UNBOUND_CONTROLLER = UnboundController()
