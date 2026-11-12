"""Response lifecycle owns acknowledgement data and its public recovery projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.coordination.errors import IdentityConflict, CoordinationError
from agent_comms.coordination.reason_codes import NoReasonCode
from agent_comms.coordination.vocabulary import CoordinatorVocabulary
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import MessageId, NullMember
from agent_comms.nominal.lifecycle import LifecycleState
from agent_comms.nominal.lifecycle_capability import Deferred, Failed, Pending, Successful, Terminal

if TYPE_CHECKING:
    from agent_comms.coordination.session import CoordinationSession
    from agent_comms.coordination.tables.responses import ResponseObligation


@dataclass(frozen=True, slots=True)
class Unpublished(NullMember):
    """An obligation has no publication acknowledgement until it is published."""


class Retryable:
    __slots__ = ()

    def is_retryable(self) -> bool:
        return True


class AllowsIntent:
    __slots__ = ()


class RequiresIntent(AllowsIntent):
    __slots__ = ()


class Published:
    __slots__ = ()


class Publishing(RequiresIntent):
    __slots__ = ()


@dataclass(frozen=True)
class ResponseState(CoordinatorVocabulary, LifecycleState, affix="Response"):

    @classmethod
    def sql_vocabulary(cls) -> dict[str, str]:
        return dict(
            response_names=cls.sql_values(cls.names()),
            successful_response_names=cls.sql_members(cls.members_with(Successful)),
            retryable_response_names=cls.sql_members(cls.members_with(Retryable)),
            intent_response_names=cls.sql_members(cls.members_with(AllowsIntent)),
            required_intent_response_names=cls.sql_members(cls.members_with(RequiresIntent)),
        )

    @property
    def published_message_id(self) -> MessageId | Unpublished:
        return Unpublished()

    @property
    def publication_seq(self) -> int | Unpublished:
        return Unpublished()

    def published_reference(self) -> MessageReference:
        raise CoordinationError("only published obligations have an acknowledgement")

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

    def is_retryable(self) -> bool:
        return False

    def is_published(self) -> bool:
        return False

    def require_nonpublication(self) -> None:
        raise IdentityConflict("wire completion requires nonpublication obligation")

    def validate_intent(self, intent) -> None:
        if intent is not None:
            raise CoordinationError("publication intent is invalid for obligation state")

    def validate_publication(self, intent, acknowledgement) -> None:
        self.validate_intent(intent)
        self.validate_acknowledgement(acknowledgement)
        if acknowledgement is not None:
            intent.validate_acknowledgement(acknowledgement)

    def validate_acknowledgement(self, acknowledgement) -> None:
        if acknowledgement is not None:
            raise CoordinationError("publication acknowledgement exists iff published")


class PendingResponse(Retryable, Pending, ResponseState):

    def require_nonpublication(self) -> None:
        pass

    def require_preparation(self) -> None:
        pass

    @classmethod
    def successors(cls):
        return PublishingResponse, DeferredResponse, SilentResponse, FailedResponse


class PublishingResponse(Publishing, ResponseState):

    def validate_intent(self, intent) -> None:
        if intent is None:
            raise CoordinationError("publishing and published obligations require intent")

    def require_publishing(self) -> None:
        pass

    def require_existing_preparation(self) -> None:
        pass

    @classmethod
    def successors(cls):
        return PublishedResponse, FailedResponse


class DeferredResponse(Retryable, Deferred, ResponseState):

    def require_nonpublication(self) -> None:
        pass

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
    def successors(cls):
        return PendingResponse, SilentResponse, FailedResponse


@dataclass(frozen=True)
class PublishedResponse(Published, RequiresIntent, Successful, ResponseState):
    message_id: MessageId
    seq: int

    def __post_init__(self):
        if self.seq <= 0:
            raise ValueError("receipt_seq must be positive")

    def is_published(self) -> bool:
        return True

    @property
    def published_message_id(self):
        return self.message_id

    @property
    def publication_seq(self):
        return self.seq

    def validate_intent(self, intent) -> None:
        if intent is None:
            raise CoordinationError("publishing and published obligations require intent")

    def published_reference(self) -> MessageReference:
        return MessageReference(self.seq, self.message_id)

    def validate_acknowledgement(self, acknowledgement) -> None:
        if acknowledgement is None:
            raise CoordinationError("publication acknowledgement exists iff published")
        if (acknowledgement.message_id, acknowledgement.seq) != (self.message_id, self.seq):
            raise CoordinationError("obligation acknowledgement does not match publication")


class SilentResponse(Successful, ResponseState):
    pass


class FailedResponse(AllowsIntent, Failed, Terminal, ResponseState):

    def validate_intent(self, intent) -> None:
        pass
