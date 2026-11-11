from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace

from agent_comms.nominal.field_codec import NullMember, PresentMember


@dataclass(frozen=True, slots=True)
class ImportLimits:
    messages: int = 200
    characters: int = 120_000
    per_message: int = 12_000

    def __post_init__(self) -> None:
        if min(self.messages, self.characters, self.per_message) <= 0:
            raise ValueError("Import budgets must be positive.")


TRUNCATED = "\n[Import excerpt truncated]"


@dataclass(frozen=True, slots=True)
class NoRetainedRequest(NullMember):
    def matches(self, latest_request: str) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ImportedMessage(ABC):
    """One transcript event as portable text in the imported Pi session."""

    body: str
    truncated: bool = False

    def import_to(self, accept: Callable[[ImportedMessage], None]) -> None:
        if self.body.strip():
            accept(self)

    def bounded(self, limits: ImportLimits) -> ImportedMessage:
        cap = min(limits.per_message, limits.characters)
        if len(self.body) <= cap:
            return self
        return replace(self, body=self.body[: max(0, cap - len(TRUNCATED))] + TRUNCATED[:cap], truncated=True)

    def retained_request(self) -> ImportedRequest | NoRetainedRequest:
        return NoRetainedRequest()

    @abstractmethod
    def pi_message(self, timestamp: int) -> dict[str, object]: ...


class ImportedInput(ImportedMessage):
    """Text the source session's model received: tool calls, results and context."""

    def pi_message(self, timestamp: int) -> dict[str, object]:
        return {"role": "user", "content": [{"type": "text", "text": self.body}], "timestamp": timestamp}


class ImportedRequest(PresentMember, ImportedInput):
    """A person's or a directing agent's prompt."""

    def retained_request(self) -> ImportedRequest:
        return self

    def matches(self, latest_request: str) -> bool:
        return self.body == latest_request or (
            self.truncated and latest_request.startswith(self.body.removesuffix(TRUNCATED))
        )


class ImportedReply(ImportedMessage):
    def pi_message(self, timestamp: int) -> dict[str, object]:
        zero = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        return {
            "role": "assistant", "content": [{"type": "text", "text": self.body}], "timestamp": timestamp,
            "api": "openai-responses", "provider": "imported", "model": "historical-context", "stopReason": "stop",
            "usage": {**zero, "totalTokens": 0, "cost": {**zero, "total": 0}},
        }
