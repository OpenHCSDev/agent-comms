"""Pi spellings decoded once; declarations own their effects in every consumer."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, FieldRepresentation
from .native_arguments import ThinkingArgument


class PiStopReason(DeclaredFamily, affix="StopReason"):
    failed = False
    successful = False
    tool_round = False
    explicit_abort = False
    tracked_failure = "Provider returned an unsuccessful terminal"

    @classmethod
    def unpublished_attempt(cls):
        from .attempt_states import UnknownOutcomeAttempt
        from .coordination_tables.attempts import ReplayFact

        return UnknownOutcomeAttempt(), ReplayFact.NONE

    @classmethod
    def from_external(cls, value):
        from .field_codec import FieldCodec

        value = FieldCodec.decode(str | None, value)
        try:
            return cls.decode(value)
        except ValueError:
            return UnreportedStopReason

    @classmethod
    def permits_progress(cls):
        return True

    @classmethod
    async def apply(cls, session, message):
        session.output.error_message = None
        tokens = message.measured_tokens
        if tokens is not None and session.accepts_output:
            session.usage.used = session.usage.confirmed = tokens
            yield session.context_info()
        elif session.usage.provisional:
            session.usage.used = session.usage.confirmed
            yield session.context_info()
        session.usage.provisional = False

    @classmethod
    def declared_failure(cls, message):
        from .turn_failure import ModelRequestFailed

        text = (message.error_message or "").strip() or cls.tracked_failure
        return ModelRequestFailed.from_diagnostics(text, message.diagnostics)

    @classmethod
    def tracked(cls, session, message):
        session.output.record_failure(cls.declared_failure(message))

    @classmethod
    def require_failed_terminal(cls):
        # A saved tool round cannot establish whether the live owner accepted it.
        if cls.successful or cls.tool_round:
            raise ValueError("Saved assistant message did not end its turn as a failure")


class PendingStopReason(PiStopReason):
    pass


class FinishedPiStop:
    @classmethod
    def unpublished_attempt(cls):
        from .attempt_states import AttemptFailedAttempt
        from .coordination_tables.attempts import ReplayFact

        # The native turn finished, but recovery cannot invent a delivered reply.
        return AttemptFailedAttempt(), ReplayFact.ASSISTANT_OUTPUT


class StopStopReason(FinishedPiStop, PiStopReason):
    successful = True

    @classmethod
    def tracked(cls, session, message):
        session.accept_final_message(message)


class FailedPiStop(FinishedPiStop):
    """An explicit failed terminal records the same failure on every live path."""

    failed = True

    @classmethod
    async def apply(cls, session, message):
        session.output.record_failure(cls.declared_failure(message))
        async for event in super().apply(session, message):
            yield event


class LengthStopReason(FailedPiStop, PiStopReason):
    tracked_failure = "Model output limit reached"


class ToolUseStopReason(PiStopReason, declared_name="toolUse"):
    tool_round = True

    @classmethod
    def tracked(cls, session, message):
        if not session.accept_tool_round(message):
            return super().tracked(session, message)


class ErrorStopReason(FailedPiStop, PiStopReason):
    @classmethod
    def permits_progress(cls):
        return False

    @classmethod
    async def apply(cls, session, message):
        async for event in super().apply(session, message):
            yield event
        if not (session.explicit_interrupt and cls.explicit_abort) and (
            session.watchdog.phase.holds_refusal_reports
        ):
            # A refusal the recovery is re-requesting is interim evidence; the
            # recovery's own end event reports the final outcome.
            return
        from .acp_failure import NativeMessageFailureSource

        error = session.output.error(
            (message.error_message or "").strip() or f"Model request {cls.declared_name}",
            message.diagnostics, source=NativeMessageFailureSource(message),
        )
        if not (session.explicit_interrupt and cls.explicit_abort):
            yield error


class AbortedStopReason(ErrorStopReason):
    explicit_abort = True


class DeferredStopReason(PiStopReason):
    pass


class UnreportedStopReason(PiStopReason):
    pass


class CompactionReason(DeclaredFamily, affix="CompactionReason"):
    """Why Pi's own engine compacted: Pi's reason names, decoded once at ingress."""

    @classmethod
    def from_external(cls, value):
        from .field_codec import FieldCodec

        value = FieldCodec.decode(str | None, value)
        try:
            return cls.decode(value)
        except ValueError:
            return UnknownCompactionReason


class ManualCompactionReason(CompactionReason):
    pass


class OverflowCompactionReason(CompactionReason):
    pass


class ThresholdCompactionReason(CompactionReason):
    pass


class UnknownCompactionReason(CompactionReason):
    pass


class ServiceTier(StrEnum):
    """Provider request service selected with a thread's model configuration."""

    STANDARD = "standard"
    PRIORITY = "priority"


SERVICE_TIER_ENVIRONMENT = "AGENT_COMMS_SERVICE_TIER"


class ThinkingLevel(DeclaredFamily, affix="ThinkingLevel"):
    ordinary_choice = True

    @classmethod
    def native_arguments(cls):
        return (ThinkingArgument(FieldCodec.encode(cls)),)

    @classmethod
    def launch_arguments(cls):
        return tuple(token for argument in cls.native_arguments() for token in argument.argv)

    @classmethod
    def applied(cls, current):
        return cls

    @classmethod
    def observed(cls, level):
        return cls


class NoThinkingOverride(ThinkingLevel):
    declared_name = None

    @classmethod
    def native_arguments(cls):
        return ()

    @classmethod
    def applied(cls, current):
        return current

    @classmethod
    def observed(cls, level):
        return level


class ThinkingLevelValue(FieldRepresentation):
    accepts_null = True

    @classmethod
    def decode(cls, value):
        return NoThinkingOverride if value is None else ThinkingLevel.decode(FieldCodec.decode(str, value))

    @classmethod
    def encode(cls, value):
        return FieldCodec.encode(value)

    @classmethod
    def schema(cls):
        return FieldCodec.value_schema(type[ThinkingLevel] | None)


ThinkingLevelField = Annotated[type[ThinkingLevel], ThinkingLevelValue]


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
