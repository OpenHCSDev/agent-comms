"""The attached transport owns controller validity and permission delivery."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel

from .coordination_errors import IdentityConflict
from .nominal.field_codec import AcpSessionId

if TYPE_CHECKING:
    from acp.schema import PermissionOption, ToolCallUpdate

    from .runtime_server import RuntimeServer
    from agent_comms.acp import AcpSelectedWriteAuthority
    from .turn_runner import TurnRunner

ACP_PERMISSION_TIMEOUT_SECONDS = 14.0


class SessionUpdateSink(ABC):
    @abstractmethod
    async def publish_update(self, *, session_id: AcpSessionId, update: BaseModel) -> bool:
        """True when someone accepted the update."""


class Controller(ABC):
    # Only the unbound default yields to an ACP client that a direct prompt brings.
    binds_client: ClassVar[bool] = False

    def attached(self, client: Controller) -> Controller:
        return self

    def require_present(self, refusal: Exception) -> Controller:
        """A turn that needs its attached controller has one."""
        return self

    @abstractmethod
    def require_current(self, authority: AcpSelectedWriteAuthority, session_id: AcpSessionId) -> None: ...

    @abstractmethod
    async def request_ui_permission(
        self, runner: TurnRunner, session_id: AcpSessionId,
        request: tuple[ToolCallUpdate, list[PermissionOption]],
    ) -> dict[str, object] | None: ...

    @abstractmethod
    def departed(self, runtime: RuntimeServer, session_id: AcpSessionId) -> bool: ...




class UnboundController(Controller):
    """No prompt has selected a transport; ingress may still bind its ACP client."""

    binds_client = True

    def attached(self, client: Controller) -> Controller:
        return client

    def require_current(self, authority: AcpSelectedWriteAuthority, session_id: AcpSessionId) -> None:
        raise IdentityConflict("Selected write ACP controller changed")

    async def request_ui_permission(
        self, runner: TurnRunner, session_id: AcpSessionId,
        request: tuple[ToolCallUpdate, list[PermissionOption]],
    ) -> dict[str, object] | None:
        return None

    def departed(self, runtime: RuntimeServer, session_id: AcpSessionId) -> bool:
        return False


class NoController(Controller):
    """This turn has no controller: autonomous, channel and goal turns, and owner-socket
    prompts without a live subscriber. Nobody can answer a permission for it."""

    def require_present(self, refusal: Exception) -> Controller:
        raise refusal

    def require_current(self, authority: AcpSelectedWriteAuthority, session_id: AcpSessionId) -> None:
        raise IdentityConflict("Selected write has no ACP controller")

    async def request_ui_permission(
        self, runner: TurnRunner, session_id: AcpSessionId,
        request: tuple[ToolCallUpdate, list[PermissionOption]],
    ) -> dict[str, object] | None:
        return None

    def departed(self, runtime: RuntimeServer, session_id: AcpSessionId) -> bool:
        return True


UNBOUND_CONTROLLER = UnboundController()
NO_CONTROLLER = NoController()
