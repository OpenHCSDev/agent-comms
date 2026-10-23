"""The attached transport owns controller validity and permission delivery."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from acp.agent.connection import AgentSideConnection
from acp.schema import PermissionOption, RequestPermissionResponse, ToolCallUpdate

from .coordination_errors import IdentityConflict

ACP_PERMISSION_TIMEOUT_SECONDS = 14.0


class Controller(ABC):
    @abstractmethod
    def require_current(self, authority, session_id: str) -> None: ...

    @abstractmethod
    async def request_ui_permission(
        self, runner, session_id: str,
        request: tuple[ToolCallUpdate, list[PermissionOption]],
    ) -> dict[str, object] | None: ...

    @abstractmethod
    def departed(self, runtime, session_id: str) -> bool: ...


class AcpController(AgentSideConnection, Controller):
    def require_current(self, authority, session_id: str) -> None:
        if self is not authority.agent.sessions.client:
            raise IdentityConflict("Selected write ACP controller changed")

    async def request_ui_permission(
        self, runner, session_id: str,
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

    def departed(self, runtime, session_id: str) -> bool:
        return False


class UnboundController(Controller):
    """No prompt has selected a transport; ingress may still bind its ACP client."""

    def require_current(self, authority, session_id: str) -> None:
        raise IdentityConflict("Selected write ACP controller changed")

    async def request_ui_permission(
        self, runner, session_id: str,
        request: tuple[ToolCallUpdate, list[PermissionOption]],
    ) -> dict[str, object] | None:
        return None

    def departed(self, runtime, session_id: str) -> bool:
        return False


UNBOUND_CONTROLLER = UnboundController()
