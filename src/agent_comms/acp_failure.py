"""Public ACP failure presentation; decode external nesting once, keep provider text.

This is observation only. No failure object grants a retry or changes a ledger.
"""

from __future__ import annotations

import json
import re
from abc import abstractmethod
from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .input_attempt import InputAttempt
from .turn_failure import PrestartCompactionFailed, PromptSendFailed, TurnFailure


class DeliveryFailure(DeclaredFamily, affix="Failure"):
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
    failure: TurnFailure
    input_state: type[InputAttempt] | None = None

    @property
    def description(self) -> str:
        return self.detail

    @property
    def feedback(self) -> str:
        return f"{self.detail}\n{self.input_disposition}\n{self.action}"

    @property
    def input_disposition(self) -> str:
        if self.input_state is not None:
            return (
                self.input_state.public_status.replace("_", " ").capitalize()
                + " — input not retried"
            )
        return (
            "UNKNOWN — input not retried"
            if self.failure.input_uncertain
            else "Unconfirmed — input not retried"
        )

    @property
    @abstractmethod
    def title(self) -> str: ...

    @property
    def action(self) -> str:
        return "Inspect the input delivery status before deciding whether to send again."

    @classmethod
    def from_error(cls, code: int | None, message: str, data: object = None) -> ACPFailure:
        detail = _error_detail(data) or message or "ACP request failed"
        uncertain = bool(
            re.search(r"outcome uncertain|input not retried|original remains unbound", detail, re.I)
        )
        failure = PrestartCompactionFailed(detail) if uncertain else PromptSendFailed(detail)
        owner = (
            ProviderQuotaFailure
            if re.search(
                r"usage limit|quota|insufficient credits|credit balance|"
                r"rate.limit|too many requests",
                detail,
                re.I,
            )
            else RequestACPFailure
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
                        member for member in InputAttempt.members_with(InputAttempt)
                        if member.public_status == status
                    ]
                    if len(matches) == 1:
                        state = matches[0]
        return owner(code, detail, failure, state)


class RequestACPFailure(ACPFailure):
    @property
    def title(self) -> str:
        return "Request failed"


class ProviderQuotaFailure(ACPFailure):
    @property
    def title(self) -> str:
        return "Provider usage limit reached"

    @property
    def action(self) -> str:
        return (
            "Wait for the provider limit to reset or restore credits, "
            "then inspect input delivery before any explicit resend."
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
