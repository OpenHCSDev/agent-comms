"""The attached transport owns controller validity and permission delivery."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel

from .coordination.errors import IdentityConflict
from .nominal.field_codec import AcpSessionId

CANCELLED_OUTCOME: dict[str, object] = {"outcome": "cancelled"}
"""ACP's denied permission outcome: what every answer without a choosing controller carries."""

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
    ) -> dict[str, object]:
        """The controller's outcome; a request nobody answers is CANCELLED_OUTCOME."""

    @abstractmethod
    def departed(self, runtime: RuntimeServer, session_id: AcpSessionId) -> bool: ...




class UncontrolledController(Controller):
    """No transport answers for this turn, so every UI permission is cancelled."""

    async def request_ui_permission(
        self, runner: TurnRunner, session_id: AcpSessionId,
        request: tuple[ToolCallUpdate, list[PermissionOption]],
    ) -> dict[str, object]:
        return dict(CANCELLED_OUTCOME)


class UnboundController(UncontrolledController):
    """No prompt has selected a transport; ingress may still bind its ACP client."""

    def attached(self, client: Controller) -> Controller:
        return client

    def require_current(self, authority: AcpSelectedWriteAuthority, session_id: AcpSessionId) -> None:
        raise IdentityConflict("Selected write ACP controller changed")

    def departed(self, runtime: RuntimeServer, session_id: AcpSessionId) -> bool:
        return False


class NoController(UncontrolledController, SessionUpdateSink):
    """No controller and no attached ACP client: autonomous, channel and goal turns,
    owner-socket prompts without a live subscriber, and a session before its client
    connects. Nobody accepts its updates or answers a permission for it."""

    def require_present(self, refusal: Exception) -> Controller:
        raise refusal

    def require_current(self, authority: AcpSelectedWriteAuthority, session_id: AcpSessionId) -> None:
        raise IdentityConflict("Selected write has no ACP controller")

    def departed(self, runtime: RuntimeServer, session_id: AcpSessionId) -> bool:
        return True

    async def publish_update(self, *, session_id: AcpSessionId, update: BaseModel) -> bool:
        return False

    async def request_permission(self, **request: object) -> dict[str, object]:
        """With no client, every permission request is cancelled."""
        return {"outcome": dict(CANCELLED_OUTCOME)}


UNBOUND_CONTROLLER = UnboundController()
NO_CONTROLLER = NoController()
