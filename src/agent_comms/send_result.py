"""A committed send and its independent post-delivery wording correction."""
from abc import abstractmethod
from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .field_codec import MessageId
from .messages import Message


class CorrectionResult(DeclaredFamily):
    @abstractmethod
    def notices(self, message_id: MessageId) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class CorrectionComplete(CorrectionResult, declared_name="complete"):
    def notices(self, message_id: MessageId) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class CorrectionFailed(CorrectionResult, declared_name="failed"):
    error: str

    def notices(self, message_id: MessageId) -> tuple[str, ...]:
        return (f"Message {message_id} was sent; wording correction failed: {self.error}",)


@dataclass(frozen=True)
class CorrectionUnreported(CorrectionResult, declared_name="unreported"):
    """Older retained tool replies did not report their correction outcome."""
    def notices(self, message_id: MessageId) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class SendResult:
    message: Message
    correction: CorrectionResult

    @property
    def notices(self) -> tuple[str, ...]:
        return self.correction.notices(self.message.message_id)
