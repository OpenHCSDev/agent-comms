"""The attached transport owns controller validity and permission delivery."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, ClassVar

from pydantic import BaseModel

from agent_comms.coordination.errors import IdentityConflict
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    AcpSessionId,
    FieldCodec,
    NominalSpelling,
    NominalText,
    WireName,
)

if TYPE_CHECKING:
    from acp.schema import PermissionOption, ToolCallUpdate

    from agent_comms.execution.acp import AcpSelectedWriteAuthority
    from agent_comms.execution.turn_runner import TurnRunner
    from agent_comms.transport.runtime_server import RuntimeServer


class PermissionOptionId(NominalText):
    """ACP's identity for one option of a permission request, spelled as the request offered it."""


class PermissionOptionIdText(NominalSpelling[PermissionOptionId]):
    spelling = re.compile(r"\S{1,256}")


class ChoiceRequest[Choice](ABC):
    """A request a permission outcome answers: the choice for a selected option, or its
    cancellation. The outcome picks which, so its reader never inspects it."""

    @abstractmethod
    def choice(self, option_id: PermissionOptionId) -> Choice: ...

    @abstractmethod
    def cancelled(self) -> Choice: ...


@dataclass(frozen=True)
class PermissionOutcome(DeclaredFamily, affix="Outcome"):
    """A controller's answer to one permission request, as ACP's wire carries it."""

    family_discriminator: ClassVar[str] = "outcome"

    @classmethod
    def from_acp(cls, outcome: BaseModel) -> PermissionOutcome:
        """An ACP client's validated outcome, without the _meta extension this answer
        never reads."""
        return FieldCodec.decode(cls, outcome.model_dump(
            by_alias=True, exclude_none=True, exclude={"field_meta"}))

    @abstractmethod
    def answer[Choice](self, request: ChoiceRequest[Choice]) -> Choice: ...


@dataclass(frozen=True)
class CancelledOutcome(PermissionOutcome):
    """No option chosen: what every answer without a choosing controller carries."""

    def answer[Choice](self, request: ChoiceRequest[Choice]) -> Choice:
        return request.cancelled()


@dataclass(frozen=True)
class SelectedOutcome(PermissionOutcome):
    option_id: Annotated[PermissionOptionId, WireName("optionId")]

    def answer[Choice](self, request: ChoiceRequest[Choice]) -> Choice:
        return request.choice(self.option_id)


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
    ) -> PermissionOutcome:
        """The controller's outcome; a request nobody answers is cancelled."""

    @abstractmethod
    def departed(self, runtime: RuntimeServer, session_id: AcpSessionId) -> bool: ...


class UncontrolledController(Controller):
    """No transport answers for this turn, so every UI permission is cancelled."""

    async def request_ui_permission(
        self, runner: TurnRunner, session_id: AcpSessionId,
        request: tuple[ToolCallUpdate, list[PermissionOption]],
    ) -> PermissionOutcome:
        return CancelledOutcome()


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
        return {"outcome": FieldCodec.encode(CancelledOutcome())}


UNBOUND_CONTROLLER = UnboundController()


NO_CONTROLLER = NoController()
