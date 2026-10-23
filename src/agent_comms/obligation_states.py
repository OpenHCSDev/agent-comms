"""Response lifecycle owns acknowledgement data and its public recovery projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .coordination_contracts import MAX_IDENTIFIER_CHARS, require_bounded
from .coordination_errors import IdentityConflict, IntegrityViolationError
from .coordinator_vocabulary import CoordinatorVocabulary
from .field_codec import NullMember
from .lifecycle import LifecycleState
from .lifecycle_capability import Deferred, Failed, Pending, Successful, Terminal
from .message_reference import MessageReference
from .reason_codes import NoReasonCode

if TYPE_CHECKING:
    from .coordination_session import CoordinationSession
    from .coordination_tables.responses import ResponseObligation


class FromPendingResponse:
    @classmethod
    def after_pending_response(cls) -> bool:
        return True


class FromPublishingResponse:
    @classmethod
    def after_publishing_response(cls) -> bool:
        return True


class FromDeferredResponse:
    @classmethod
    def after_deferred_response(cls) -> bool:
        return True



@dataclass(frozen=True, slots=True)
class Unpublished(NullMember):
    """An obligation has no publication acknowledgement until it is published."""


@dataclass(frozen=True)
class ResponseState(CoordinatorVocabulary, LifecycleState, affix="Response"):
    retryable: ClassVar[bool] = False
    allows_intent: ClassVar[bool] = False
    requires_intent: ClassVar[bool] = False
    published: ClassVar[bool] = False
    publishing: ClassVar[bool] = False

    @classmethod
    def sql_vocabulary(cls) -> dict[str, str]:
        members = cls.members_with(cls)
        return dict(
            response_names=cls.sql_values(cls.names()),
            successful_response_names=cls.sql_members(cls.members_with(Successful)),
            retryable_response_names=cls.sql_members(
                member for member in members if member.retryable
            ),
            intent_response_names=cls.sql_members(
                member for member in members if member.allows_intent
            ),
            required_intent_response_names=cls.sql_members(
                member for member in members if member.requires_intent
            ),
        )

    @property
    def published_message_id(self) -> str | Unpublished:
        return Unpublished()

    @property
    def publication_seq(self) -> int | Unpublished:
        return Unpublished()

    def published_reference(self) -> MessageReference:
        raise IntegrityViolationError("only published obligations have an acknowledgement")

    @classmethod
    def load(cls, message_id: str | None, seq: int | None) -> ResponseState:
        if message_id is not None or seq is not None:
            raise IntegrityViolationError("only published obligations may have a acknowledgement")
        return cls()

    def require_preparation(self) -> None:
        raise IdentityConflict("response obligation cannot prepare a new intent")

    def require_existing_preparation(self) -> None:
        raise IdentityConflict("prepared response envelope conflicts")

    def require_publishing(self) -> None:
        raise IdentityConflict("no frozen publishing intent for current owner")

    @classmethod
    def resume_response(
        cls, session: CoordinationSession, obligation: ResponseObligation, execution_id: str
    ) -> None:
        """Only a deferred response needs to reengage."""

    def require_nonpublication(self) -> None:
        if not self.retryable:
            raise IdentityConflict("wire completion requires nonpublication obligation")

    def validate_publication(self, intent, acknowledgement) -> None:
        if intent is not None and not self.allows_intent:
            raise IntegrityViolationError("publication intent is invalid for obligation state")
        if intent is None and self.requires_intent:
            raise IntegrityViolationError("publishing and published obligations require intent")
        self.validate_acknowledgement(acknowledgement)
        if acknowledgement is not None:
            intent.validate_acknowledgement(acknowledgement)

    def validate_acknowledgement(self, acknowledgement) -> None:
        if acknowledgement is not None:
            raise IntegrityViolationError("publication acknowledgement exists iff published")


class PendingResponse(FromDeferredResponse, Pending, ResponseState):
    retryable = True

    def require_preparation(self) -> None:
        pass

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromPendingResponse.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_pending_response()


class PublishingResponse(FromPendingResponse, ResponseState):
    publishing = True
    allows_intent = True
    requires_intent = True

    def require_publishing(self) -> None:
        pass

    def require_existing_preparation(self) -> None:
        pass

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromPublishingResponse.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_publishing_response()


class DeferredResponse(FromPendingResponse, Deferred, ResponseState):
    retryable = True

    @classmethod
    def resume_response(
        cls, session: CoordinationSession, obligation: ResponseObligation, execution_id: str
    ) -> None:
        obligation.update(
            session._connection,
            where="execution_id=? AND exact_target=?",
            parameters=(execution_id, obligation.exact_target),
            lifecycle=PendingResponse(),
            revision=obligation.revision + 1,
            reason_code=NoReasonCode(),
            updated_at_ms=session.now(obligation.updated_at_ms),
        )

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromDeferredResponse.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_deferred_response()


@dataclass(frozen=True)
class PublishedResponse(FromPublishingResponse, Successful, ResponseState):
    message_id: str
    seq: int
    allows_intent = True
    requires_intent = True
    published = True

    def __post_init__(self):
        if not isinstance(self.message_id, str) or not self.message_id:
            raise IntegrityViolationError("published obligations require a acknowledgement")
        require_bounded(self.message_id, "receipt_message_id", MAX_IDENTIFIER_CHARS)
        if type(self.seq) is not int or self.seq <= 0:
            raise ValueError("receipt_seq must be positive")

    @property
    def published_message_id(self):
        return self.message_id

    @property
    def publication_seq(self):
        return self.seq

    def published_reference(self) -> MessageReference:
        return MessageReference(self.seq, self.message_id)

    def validate_acknowledgement(self, acknowledgement) -> None:
        if acknowledgement is None:
            raise IntegrityViolationError("publication acknowledgement exists iff published")
        if (acknowledgement.message_id, acknowledgement.seq) != (self.message_id, self.seq):
            raise IntegrityViolationError("obligation acknowledgement does not match publication")

    @classmethod
    def load(cls, message_id, seq):
        if message_id is None or seq is None:
            raise IntegrityViolationError("published obligations require a acknowledgement")
        return cls(message_id, seq)


class SilentResponse(FromPendingResponse, FromDeferredResponse, Successful, ResponseState):
    pass


class FailedResponse(FromPendingResponse, FromPublishingResponse, FromDeferredResponse, Failed, Terminal, ResponseState):
    allows_intent = True
