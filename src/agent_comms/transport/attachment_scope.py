from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace

from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.nominal.field_codec import (
    AcceptsNull,
    AcpSessionId,
    ExternalJsonShape,
    FieldCodec,
    RootId,
    WireValue,
)
from agent_comms.registry.thread_identity import ClaimScoped


class AttachmentScope(AcceptsNull, ExternalJsonShape, WireValue):
    """One decoded attachment; JSON null is an absent attachment, not Python None."""

    @classmethod
    def from_wire(cls, data):
        return cls.empty_scope() if data is None else FieldCodec.decode_record(cls.record_type(), data)

    @classmethod
    @abstractmethod
    def empty_scope(cls): ...

    @classmethod
    @abstractmethod
    def record_type(cls): ...

    @classmethod
    def schema(cls):
        return {"anyOf": [{"type": "null"}, FieldCodec.record_schema(cls.record_type())]}

    def to_wire(self):
        return None

    def for_session(self, session_id: AcpSessionId):
        return self

    def matches_session(self, session_id: AcpSessionId) -> bool:
        return False

    def foreign_session(self, session_id: AcpSessionId) -> bool:
        return False

    def relation(self, other: AttachmentScope) -> AttachmentRelation:
        return AttachmentRelation()

    def relation_from(self, original: LiveAttachmentScope) -> AttachmentRelation:
        return AttachmentRelation()

    def current_for(self, other: AttachmentScope) -> bool:
        return self.relation(other).current

    def same_request(self, other: AttachmentScope) -> bool:
        return self == other or self.current_for(other)

    def require_live(self):
        raise ValueError('Attachment has no live local owner scope.')


@dataclass(frozen=True)
class LiveAttachmentScope(AttachmentScope, ClaimScoped):
    """Logical ACP attachment and executor evidence have separate identities.

    A dataclass, so every live scope inherits these two fields from here.
    """
    session_id: AcpSessionId
    owner: ProcessIdentity

    @property
    @abstractmethod
    def logical_key(self) -> tuple[str, ...]: ...

    @property
    def owner_created_at(self):
        return self.claimed_identity.incarnation.require_recorded().created_at

    @property
    def claimed_generation(self):
        return self.claimed_identity.claimed_generation

    def to_wire(self):
        return FieldCodec.encode_record(self)

    def for_session(self, session_id: AcpSessionId):
        return replace(self, session_id=session_id)

    def matches_session(self, session_id: AcpSessionId) -> bool:
        return self.session_id == session_id

    def foreign_session(self, session_id: AcpSessionId) -> bool:
        return self.session_id != session_id

    def require_live(self):
        return self

    def relation(self, other: AttachmentScope) -> AttachmentRelation:
        return other.relation_from(self)

    def relation_from(self, original: LiveAttachmentScope) -> AttachmentRelation:
        return ObservedAttachmentRelation(original, self)


class AttachmentRelation:
    """Absent evidence cannot identify a common live attachment."""
    foreign = True
    same_incarnation = False
    ambiguous = True
    newer = False
    older = False
    current = False


@dataclass(frozen=True)
class ObservedAttachmentRelation(AttachmentRelation):
    """Compare original scope witnesses once; no state or alias registry."""
    original: AttachmentScope
    received: AttachmentScope

    @property
    def foreign(self):
        return self.original.logical_key != self.received.logical_key

    @property
    def same_incarnation(self):
        return (not self.foreign and self.original.owner_created_at == self.received.owner_created_at)

    @property
    def ambiguous(self):
        return (not self.same_incarnation or
                (self.original.claimed_generation == self.received.claimed_generation
                 and self.original.owner != self.received.owner))

    @property
    def newer(self):
        return self.same_incarnation and self.received.claimed_generation > self.original.claimed_generation

    @property
    def older(self):
        return self.same_incarnation and self.received.claimed_generation < self.original.claimed_generation

    @property
    def current(self):
        return (self.same_incarnation
                and self.original.claimed_generation == self.received.claimed_generation
                and self.original.owner == self.received.owner)


class CursorScope(AttachmentScope):
    @classmethod
    def empty_scope(cls):
        return NoCursorScope()

    @classmethod
    def record_type(cls):
        return LiveCursorScope


@dataclass(frozen=True)
class NoCursorScope(CursorScope):
    pass


@dataclass(frozen=True)
class LiveCursorScope(CursorScope, LiveAttachmentScope):
    wire_root_id: RootId

    def __post_init__(self):
        self.claimed_identity.incarnation.require_recorded()
        if (
            not self.session_id
            or self.claimed_identity.claimed_generation <= 0
        ):
            raise ValueError("Invalid native cursor scope")

    @property
    def logical_key(self):
        return self.session_id, self.wire_root_id
