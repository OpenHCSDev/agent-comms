"""Update native prompt-send state only from events that match the prompt."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Never

from . import turn_failure as failures
from .agent_backend import TurnInboxItem
from .native_pi import NativePiInputNotSent
from .native_input_proof import NativeInputCommit

if TYPE_CHECKING:
    from .backend import NativeSession
    from .pi_events import AgentSettled, MessageStart, Response


class PromptState:
    acknowledged = False
    awaiting_start = False
    started = False

    def dispatch(self) -> PromptState:
        return self

    def raise_native_failure(self, error, attestation) -> Never:
        raise error

    def acknowledge(self, response: Response) -> PromptState:
        return self

    def start(self, event: MessageStart | NativeInputCommit) -> PromptState:
        return self

    def settle(self, event: AgentSettled) -> PromptState:
        return self

    def validate_terminal(self, session: NativeSession, transport_ok: bool) -> None:
        """A user prompt owes its declared input, answer and queue evidence."""
        for cause in failures.TerminalFailure.members_with(failures.TerminalFailure):
            if cause.detected(session, transport_ok):
                session.output.record_failure(cause(cause.explanation(session.output)))

    def permits_extension_ui(self, session: NativeSession) -> bool:
        return False

    def permits_retention(self, session: NativeSession) -> bool:
        return False


class UnacknowledgedPrompt(PromptState):
    def acknowledge(self, response):
        return AcknowledgedPrompt(response)


class QueuedOperation(PromptState, TurnInboxItem):
    """An initial native operation can be placed in the turn inbox."""


class UnwrittenPrompt(QueuedOperation):
    """The owning send seam has not yet written this prompt."""

    def dispatch(self):
        return UnacknowledgedPrompt()

    def refuse(self, inputs, session):
        session.output.record_failure(failures.InputMissing("Original input was cleared before send."))
        session.finish_request()
        session.rejected_signal.set()

    def raise_native_failure(self, error, attestation) -> Never:
        raise NativePiInputNotSent(error, attestation) from error


class PreparedSession(QueuedOperation):
    """An idle-session preparation owns metadata, not an unwritten user prompt."""

    def validate_terminal(self, session: NativeSession, transport_ok: bool) -> None:
        """Preparation owes metadata replies, not a user input or assistant answer."""

    def permits_retention(self, session: NativeSession) -> bool:
        return session.stats.complete and session.output.clean


@dataclass(frozen=True)
class AcknowledgedPrompt(PromptState):
    response: Response
    acknowledged = True
    awaiting_start = True

    def start(self, event):
        return StartedPrompt(self.response, event)


@dataclass(frozen=True)
class StartedPrompt(AcknowledgedPrompt):
    input_start: MessageStart | NativeInputCommit
    started = True
    awaiting_start = False

    def permits_extension_ui(self, session):
        return session.native.attestation.allows_extension_input(session.inputs)

    def settle(self, event):
        return SettledPrompt(self.response, self.input_start, event)


@dataclass(frozen=True)
class SettledPrompt(StartedPrompt):
    event: AgentSettled

    def permits_extension_ui(self, session):
        return False

    def permits_retention(self, session):
        return (
            session.inputs.settled
            and session.stats.complete
            and session.output.permits_retention(session)
        )
