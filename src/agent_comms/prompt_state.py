"""Update native prompt-send state only from events that match the prompt."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Never

from . import turn_failure as failures
from .agent_backend import TurnInboxItem
from .native_pi import NativePiInputNotSent

if TYPE_CHECKING:
    from .backend import NativeSession, TurnSession
    from .pi_events import AgentSettled, InputCommitted, MessageStart, Response


class PromptState(ABC):
    @property
    @abstractmethod
    def acknowledged(self) -> bool: ...

    awaiting_start = False
    started = False
    settled = False
    dispatched = True

    def dispatch(self) -> PromptState:
        return self

    def raise_native_failure(self, error, attestation) -> Never:
        raise error

    def acknowledge(self, response: Response) -> PromptState:
        return self

    def start(self, event: MessageStart | InputCommitted) -> PromptState:
        return self

    def settle(self, event: AgentSettled) -> PromptState:
        return self

    def permits_extension_ui(self, session: TurnSession) -> bool:
        return False

    def permits_retention(self, session: TurnSession) -> bool:
        return False


class UnacknowledgedPrompt(PromptState):
    acknowledged = False

    def acknowledge(self, response):
        return AcknowledgedPrompt(response)


class UnwrittenPrompt(PromptState, TurnInboxItem):
    """The owning send seam has not yet written this prompt."""

    acknowledged = False
    dispatched = False

    def dispatch(self):
        return UnacknowledgedPrompt()

    def refuse(self, inputs, session):
        session.output.record_failure(failures.InputMissing("Original input was cleared before send."))
        session.finish_request()
        session.rejected_signal.set()

    def raise_native_failure(self, error, attestation) -> Never:
        raise NativePiInputNotSent(error, attestation) from error


class PreparedSession(UnwrittenPrompt):
    """An idle-session preparation owns no user prompt."""

    def permits_retention(self, session: NativeSession) -> bool:
        return session.output.clean


@dataclass(frozen=True)
class AcknowledgedPrompt(PromptState):
    response: Response
    acknowledged = True
    awaiting_start = True

    def start(self, event):
        return StartedPrompt(self.response, event)


@dataclass(frozen=True)
class StartedPrompt(AcknowledgedPrompt):
    input_start: MessageStart | InputCommitted
    started = True
    awaiting_start = False

    def permits_extension_ui(self, session):
        return session.native.attestation.allows_extension_input(session.inputs)

    def settle(self, event):
        return SettledPrompt(self.response, self.input_start, event)


@dataclass(frozen=True)
class SettledPrompt(StartedPrompt):
    event: AgentSettled
    settled = True

    def permits_extension_ui(self, session):
        return False

    def permits_retention(self, session):
        return (
            session.inputs.settled
            and session.stats.complete
            and session.output.permits_retention(session)
        )
