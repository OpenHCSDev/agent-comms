"""Turn failure precedence, presentation and uncertain delivery in one owner."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .diagnostics import FailureReason

if TYPE_CHECKING:
    from .backend import TurnSession
    from .turn_output import TurnOutput


@dataclass(frozen=True)
class TurnFailure(DeclaredFamily):
    text: str
    code: ClassVar[str | None] = None
    precedence: ClassVar[int] = 0
    input_uncertain: ClassVar[bool] = False

    def supersedes(self, previous: TurnFailure | None) -> bool:
        return previous is None or self.precedence >= previous.precedence


class InputIdUnavailable(TurnFailure):
    code = FailureReason.INPUT_ID_UNAVAILABLE
    precedence = 100


class PrestartCompactionFailed(TurnFailure):
    code = FailureReason.COMPACTION_FAILED
    precedence = 90
    input_uncertain = True


class IdentityUncertain(TurnFailure):
    code = FailureReason.IDENTITY_UNCERTAIN
    precedence = 80
    input_uncertain = True


class AuthorityChanged(TurnFailure):
    code = FailureReason.AUTHORITY_CHANGED
    precedence = 70
    input_uncertain = True


class FollowupUnrecognized(TurnFailure):
    code = FailureReason.FOLLOWUP_UNRECOGNIZED
    precedence = 60
    input_uncertain = True


class TerminalFailure(TurnFailure):
    """Missing terminal evidence, discovered from the same declaration as its verdict."""

    default_text: ClassVar[str]

    @classmethod
    @abstractmethod
    def detected(cls, session: TurnSession, transport_ok: bool) -> bool: ...

    @classmethod
    def explanation(cls, output: TurnOutput) -> str:
        return output.failure_text or cls.default_text


class InputMissing(TerminalFailure):
    code = FailureReason.INPUT_MISSING
    precedence = 50
    input_uncertain = True
    default_text = "Pi RPC run ended without this prompt's user message start."

    @classmethod
    def detected(cls, session: TurnSession, transport_ok: bool) -> bool:
        return not session.admission.started and bool(
            transport_ok or session.inputs.uncertain or session.output.failure_text
        )


class FinalStopMissing(TerminalFailure):
    code = FailureReason.FINAL_STOP_MISSING
    precedence = 40
    input_uncertain = True
    default_text = "Pi RPC run ended without an authoritative final assistant stop."

    @classmethod
    def detected(cls, session: TurnSession, transport_ok: bool) -> bool:
        return transport_ok and not session.output.final_assistant_stop

    @classmethod
    def explanation(cls, output: TurnOutput) -> str:
        return output.failure_text or output.error_message or cls.default_text


class QueuedInputMissing(TerminalFailure):
    code = FailureReason.QUEUED_INPUT_MISSING
    precedence = 30
    input_uncertain = True
    default_text = "Pi RPC run ended with an unstarted queued input; delivery is uncertain."

    @classmethod
    def detected(cls, session: TurnSession, transport_ok: bool) -> bool:
        return transport_ok and session.output.error_message is None and session.inputs.unresolved


class ModelStalled(TurnFailure):
    input_uncertain = True


class PromptSendFailed(TurnFailure):
    pass


class ExtensionUiFailed(TurnFailure):
    pass


class BackendDidNotExit(TurnFailure):
    pass
