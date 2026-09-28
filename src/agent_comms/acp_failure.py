"""Public ACP failure presentation; decode external nesting once, keep provider text.

This is observation only. No failure object grants a retry or changes a ledger.
"""

from __future__ import annotations

import json
import re
from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .declared_family import DeclaredFamily
from .delivery_presentation import DeliveryPresentation
from .input_attempt import InputAttempt
from .pi_payloads import PiDiagnostic


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
        detail = _error_detail(data) or message or "ACP request failed"
        if isinstance(data, dict) and "diagnostics" in data:
            records = data["diagnostics"]
            if not isinstance(records, list):
                raise ValueError("Provider diagnostics must be an array")
            diagnostics = tuple(PiDiagnostic.from_wire(record) for record in records)
        owner = next(
            member
            for member in sorted(
                cls.members_with(ACPFailure),
                key=lambda member: member.classification_priority,
                reverse=True,
            )
            if member.matches(code, detail, diagnostics)
        )
        state = None
        if isinstance(data, dict):
            status = data.get("inputStatus")
            if isinstance(status, str):
                try:
                    state = InputAttempt.decode(status)
                except ValueError:
                    # A redacted public status has no provenance. Resolve only
                    # when its declaration is unique; unknown cannot distinguish
                    # a reserved input from a bound uncertain input.
                    matches = [
                        member
                        for member in InputAttempt.members_with(InputAttempt)
                        if member.public_status == status
                    ]
                    if len(matches) == 1:
                        state = matches[0]
        return owner(code, detail, state, diagnostics)


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


def _error_detail(data: object) -> str | None:
    # JSON-RPC errors and provider adapters wrap the same reason at different
    # depths. Only this external decoder knows their keys; consumers use fields.
    pending = [data]
    seen: set[int] = set()
    fallback = None
    while pending:
        value = pending.pop()
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                continue
            seen.add(id(value))
        if isinstance(value, dict):
            # Prefer concrete reasons over an enclosing "Internal error".
            for key in ("details", "reason", "detail", "error", "data", "message"):
                item = value.get(key)
                if (
                    isinstance(item, str)
                    and item.strip()
                    and item.strip().casefold() not in {"internal error", "internal server error"}
                ):
                    try:
                        nested = json.loads(item)
                    except (ValueError, TypeError):
                        return item.strip()
                    if isinstance(nested, (dict, list)):
                        pending.append(nested)
                    else:
                        return item.strip()
                elif isinstance(item, (dict, list)):
                    pending.append(item)
        elif isinstance(value, list):
            pending.extend(reversed(value))
        elif isinstance(value, str) and value.strip():
            fallback = value.strip()
    return fallback
