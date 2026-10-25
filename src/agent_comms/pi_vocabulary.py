"""Pi spellings decoded once; declarations own their effects in every consumer."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

from .declared_family import DeclaredFamily
from .field_codec import (
    AcceptsNull,
    DefaultExternalName,
    ExternalName,
    FieldCodec,
    FieldRepresentation,
)
from .lifecycle_capability import Failed, LifecycleCapability, Successful
from .pi_counts import PiCount, UnreportedCount

if TYPE_CHECKING:
    import sqlite3

    from .attempt_states import UnsuccessfulTerminal
    from .backend import TurnSession
    from .coordination_tables.attempts import ReplayFact
    from .coordination_tables.executions import ExecutionRecord


class PiStopReason(LifecycleCapability, ExternalName, affix="StopReason"):
    tool_round = False
    explicit_abort = False
    tracked_failure = "Provider returned an unsuccessful terminal"

    @classmethod
    def error_notice(cls, message) -> str:
        return ""

    @classmethod
    def measured(cls, total: PiCount) -> PiCount:
        """The context usage a message ending this way measures with its reported total."""
        return total

    @classmethod
    def recovery_outcome(cls) -> UnsuccessfulTerminal:
        from .attempt_states import UnknownOutcomeAttempt

        return UnknownOutcomeAttempt()

    @classmethod
    def recovery_facts(cls) -> ReplayFact:
        from .coordination_tables.attempts import ReplayFact

        return ReplayFact.NONE

    @classmethod
    def assess_recovery_replay(cls, db: sqlite3.Connection, execution: ExecutionRecord) -> None:
        """A terminal alone supplies no new permission to replay."""

    @classmethod
    def interrupted(cls, session: TurnSession) -> bool:
        """An abort the user asked for by sending now: the request was replaced, not failed."""
        return session.explicit_interrupt and cls.explicit_abort

    @classmethod
    async def apply(cls, session, message):
        session.output.error_message = None
        for event in message.measured_tokens.settle(session):
            yield event
        session.usage.provisional = False

    @classmethod
    def declared_failure(cls, message):
        from .turn_failure import ModelRequestFailed

        text = message.error_message.value_or("").strip() or cls.tracked_failure
        return ModelRequestFailed.from_diagnostics(text, message.diagnostics)

    @classmethod
    def require_failed_terminal(cls) -> None:
        # A saved tool round cannot establish whether the live owner accepted it.
        if cls.items_with(Successful, cls):
            raise ValueError("Saved assistant message did not end its turn as a failure")


class UnfinishedPiStop:
    """A message that ended without a terminal its session can take as the answer."""

    @classmethod
    async def apply(cls, session, message):
        session.accept_unfinished_message(cls.declared_failure(message))
        async for event in super().apply(session, message):
            yield event


class PendingStopReason(UnfinishedPiStop, PiStopReason):
    pass


class FailedUnpublishedPiStop:
    """Recovery knows this attempt failed, independently of its output facts."""

    @classmethod
    def recovery_outcome(cls) -> UnsuccessfulTerminal:
        from .attempt_states import AttemptFailedAttempt

        return AttemptFailedAttempt()


class StoppedBeforeGrantStopReason(FailedUnpublishedPiStop, UnfinishedPiStop, PiStopReason):
    """The stopped owner never received permission to write this input."""

    @classmethod
    def assess_recovery_replay(cls, db: sqlite3.Connection, execution: ExecutionRecord) -> None:
        from .coordination_tables.executions import ReplayAssessments

        # A grant was required before the writer could send any bytes. Existing
        # effects from earlier attempts still prevent authorization.
        ReplayAssessments.authorize_if_unobserved(db, execution.execution_id)


class FinishedPiStop(FailedUnpublishedPiStop):
    @classmethod
    def recovery_facts(cls) -> ReplayFact:
        from .coordination_tables.attempts import ReplayFact

        # The native turn finished, but recovery cannot invent a delivered reply.
        return ReplayFact.ASSISTANT_OUTPUT


class StopStopReason(Successful, FinishedPiStop, PiStopReason):

    @classmethod
    async def apply(cls, session, message):
        session.accept_final_message(message)
        async for event in super().apply(session, message):
            yield event


class FailedPiStop(Failed, FinishedPiStop):
    """An explicit failed terminal records the same failure on every live path."""

    @classmethod
    def error_notice(cls, message) -> str:
        return message.error_message.value_or("").strip()

    @classmethod
    async def apply(cls, session, message):
        session.output.record_native_failure(message)
        if not cls.interrupted(session):
            session.output.record_failure(cls.declared_failure(message))
        async for event in super().apply(session, message):
            yield event


class LengthStopReason(FailedPiStop, PiStopReason):
    tracked_failure = "Model output limit reached"


class ToolUseStopReason(PiStopReason, declared_name="toolUse"):
    tool_round = True

    @classmethod
    def require_failed_terminal(cls):
        # A saved tool round cannot establish whether the live owner accepted it.
        raise ValueError("Saved assistant message did not end its turn as a failure")

    @classmethod
    async def apply(cls, session, message):
        session.accept_tool_round(message)
        async for event in super().apply(session, message):
            yield event


class ErrorStopReason(FailedPiStop, PiStopReason):
    @classmethod
    def measured(cls, total: PiCount) -> PiCount:
        # Pi measures context from its last message that neither errored nor
        # aborted; a failed request's billed total is not the session's context.
        return UnreportedCount()

    @classmethod
    async def apply(cls, session, message):
        async for event in super().apply(session, message):
            yield event
        if not cls.interrupted(session) and session.watchdog.phase.holds_refusal_reports:
            # A refusal the recovery is re-requesting is interim evidence; the
            # recovery's own end event reports the final outcome.
            return
        for diagnostic in message.diagnostics:
            session.watchdog.phase = diagnostic.turn_phase(session.watchdog.phase)
        error = session.output.error(
            message.error_message.value_or("").strip() or f"Model request {cls.declared_name}",
            message.diagnostics, source=session.output.native_failure_source,
        )
        if not cls.interrupted(session):
            yield error


class AbortedStopReason(ErrorStopReason):
    explicit_abort = True


class DeferredStopReason(UnfinishedPiStop, PiStopReason):
    pass


class UnreportedStopReason(DefaultExternalName, UnfinishedPiStop, PiStopReason):
    pass


class CompactionReason(ExternalName, affix="CompactionReason"):
    """Why Pi's own engine compacted: Pi's reason names, decoded once at ingress."""


class ManualCompactionReason(CompactionReason):
    pass


class OverflowCompactionReason(CompactionReason):
    pass


class ThresholdCompactionReason(CompactionReason):
    pass


class UnknownCompactionReason(DefaultExternalName, CompactionReason):
    pass


class ServiceTier(StrEnum):
    """Provider request service selected with a thread's model configuration."""

    STANDARD = "standard"
    PRIORITY = "priority"


class ThinkingChoice(ABC):
    @classmethod
    @abstractmethod
    def native_arguments[T](cls, argument: Callable[[type[ThinkingLevel]], T]) -> tuple[T, ...]: ...

    @classmethod
    def applied(cls, current: type[ThinkingChoice]) -> type[ThinkingChoice]:
        return cls

    @classmethod
    def observed(cls, level: type[ThinkingChoice]) -> type[ThinkingChoice]:
        return cls

    @classmethod
    @abstractmethod
    def wire_value(cls) -> str | None:
        raise NotImplementedError("Abstract thinking choices have no wire value.")

    @classmethod
    @abstractmethod
    def require_reported(cls, refusal: Exception) -> type[ThinkingLevel]:
        """The level a native report named; no override raises ``refusal``."""


class ThinkingLevel(ThinkingChoice, DeclaredFamily, affix="ThinkingLevel"):
    ordinary_choice = True

    @classmethod
    def native_arguments[T](cls, argument: Callable[[type[ThinkingLevel]], T]) -> tuple[T, ...]:
        return (argument(cls),)

    @classmethod
    def wire_value(cls) -> str:
        return cls.declared_name

    @classmethod
    def require_reported(cls, refusal: Exception) -> type[ThinkingLevel]:
        return cls


class NoThinkingOverride(ThinkingChoice):
    @classmethod
    def native_arguments[T](cls, argument: Callable[[type[ThinkingLevel]], T]) -> tuple[T, ...]:
        return ()

    @classmethod
    def applied(cls, current: type[ThinkingChoice]) -> type[ThinkingChoice]:
        return current

    @classmethod
    def observed(cls, level: type[ThinkingChoice]) -> type[ThinkingChoice]:
        return level

    @classmethod
    def wire_value(cls) -> None:
        return None

    @classmethod
    def require_reported(cls, refusal: Exception) -> type[ThinkingLevel]:
        raise refusal


class ThinkingLevelValue(AcceptsNull, FieldRepresentation):

    @classmethod
    def decode(cls, value: object) -> type[ThinkingChoice]:
        if value is None:
            return NoThinkingOverride
        return ThinkingLevel.member_named(FieldCodec.decode(str, value))

    @classmethod
    def encode(cls, value: object) -> str | None:
        if isinstance(value, type) and issubclass(value, ThinkingChoice):
            return value.wire_value()
        raise ValueError("Thinking level requires a thinking choice declaration.")

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(type[ThinkingLevel] | None)


ThinkingLevelField = Annotated[type[ThinkingChoice], ThinkingLevelValue]


class OffThinkingLevel(ThinkingLevel):
    pass


class MinimalThinkingLevel(ThinkingLevel):
    pass


class LowThinkingLevel(ThinkingLevel):
    pass


class MediumThinkingLevel(ThinkingLevel):
    pass


class HighThinkingLevel(ThinkingLevel):
    pass


class XhighThinkingLevel(ThinkingLevel):
    ordinary_choice = False


class MaxThinkingLevel(ThinkingLevel):
    ordinary_choice = False
