"""Pi spellings decoded once; declarations own their effects in every consumer."""

from __future__ import annotations

import asyncio

from .declared_family import DeclaredFamily


class PiStopReason(DeclaredFamily, affix="StopReason"):
    failed = False
    successful = False
    tool_round = False
    explicit_abort = False
    tracked_failure = "Provider returned an unsuccessful terminal"

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
    def tracked(cls, session, message):
        session.fail_terminal(message.error_message or cls.tracked_failure)


class PendingStopReason(PiStopReason):
    pass


class StopStopReason(PiStopReason):
    successful = True

    @classmethod
    def tracked(cls, session, message):
        session.accept_final_message(message)


class LengthStopReason(PiStopReason):
    tracked_failure = "Model output limit reached"


class ToolUseStopReason(PiStopReason, declared_name="toolUse"):
    tool_round = True

    @classmethod
    def tracked(cls, session, message):
        if not session.accept_tool_round(message):
            return super().tracked(session, message)


class ErrorStopReason(PiStopReason):
    failed = True

    @classmethod
    def permits_progress(cls):
        return False

    @classmethod
    async def apply(cls, session, message):
        if session.usage.provisional:
            session.usage.used = session.usage.confirmed
            session.usage.provisional = False
            yield session.context_info()
        error = session.output.error(
            (message.error_message or "").strip() or f"Model request {cls.declared_name}",
            message.diagnostics,
        )
        if not (session.explicit_interrupt and cls.explicit_abort):
            from .turn_failure import ModelRequestFailed

            session.output.record_failure(ModelRequestFailed(error.text))
            yield error


class AbortedStopReason(ErrorStopReason):
    explicit_abort = True


class DeferredStopReason(PiStopReason):
    pass


class UnreportedStopReason(PiStopReason):
    pass


class CompactionReason(DeclaredFamily, affix="CompactionReason"):
    triggers = True

    @classmethod
    def prepare(cls, preparation):
        return preparation

    @classmethod
    def require_prepared(cls, result):
        result.require_prepared()

    @classmethod
    async def boundary_current(cls, retained, boundary, owner, registry):
        return True

    @classmethod
    def declined_manual(cls, data, journal, settle_refusal):
        journal.summaries.refuse(data.operation_id, data.reason)
        raise ValueError(f"Selected Pi declined manual summary ({data.reason})")

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
    triggers = False


class UnneededCompactionReason(CompactionReason):
    triggers = False


class TaskBoundaryCompactionReason(CompactionReason):
    @classmethod
    def declined_manual(cls, data, journal, settle_refusal):
        from .compaction_result import RefusedCompactionResult

        data.require_clean_prestart()
        settle_refusal(data)
        return RefusedCompactionResult(f"Optional subtask compaction skipped: {data.reason}")

    @classmethod
    async def boundary_current(cls, retained, boundary, owner, registry):
        # Only authored-task timing needs this read. Read and resolve its scope
        # together off the owner loop, through the original registry resource.
        return bool(boundary) and await asyncio.to_thread(
            lambda: retained.optional_boundary(owner, registry.snapshot()) == boundary
        )

    @classmethod
    def prepare(cls, preparation):
        return preparation.at_complete_boundary()

    @classmethod
    def require_prepared(cls, result):
        # A clean optional refusal keeps original context. UNKNOWN/transport
        # failures raise before a result and never become permission to retry.
        pass


class ThinkingLevel(DeclaredFamily, affix="ThinkingLevel"):
    selected_fresh = False
    ordinary_choice = True

    @classmethod
    def supports_selected(cls, value):
        try:
            return cls.decode(value).selected_fresh
        except ValueError:
            return False

    @classmethod
    def selected_names(cls):
        return tuple(
            member.declared_name for member in cls.members_with(cls) if member.selected_fresh
        )

    @classmethod
    def field_value(cls, value):
        if isinstance(value, type) and issubclass(value, cls):
            return value
        from .field_codec import FieldCodec

        return FieldCodec.decode(type[cls], value)

    @classmethod
    def optional_name(cls, level):
        return None if level is None else level.declared_name


class OffThinkingLevel(ThinkingLevel):
    pass


class MinimalThinkingLevel(ThinkingLevel):
    pass


class LowThinkingLevel(ThinkingLevel):
    selected_fresh = True


class MediumThinkingLevel(ThinkingLevel):
    pass


class HighThinkingLevel(ThinkingLevel):
    selected_fresh = True


class XhighThinkingLevel(ThinkingLevel):
    ordinary_choice = False


class MaxThinkingLevel(ThinkingLevel):
    ordinary_choice = False
