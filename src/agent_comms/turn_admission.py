"""Native prompt admission advances only from the corresponding observed events."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import TurnSession
    from .pi_events import AgentSettled, MessageStart, Response


class PromptAdmission(ABC):
    @property
    @abstractmethod
    def acknowledged(self) -> bool: ...

    awaiting_start = False
    started = False
    settled = False

    def acknowledge(self, response: Response) -> PromptAdmission:
        return self

    def start(self, event: MessageStart) -> PromptAdmission:
        return self

    def settle(self, event: AgentSettled) -> PromptAdmission:
        return self

    def permits_extension_ui(self, session: TurnSession) -> bool:
        return False

    def permits_retention(self, session: TurnSession) -> bool:
        return False


class UnacknowledgedPrompt(PromptAdmission):
    acknowledged = False

    def acknowledge(self, response):
        return AcknowledgedPrompt(response)


@dataclass(frozen=True)
class AcknowledgedPrompt(PromptAdmission):
    response: Response
    acknowledged = True
    awaiting_start = True

    def start(self, event):
        return StartedPrompt(self.response, event)


@dataclass(frozen=True)
class StartedPrompt(AcknowledgedPrompt):
    message: MessageStart
    started = True
    awaiting_start = False

    def permits_extension_ui(self, session):
        return session.native.attestation.admits_extension_input(session.inputs)

    def settle(self, event):
        return SettledPrompt(self.response, self.message, event)


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
