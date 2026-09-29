"""Pi spellings decoded once; declarations own their effects in every consumer."""

from __future__ import annotations

from .declared_family import DeclaredFamily


class PiStopReason(DeclaredFamily, affix="StopReason"):
    failed = False
    successful = False
    tool_round = False
    explicit_abort = False
    recoverable_terminal = False
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
    async def apply(cls, session, message):
        session.output.error_message = None
        tokens = message.measured_tokens
        if tokens is not None and not session.native.attestation.uncertain:
            session.usage.used = session.usage.confirmed = tokens
            yield session.context_info()
        elif session.usage.provisional:
            session.usage.used = session.usage.confirmed
            yield session.context_info()
        session.usage.provisional = False

    @classmethod
    def tracked(cls, session, message):
        session.terminal_error = cls.tracked_failure


class PendingStopReason(PiStopReason):
    pass


class StopStopReason(PiStopReason):
    successful = True

    @classmethod
    def tracked(cls, session, message):
        from .native_pi import NativePiUnavailable

        if session.tool_socket is not None:
            session.tool_socket.assert_complete()
        if any(not item.final_text_allowed for item in message.content):
            raise NativePiUnavailable("Native Pi assistant returned non-text content")
        session.final_messages.append("".join(item.text for item in message.content))


class LengthStopReason(PiStopReason):
    tracked_failure = "Model output limit reached"


class ToolUseStopReason(PiStopReason, declared_name="toolUse"):
    tool_round = True

    @classmethod
    def tracked(cls, session, message):
        if session.tool_socket is None:
            return super().tracked(session, message)
        session.tool_socket.announce(message.content)
        session.text_parts.clear()
        session.final_messages.clear()


class ErrorStopReason(PiStopReason):
    failed = True
    recoverable_terminal = True

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
            yield error


class AbortedStopReason(ErrorStopReason):
    explicit_abort = True
    recoverable_terminal = False


class DeferredStopReason(PiStopReason):
    pass


class UnreportedStopReason(PiStopReason):
    pass


class CompactionReason(DeclaredFamily, affix="CompactionReason"):
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
