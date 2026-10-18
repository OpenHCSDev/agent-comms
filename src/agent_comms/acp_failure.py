"""Public ACP failure presentation; decode external nesting once, keep provider text.

This is observation only. No failure object grants a retry or changes a ledger.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import ClassVar, TYPE_CHECKING

from acp import RequestError

if TYPE_CHECKING:
    from .agent_events import Done, Error
    from .input_disposition import InputDocument
    from .turn_input_source import OriginalTurnInput

from .declared_family import DeclaredFamily
from .delivery_presentation import DeliveryPresentation
from .input_attempt import InputAttempt
from .pi_payloads import AssistantMessage, PiDiagnostic
from .field_codec import FieldCodec, JsonShapeFamily, JsonShapeMember


class FailureSource(DeclaredFamily, affix="FailureSource"):
    """Native message provenance, distinct from failures without a history source."""

    @property
    @abstractmethod
    def retained_in_history(self) -> bool:
        """Whether feedback belongs to native history, not a local failure notice.

        This declares ownership; it does not claim the journal append has finished.
        """

    def for_publication(self, text: str, sensitive: bool) -> FailureSource:
        return self


@dataclass(frozen=True)
class UnrecordedFailureSource(FailureSource):
    @property
    def retained_in_history(self) -> bool:
        return False


@dataclass(frozen=True)
class NativeMessageFailureSource(FailureSource):
    message: AssistantMessage

    @property
    def retained_in_history(self) -> bool:
        return True

    def for_publication(self, text: str, sensitive: bool) -> FailureSource:
        if not sensitive:
            return self
        # Retain native provenance without exporting private response content.
        return replace(
            self, message=replace(self.message, content=(), diagnostics=(), error_message=text)
        )


class DeliveryFailure(DeliveryPresentation, DeclaredFamily, affix="Failure"):
    @property
    def title(self) -> str:
        return "Delivery unconfirmed"

    @property
    def action(self) -> str:
        return "Inspect the input delivery status before deciding whether to send again."

    @property
    def input_disposition(self) -> str:
        return "Unconfirmed"

    @property
    @abstractmethod
    def description(self) -> str: ...


@dataclass(frozen=True)
class BackendDeliveryFailure(DeliveryFailure):
    message: str

    @property
    def description(self) -> str:
        return self.message


@dataclass(frozen=True, kw_only=True)
class ProviderRetryResult:
    """One decoded native retry end, retained by the turn output and its outcome."""

    success: bool
    attempt: int
    final_error: str | None = field(default=None, metadata={"wire_name": "finalError"})

    def __post_init__(self) -> None:
        if self.attempt < 0:
            raise ValueError("Native retry count must be nonnegative")

    def for_failure(self, redact) -> ProviderRetryResult:
        """Create the one privacy-safe result stored by this turn's output."""
        return ProviderRetryResult(
            success=self.success, attempt=self.attempt,
            final_error=redact(self.final_error) if self.final_error else None,
        )

    def refine_failure(self, failure):
        return failure if self.success else failure.after_provider_retry_exhausted()

    @property
    def summary(self) -> str:
        return " ".join(part for part in (
            f"Provider request retries: {self.attempt}.",
            "Recovery completed." if self.success else "",
            f"Final provider response: {self.final_error}" if self.final_error else "",
        ) if part)


@dataclass(frozen=True)
class ACPFailure(DeliveryFailure):
    code: int | None
    detail: str
    classification_priority: ClassVar[int] = 0
    input_state: type[InputAttempt] | None = None
    diagnostics: tuple[PiDiagnostic, ...] = ()
    # Public failed-outcome projection of the turn-owned immutable result.
    retry_result: ProviderRetryResult | None = field(
        default=None, metadata={"wire_omit_default": True},
    )

    @property
    def description(self) -> str:
        return self.detail

    @property
    def feedback(self) -> str:
        return f"{self.description}\n{self.input_disposition}\n{self.action}"

    @property
    def input_disposition(self) -> str:
        if self.input_state is not None:
            return self.input_state.public_status.replace("_", " ").capitalize()
        return super().input_disposition

    @property
    @abstractmethod
    def title(self) -> str: ...

    @property
    def action(self) -> str:
        return " ".join(part for part in (
            self.retry_result.summary if self.retry_result is not None else "",
            self.next_step,
        ) if part)

    @property
    def next_step(self) -> str:
        return "Inspect the input delivery status before deciding whether to send again."

    @classmethod
    @abstractmethod
    def matches(
        cls, code: int | None, detail: str, diagnostics: tuple[PiDiagnostic, ...] = ()
    ) -> bool:
        """Display classification only; input state comes from structured facts."""

    @classmethod
    def from_error(cls, error: RequestError) -> ACPFailure:
        return cls.request_from_error(error).failure

    @classmethod
    def request_from_error(cls, error: RequestError) -> RequestFailure:
        return FieldCodec.decode(ErrorValue, error.data).original_root().request_failure(
            error.code, str(error)
        )

    @classmethod
    def from_detail(cls, code, detail, *, diagnostics=()):
        detail = detail or "ACP request failed"
        owner = next(
            member
            for member in sorted(
                cls.members_with(ACPFailure),
                key=lambda member: member.classification_priority,
                reverse=True,
            )
            if member.matches(code, detail, diagnostics)
        )
        return owner(code, detail, diagnostics=diagnostics)


@dataclass(frozen=True)
class RequestFailure:
    """Failure returned on the request, with whether its notification was sent."""

    failure: ACPFailure
    notification_published: bool

    def error_data(self):
        return {"agentCommsFailure": FieldCodec.encode(self)}

    def request_error(self):
        return RequestError.internal_error(self.error_data())

    @classmethod
    def from_error(cls, error: RequestError):
        return ACPFailure.request_from_error(error)


class NativeTurnOutcome(ABC):
    """Resolved completion owns failure presentation and notification delivery."""

    @abstractmethod
    async def publish(self, client, session_id: str) -> NativeTurnOutcome: ...

    @abstractmethod
    def raise_if_failed(self) -> None: ...

    @property
    def request_failure(self) -> RequestFailure | None:
        return None


@dataclass(frozen=True)
class UnresolvedNativeTurnOutcome(NativeTurnOutcome):
    async def publish(self, client, session_id: str) -> NativeTurnOutcome:
        raise RuntimeError("Native completion has not been resolved at its input boundary")

    def raise_if_failed(self) -> None:
        raise RuntimeError("Native completion has not been resolved at its input boundary")


@dataclass(frozen=True)
class SuccessfulNativeTurnOutcome(NativeTurnOutcome):
    async def publish(self, client, session_id: str) -> NativeTurnOutcome:
        return self

    def raise_if_failed(self) -> None:
        pass


@dataclass(frozen=True)
class FailedNativeTurnOutcome(NativeTurnOutcome):
    request: RequestFailure
    source: FailureSource = UnrecordedFailureSource()
    failed_input_text: str | None = None

    @classmethod
    def resolve(
        cls, terminal: Done, error: Error | None,
        document: InputDocument, original: OriginalTurnInput,
    ) -> FailedNativeTurnOutcome:
        failure = replace(
            ACPFailure.from_detail(-32603, terminal.text or "Backend failed",
                                  diagnostics=error.diagnostics if error is not None else ()),
            input_state=document.shared_state(original.notice_keys),
            retry_result=terminal.retry_result,
        )
        return cls(
            request=RequestFailure(failure, False),
            source=error.source if error is not None else UnrecordedFailureSource(),
            failed_input_text=(original.notice_text
                               if not document.all_started(original.notice_keys) else None),
        )

    async def publish(self, client, session_id: str) -> NativeTurnOutcome:
        from acp.schema import AgentMessageChunk, TextContentBlock
        from .acp_extension import (
            InputFailedUpdate, RequestFailedUpdate, TextRouteUpdate, encode_updates,
        )

        failure = self.request.failure
        accepted = await client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(
                    TextRouteUpdate(None), RequestFailedUpdate(failure, self.source),
                    *((InputFailedUpdate(self.failed_input_text, failure),)
                      if self.failed_input_text else ()),
                ),
            ),
        )
        # ACP SDK clients return None after acknowledgement; Core's transport
        # returns False when nobody accepted the notification.
        return replace(self, request=replace(self.request, notification_published=accepted is not False))

    @property
    def request_failure(self) -> RequestFailure:
        return self.request

    def raise_if_failed(self) -> None:
        raise self.request.request_error()


class RequestACPFailure(ACPFailure):
    @classmethod
    def matches(
        cls, code: int | None, detail: str, diagnostics: tuple[PiDiagnostic, ...] = ()
    ) -> bool:
        return True

    @property
    def title(self) -> str:
        return "Request failed"


class ProviderQuotaFailure(ACPFailure):
    classification_priority = 100
    display_pattern = re.compile(
        r"usage limit|quota|insufficient credits|credit balance|rate.limit|too many requests",
        re.I,
    )

    @classmethod
    def matches(
        cls, code: int | None, detail: str, diagnostics: tuple[PiDiagnostic, ...] = ()
    ) -> bool:
        return cls.display_pattern.search(detail) is not None

    @property
    def title(self) -> str:
        return "Provider usage limit reached"

    @property
    def next_step(self) -> str:
        return (
            "Wait for the provider limit to reset or restore credits, "
            "then inspect input delivery before any explicit resend."
        )


class ProviderConnectionFailure(ACPFailure):
    classification_priority = 150

    @classmethod
    def matches(cls, code, detail, diagnostics=()) -> bool:
        return any(diagnostic.provider_connection_failure for diagnostic in diagnostics)

    @property
    def title(self) -> str:
        return "Provider connection failed"

    @property
    def description(self) -> str:
        known = "\n".join(
            diagnostic.description
            for diagnostic in self.diagnostics
            if diagnostic.provider_connection_failure
        )
        return "\n".join(part for part in (self.detail, known) if part)

    @property
    def next_step(self) -> str:
        return (
            "The provider connection failed. Inspect the response and input delivery "
            "before choosing whether to send a new message."
        )


class ErrorValue(DeclaredFamily, JsonShapeFamily, affix="ErrorValue"):
    """Display provider JSON; it cannot permit input or retries."""

    @property
    @abstractmethod
    def detail(self) -> str: ...

    def original_root(self) -> ErrorValue:
        """Only an original object may interpret root request-failure/input facts."""
        return self.reason()

    def reason(self) -> ErrorValue:
        return self

    def failure(self, code, message, *, diagnostics=()) -> ACPFailure:
        return ACPFailure.from_detail(
            code, self.detail or message, diagnostics=diagnostics
        )

    def request_failure(self, code, message) -> RequestFailure:
        return RequestFailure(self.failure(code, message), False)

    def encoded_reason(self, original: str) -> ErrorValue:
        # JSON scalars inside provider text remain the original plain text.
        return TextErrorValue(original)

    def declared_input(self, body: ErrorValue) -> ErrorValue:
        return body

    def native_diagnostics(self) -> tuple[PiDiagnostic, ...]:
        raise ValueError("Provider diagnostics must be an array")


class EmptyErrorValue(ErrorValue):
    @property
    def detail(self) -> str:
        return ""


@dataclass(frozen=True)
class NullErrorValue(EmptyErrorValue, JsonShapeMember):
    value: None


@dataclass(frozen=True)
class BooleanErrorValue(EmptyErrorValue, JsonShapeMember):
    value: bool


@dataclass(frozen=True)
class IntegerErrorValue(EmptyErrorValue, JsonShapeMember):
    value: int


@dataclass(frozen=True)
class NumberErrorValue(EmptyErrorValue, JsonShapeMember):
    value: float


@dataclass(frozen=True)
class TextErrorValue(ErrorValue, JsonShapeMember):
    value: str

    def reason(self) -> ErrorValue:
        try:
            nested = json.loads(self.value)
        except ValueError:
            return self
        return FieldCodec.decode(ErrorValue, nested).reason().encoded_reason(self.value)

    @property
    def detail(self) -> str:
        text = self.value.strip()
        return "" if text.casefold() in {"internal error", "internal server error"} else text

    def declared_input(self, body: ErrorValue) -> ErrorValue:
        try:
            attempt = InputAttempt.decode(self.value)
        except ValueError:
            # An external public status may be unambiguous; redacted UNKNOWN
            # cannot distinguish reserved from bound input and grants nothing.
            matches = tuple(member for member in InputAttempt.members_with(InputAttempt)
                            if member.public_status == self.value)
            if len(matches) != 1:
                return body
            (attempt,) = matches
        return DeclaredInputErrorValue(body, attempt)


class NestedErrorValue(ErrorValue):
    def encoded_reason(self, original: str) -> ErrorValue:
        return EncodedErrorValue(self) if self.detail else TextErrorValue(original)


@dataclass(frozen=True)
class ArrayErrorValue(NestedErrorValue, JsonShapeMember):
    value: tuple[ErrorValue, ...]

    @property
    def detail(self) -> str:
        return next((detail for child in self.value if (detail := child.detail)), "")

    def reason(self) -> ErrorValue:
        return ArrayErrorValue(tuple(child.reason() for child in self.value))

    def native_diagnostics(self) -> tuple[PiDiagnostic, ...]:
        return tuple(PiDiagnostic.from_wire(FieldCodec.encode(child)) for child in self.value)


@dataclass(frozen=True)
class ObjectErrorValue(NestedErrorValue, JsonShapeMember):
    value: dict[str, ErrorValue]
    reason_fields: ClassVar[tuple[str, ...]] = ("details", "reason", "detail", "error", "data", "message")

    @property
    def detail(self) -> str:
        # These are the external provider's ordered reason fields, not
        # application state names or a nominal-family membership roster.
        for name in self.reason_fields:
            if name in self.value:
                detail = self.value[name].detail
                if detail:
                    return detail
        return ""

    def reason(self) -> ErrorValue:
        return ObjectErrorValue({name: child.reason() if name in self.reason_fields else child
                                 for name, child in self.value.items()})

    def original_root(self) -> ErrorValue:
        values = dict(self.value)
        if "agentCommsFailure" in values:
            request_failure = FieldCodec.decode(
                RequestFailure, FieldCodec.encode(values.pop("agentCommsFailure"))
            )
            # Keep the reported failure, not a generic tree or reclassification.
            return PublishedErrorValue(request_failure)
        status = values.pop("inputStatus", NullErrorValue(None))
        if "diagnostics" in values:
            diagnostics = values.pop("diagnostics").native_diagnostics()
            body = DiagnosticErrorValue(ObjectErrorValue(values).reason(), diagnostics)
        else:
            body = ObjectErrorValue(values).reason()
        return status.declared_input(body)


@dataclass(frozen=True)
class EncodedErrorValue(ErrorValue):
    value: ErrorValue

    @property
    def detail(self) -> str:
        return self.value.detail

    def encoded_reason(self, original: str) -> ErrorValue:
        return EncodedErrorValue(self)

    def to_wire(self):
        return json.dumps(FieldCodec.encode(self.value))


@dataclass(frozen=True)
class DeclaredInputErrorValue(ErrorValue):
    value: ErrorValue
    attempt: type[InputAttempt]

    @property
    def detail(self) -> str:
        return self.value.detail

    def failure(self, code, message, *, diagnostics=()) -> ACPFailure:
        return replace(self.value.failure(code, message, diagnostics=diagnostics),
                       input_state=self.attempt)

    def to_wire(self):
        return {**FieldCodec.encode(self.value), "inputStatus": self.attempt.declared_name}


@dataclass(frozen=True)
class DiagnosticErrorValue(ErrorValue):
    value: ErrorValue
    diagnostics: tuple[PiDiagnostic, ...]

    @property
    def detail(self) -> str:
        return self.value.detail

    def failure(self, code, message, *, diagnostics=()) -> ACPFailure:
        return self.value.failure(code, message, diagnostics=self.diagnostics)

    def to_wire(self):
        return {**FieldCodec.encode(self.value),
                "diagnostics": [item.to_wire() for item in self.diagnostics]}


@dataclass(frozen=True)
class PublishedErrorValue(ErrorValue):
    reported_failure: RequestFailure

    @property
    def detail(self) -> str:
        return self.reported_failure.failure.detail

    def failure(self, code, message, *, diagnostics=()) -> ACPFailure:
        return self.reported_failure.failure

    def request_failure(self, code, message) -> RequestFailure:
        return self.reported_failure

    def to_wire(self):
        return self.reported_failure.error_data()
