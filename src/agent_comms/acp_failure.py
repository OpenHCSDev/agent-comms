"""Public ACP failure presentation; decode external nesting once, keep provider text.

This is observation only. No failure object grants a retry or changes a ledger.
"""

from __future__ import annotations

import json
import re
from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import ClassVar

from .declared_family import DeclaredFamily
from .delivery_presentation import DeliveryPresentation
from .input_attempt import InputAttempt
from .pi_payloads import PiDiagnostic
from .field_codec import FieldCodec, JsonShapeFamily, JsonShapeMember


class DeliveryFailure(DeliveryPresentation, DeclaredFamily, affix="Failure"):
    @property
    def title(self) -> str:
        return "Delivery unconfirmed"

    @property
    def action(self) -> str:
        return "Inspect the input delivery status before deciding whether to send again."

    @property
    def input_disposition(self) -> str:
        return "Unconfirmed — input not retried"

    @property
    @abstractmethod
    def description(self) -> str: ...


@dataclass(frozen=True)
class BackendDeliveryFailure(DeliveryFailure):
    message: str

    @property
    def description(self) -> str:
        return self.message


@dataclass(frozen=True)
class ACPFailure(DeliveryFailure):
    code: int | None
    detail: str
    classification_priority: ClassVar[int] = 0
    input_state: type[InputAttempt] | None = None
    diagnostics: tuple[PiDiagnostic, ...] = ()

    @property
    def description(self) -> str:
        return self.detail

    @property
    def feedback(self) -> str:
        return f"{self.description}\n{self.input_disposition}\n{self.action}"

    @property
    def input_disposition(self) -> str:
        if self.input_state is not None:
            return (
                self.input_state.public_status.replace("_", " ").capitalize()
                + " — input not retried"
            )
        return super().input_disposition

    @property
    @abstractmethod
    def title(self) -> str: ...

    @property
    def action(self) -> str:
        return "Inspect the input delivery status before deciding whether to send again."

    @classmethod
    @abstractmethod
    def matches(
        cls, code: int | None, detail: str, diagnostics: tuple[PiDiagnostic, ...] = ()
    ) -> bool:
        """Display classification only; input state comes from structured facts."""

    @classmethod
    def from_error(
        cls,
        code: int | None,
        message: str,
        data: object = None,
        *,
        diagnostics: tuple[PiDiagnostic, ...] = (),
    ) -> ACPFailure:
        return FieldCodec.decode(ErrorValue, data).original_root().failure(
            code, message, diagnostics=diagnostics
        )

    @classmethod
    def from_detail(cls, code, detail, *, diagnostics=()):
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
class PromptFailureReceipt:
    """One request's failure and proof of its already published notification."""

    failure: ACPFailure
    notification_published: bool

    def error_data(self):
        from .field_codec import FieldCodec

        return {"agentCommsFailure": FieldCodec.encode(self)}

    def request_error(self):
        from acp import RequestError

        return RequestError.internal_error(self.error_data())

    @classmethod
    def from_error(cls, code, message, data):
        return FieldCodec.decode(ErrorValue, data).original_root().failure_receipt(code, message)


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
    def action(self) -> str:
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
    def action(self) -> str:
        return (
            "The provider connection failed. Inspect the response and input delivery "
            "before choosing whether to send a new message; this input was not retried."
        )


class ErrorValue(DeclaredFamily, JsonShapeFamily, affix="ErrorValue"):
    """Provider JSON presentation, never input admission or retry authority."""

    @property
    @abstractmethod
    def detail(self) -> str: ...

    def original_root(self) -> ErrorValue:
        """Only an original object may interpret root receipt/disposition facts."""
        return self.reason()

    def reason(self) -> ErrorValue:
        return self

    def failure(self, code, message, *, diagnostics=()) -> ACPFailure:
        return ACPFailure.from_detail(
            code, self.detail or message or "ACP request failed", diagnostics=diagnostics
        )

    def failure_receipt(self, code, message) -> PromptFailureReceipt:
        return PromptFailureReceipt(self.failure(code, message), False)

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
            receipt = FieldCodec.decode(
                PromptFailureReceipt, FieldCodec.encode(values.pop("agentCommsFailure"))
            )
            # The canonical receipt owns ALL its presentation facts. Do not
            # retain a second generic tree of its fields or reclassify it.
            return PublishedErrorValue(receipt)
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
    receipt: PromptFailureReceipt

    @property
    def detail(self) -> str:
        return self.receipt.failure.detail

    def failure(self, code, message, *, diagnostics=()) -> ACPFailure:
        return self.receipt.failure

    def failure_receipt(self, code, message) -> PromptFailureReceipt:
        return self.receipt

    def to_wire(self):
        return self.receipt.error_data()
