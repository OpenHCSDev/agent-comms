"""Turn failure precedence, presentation and uncertain delivery in one owner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .declared_family import DeclaredFamily
from .diagnostics import FailureReason


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


class InputMissing(TurnFailure):
    code = FailureReason.INPUT_MISSING
    precedence = 50
    input_uncertain = True


class FinalStopMissing(TurnFailure):
    code = FailureReason.FINAL_STOP_MISSING
    precedence = 40
    input_uncertain = True


class QueuedInputMissing(TurnFailure):
    code = FailureReason.QUEUED_INPUT_MISSING
    precedence = 30
    input_uncertain = True


class ModelStalled(TurnFailure):
    input_uncertain = True


class PromptSendFailed(TurnFailure):
    pass


class ExtensionUiFailed(TurnFailure):
    pass


class BackendDidNotExit(TurnFailure):
    pass
