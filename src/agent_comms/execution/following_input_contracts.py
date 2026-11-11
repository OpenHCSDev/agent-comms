"""Following-input contracts below the turn runner and its input owners."""

from abc import ABC, abstractmethod

from agent_comms.execution.native_prompt_send import PromptBoundary
from agent_comms.execution.pi_commands import Prompt
from agent_comms.execution.turn_inbox import TurnInbox
from agent_comms.nominal.field_codec import AcpSessionId


class FollowingInputRetirement(ABC):
    """The existing inbox owner hands unread acceptances on after its turn ends."""

    session_id: AcpSessionId
    inbox: TurnInbox

    @abstractmethod
    async def pass_on_unread(self) -> None: ...


class FollowingPromptBoundary(PromptBoundary):
    """A selected prompt identifies sources already consumed by its first input."""

    @property
    @abstractmethod
    def following_after_seq(self) -> int: ...


class FollowingTurnSource[Sender](ABC):
    """The connected selected turn that admits its original following sender."""

    command: Prompt
    prompt_send_boundary: FollowingPromptBoundary

    @abstractmethod
    def forward_inbox(self, inbox: TurnInbox, sender: Sender) -> None: ...
