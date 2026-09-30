"""Public ACP failure presentation; decode external nesting once, keep provider text.

This is observation only. No failure object grants a retry or changes a ledger.
"""

from __future__ import annotations

import json
import re
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from .declared_family import DeclaredFamily
from .delivery_presentation import DeliveryPresentation
from .input_attempt import InputAttempt
from .pi_payloads import PiDiagnostic
from .field_codec import FieldCodec
from .mro_dispatch import MroDispatch, handles


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
        return cls.from_payload(
            code, message, ExternalFailureData.decode(data), diagnostics=diagnostics
        )

    @classmethod
    def from_payload(cls, code, message, payload, *, diagnostics=()):
        detail = payload.detail or message or "ACP request failed"
        diagnostics = payload.diagnostics if payload.has_diagnostics else diagnostics
        owner = next(
            member
            for member in sorted(
                cls.members_with(ACPFailure),
                key=lambda member: member.classification_priority,
                reverse=True,
            )
            if member.matches(code, detail, diagnostics)
        )
        return owner(code, detail, payload.input_state, diagnostics)


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
        return ExternalFailureData.decode(data).failure_receipt(code, message)


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


class StructuredErrorValue(MroDispatch):
    """Only external JSON containers continue the existing traversal."""

    def __init__(self, payload):
        self.payload = payload

    @handles(dict, list)
    def container(self, value):
        self.payload.pending.append(value)


class ErrorReasonField(StructuredErrorValue):
    """A provider reason field owns text priority and encoded JSON handling."""

    @handles(str)
    def reason(self, value):
        text = value.strip()
        if not text or text.casefold() in {"internal error", "internal server error"}:
            return
        try:
            nested = json.loads(text)
        except ValueError:
            self.payload.detail = text
            return
        containers = StructuredErrorValue(self.payload)
        if tuple(containers.handlers_for(nested)):
            containers.dispatch_sync(nested)
        else:
            self.payload.detail = text


class ExternalFailureData(MroDispatch):
    """Decode external JSON nesting once; no result grants retry authority.

    The key ordering is the provider/JSON-RPC presentation boundary. It is not
    a roster of application states. Diagnostics and input state keep their
    existing declaration owners; a published receipt uses the canonical codec.
    """

    def __init__(self, root):
        self.root = root
        self.pending = [root]
        self.seen = set()
        self.detail = self.fallback = None
        self.input_state = None
        self.diagnostics = ()
        self.has_diagnostics = False
        self.receipt = None

    @classmethod
    def decode(cls, root):
        result = cls(root)
        result.read()
        return result

    def read(self):
        while self.pending and self.detail is None:
            self.dispatch_sync(self.pending.pop())
        self.detail = self.detail or self.fallback

    def failure_receipt(self, code, message):
        if self.receipt is not None:
            return self.receipt
        return PromptFailureReceipt(ACPFailure.from_payload(code, message, self), False)

    def first_visit(self, value):
        identity = id(value)
        if identity in self.seen:
            return False
        self.seen.add(identity)
        return True

    def root_metadata(self, value):
        if "agentCommsFailure" in value:
            self.receipt = FieldCodec.decode(PromptFailureReceipt, value["agentCommsFailure"])
        if "diagnostics" in value:
            self.has_diagnostics = True
            try:
                records = FieldCodec.decode(list[Any], value["diagnostics"])
            except (TypeError, ValueError) as error:
                raise ValueError("Provider diagnostics must be an array") from error
            self.diagnostics = tuple(PiDiagnostic.from_wire(record) for record in records)
        status = value.get("inputStatus")
        if isinstance(status, str):
            try:
                self.input_state = InputAttempt.decode(status)
            except ValueError:
                # Redacted UNKNOWN cannot distinguish reserved from bound input.
                matches = tuple(
                    member
                    for member in InputAttempt.members_with(InputAttempt)
                    if member.public_status == status
                )
                if len(matches) == 1:
                    self.input_state = matches[0]

    @handles(dict)
    def object(self, value):
        if not self.first_visit(value):
            return
        if value is self.root:
            self.root_metadata(value)
        for key in ("details", "reason", "detail", "error", "data", "message"):
            ErrorReasonField(self).dispatch_sync(value.get(key))
            if self.detail is not None:
                return

    @handles(list)
    def array(self, value):
        if self.first_visit(value):
            self.pending.extend(reversed(value))

    @handles(str)
    def text(self, value):
        if value.strip():
            self.fallback = value.strip()
