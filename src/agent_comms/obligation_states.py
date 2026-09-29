"""Response lifecycle owns receipt data and its public recovery projection."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .coordination_errors import IntegrityViolationError
from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState


@dataclass(frozen=True)
class ResponseState(DeclaredFamily, LifecycleState, affix="Response"):
    terminal: ClassVar[bool] = False
    successful: ClassVar[bool] = False
    retryable: ClassVar[bool] = False
    allows_intent: ClassVar[bool] = False
    requires_intent: ClassVar[bool] = False
    published: ClassVar[bool] = False
    pending: ClassVar[bool] = False
    publishing: ClassVar[bool] = False
    deferred: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[ResponseState], ...]: ...

    @classmethod
    def publication(cls) -> str:
        return cls.declared_name

    @property
    def receipt_message_id(self) -> str | None:
        return None

    @property
    def receipt_seq(self) -> int | None:
        return None

    @classmethod
    def load(cls, message_id: str | None, seq: int | None) -> ResponseState:
        if message_id is not None or seq is not None:
            raise IntegrityViolationError("only published obligations may have a receipt")
        return cls()

    def require_nonpublication(self) -> None:
        from .coordination_errors import IdentityConflict

        if not self.retryable:
            raise IdentityConflict("wire completion requires nonpublication obligation")

    def validate_publication(self, intent, receipt) -> None:
        if intent is not None and not self.allows_intent:
            raise IntegrityViolationError("publication intent is invalid for obligation state")
        if intent is None and self.requires_intent:
            raise IntegrityViolationError("publishing and published obligations require intent")
        if (receipt is not None) != self.published:
            raise IntegrityViolationError("publication receipt exists iff published")


class PendingResponse(ResponseState):
    retryable = True
    pending = True

    @classmethod
    def successors(cls):
        return PublishingResponse, DeferredResponse, SilentResponse, FailedResponse


class PublishingResponse(ResponseState):
    publishing = True
    allows_intent = True
    requires_intent = True

    @classmethod
    def successors(cls):
        return PublishedResponse, FailedResponse

    @classmethod
    def publication(cls):
        return "uncertain"


class DeferredResponse(ResponseState):
    retryable = True
    deferred = True

    @classmethod
    def successors(cls):
        return PendingResponse, SilentResponse, FailedResponse


@dataclass(frozen=True)
class PublishedResponse(ResponseState):
    message_id: str
    seq: int
    terminal = True
    successful = True
    allows_intent = True
    requires_intent = True
    published = True

    def __post_init__(self):
        if not isinstance(self.message_id, str) or not self.message_id:
            raise IntegrityViolationError("published obligations require a receipt")
        if type(self.seq) is not int or self.seq <= 0:
            raise ValueError("receipt_seq must be positive")

    @property
    def receipt_message_id(self):
        return self.message_id

    @property
    def receipt_seq(self):
        return self.seq

    @classmethod
    def load(cls, message_id, seq):
        if message_id is None or seq is None:
            raise IntegrityViolationError("published obligations require a receipt")
        return cls(message_id, seq)

    @classmethod
    def successors(cls):
        return ()


class SilentResponse(ResponseState):
    terminal = True
    successful = True

    @classmethod
    def successors(cls):
        return ()


class FailedResponse(ResponseState):
    terminal = True
    allows_intent = True

    @classmethod
    def successors(cls):
        return ()
