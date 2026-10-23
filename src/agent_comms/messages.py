"""Messages: declaration and persistence owners."""

from __future__ import annotations

import builtins
import hashlib
import re
import sqlite3
import time
from abc import abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Self, Type, cast, dataclass_transform

from agent_comms.bus_tables import (
    Addressed,
    BusCapability,
    BusRow,
    ClaimHolders,
    HeldResources,
    StoredHolders,
)
from agent_comms.response_policy import InformationalPolicy, ResponseEligibility, ResponsePolicy
from agent_comms.typed_table import Column

from .change_report import FileLine, NoChangeReport, ReportEvidence
from .channel_targets import CommandTarget
from .declared_family import DeclaredFamily
from .envelope_claim_transitions import ClaimTransition
from .errors import RelationViolationError
from .field_codec import (
    FieldCodec,
    InlineRecord,
    NullMember,
    PublicationExclude,
    WireName,
    WireOmitDefault,
    WireOrder,
    compile_decoder,
    projected,
)
from .mentions import ThreadMention
from .message_reference import MessageReference
from .page_traversal import LiveCursor, PageCursor
from .routing import TurnOrigin
from .task_sources import Decision, NoTaskAttachment, TaskAttachment
from .thread_identity import AgentThreadRole, ThreadRole
from .ui_model.message_category import InboundCategory, MessageCategory, OtherCategory
from .wake_policy import BoundedTriageWake, PassiveWake

if TYPE_CHECKING:
    from .input_disposition import InputDispositions
    from .historical_views import HistorySource
    from .retained_task_facts import ExactTaskFact
    from .threads import Thread


class MessageKind(DeclaredFamily, affix="Kind"):
    """A declared message kind; each member owns the validation it requires.

    Members are the kind themselves, never instances: the wire carries the
    plain declared name (``"type": "info"``), exactly as the previous enum
    stored it. Publication-time checks run once, from the single commit path.
    """

    conversation_message: ClassVar[bool] = True

    @classmethod
    def route_rows(cls, message):
        from .bus_route_counts import BusRouteCounts

        return (BusRouteCounts.from_message(message),)

    @classmethod
    def decode_message(cls, data: Mapping) -> Message:
        return FieldCodec.decode(Message, data)

    @classmethod
    def require_sendable(cls) -> None:
        if not issubclass(cls, SendableMessageKind):
            raise ValueError(f"Message kind {cls.declared_name!r} is not generically sendable")

    @classmethod
    def require_publishable(cls, message: Message, worktree: Path) -> None:
        """Checks a send must pass before publication; base kinds declare none."""


class SendableMessageKind(MessageKind, declared_name=None):
    """The declared kinds accepted by generic message commands."""


class ClaimNoticeKind(MessageKind, declared_name="claim-notice"):
    """A claim-owner record, not a delivery to a reader."""

    conversation_message = False

    @classmethod
    def route_rows(cls, message):
        return ()

    @classmethod
    def decode_message(cls, data: Mapping) -> Message:
        return FieldCodec.decode(ClaimMessage, data)

    @classmethod
    def claimed(cls, owner: Thread) -> Message:
        return Message(
            owner.name, owner.name, "Claimed", cls,
        )


class ClaimHistoryKind(ClaimNoticeKind, declared_name="claim-history"):
    """A copied claim notice has display facts, not a live claim envelope."""

    @classmethod
    def decode_message(cls, data: Mapping) -> Message:
        return FieldCodec.decode(Message, data)


class InfoKind(SendableMessageKind):
    @classmethod
    def decode_message(cls, data: Mapping) -> Message:
        """Committed info rows include the original claim-bearing envelopes."""
        return cast(Message, compile_decoder(Message | ClaimMessage).decode(data))


class NeedsAnswerKind(SendableMessageKind, declared_name=None):
    """Questions, alerts and handoffs ask their recipient to act."""


class QuestionKind(NeedsAnswerKind):
    pass


class AckKind(SendableMessageKind):
    pass


class HandoffKind(NeedsAnswerKind):
    """A handoff declares its evidence; completion claims carry references."""

    CLAIMS = ("fixed", "single owner", "no other callers")
    # Evidence command: a command literal inside backticks, not a bare word.
    COMMANDS = re.compile(r"`[^`]*\b(grep|rg|git|sed|awk|find|python|ast)\b[^`]*`")

    @classmethod
    def require_publishable(cls, message: Message, worktree: Path) -> None:
        claims = [
            claim for claim in cls.CLAIMS
            if re.search(rf"\b{re.escape(claim)}\b", message.body, re.IGNORECASE)
        ]
        if not claims or FileLine.scan(message.body) or cls.COMMANDS.search(message.body):
            return
        raise ValueError(
            "Handoff message claims " + ", ".join(claims)
            + " without a file:line reference or a command in backticks as evidence."
        )


class AlertKind(NeedsAnswerKind):
    pass


class ChangeReportKind(SendableMessageKind, declared_name="change-report"):
    """A review request declares each touched fact with its single owner."""

    @classmethod
    def require_publishable(cls, message: Message, worktree: Path) -> None:
        message.report.require_references_exist(worktree)


class MembershipTransition(DeclaredFamily, affix="Membership"):
    @classmethod
    def conversation_role(cls) -> MembershipConversationRole:
        return MembershipConversationRole(cls)

    @classmethod
    def between(cls, previous: bool, current: bool) -> tuple[type[MembershipTransition], ...]:
        return tuple(member for member in cls.members_with(cls) if member.applies(previous, current))

    @staticmethod
    @abstractmethod
    def applies(previous: bool, current: bool) -> bool: ...


class JoinedMembership(MembershipTransition):
    @staticmethod
    def applies(previous: bool, current: bool) -> bool:
        return not previous and current


class LeftMembership(MembershipTransition):
    @staticmethod
    def applies(previous: bool, current: bool) -> bool:
        return previous and not current


@dataclass(frozen=True, slots=True)
class NoMembershipChange(NullMember):
    def conversation_role(self):
        return ConversationRole()


@dataclass(frozen=True, slots=True)
class MessageRoleWire:
    """The unchanged external fields, decoded once into a message role."""

    change: Annotated[
        type[MembershipTransition] | NoMembershipChange,
        WireName("membership"),
        WireOmitDefault(),
    ] = field(default_factory=NoMembershipChange)
    notice: Annotated[bool, WireOmitDefault()] = False

    def role(self):
        role = self.change.conversation_role()
        return role.as_notice() if self.notice else role


class MessageRole(InlineRecord):
    category: ClassVar[type[MessageCategory]]

    @abstractmethod
    def response_policy(self, message: Message) -> ResponsePolicy: ...

    @abstractmethod
    def as_historical(self) -> MessageRole: ...

    @classmethod
    def wire_record(cls):
        return MessageRoleWire

    @classmethod
    def from_wire(cls, data):
        return FieldCodec.decode(MessageRoleWire, data).role()

    def to_wire(self):
        return FieldCodec.encode_record(self.wire_fields())

    def notification_references(self, message):
        return ()

    def response_wake_mode(self):
        return BoundedTriageWake()

    def as_notice(self):
        return NoticeRole()

    def require_recipients(self, target, audience):
        if len(audience.recipients) != len(target.direct_names):
            raise ValueError("a direct message must have exactly one frozen eligible recipient.")
        if tuple(member.canonical_thread for member in audience.recipients) != target.direct_names:
            raise ValueError("direct frozen recipient must match the exact stored target.")


class SendableRole(MessageRole, DeclaredFamily, affix="Role"):
    pass


class InformationalMessage:
    def response_policy(self, message):
        return InformationalPolicy.instance()

    def counts_as_conversation(self):
        return False

    def activity_rows(self, message):
        return ()

    def wake_mode(self, target, recipient, mentions, control):
        return PassiveWake()


@dataclass(frozen=True, slots=True)
class ConversationRole(SendableRole):
    category = InboundCategory

    def wire_fields(self):
        return MessageRoleWire()

    def export_attributes(self):
        return ()

    def counts_as_conversation(self):
        return True

    def activity_rows(self, message):
        from .bus_activity_index import SentActivity

        return (SentActivity(message.sender, message.timestamp),)

    def as_historical(self):
        return HistoricalConversationRole()

    def response_policy(self, message):
        return (CommandTarget.named(message.target).response_policy(message)
                if message.type.conversation_message else InformationalPolicy.instance())

    def notification_references(self, message):
        return (message.reference,) if message.type.conversation_message else ()

    def wake_mode(self, target, recipient, mentions, control):
        return control.wake_mode(target, recipient, mentions)


@dataclass(frozen=True, slots=True)
class NoticeRole(InformationalMessage, SendableRole):
    category = OtherCategory

    def wire_fields(self):
        return MessageRoleWire(notice=True)

    def export_attributes(self):
        return ("notice=true",)

    def as_historical(self):
        return HistoricalNoticeRole()

    def response_wake_mode(self):
        return PassiveWake()

    def require_recipients(self, target, audience):
        if audience.recipients:
            super(NoticeRole, self).require_recipients(target, audience)


@dataclass(frozen=True)
class Membership(InformationalMessage):
    change: type[MembershipTransition]

    def wire_fields(self):
        return replace(super().wire_fields(), change=self.change)

    def export_attributes(self):
        return (*super().export_attributes(), f"membership={self.change.declared_name}")

    def as_notice(self):
        return MembershipNoticeRole(self.change)


@dataclass(frozen=True, slots=True)
class MembershipConversationRole(Membership, ConversationRole, declared_name=None):
    def as_historical(self):
        return HistoricalMembershipConversationRole(self.change)


@dataclass(frozen=True, slots=True)
class MembershipNoticeRole(Membership, NoticeRole, declared_name=None):
    def as_historical(self):
        return HistoricalMembershipNoticeRole(self.change)


class HistoricalRole:
    """Historical rows cannot create current reply or notification work."""

    def notification_references(self, message):
        return ()

    def response_policy(self, message):
        return InformationalPolicy.instance()

    def as_historical(self):
        return self

    def as_notice(self):
        return super().as_notice().as_historical()


class HistoricalConversationRole(HistoricalRole, ConversationRole, declared_name=None):
    pass


class HistoricalNoticeRole(HistoricalRole, NoticeRole, declared_name=None):
    pass


class HistoricalMembershipConversationRole(
    HistoricalRole, MembershipConversationRole, declared_name=None):
    pass


class HistoricalMembershipNoticeRole(HistoricalRole, MembershipNoticeRole, declared_name=None):
    pass


@dataclass(frozen=True, slots=True)
class Message(Addressed, BusCapability, TurnOrigin):
    """Declares one inter-thread message.

    The bus assigns ``seq`` (a monotonically increasing log position) at send
    time; the hash-derived ``message_id`` is identity only and is not ordered.
    """

    body: Annotated[str, WireName("text"), WireOrder(6)]
    type: Annotated[builtins.type[MessageKind], WireOrder(5)]
    timestamp: Annotated[float, WireName("ts"), WireOrder(4), Column(index=True)] = (
        field(default_factory=time.time)
    )
    seq: Annotated[int, WireOrder(0), PublicationExclude()] = (
        0
    )
    sender_role: Annotated[
        Type[ThreadRole], WireOrder(7), PublicationExclude()
    ] = AgentThreadRole
    role: Annotated[MessageRole, WireOrder(8)] = field(
        default_factory=ConversationRole
    )
    mentions: Annotated[
        tuple[ThreadMention, ...],
        WireOmitDefault(),
        WireOrder(10),
        PublicationExclude(),
    ] = ()
    task: Annotated[
        TaskAttachment, WireOmitDefault(), WireOrder(12)
    ] = field(default_factory=NoTaskAttachment)
    report: Annotated[
        ReportEvidence, WireOmitDefault(), WireOrder(13), Column(nullable=True)
    ] = NoChangeReport()
    answers: Annotated[
        tuple[MessageReference, ...], WireOmitDefault(), WireOrder(14)
    ] = ()

    def projections(self):
        from .bus_activity_index import BusActivityIndex
        from .bus_display_index import BusDisplayIndex
        from .bus_tables import AnsweredSource, MessageIdentitySource

        yield from super().projections()
        yield BusActivityIndex.from_message(self, target=self.target)
        participants = {self.sender, *CommandTarget.named(self.target).direct_names,
                        *(mention.thread for mention in self.mentions)}
        for participant in participants:
            yield BusDisplayIndex.from_message(self, participant=participant)
        yield from self.role.activity_rows(self)
        yield from self.type.route_rows(self)
        yield MessageIdentitySource(self.seq, self.message_id, self.sender)
        for original in self.answers:
            yield AnsweredSource(original, self.seq)

    @classmethod
    def from_message(cls, message: Message, **capabilities: object) -> Self:
        return FieldCodec.construct(cls, {
            **{item.name: getattr(message, item.name) for item in fields(Message)},
            **capabilities,
        })

    @property
    def sender_lookup(self) -> str:
        """Public history has no frozen participant incarnation."""
        return ""

    @property
    def message(self) -> Self:
        return self

    @classmethod
    def envelope_type(cls) -> builtins.type[Message]:
        """The public message declaration this class's values carry."""
        return Message

    def messages(self) -> tuple[Self, ...]:
        return (self,)

    @property
    def text_size(self) -> int:
        return len(self.body)

    @property
    def publication_snapshot(self) -> Message:
        """Frozen publication content before independently owned delivery enrichment.

        Sequence, role, resolved mentions and claim evidence are assigned by the
        bus. Their declarations exclude them from publication content identity;
        all other declared fields participate in the Message value comparison.
        This is an in-memory snapshot, never a different wire representation.
        """
        return replace(
            self,
            **{
                plan.name: plan.declared.default
                for plan in FieldCodec.field_plans(type(self))
                if plan.setting(PublicationExclude, False)
            },
        )

    @property
    def reference(self) -> MessageReference:
        return MessageReference(self.seq, self.message_id)

    def require_decision(self) -> Decision:
        return self.task.require_decision()

    @property
    def retains_authored_task(self) -> bool:
        return self.task.retains_authored_task

    def require_task_publication(self, sender, snapshot, original_source) -> None:
        self.task.require_target(self.target, snapshot)
        self.task.require_publication(sender, snapshot, original_source)

    @property
    def history_snapshot(self) -> Message:
        """Retain display facts, never another root's resource-claim authority."""
        return self

    def require_claim_transition(self) -> ClaimTransition:
        raise RelationViolationError("Message has no claim transition")

    def retained_task_facts(self):
        source = Message.from_message(self)
        yield from self.sender_role.user_task_facts(source)
        yield from self.task.retained_task_facts(source)

    def __post_init__(self) -> None:
        if not self.sender:
            raise RelationViolationError("Message sender cannot be empty.")
        if not self.target:
            raise RelationViolationError("Message target cannot be empty.")
        if not self.body:
            raise ValueError("Message body cannot be empty.")
        self.task.require_sender(self.sender)
        if any(
            not (0 <= mention.start < mention.end <= len(self.body))
            or self.body[mention.start] != "@"
            for mention in self.mentions
        ):
            raise ValueError("Mention ranges must identify text in the message body.")
        CommandTarget.named(self.target).validate(self)

    @property
    def view_cursor(self) -> PageCursor:
        return LiveCursor(self.seq)

    @property
    def view_key(self) -> tuple[str, int]:
        return "", self.seq

    @property
    def view_order(self) -> tuple[int, int, int]:
        return 1, 0, self.seq

    @projected(view="wire", name="id")
    def message_id(self) -> str:
        digest = hashlib.sha256(
            f"{self.sender}:{self.target}:{self.timestamp}:{self.body}".encode()
        ).hexdigest()[:12]
        return digest

    @property
    def history_source(self) -> HistorySource | None:
        return None

    @property
    def display_metadata(self) -> dict:
        """Live messages carry no historical provenance decoration."""
        return {}

    def to_wire(self) -> dict:
        return {"seq": self.seq, "id": self.message_id, **FieldCodec.encode(self)}

    @classmethod
    def from_wire(cls, data: Mapping) -> Message:
        # Content identity is derived from the declared message fields.
        plan = FieldCodec.field_plan(Message, 'type')
        kind = cast(builtins.type[MessageKind], plan.value.decode(plan.raw(data)))
        return kind.decode_message({"ts": 0.0, **data})

    def response_eligibility(
        self, audience: Sequence[str], *, aliases: Mapping[str, str] | None = None
    ) -> ResponseEligibility:
        """Resolve channel responders from canonical audience identities.

        Mention identities were canonicalized when the message was committed.
        The audience remains authoritative for membership, so a valid mention
        outside the channel cannot acquire delivery by being named.
        """
        policy = self.role.response_policy(self)
        return ResponseEligibility(policy, policy.recipients(self, audience, aliases=aliases))

    @property
    def exact_reply_target(self) -> str:
        """The exact response destination owned by this opening message."""
        return CommandTarget.named(self.target).reply_target(self.sender)

    autonomous: ClassVar[bool] = False

    @property
    def separate_turn(self) -> bool:
        return self.role.response_policy(self).separate_turn

    @property
    def batch_target(self) -> str:
        return self.exact_reply_target

    def bus_keys(self, dispositions: InputDispositions, owner: Thread) -> tuple[str, ...]:
        return (dispositions.bus_key(self, owner),)

    @property
    def reply_target(self) -> str | None:
        """An external sender may receive an automatic live reply."""
        return self.sender_role.reply_target(self)


@dataclass_transform(frozen_default=True, kw_only_default=True)
class BusRecord(Message, DeclaredFamily, affix="Record"):
    """Every record declaration owns its frozen value and derived tables."""

    def append_to(self, db: sqlite3.Connection, seq: int) -> int:
        """The caller's transaction owns the sequence, including preserved history."""
        BusRow.insert_values(db, seq=seq, kind=type(self), at=time.time())
        stored = replace(self, seq=seq)
        for row in stored.rows():
            row.insert(db)
        stored.project_into(db, StoredHolders())
        return stored.seq

    @property
    def publication_snapshot(self) -> Message:
        """Delivery capabilities are not message content in a publication intent."""
        return Message.from_message(self).publication_snapshot

    @classmethod
    def envelope_type(cls) -> builtins.type[Message]:
        """A record carries the envelope of the message declaration after it in the MRO."""
        return super().envelope_type()

    @property
    def envelope(self) -> Message:
        public = self.envelope_type()
        return FieldCodec.construct(public, {item.name: getattr(self, item.name)
                                             for item in fields(public)})

    def to_wire(self) -> dict:
        """The public envelope; delivery and publication capabilities stay in their tables."""
        return self.envelope.to_wire()

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        dataclass(cls, frozen=True, kw_only=True)
        cls.tables()


@dataclass(frozen=True, kw_only=True)
class Claimed(BusCapability):
    """The transition attesting this row, stored in its append transaction."""

    claim_transition: Annotated[ClaimTransition, WireOrder(11)]

    def require_claim_transition(self) -> ClaimTransition:
        return self.claim_transition

    def project_into(self, db: sqlite3.Connection, holders: ClaimHolders) -> None:
        """Append and the shared derived rebuild replay this same original transition."""
        super().project_into(db, holders)
        projected = HeldResources.project(db, self.claim_transition, holders)
        HeldResources.apply(db, self.claim_transition, projected, holders)


@dataclass(frozen=True, kw_only=True)
class ClaimMessage(Claimed, Message):
    """A claimed message binds its transition to its public envelope identity."""

    @classmethod
    def envelope_type(cls) -> builtins.type[Message]:
        return ClaimMessage

    @classmethod
    def of(cls, message: Message, transition: ClaimTransition) -> Self:
        return cls.from_message(message, claim_transition=transition)

    def __post_init__(self) -> None:
        super().__post_init__()
        transition = self.claim_transition
        if (transition.owner != self.sender or transition.seq != self.seq
                or transition.message_id != self.message_id):
            raise RelationViolationError('Claim transition does not bind this message.')

    @property
    def history_snapshot(self) -> Message:
        """A copied history keeps the envelope's display facts and drops its claim."""
        return Message(**{
            **{item.name: getattr(self, item.name) for item in fields(Message)},
            "type": ClaimHistoryKind,
        })

    def retained_task_facts(self) -> Iterator[ExactTaskFact]:
        from .retained_task_facts import ClaimTaskFact
        yield from super().retained_task_facts()
        yield ClaimTaskFact(ClaimMessage.of(self, self.claim_transition))
